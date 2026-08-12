# SPDX-License-Identifier: MIT
"""FakeBoard rejects what oca_slip_rx.sv and the host protocol's status
table reject -- a permissive fake cannot catch a protocol bug in the
host tool. Drives FakeBoard directly (no OcaLink), building requests
with proto_model.py and SLIP-encoding them with slip_model.py, so this
also doubles as a check that FakeBoard's wire format matches what the
real board is specified to speak.
"""

import struct
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SIM_DIR = _HERE.parent / "sim"
for _p in (_HERE, _SIM_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import proto_model
import slip_model
from fake_device import FakeBoard
from slip_stream import SlipDecodeError, SlipReader


def _send(board: FakeBoard, frame: bytes) -> None:
    board.write(slip_model.encode(frame))


def _recv(board: FakeBoard):
    data = board.read(0)
    if not data:
        return None
    frames = [f for f in slip_model.decode(data) if f]
    assert len(frames) == 1, f"expected exactly one frame, got {len(frames)}"
    return frames[0]


def test_stats_wire_format():
    board = FakeBoard()
    _send(board, proto_model.build_stats(1))
    resp = proto_model.parse_response(_recv(board))
    assert resp["status"] == proto_model.ST_OK
    assert len(resp["body"]) == 16
    struct.unpack("<4I", resp["body"])  # must not raise


def test_bad_magic_status():
    board = FakeBoard()
    frame = bytearray(proto_model.build_stats(1))
    frame[0] = 0x00
    _send(board, bytes(frame))
    resp = proto_model.parse_response(_recv(board))
    assert resp["status"] == proto_model.ST_BAD_MAGIC


def test_bad_version_status():
    board = FakeBoard()
    frame = bytearray(proto_model.build_stats(1))
    frame[2] = 0x02
    _send(board, bytes(frame))
    resp = proto_model.parse_response(_recv(board))
    assert resp["status"] == proto_model.ST_BAD_VERSION


def test_bad_opcode_status():
    board = FakeBoard()
    frame = bytearray(proto_model.build_stats(1))
    frame[3] = 0xEE
    _send(board, bytes(frame))
    resp = proto_model.parse_response(_recv(board))
    assert resp["status"] == proto_model.ST_BAD_OPCODE


def test_unloaded_slot_status():
    board = FakeBoard()
    _send(board, proto_model.build_seal(1, 3, b"n" * 12, b"", b"msg"))
    resp = proto_model.parse_response(_recv(board))
    assert resp["status"] == proto_model.ST_BAD_SLOT


def test_slot_out_of_range_status():
    board = FakeBoard()
    _send(board, proto_model.build_load_key(1, 200, b"k" * 32))
    resp = proto_model.parse_response(_recv(board))
    assert resp["status"] == proto_model.ST_BAD_SLOT


def test_bad_length_status():
    board = FakeBoard()
    frame = bytearray(proto_model.build_seal(1, 0, b"n" * 12, b"aad", b"msg"))
    frame += b"\x00"  # one byte more than the declared lengths account for
    _send(board, bytes(frame))
    resp = proto_model.parse_response(_recv(board))
    assert resp["status"] == proto_model.ST_BAD_LENGTH


def test_seal_and_open_round_trip():
    board = FakeBoard()
    key = bytes(range(32))
    _send(board, proto_model.build_load_key(1, 0, key))
    proto_model.parse_response(_recv(board))

    nonce, aad, msg = b"n" * 12, b"header", b"the message"
    _send(board, proto_model.build_seal(2, 0, nonce, aad, msg))
    resp = proto_model.parse_response(_recv(board))
    assert resp["status"] == proto_model.ST_OK
    tag, ct = resp["body"][:16], resp["body"][16:]

    _send(board, proto_model.build_open(3, 0, nonce, aad, ct, tag))
    resp = proto_model.parse_response(_recv(board))
    assert resp["status"] == proto_model.ST_OK
    assert resp["body"] == msg


def test_corrupted_tag_yields_no_plaintext():
    board = FakeBoard()
    key = bytes(range(32))
    _send(board, proto_model.build_load_key(1, 0, key))
    proto_model.parse_response(_recv(board))
    nonce, aad, msg = b"n" * 12, b"", b"secret"
    _send(board, proto_model.build_seal(2, 0, nonce, aad, msg))
    resp = proto_model.parse_response(_recv(board))
    tag, ct = resp["body"][:16], resp["body"][16:]
    bad_tag = bytes([tag[0] ^ 1]) + tag[1:]
    _send(board, proto_model.build_open(3, 0, nonce, aad, ct, bad_tag))
    resp = proto_model.parse_response(_recv(board))
    assert resp["status"] == proto_model.ST_AUTH_FAIL
    assert resp["body"] == b""  # the security property: nothing leaks


def test_dangling_escape_is_silently_dropped():
    """cnt_esc on real hardware: the frame never reaches oca_proto and
    nothing answers it. read() must stay empty."""
    board = FakeBoard()
    good = slip_model.encode(proto_model.build_stats(1))
    bad = bytes([slip_model.ESC, slip_model.END])  # dangling escape, no frame
    board.write(bad + good)
    resp = proto_model.parse_response(_recv(board))
    assert resp["req_id"] == 1  # only the well-formed frame was answered


def test_short_frame_is_silently_dropped():
    """cnt_short on real hardware: below MIN_BYTES, P_DROP, no answer at
    all (docs/design/2026-08-03-host-protocol.md, section 4)."""
    board = FakeBoard()
    _send(board, b"\x00" * 4)  # shorter than the 8-byte header
    assert board.read(0) == b""


def test_oversized_frame_is_truncated_and_still_answered():
    board = FakeBoard()
    over = proto_model.build_stats(1) + b"\x00" * (board.BYTES + 100)
    _send(board, over)
    resp = proto_model.parse_response(_recv(board))
    assert resp["status"] == proto_model.ST_OK  # answered about the prefix


def test_break_req_id_hook_produces_mismatch():
    board = FakeBoard()
    board.break_req_id = True
    _send(board, proto_model.build_stats(0x1234))
    resp = proto_model.parse_response(_recv(board))
    assert resp["req_id"] != 0x1234


def test_break_response_slip_hook_is_invalid_slip():
    board = FakeBoard()
    board.break_response_slip = True
    _send(board, proto_model.build_stats(1))
    data = board.read(0)
    reader = SlipReader()
    raised = False
    for b in data:
        try:
            reader.feed(b)
        except SlipDecodeError:
            raised = True
    assert raised, "the injected bytes must be invalid SLIP"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"test_fake_device: OK ({len(tests)} tests)")
