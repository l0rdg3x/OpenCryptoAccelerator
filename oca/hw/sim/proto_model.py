# SPDX-License-Identifier: MIT
"""Builder and parser for the OCA host protocol.

The wire format is defined in docs/design/2026-08-03-host-protocol.md.
This module is the reference for it: the RTL is judged against what
these functions produce, and the cryptography comes from aead_model so
no expected value is ever written by hand.
"""

import struct

MAGIC = b"\x4f\x43"
VERSION = 1

OP_LOAD_KEY = 0x01
OP_SEAL = 0x02
OP_OPEN = 0x03
OP_STATS = 0x04
OP_BENCH = 0x05

ST_OK = 0x00
ST_BAD_MAGIC = 0x01
ST_BAD_VERSION = 0x02
ST_BAD_OPCODE = 0x03
ST_BAD_SLOT = 0x04
ST_BAD_LENGTH = 0x05
ST_AUTH_FAIL = 0x06
ST_ENGINE_ERR = 0x07

HDR_LEN = 8


def _header(opcode: int, req_id: int, slot: int, status: int = 0) -> bytes:
    return MAGIC + bytes([VERSION, opcode]) + struct.pack("<H", req_id) \
        + bytes([slot, status])


def build_load_key(req_id: int, slot: int, key: bytes) -> bytes:
    assert len(key) == 32
    return _header(OP_LOAD_KEY, req_id, slot) + key


def build_seal(req_id: int, slot: int, nonce: bytes, aad: bytes,
               msg: bytes) -> bytes:
    assert len(nonce) == 12
    return (_header(OP_SEAL, req_id, slot) + nonce
            + struct.pack("<HH", len(aad), len(msg)) + aad + msg)


def build_open(req_id: int, slot: int, nonce: bytes, aad: bytes,
               ct: bytes, tag: bytes) -> bytes:
    assert len(nonce) == 12 and len(tag) == 16
    return (_header(OP_OPEN, req_id, slot) + nonce
            + struct.pack("<HH", len(aad), len(ct)) + tag + aad + ct)


def build_stats(req_id: int) -> bytes:
    return _header(OP_STATS, req_id, 0)


def build_bench(req_id: int, slot: int, nonce: bytes, nblocks: int,
                block: bytes) -> bytes:
    """The on-chip benchmark: one 64-byte block, run nblocks times.

    The layout is the seal's, with the block count in the msg_len field
    (counting blocks, not bytes) and the aad_len field reserved as zero.
    """
    assert len(nonce) == 12 and len(block) == 64
    assert 1 <= nblocks <= 0xFFFF
    return (_header(OP_BENCH, req_id, slot) + nonce
            + struct.pack("<HH", 0, nblocks) + block)


def parse_bench(rsp: dict) -> dict:
    """Unpack a bench response body: duration, timestamp, 4 zero bytes."""
    body = rsp["body"]
    assert len(body) == 16, f"bench body is {len(body)} bytes, want 16"
    duration, timestamp, reserved = struct.unpack("<IQ4s", body)
    assert reserved == b"\x00" * 4, f"reserved bytes not zero: {reserved!r}"
    return {"duration": duration, "timestamp": timestamp}


def parse_response(pkt: bytes) -> dict:
    assert len(pkt) >= HDR_LEN, f"response shorter than a header: {len(pkt)}"
    return {
        "magic_ok": pkt[0:2] == MAGIC,
        "version": pkt[2],
        "opcode": pkt[3],
        "req_id": struct.unpack("<H", pkt[4:6])[0],
        "slot": pkt[6],
        "status": pkt[7],
        "body": pkt[HDR_LEN:],
    }
