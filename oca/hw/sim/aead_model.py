# SPDX-License-Identifier: MIT
"""AEAD_CHACHA20_POLY1305 reference model (RFC 8439 section 2.8).

Composed from the two core models. Validated against the official 2.8.2
and A.5 vectors before it is used as an oracle for randomised tests.
"""

from chacha20_model import chacha20_block, chacha20_xor
from poly1305_model import poly1305_tag


def _pad16(data: bytes) -> bytes:
    return b"" if len(data) % 16 == 0 else bytes(16 - (len(data) % 16))


def _mac_data(aad: bytes, ct: bytes) -> bytes:
    return (aad + _pad16(aad) + ct + _pad16(ct)
            + len(aad).to_bytes(8, "little") + len(ct).to_bytes(8, "little"))


def aead_encrypt(key: bytes, nonce: bytes, aad: bytes,
                 pt: bytes) -> tuple[bytes, bytes]:
    """RFC 8439 2.8.1. Returns (ciphertext, tag)."""
    otk = chacha20_block(key, 0, nonce)[:32]
    ct = b"".join(chacha20_xor(key, i + 1, nonce, pt[o:o + 64])
                  for i, o in enumerate(range(0, len(pt), 64)))
    return ct, poly1305_tag(otk, _mac_data(aad, ct))


def aead_decrypt(key: bytes, nonce: bytes, aad: bytes,
                 ct: bytes) -> tuple[bytes, bytes]:
    """Returns (plaintext, expected tag). The caller compares tags."""
    otk = chacha20_block(key, 0, nonce)[:32]
    pt = b"".join(chacha20_xor(key, i + 1, nonce, ct[o:o + 64])
                  for i, o in enumerate(range(0, len(ct), 64)))
    return pt, poly1305_tag(otk, _mac_data(aad, ct))
