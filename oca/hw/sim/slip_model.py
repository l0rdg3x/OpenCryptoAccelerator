# SPDX-License-Identifier: MIT
"""SLIP framing, RFC 1055, as the reference the two bridge halves are
judged against.

The four byte values are the RFC's: END (0xC0) terminates a frame, ESC
(0xDB) introduces an escape, and the two escaped forms are ESC_END
(0xDC) and ESC_ESC (0xDD). There is no vector file to parse the way
there is for the cryptography -- RFC 1055 gives the encoding in prose --
so the encoder and the decoder below are written separately rather than
one as the inverse of the other, and every assertion in the suites
anchors on the payload or on the encoding and never on one of these two
agreeing with the other.

`decode` is deliberately stricter than an inverse would be: it has to
say something a test can assert on about a stream the encoder could not
have produced -- a bad escape, a dangling one, a run of ENDs -- because
those are exactly the streams the RTL has to refuse.
"""

END = 0xC0
ESC = 0xDB
ESC_END = 0xDC
ESC_ESC = 0xDD


def encode(payload: bytes) -> bytes:
    """One frame: the payload with END and ESC escaped, then END."""
    out = bytearray()
    for b in payload:
        if b == END:
            out += bytes([ESC, ESC_END])
        elif b == ESC:
            out += bytes([ESC, ESC_ESC])
        else:
            out.append(b)
    out.append(END)
    return bytes(out)


def decode(stream: bytes) -> list:
    """Every complete frame in a byte stream, empty frames included.

    Bytes after the last END are not a frame and are dropped: a frame
    exists only once its terminator has arrived, which is the whole
    reason SLIP resynchronises.
    """
    frames = []
    cur = bytearray()
    esc = False
    for b in stream:
        if b == END:
            if esc:
                raise ValueError("frame ended on a dangling ESC")
            frames.append(bytes(cur))
            cur = bytearray()
        elif esc:
            esc = False
            if b == ESC_END:
                cur.append(END)
            elif b == ESC_ESC:
                cur.append(ESC)
            else:
                raise ValueError(f"bad escape: ESC 0x{b:02x}")
        elif b == ESC:
            esc = True
        else:
            cur.append(b)
    return frames
