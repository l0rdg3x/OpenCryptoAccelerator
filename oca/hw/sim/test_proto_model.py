# SPDX-License-Identifier: MIT
"""Self-consistency checks for proto_model, run without any RTL."""

import struct

from proto_model import (HDR_LEN, OP_SEAL, ST_OK, build_bench, build_seal,
                         parse_bench, parse_response, _header)


def test_header_round_trips():
    pkt = _header(OP_SEAL, 0x1234, 5, ST_OK)
    got = parse_response(pkt)
    assert got["magic_ok"] and got["version"] == 1
    assert got["opcode"] == OP_SEAL and got["req_id"] == 0x1234
    assert got["slot"] == 5 and got["status"] == ST_OK


def test_seal_layout():
    pkt = build_seal(1, 0, b"n" * 12, b"aad", b"msg")
    # literal, not the imported MAGIC: comparing the packet against the
    # same constant that built it passes for any value and proves nothing
    assert pkt[:2] == b"\x4f\x43"
    assert len(pkt) == HDR_LEN + 12 + 4 + 3 + 3
    assert pkt[HDR_LEN + 12:HDR_LEN + 16] == b"\x03\x00\x03\x00"
    assert pkt[HDR_LEN + 16:] == b"aadmsg"


def test_bench_layout():
    pkt = build_bench(2, 1, b"n" * 12, 5, bytes(range(64)))
    assert pkt[:2] == b"\x4f\x43"
    assert pkt[3] == 0x05
    assert len(pkt) == HDR_LEN + 16 + 64
    # reserved zero at 20..21, the block count at 22..23
    assert pkt[HDR_LEN + 12:HDR_LEN + 16] == b"\x00\x00\x05\x00"
    assert pkt[HDR_LEN + 16:] == bytes(range(64))


def test_bench_extra_round_trips():
    body = struct.pack("<IQ4x", 1234, 987654321)
    got = parse_bench({"body": body})
    assert got == {"duration": 1234, "timestamp": 987654321}


if __name__ == "__main__":
    test_header_round_trips()
    test_seal_layout()
    test_bench_layout()
    test_bench_extra_round_trips()
    print("proto_model: OK")
