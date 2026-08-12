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
import termios

DEFAULT_BAUD = 115200


class RawSerial:
    """A tty configured for 8N1, no flow control, no line discipline --
    every byte in, every byte out, unmodified.

    Opened O_NONBLOCK so os.open() itself cannot hang waiting on modem
    control lines; reads and writes then go through select() so this
    class, not the kernel, owns the timeout.
    """

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
        self._fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            fcntl.ioctl(self._fd, termios.TIOCEXCL)
        except OSError:
            pass  # exclusivity is a courtesy against a second opener, not a requirement
        try:
            self._configure(speed)
            # Discards whatever the kernel already queued for this tty
            # before this process opened it -- a reply left over from
            # whatever held the device before us must not be mistaken
            # for the answer to this process's own first request.
            termios.tcflush(self._fd, termios.TCIFLUSH)
        except Exception:
            self.close()
            raise

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
