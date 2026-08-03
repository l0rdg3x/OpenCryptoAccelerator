# SPDX-License-Identifier: MIT
"""Poly1305 reference model and RFC 8439 vector parser.

The model is plain integer arithmetic straight from RFC 8439 2.5.1. It
is the oracle for the randomised RTL tests, so it is itself checked
against the official vectors first (see test_poly1305.py).
"""

import re
from pathlib import Path

P = (1 << 130) - 5
CLAMP = 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF

SRC = Path(__file__).resolve().parents[2] / "tests" / "vectors" / "sources" / "rfc8439.txt"


def poly1305_tag(key: bytes, msg: bytes) -> bytes:
    """RFC 8439 2.5.1. key = r || s, 32 bytes."""
    assert len(key) == 32
    r = int.from_bytes(key[:16], "little") & CLAMP
    s = int.from_bytes(key[16:], "little")
    acc = 0
    for off in range(0, len(msg), 16):
        chunk = msg[off:off + 16]
        n = int.from_bytes(chunk, "little") | (1 << (8 * len(chunk)))
        acc = ((acc + n) * r) % P
    return ((acc + s) % (1 << 128)).to_bytes(16, "little")


def _section(text: str, start: str, end: str) -> str:
    m = re.search(rf"(?ms)^{re.escape(start)}.*?^(?={re.escape(end)})", text)
    assert m, f"section {start!r}..{end!r} not found"
    return m.group(0)


def _colonhex_after(flat: str, marker: str) -> bytes:
    i = flat.index(marker) + len(marker)
    m = re.match(r"[()\s0-9a-f:]+", flat[i:])
    assert m, f"no colon-hex after {marker!r}"
    s = re.sub(r"[^0-9a-f:]", "", m.group(0)).strip(":")
    return bytes(int(b, 16) for b in s.split(":"))


def _hex_lines(text: str) -> bytes:
    """Concatenate consecutive lines of whitespace-separated hex byte pairs
    at the start of `text`, skipping leading blank lines. Stops at the
    first line that is not entirely hex pairs: a marker keyword (e.g.
    "tag:"), a page-break footer/header ("Nir & Langley ... [Page N]"),
    or a blank line once collection has started. This is what lets the
    A.3 #5-#11 layout (R:/S:/data:/tag:, no ASCII gutter to confuse) span
    page breaks without swallowing garbage: those footers mix non-hex
    letters into every line, so they never match the strict hex-pair
    pattern below.
    """
    out = bytearray()
    started = False
    for line in text.splitlines():
        s = line.strip()
        if not s:
            if started:
                break
            continue
        if not re.fullmatch(r"(?:[0-9A-Fa-f]{2}\s*)+", s):
            break
        out.extend(bytes.fromhex(re.sub(r"\s+", "", s)))
        started = True
    return bytes(out)


def _labeled_hex(body: str, label: str) -> bytes:
    """Hex bytes following a `label` that stands alone on its own line
    (the A.3 #5-#11 "R:" / "S:" / "data:" / "tag:" layout)."""
    m = re.search(rf"(?m)^\s*{re.escape(label)}\s*$", body)
    assert m, f"marker {label!r} not found"
    return _hex_lines(body[m.end():])


def _hexdumps(sec: str) -> list[bytes]:
    # at most 16 bytes per line: the ASCII gutter can start with
    # hex-looking characters (e.g. 'ed an "IETF Cont') and must not
    # be captured
    runs, cur = [], []
    for line in sec.splitlines():
        m = re.match(r"^\s*\d{3}\s+((?:[0-9a-f]{2}\s+){1,16})", line)
        if m:
            cur.extend(int(b, 16) for b in m.group(1).split())
        elif cur:
            runs.append(bytes(cur))
            cur = []
    if cur:
        runs.append(bytes(cur))
    return runs


def parse_rfc8439() -> list[tuple[str, bytes, bytes, bytes]]:
    """Returns [(name, key32, msg, tag16), ...]."""
    text = SRC.read_text()
    vecs = []

    sec = _section(text, "2.5.2.", "2.6.")
    flat = " ".join(sec.split())
    key = _colonhex_after(flat, "o Key Material:")
    tag = _colonhex_after(flat, "Tag:")
    (msg,) = _hexdumps(sec)
    assert len(key) == 32 and len(tag) == 16 and len(msg) == 34
    vecs.append(("rfc8439-2.5.2", key, msg, tag))

    sec = _section(text, "A.3.", "A.4.")
    parts = re.split(r"Test Vector #(\d+):", sec)
    # parts = [pre, "1", body1, "2", body2, ...]
    for i in range(1, len(parts), 2):
        num, body = parts[i], parts[i + 1]
        if "Text to MAC" not in body:
            # #5-#11: partial-reduction edge cases, given as separate
            # R:/S:/data:/tag: hex blocks instead of a "Text to MAC"
            # hexdump. The Poly1305 key is r || s (RFC 8439 2.5), both
            # little-endian, so key = R bytes || S bytes.
            r = _labeled_hex(body, "R:")
            s = _labeled_hex(body, "S:")
            msg = _labeled_hex(body, "data:")
            tag = _labeled_hex(body, "tag:")
            assert len(r) == 16 and len(s) == 16 and len(tag) == 16, (
                f"A.3 vector #{num}: bad lengths")
            vecs.append((f"rfc8439-A.3-{num}", r + s, msg, tag))
            continue
        dumps = _hexdumps(body)
        assert len(dumps) == 3, f"A.3 vector #{num}: expected 3 hexdumps"
        k, m, t = dumps
        assert len(k) == 32 and len(t) == 16, f"A.3 vector #{num}: bad lengths"
        vecs.append((f"rfc8439-A.3-{num}", k, m, t))

    return vecs
