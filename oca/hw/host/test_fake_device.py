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
    key = bytes(range(32))
    _send(board, proto_model.build_load_key(1, 0, key))
    proto_model.parse_response(_recv(board))  # slot 0 loaded: isolate length

    frame = bytearray(proto_model.build_seal(2, 0, b"n" * 12, b"aad", b"msg"))
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


def test_oversized_frame_gets_no_response():
    """cnt_long on real hardware: oca_slip_rx.sv:312-313 never reaches
    S_PRIME for a frame over BYTES, so oca_proto never sees it and
    nothing answers -- not truncated-and-answered."""
    board = FakeBoard()
    over = proto_model.build_stats(1) + b"\x00" * (board.BYTES + 100)
    _send(board, over)
    assert board.read(0) == b""


def test_full_buffer_seal_frame_is_bad_length():
    """A frame that fills the buffer exactly (BYTES bytes) is always
    ST_BAD_LENGTH, whatever its own aad_len/msg_len say: oca_pktbuf.sv:151's
    rd_full triggers on a full bank, and oca_proto.sv:372 folds that into
    len_bad ahead of comparing the declared lengths to what was received."""
    board = FakeBoard()
    key = bytes(range(32))
    _send(board, proto_model.build_load_key(1, 0, key))
    proto_model.parse_response(_recv(board))

    fixed = proto_model.HDR_LEN + 16  # header + nonce/aad_len/msg_len
    msg = b"m" * (board.BYTES - fixed)  # message alone fills the rest
    frame = proto_model.build_seal(2, 0, b"n" * 12, b"", msg)
    assert len(frame) == board.BYTES
    _send(board, frame)
    resp = proto_model.parse_response(_recv(board))
    assert resp["status"] == proto_model.ST_BAD_LENGTH


def test_load_key_checks_slot_before_length():
    """oca_proto.sv:818-824 checks slot before length for load-key; a
    request that is wrong both ways must read as ST_BAD_SLOT, not
    ST_BAD_LENGTH."""
    board = FakeBoard()
    frame = bytearray(proto_model.build_load_key(1, 200, b"k" * 32))
    frame += b"\x00"  # also the wrong length
    _send(board, bytes(frame))
    resp = proto_model.parse_response(_recv(board))
    assert resp["status"] == proto_model.ST_BAD_SLOT


def test_seal_checks_slot_before_length():
    """oca_proto.sv:831-836 checks slot (ks_rd_valid) before len_bad for
    seal; a request to an unloaded slot with an inconsistent length must
    read as ST_BAD_SLOT, not ST_BAD_LENGTH."""
    board = FakeBoard()
    frame = bytearray(proto_model.build_seal(1, 3, b"n" * 12, b"aad", b"msg"))
    frame += b"\x00"  # also the wrong length
    _send(board, bytes(frame))
    resp = proto_model.parse_response(_recv(board))
    assert resp["status"] == proto_model.ST_BAD_SLOT


def test_open_checks_slot_before_length():
    """Same as above, open side."""
    board = FakeBoard()
    frame = bytearray(
        proto_model.build_open(1, 3, b"n" * 12, b"aad", b"ct", b"t" * 16))
    frame += b"\x00"  # also the wrong length
    _send(board, bytes(frame))
    resp = proto_model.parse_response(_recv(board))
    assert resp["status"] == proto_model.ST_BAD_SLOT


def test_bench_wire_format_and_deterministic_count():
    """A bench response is header + 16 bytes: duration, timestamp,
    reserved-zero -- parse_bench (the sim side's own parser) must accept
    it byte for byte, and the duration must equal the fake's documented
    model exactly, because a count a test cannot predict is a count a
    test cannot assert."""
    board = FakeBoard()
    _send(board, proto_model.build_load_key(1, 0, bytes(range(32))))
    proto_model.parse_response(_recv(board))
    _send(board, proto_model.build_bench(2, 0, b"n" * 12, 8, bytes(64)))
    resp = proto_model.parse_response(_recv(board))
    assert resp["status"] == proto_model.ST_OK
    r = proto_model.parse_bench(resp)
    assert r["duration"] == board.BENCH_BASE + 8 * board.BENCH_PER_BLOCK


def test_bench_unloaded_slot_status():
    board = FakeBoard()
    _send(board, proto_model.build_bench(1, 3, b"n" * 12, 8, bytes(64)))
    resp = proto_model.parse_response(_recv(board))
    assert resp["status"] == proto_model.ST_BAD_SLOT


def test_bench_checks_slot_before_length():
    """oca_proto.sv judges ks_rd_valid ahead of the OP_BENCH length
    checks; a request wrong both ways must read as ST_BAD_SLOT."""
    board = FakeBoard()
    frame = bytearray(proto_model.build_bench(1, 3, b"n" * 12, 8, bytes(64)))
    frame += b"\x00"  # also the wrong length
    _send(board, bytes(frame))
    resp = proto_model.parse_response(_recv(board))
    assert resp["status"] == proto_model.ST_BAD_SLOT


def test_bench_bad_shapes_are_bad_length():
    """Everything that is not a header, sixteen argument bytes and one
    whole 64-byte block is ST_BAD_LENGTH (host-protocol.md section 8):
    a truncated block, a nonzero reserved field, an N of zero."""
    board = FakeBoard()
    _send(board, proto_model.build_load_key(1, 0, bytes(range(32))))
    proto_model.parse_response(_recv(board))
    good = proto_model.build_bench(2, 0, b"n" * 12, 8, bytes(64))

    _send(board, good[:-1])  # one byte short of a whole block
    resp = proto_model.parse_response(_recv(board))
    assert resp["status"] == proto_model.ST_BAD_LENGTH

    reserved = bytearray(good)
    reserved[proto_model.HDR_LEN + 12] = 0x01  # the reserved field
    _send(board, bytes(reserved))
    resp = proto_model.parse_response(_recv(board))
    assert resp["status"] == proto_model.ST_BAD_LENGTH

    zero_n = bytearray(good)
    zero_n[proto_model.HDR_LEN + 14:proto_model.HDR_LEN + 16] = b"\x00\x00"
    _send(board, bytes(zero_n))
    resp = proto_model.parse_response(_recv(board))
    assert resp["status"] == proto_model.ST_BAD_LENGTH


def test_force_engine_err_hook_produces_status_07():
    """ST_ENGINE_ERR (07) cannot arise from any request the host protocol
    can express -- chacha20_poly1305.sv's `err` only fires on a block
    with in_len > 64, and oca_proto's own splitter never presents one
    (oca_proto.sv:390, :406). force_engine_err exists purely so the host
    tool's handling of the status can be tested, the same way
    break_req_id and break_response_slip test detection of link-level
    misbehaviour no legitimate traffic can trigger either."""
    board = FakeBoard()
    board.force_engine_err = True
    key = bytes(range(32))
    _send(board, proto_model.build_load_key(1, 0, key))
    proto_model.parse_response(_recv(board))
    _send(board, proto_model.build_seal(2, 0, b"n" * 12, b"", b"m"))
    resp = proto_model.parse_response(_recv(board))
    assert resp["status"] == proto_model.ST_ENGINE_ERR


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
