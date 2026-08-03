# SPDX-License-Identifier: MIT
"""Self-consistency checks for proto_model, run without any RTL."""

from proto_model import (HDR_LEN, OP_SEAL, ST_OK, build_seal,
                         parse_response, _header)


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


if __name__ == "__main__":
    test_header_round_trips()
    test_seal_layout()
    print("proto_model: OK")
