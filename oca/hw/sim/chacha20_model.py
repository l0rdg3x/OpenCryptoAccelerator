# SPDX-License-Identifier: MIT
"""ChaCha20 reference model (RFC 8439 section 2.3).

Plain integer arithmetic, used as the oracle for randomised RTL tests.
It is validated against the official vectors first — see test_chacha20.py.
"""

MASK = 0xFFFFFFFF


def _rotl(v: int, n: int) -> int:
    return ((v << n) | (v >> (32 - n))) & MASK


def _qr(s: list[int], a: int, b: int, c: int, d: int) -> None:
    s[a] = (s[a] + s[b]) & MASK; s[d] = _rotl(s[d] ^ s[a], 16)
    s[c] = (s[c] + s[d]) & MASK; s[b] = _rotl(s[b] ^ s[c], 12)
    s[a] = (s[a] + s[b]) & MASK; s[d] = _rotl(s[d] ^ s[a], 8)
    s[c] = (s[c] + s[d]) & MASK; s[b] = _rotl(s[b] ^ s[c], 7)


def chacha20_block(key: bytes, counter: int, nonce: bytes) -> bytes:
    """RFC 8439 2.3.1: returns the 64-byte keystream block."""
    assert len(key) == 32 and len(nonce) == 12
    const = [0x61707865, 0x3320646E, 0x79622D32, 0x6B206574]
    st = const + [int.from_bytes(key[i:i + 4], "little") for i in range(0, 32, 4)]
    st += [counter] + [int.from_bytes(nonce[i:i + 4], "little") for i in range(0, 12, 4)]
    work = list(st)
    for _ in range(10):
        _qr(work, 0, 4, 8, 12); _qr(work, 1, 5, 9, 13)
        _qr(work, 2, 6, 10, 14); _qr(work, 3, 7, 11, 15)
        _qr(work, 0, 5, 10, 15); _qr(work, 1, 6, 11, 12)
        _qr(work, 2, 7, 8, 13); _qr(work, 3, 4, 9, 14)
    out = b""
    for i in range(16):
        out += ((work[i] + st[i]) & MASK).to_bytes(4, "little")
    return out


def chacha20_xor(key: bytes, counter: int, nonce: bytes, data: bytes) -> bytes:
    """One block of keystream XORed with data (data must be <= 64 bytes)."""
    assert len(data) <= 64
    ks = chacha20_block(key, counter, nonce)
    return bytes(a ^ b for a, b in zip(data, ks))
