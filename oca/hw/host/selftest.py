# SPDX-License-Identifier: MIT
"""Runs official RFC 8439 vectors end to end over a live OcaLink and
judges every byte against aead_model.py -- the same oracle the RTL is
judged against, so this never hand-types an expected value.

Vector parsing is reused, not reimplemented. parse_rfc8439() lives in
oca/hw/sim/test_chacha20_poly1305.py and pulls sections 2.8.2 and A.5 out
of oca/tests/vectors/sources/rfc8439.txt with the project's hexdump
parsing rules (16 bytes/line cap so the ASCII gutter is never mistaken
for data, page-break awareness). Importing that module here costs one
`import cocotb`, already a pinned dependency of oca/.venv for the RTL
suites; the alternative was a second hexdump parser for the same text,
which is exactly what the "vectors only from official sources, never
hand-typed" rule exists to prevent.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Callable

_HERE = Path(__file__).resolve().parent
_SIM_DIR = _HERE.parent / "sim"
for _p in (_HERE, _SIM_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from aead_model import aead_decrypt, aead_encrypt  # noqa: E402
from test_chacha20_poly1305 import parse_rfc8439  # noqa: E402

__all__ = ["SelftestFailure", "run_selftest"]

# link.OcaLink is used only by type hint below; imported lazily to keep
# this module importable even if link.py's sim-dir bootstrap has not
# run yet in the caller's process.
LogFn = Callable[[str], None]


class SelftestFailure(Exception):
    """A step's result did not match aead_model, or the link misbehaved
    in a way the individual steps below check for."""


def run_selftest(link, log: LogFn = print) -> None:
    """Raises SelftestFailure on the first mismatch; returns normally on
    a clean pass. `link` is an link.OcaLink (or anything with the same
    load_key/seal/open/stats/bytes_written/bytes_read surface)."""
    vec = parse_rfc8439()
    key, nonce, aad, pt, ct, tag = vec["enc"]
    key5, nonce5, aad5, pt5, ct5, tag5 = vec["dec"]

    t0 = time.monotonic()
    wire_before = link.bytes_written + link.bytes_read

    log("RFC 8439 2.8.2 (AEAD encryption example), slot 0")
    link.load_key(0, key)
    log("  load_key: OK")

    got_tag, got_ct = link.seal(0, nonce, aad, pt)
    want_ct, want_tag = aead_encrypt(key, nonce, aad, pt)
    if (got_ct, got_tag) != (want_ct, want_tag):
        raise SelftestFailure(
            "2.8.2 seal mismatch against aead_model\n"
            f"  ct  got={got_ct.hex()}\n      want={want_ct.hex()}\n"
            f"  tag got={got_tag.hex()}\n      want={want_tag.hex()}")
    log("  seal: OK, matches aead_model.aead_encrypt and the RFC text")

    got_pt = link.open(0, nonce, aad, got_ct, got_tag)
    if got_pt != pt:
        raise SelftestFailure(
            f"2.8.2 open(seal(x)) mismatch: got {len(got_pt)} bytes back, "
            f"want {len(pt)}")
    log("  open (round trip of the board's own output): OK")

    bad_tag = bytes([got_tag[0] ^ 0x01]) + got_tag[1:]
    leaked = link.open(0, nonce, aad, got_ct, bad_tag, allow_auth_fail=True)
    if leaked is not None:
        raise SelftestFailure(
            "a corrupted tag opened: expected no plaintext at all, got "
            f"{len(leaked)} bytes")
    log("  tampered tag: refused with no plaintext -- OK")

    log("RFC 8439 A.5 (AEAD decryption example), slot 1")
    link.load_key(1, key5)
    log("  load_key: OK")

    got_pt5 = link.open(1, nonce5, aad5, ct5, tag5)
    want_pt5, _ = aead_decrypt(key5, nonce5, aad5, ct5)
    if got_pt5 != want_pt5 or got_pt5 != pt5:
        raise SelftestFailure(
            f"A.5 open mismatch: got {len(got_pt5)} bytes, want {len(pt5)}")
    log("  open: OK, matches aead_model.aead_decrypt and the RFC text")

    s = link.stats()
    missing = [f for f in
               ("received", "dropped_header", "completed", "auth_failures")
               if f not in s]
    if missing:
        raise SelftestFailure(f"stats response missing fields: {missing}")
    log(f"  stats: OK ({s})")

    elapsed = time.monotonic() - t0
    wire_bytes = (link.bytes_written + link.bytes_read) - wire_before
    rate_kb_s = (wire_bytes / elapsed / 1000.0) if elapsed > 0 else float("inf")
    # Whose rate this is depends on what answered, and saying "the serial
    # link" while a Python object answered in memory would be a figure
    # about nothing at all -- the fake reaches hundreds of KB/s on a link
    # whose ceiling is 11.5, so the label has to follow the transport.
    if getattr(link.transport, "is_real_wire", False):
        whose = ("That is the SERIAL LINK's rate, SLIP overhead included "
                 "-- 115200 8N1 tops out around 11.5 KB/s of payload -- "
                 "and it is NOT a measurement of the accelerator, which "
                 "this link is far too slow to show at all.")
    else:
        whose = ("NO WIRE WAS INVOLVED: this ran against the in-process "
                 "fake, so the figure measures Python and says nothing "
                 "about the link, the board or the accelerator.")
    log(f"selftest: 6/6 steps passed, {wire_bytes} protocol bytes in "
        f"{elapsed:.3f}s = {rate_kb_s:.2f} KB/s. {whose}")
