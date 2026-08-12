# SPDX-License-Identifier: MIT
"""RawSerial against a pty pair -- no real hardware, no /dev/ttyACM*,
just the POSIX pseudo-terminal the kernel provides for exactly this.
"""

import os
import pty
import sys
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


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"test_transport: OK ({len(tests)} tests)")
