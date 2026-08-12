# SPDX-License-Identifier: MIT
"""Self-consistency checks for slip_stream.SlipReader, run without any
RTL and without the board: round trips through slip_model.encode(),
cross-checked against slip_model.decode() on well-formed streams, and
the two refusals oca_slip_rx.sv documents for its own direction, applied
here to the reply direction.

Convention follows oca/hw/sim/test_proto_model.py: no framework, bare
functions, non-zero exit on an uncaught AssertionError.
"""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SIM_DIR = _HERE.parent / "sim"
for _p in (_HERE, _SIM_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import slip_model
from slip_stream import SlipDecodeError, SlipReader


def _decode_all(stream: bytes) -> list:
    """Feed a whole buffer through a fresh SlipReader, byte at a time."""
    reader = SlipReader()
    frames = []
    for b in stream:
        frame = reader.feed(b)
        if frame is not None:
            frames.append(frame)
    return frames


def test_round_trip_plain_payload():
    payload = b"the quick brown fox jumps over the lazy dog"
    assert _decode_all(slip_model.encode(payload)) == [payload]


def test_round_trip_contains_end_byte():
    payload = bytes([1, 2, slip_model.END, 3, 4])
    wire = slip_model.encode(payload)
    assert slip_model.END not in wire[:-1]  # escaped everywhere but the terminator
    assert _decode_all(wire) == [payload]


def test_round_trip_contains_esc_byte():
    payload = bytes([1, slip_model.ESC, 2, slip_model.ESC, slip_model.ESC])
    assert _decode_all(slip_model.encode(payload)) == [payload]


def test_round_trip_all_256_byte_values():
    payload = bytes(range(256))
    assert _decode_all(slip_model.encode(payload)) == [payload]


def test_agrees_with_slip_model_on_well_formed_streams():
    payloads = (b"", b"x", bytes([slip_model.END, slip_model.ESC]) * 3,
                bytes(range(256)))
    for payload in payloads:
        wire = slip_model.encode(payload)
        # slip_model.decode() reports empty frames explicitly; a single
        # encode() call never produces one, so filtering them out is
        # the only difference between the two decoders here.
        want = [f for f in slip_model.decode(wire) if f]
        assert _decode_all(wire) == want, payload


def test_multiple_frames_split_across_feed_calls():
    reader = SlipReader()
    wire = slip_model.encode(b"first") + slip_model.encode(b"second")
    got = []
    for chunk in (wire[:3], wire[3:7], wire[7:]):
        for b in chunk:
            frame = reader.feed(b)
            if frame is not None:
                got.append(frame)
    assert got == [b"first", b"second"]


def test_empty_frame_is_silently_absorbed():
    """Two ENDs in a row: oca_slip_rx.sv calls this ordinary SLIP (a
    leading END flushing line noise) and neither counts nor answers it.
    feed() must return None, not a frame a caller could mistake for a
    zero-length reply."""
    reader = SlipReader()
    assert reader.feed(slip_model.END) is None
    assert reader.feed(slip_model.END) is None
    for b in b"payload":
        assert reader.feed(b) is None
    assert reader.feed(slip_model.END) == b"payload"


def test_bad_escape_raises_and_resyncs():
    reader = SlipReader()
    stream = bytes([slip_model.ESC, 0x00]) + b"garbage" + bytes([slip_model.END])
    raised = False
    for b in stream:
        try:
            reader.feed(b)
        except SlipDecodeError:
            raised = True
    assert raised
    # the END that closed the bad frame leaves the reader clean for the
    # next one
    assert reader.feed(0x41) is None
    assert reader.feed(slip_model.END) == b"\x41"


def test_dangling_escape_before_end_raises():
    reader = SlipReader()
    raised = False
    for b in bytes([0x01, slip_model.ESC, slip_model.END]):
        try:
            reader.feed(b)
        except SlipDecodeError:
            raised = True
    assert raised


def test_bad_escape_does_not_raise_before_the_closing_end():
    """The refusal is discovered at END, not at the bad byte -- matching
    oca_slip_rx.sv's "sticky for the rest of the frame". A decoder that
    raised immediately would desynchronise mid-frame instead of at the
    frame boundary."""
    reader = SlipReader()
    reader.feed(slip_model.ESC)
    reader.feed(0x00)  # bad escape byte; must not raise here
    reader.feed(0x41)  # more of the same doomed frame; must not raise here


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"test_slip_stream: OK ({len(tests)} tests)")
