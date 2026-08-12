# SPDX-License-Identifier: MIT
"""OcaLink against FakeBoard: request construction matches proto_model
(reused, not reimplemented), and each of the four conditions the tool
must fail loudly and specifically on actually raises its own exception
type rather than being swallowed or folded into another one.
"""

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
from link import (FrameRejected, OcaLink, ProtocolError, RequestIdMismatch,
                   SerialTimeout, StatusError)


class _LeakyAuthFail:
    """A transport that always answers status 06 (auth failure) with a
    non-empty body attached -- what a broken board would look like if
    the security property in docs/design/2026-08-03-host-protocol.md
    section 5/6 ("a corrupted tag must produce status 06 and zero bytes
    of plaintext") ever regressed. open()'s own leak check has to catch
    this; nothing upstream of it does."""

    def __init__(self) -> None:
        self._out = bytearray()

    def write(self, data: bytes) -> None:
        frames = [f for f in slip_model.decode(data) if f]
        assert len(frames) == 1
        req = proto_model.parse_response(frames[0])  # same header layout
        resp = (proto_model.MAGIC
                + bytes([proto_model.VERSION, req["opcode"]])
                + req["req_id"].to_bytes(2, "little")
                + bytes([req["slot"], proto_model.ST_AUTH_FAIL])
                + b"this must never be sent")
        self._out += slip_model.encode(resp)

    def read(self, timeout: float) -> bytes:
        out, self._out = bytes(self._out), bytearray()
        return out


class _BlackHole:
    """A transport that accepts every write and never answers -- what a
    dead or disconnected board looks like from here."""

    def write(self, data: bytes) -> None:
        pass

    def read(self, timeout: float) -> bytes:
        return b""


def _link(timeout: float = 0.2) -> OcaLink:
    return OcaLink(FakeBoard(), timeout=timeout)


def test_seal_open_round_trip_matches_proto_model_framing():
    link = _link()
    key = bytes(range(32))
    link.load_key(0, key)
    nonce, aad, msg = b"n" * 12, b"aad", b"the message"
    tag, ct = link.seal(0, nonce, aad, msg)
    assert len(tag) == 16
    pt = link.open(0, nonce, aad, ct, tag)
    assert pt == msg


def test_request_ids_increment_and_are_distinct():
    link = _link()
    seen = {link._next_req_id() for _ in range(5)}
    assert len(seen) == 5


def test_status_error_on_bad_slot():
    link = _link()
    try:
        link.seal(7, b"n" * 12, b"", b"m")
    except StatusError as exc:
        assert exc.status == proto_model.ST_BAD_SLOT
    else:
        raise AssertionError("expected StatusError")


def test_auth_failure_without_allow_flag_raises_status_error():
    link = _link()
    key = bytes(range(32))
    link.load_key(0, key)
    nonce = b"n" * 12
    tag, ct = link.seal(0, nonce, b"", b"m")
    bad_tag = bytes([tag[0] ^ 1]) + tag[1:]
    try:
        link.open(0, nonce, b"", ct, bad_tag)
    except StatusError as exc:
        assert exc.status == proto_model.ST_AUTH_FAIL
    else:
        raise AssertionError("expected StatusError")


def test_auth_failure_with_allow_flag_returns_none():
    link = _link()
    key = bytes(range(32))
    link.load_key(0, key)
    nonce = b"n" * 12
    tag, ct = link.seal(0, nonce, b"", b"m")
    bad_tag = bytes([tag[0] ^ 1]) + tag[1:]
    assert link.open(0, nonce, b"", ct, bad_tag, allow_auth_fail=True) is None


def test_protocol_error_when_auth_failure_carries_a_body():
    link = OcaLink(_LeakyAuthFail(), timeout=0.2)
    try:
        link.open(0, b"n" * 12, b"", b"ct", b"t" * 16, allow_auth_fail=True)
    except ProtocolError:
        pass
    else:
        raise AssertionError(
            "expected ProtocolError: an auth failure must never carry "
            "plaintext, allow_auth_fail or not")


def test_request_id_mismatch_is_detected():
    link = _link()
    link.transport.break_req_id = True
    try:
        link.stats()
    except RequestIdMismatch:
        pass
    else:
        raise AssertionError("expected RequestIdMismatch")


def test_frame_rejected_on_garbled_response():
    link = _link()
    link.transport.break_response_slip = True
    try:
        link.stats()
    except FrameRejected:
        pass
    else:
        raise AssertionError("expected FrameRejected")


def test_serial_timeout_when_nothing_answers():
    link = OcaLink(_BlackHole(), timeout=0.05)
    try:
        link.stats()
    except SerialTimeout:
        pass
    else:
        raise AssertionError("expected SerialTimeout")


def test_stats_counters_present():
    link = _link()
    s = link.stats()
    assert set(s) == {"received", "dropped_header", "completed", "auth_failures"}


def test_wire_byte_counters_advance():
    link = _link()
    before = link.bytes_written + link.bytes_read
    link.load_key(0, bytes(32))
    assert link.bytes_written + link.bytes_read > before


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"test_link: OK ({len(tests)} tests)")
