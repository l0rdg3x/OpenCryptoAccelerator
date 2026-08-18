# SPDX-License-Identifier: MIT
"""Raw serial I/O without pyserial.

oca/.venv has no `serial` module -- this project installs nothing beyond
cocotb, and a missing dependency here is a boundary to work within, not
something to pip install. This configures the tty directly through
termios and fcntl, the same primitives pyserial itself sits on, and
drives reads through select() so a caller with a deadline can poll it
instead of blocking forever on a silent line.

The board's DAPLink presents a CDC-ACM tty; nothing here is specific to
that hardware, only to POSIX termios.
"""

from __future__ import annotations

import fcntl
import os
import select
import sys
import termios
import time

DEFAULT_BAUD = 115200


class RawSerial:
    """A tty configured for 8N1, no flow control, no line discipline --
    every byte in, every byte out, unmodified.

    Opened O_NONBLOCK so os.open() itself cannot hang waiting on modem
    control lines; reads and writes then go through select() so this
    class, not the kernel, owns the timeout.
    """

    # How long the line has to stay quiet before open() calls it drained,
    # and the ceiling on the whole drain.
    #
    # What was measured: after openFPGALoader reconfigures the FPGA the
    # stray byte is ALREADY QUEUED when open() returns -- on three
    # separate reconfigurations the first read() after open got it with
    # no wait at all, under the 0.05 ms the measurement could resolve
    # (docs/RECORD.md, 2026-08-18). So the measurement gives an upper
    # bound on the arrival and no scale to size a window against; what
    # sizes it is the transport underneath. USB schedules in 1 ms
    # frames and a CDC bulk endpoint is polled on that grid, so 20 ms is
    # twenty frame times of margin over a byte that needed none. It is a
    # hundredth of link.py's 2 s reply timeout and 230 character times at
    # 115200, which is why it costs nothing a caller can feel.
    #
    # The ceiling exists because a line that never goes quiet is a fault
    # and not a drain: open() must not hang on one, and hitting it is
    # reported rather than passed off as a completed drain. WHAT THE
    # CEILING GIVES UP AGAINST tcflush: a flush emptied the kernel queue
    # in one operation whatever its size, while this reads it out at
    # 4096 bytes a call. At 115200 baud, 0.5 s of arrivals is under
    # 5.8 kB and the measured backlog is one byte, so the bound is far
    # from binding here -- but a queue filled by something other than
    # this wire could outlast it, and then bytes remain. That case
    # reports itself through drain_truncated and is not silently
    # cleaned up.
    DRAIN_WINDOW = 0.02
    DRAIN_LIMIT = 0.5

    # Read by selftest before it names whose rate it just measured. A
    # figure taken against the fake is a figure about Python, and the two
    # differ by two orders of magnitude, so the label has to be able to
    # tell them apart.
    is_real_wire = True

    def __init__(self, path: str, baudrate: int = DEFAULT_BAUD):
        self.path = path
        speed = getattr(termios, f"B{baudrate}", None)
        if speed is None:
            raise ValueError(f"termios has no baud rate constant for {baudrate}")
        # What the drain threw away, and whether it gave up early. Both
        # are set before __init__ returns and both are part of this
        # class's interface: a test proves the first is empty on a clean
        # line and carries the byte on a dirty one.
        self.discarded_at_open = b""
        self.drain_truncated = False
        self._fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            fcntl.ioctl(self._fd, termios.TIOCEXCL)
        except OSError:
            pass  # exclusivity is a courtesy against a second opener, not a requirement
        try:
            self._configure(speed)
            # Discards whatever was already on this line before this
            # process opened it -- a reply left over from whatever held
            # the device before us must not be mistaken for the answer
            # to this process's own first request.
            #
            # THIS USED TO BE termios.tcflush(TCIFLUSH) AND THAT WAS NOT
            # ENOUGH. A tty flush empties the kernel's queue, and the
            # byte that reconfiguring the FPGA leaves behind is not in
            # it: it sits inside the DAPLink, which hands it over the
            # moment a host opens the CDC endpoint, after the flush has
            # run. On 2026-08-18 that byte opened a SLIP frame and put
            # the next reply's magic one byte late, failing the first
            # selftest on silicon with "bad magic in response" against a
            # board whose reply was byte-perfect. Reading the line until
            # it goes quiet catches both cases; a flush catches only the
            # one it can see.
            #
            # And it says what it ate. The flush discarded in silence,
            # which is the failure this project's own rule names: a
            # thing that drops data must not let the operator read the
            # drop as a clean start.
            self.discarded_at_open = self._drain()
            # `or self.drain_truncated` is not redundant: a ceiling
            # reached with nothing discarded is still a line that would
            # not go quiet, and reporting only on bytes would let that
            # read as a clean open.
            if self.discarded_at_open or self.drain_truncated:
                shown = self.discarded_at_open[:32].hex()
                if len(self.discarded_at_open) > 32:
                    shown += "..."
                print(f"{path}: discarded {len(self.discarded_at_open)} "
                      f"stale byte(s) on open: {shown}"
                      + (" -- DRAIN HIT ITS CEILING after "
                         f"{self.DRAIN_LIMIT:g} s, the line is still "
                         "talking and more may follow"
                         if self.drain_truncated else ""),
                      file=sys.stderr)
        except Exception:
            self.close()
            raise

    def _drain(self) -> bytes:
        """Read and discard until the line has been quiet for
        DRAIN_WINDOW, or DRAIN_LIMIT elapses. Returns every byte thrown
        away, so the caller can report it; sets drain_truncated if the
        ceiling ended it rather than the silence.

        Repeated reads, not one: that is the whole difference from the
        tcflush this replaced. A flush is a single point in time and the
        byte this exists to catch arrives after it, handed over by the
        DAPLink when the host opens the CDC endpoint. A loop that waits
        for silence catches both what was already queued and what shows
        up while it is waiting.

        drain_truncated is advisory and NOTHING GATES ON IT, which is a
        decision and not an omission. Refusing to open a port because
        its line is noisy would take the tool away exactly when it is
        needed to diagnose the noise; the honest thing is to open, say
        loudly what was dropped and that more may follow, and let
        link.py's magic, opcode and request-id checks refuse any reply
        that is actually corrupt -- which they do, by raising, not by
        returning a wrong answer.
        """
        got = bytearray()
        deadline = time.monotonic() + self.DRAIN_LIMIT
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                self.drain_truncated = True
                return bytes(got)
            chunk = self.read(min(self.DRAIN_WINDOW, left))
            if not chunk:
                return bytes(got)
            got += chunk

    def _configure(self, speed: int) -> None:
        iflag, oflag, cflag, lflag, ispeed, ospeed, cc = termios.tcgetattr(self._fd)

        iflag &= ~(termios.IGNBRK | termios.BRKINT | termios.PARMRK
                   | termios.ISTRIP | termios.INLCR | termios.IGNCR
                   | termios.ICRNL | termios.IXON | termios.IXOFF)
        oflag &= ~termios.OPOST
        lflag &= ~(termios.ECHO | termios.ECHONL | termios.ICANON
                   | termios.ISIG | termios.IEXTEN)

        cmask = termios.CSIZE | termios.PARENB | termios.CSTOPB
        if hasattr(termios, "CRTSCTS"):
            cmask |= termios.CRTSCTS
        cflag &= ~cmask
        cflag |= termios.CS8 | termios.CREAD | termios.CLOCAL

        cc = list(cc)
        cc[termios.VMIN] = 0
        cc[termios.VTIME] = 0

        termios.tcsetattr(
            self._fd, termios.TCSANOW,
            [iflag, oflag, cflag, lflag, speed, speed, cc])

    def write(self, data: bytes) -> None:
        """Blocks (via select) until every byte has been handed to the
        kernel. A short os.write() is retried, not treated as done."""
        view = memoryview(data)
        while view:
            try:
                n = os.write(self._fd, view)
            except BlockingIOError:
                select.select([], [self._fd], [], 1.0)
                continue
            view = view[n:]

    def read(self, timeout: float) -> bytes:
        """Whatever is available within `timeout` seconds, b"" on
        nothing arriving. Never blocks past the deadline it is given."""
        if timeout <= 0:
            return b""
        ready, _, _ = select.select([self._fd], [], [], timeout)
        if not ready:
            return b""
        try:
            return os.read(self._fd, 4096)
        except BlockingIOError:
            return b""

    def close(self) -> None:
        try:
            os.close(self._fd)
        except OSError:
            pass

    def __enter__(self) -> "RawSerial":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
