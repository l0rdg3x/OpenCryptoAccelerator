# SPDX-License-Identifier: MIT
"""RawSerial against a pty pair -- no real hardware, no /dev/ttyACM*,
just the POSIX pseudo-terminal the kernel provides for exactly this.
"""

import contextlib
import io
import os
import pty
import sys
import threading
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from transport import RawSerial


def test_open_discards_bytes_queued_before_it():
    """A reply left in the tty's input queue by whatever was talking to
    this device before this process opened it must not be handed to the
    first read() after open(): that is the transport-layer half of the
    review's stale-reply finding (link.py's request id and opcode checks
    are the other half, further up the stack, for what a flush on open
    cannot catch -- a reply that arrives after the flush but before this
    process's own)."""
    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)
    # slave_fd is kept open for the whole test, standing in for a real
    # tty's driver-owned receive queue, which -- unlike a pty's -- does
    # not depend on a process holding the device open to keep bytes
    # queued.
    os.write(master_fd, b"a reply that predates this open")

    ser = RawSerial(slave_path)
    try:
        assert ser.read(0.3) == b""
    finally:
        ser.close()
        os.close(slave_fd)
        os.close(master_fd)


def test_open_reports_what_it_discarded():
    """Discarding is not enough: the operator has to be told. Loading a
    bitstream leaves at least one 0x00 in the DAPLink's own buffer, which
    a tty flush cannot reach and which arrives the moment a host opens
    the port (docs/RECORD.md, 2026-08-18). It opened a SLIP frame and put
    the next reply's magic one byte late, and the failure it produced --
    "bad magic in response" against a board whose reply was byte-perfect
    -- pointed at the device instead of at the line. A drain that ate it
    in silence would have hidden the same thing one layer down."""
    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)
    os.write(master_fd, b"\x00")

    ser = RawSerial(slave_path)
    try:
        assert ser.discarded_at_open == b"\x00", ser.discarded_at_open
        assert ser.read(0.3) == b""
    finally:
        ser.close()
        os.close(slave_fd)
        os.close(master_fd)


def test_open_reports_nothing_when_the_line_was_clean():
    """The counterpart, and the one that keeps the attribute honest: a
    quiet line must read as a quiet line. An attribute that is truthy
    after every open would be as useless as no attribute at all."""
    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)

    ser = RawSerial(slave_path)
    try:
        assert ser.discarded_at_open == b"", ser.discarded_at_open
    finally:
        ser.close()
        os.close(slave_fd)
        os.close(master_fd)


def test_open_catches_a_byte_that_arrives_after_it_started():
    """THE TEST THE FIX EXISTS FOR, and the one the other three do not
    make. They all queue their byte before RawSerial is built, which is
    the case the old termios.tcflush already handled. The byte that broke
    the first selftest on silicon is not that one: the DAPLink hands it
    over when the host opens the CDC endpoint, which is *after* a flush
    placed at the top of __init__ has run. A single point-in-time flush
    cannot see it and a loop that waits for silence can, so this is where
    the two designs actually differ -- and a mutation putting tcflush
    back fails here."""
    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)

    # Fires 5 ms in, while _drain() is inside its 20 ms window: open()
    # and _configure() together measured 0.2 ms, so the byte lands well
    # after any flush would have run and well before the drain gives up.
    late = threading.Timer(0.005, lambda: os.write(master_fd, b"\x00"))
    late.start()
    try:
        ser = RawSerial(slave_path)
        try:
            assert ser.discarded_at_open == b"\x00", ser.discarded_at_open
            assert ser.read(0.3) == b""
        finally:
            ser.close()
    finally:
        late.join()
        os.close(slave_fd)
        os.close(master_fd)


def test_the_ceiling_reports_even_when_it_discarded_nothing():
    """The ceiling's own path, which nothing else reaches, and its
    nastiest corner: DRAIN_LIMIT can end the drain before a single byte
    has been read, and then bytes ARE still on the line while
    discarded_at_open is empty. Reporting only on bytes would print
    nothing at all there and let the operator read a truncated drain as
    a clean open -- which is the silent-loss failure this project's rules
    name outright.

    A zero ceiling makes that deterministic. A noisy-line test would be
    the more lifelike shape and is not written, because it turns on
    whether a writer thread is scheduled inside a 20 ms window and would
    be a flake, not a test: WHAT IS COVERED HERE IS THE BRANCH, NOT THE
    RACE THAT REACHES IT."""

    class _NoCeiling(RawSerial):
        DRAIN_LIMIT = 0.0

    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)
    os.write(master_fd, b"still talking")

    said = io.StringIO()
    try:
        with contextlib.redirect_stderr(said):
            ser = _NoCeiling(slave_path)
        try:
            assert ser.drain_truncated is True
            assert ser.discarded_at_open == b"", ser.discarded_at_open
            # It discarded nothing, so only the ceiling can be speaking.
            assert "CEILING" in said.getvalue(), repr(said.getvalue())
            # And what it could not drain is still there to be read.
            assert ser.read(0.3) == b"still talking"
        finally:
            ser.close()
    finally:
        os.close(slave_fd)
        os.close(master_fd)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"test_transport: OK ({len(tests)} tests)")
