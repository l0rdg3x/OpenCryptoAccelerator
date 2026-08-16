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
from link import (FrameRejected, FrameTooLarge, OcaLink, OpcodeMismatch,
                   ProtocolError, RequestIdMismatch, SerialTimeout,
                   StatusError)


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


class _WrongOpcodeReply:
    """A transport that answers every request with the right req_id but
    the wrong opcode -- what a reply queued from an earlier, unrelated
    command looks like once its req_id happens to coincide with this
    one's (link.py:120's old itertools.count(1) made that the common
    case across process restarts, not a rare one)."""

    def __init__(self) -> None:
        self._out = bytearray()

    def write(self, data: bytes) -> None:
        frames = [f for f in slip_model.decode(data) if f]
        assert len(frames) == 1
        req = proto_model.parse_response(frames[0])  # same header layout
        wrong_opcode = (proto_model.OP_LOAD_KEY
                         if req["opcode"] != proto_model.OP_LOAD_KEY
                         else proto_model.OP_STATS)
        resp = (proto_model.MAGIC
                + bytes([proto_model.VERSION, wrong_opcode])
                + req["req_id"].to_bytes(2, "little")
                + bytes([req["slot"], proto_model.ST_OK]))
        self._out += slip_model.encode(resp)

    def read(self, timeout: float) -> bytes:
        out, self._out = bytes(self._out), bytearray()
        return out


class _CannedBenchReply:
    """A transport that answers every request with status 00 and a body
    of this test's choosing -- what a broken or foreign device would
    look like if its bench response did not carry the 16 bytes
    (duration, timestamp, reserved-zero) section 8 of
    docs/design/2026-08-03-host-protocol.md promises. bench()'s own
    body checks have to catch it; nothing upstream of them does."""

    def __init__(self, body: bytes) -> None:
        self._body = body
        self._out = bytearray()

    def write(self, data: bytes) -> None:
        frames = [f for f in slip_model.decode(data) if f]
        assert len(frames) == 1
        req = proto_model.parse_response(frames[0])  # same header layout
        resp = (proto_model.MAGIC
                + bytes([proto_model.VERSION, req["opcode"]])
                + req["req_id"].to_bytes(2, "little")
                + bytes([req["slot"], proto_model.ST_OK])
                + self._body)
        self._out += slip_model.encode(resp)

    def read(self, timeout: float) -> bytes:
        out, self._out = bytes(self._out), bytearray()
        return out


class _NoWriteAllowed:
    """A transport that fails the test if write() is ever called -- for
    proving a request is refused locally, before touching the wire."""

    def write(self, data: bytes) -> None:
        raise AssertionError("must not write an oversized request to the wire")

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


def test_opcode_mismatch_is_detected():
    """A reply that echoes the right req_id but the wrong opcode must be
    caught here, not read as the current request's answer -- the fix for
    the review's stale-queued-reply finding: req_id alone is not enough
    once the id space can repeat across process restarts."""
    link = OcaLink(_WrongOpcodeReply(), timeout=0.2)
    try:
        link.stats()
    except OpcodeMismatch:
        pass
    else:
        raise AssertionError("expected OpcodeMismatch")


def test_fresh_links_do_not_all_start_at_request_id_one():
    """link.py used to seed every process's counter at itertools.count(1),
    so a reply queued by an earlier process's request 1 could be mistaken
    for a brand new process's own first request. Sampled over 200
    independent links, landing on 1 every single time would mean the seed
    is still fixed (probability of that occurring by chance with a
    uniformly random seed is on the order of 1e-964)."""
    ids = {OcaLink(FakeBoard())._next_req_id() for _ in range(200)}
    assert ids != {1}, "every fresh link started at request id 1"


def test_oversized_seal_request_is_rejected_locally():
    """A seal whose frame would exceed the board's receive buffer must
    never reach the wire: over BYTES, oca_slip_rx.sv answers nothing at
    all (cnt_long), so sending it would only ever produce a timeout for
    a size the CLI already knows in advance."""
    link = OcaLink(_NoWriteAllowed(), timeout=0.05)
    msg = b"m" * OcaLink.MAX_FRAME_BYTES  # guarantees frame > MAX_FRAME_BYTES
    try:
        link.seal(0, b"n" * 12, b"", msg)
    except FrameTooLarge as exc:
        assert str(OcaLink.MAX_FRAME_BYTES) in str(exc)
    else:
        raise AssertionError("expected FrameTooLarge")


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


def test_bench_happy_path_count_is_deterministic():
    """The fake's count model is its contract: BENCH_BASE + N *
    BENCH_PER_BLOCK fake cycles, openly arbitrary constants -- so the
    whole wire round trip (build_bench out, <IQ4s back) is pinned to an
    exact number, not just to 'some integer came back'."""
    link = _link()
    link.load_key(0, bytes(range(32)))
    r = link.bench(0, 8)
    assert r["duration"] == FakeBoard.BENCH_BASE + 8 * FakeBoard.BENCH_PER_BLOCK
    assert r["timestamp"] >= r["duration"]  # the window closed after it opened


def test_bench_durations_differ_by_the_marginal_cost():
    """The property section 8 promises of the real counter, held by the
    fake's linear model: two durations differ by exactly the per-block
    cost times the block difference, and consecutive windows
    [timestamp - duration, timestamp] do not overlap."""
    link = _link()
    link.load_key(0, bytes(range(32)))
    r1 = link.bench(0, 4)
    r2 = link.bench(0, 12)
    assert r2["duration"] - r1["duration"] == 8 * FakeBoard.BENCH_PER_BLOCK
    assert r2["timestamp"] - r2["duration"] >= r1["timestamp"]


def test_bench_unloaded_slot_is_refused():
    """Status 04, judged before anything touches the engine -- the same
    fail-closed slot check as a seal."""
    link = _link()
    try:
        link.bench(3, 8)
    except StatusError as exc:
        assert exc.status == proto_model.ST_BAD_SLOT
    else:
        raise AssertionError("expected StatusError")


def test_bench_short_response_is_rejected():
    """A status-00 bench reply whose body is shorter than the 16 bytes
    the format promises must raise ProtocolError, not be sliced into
    nonsense numbers."""
    link = OcaLink(_CannedBenchReply(b"\x00" * 4), timeout=0.2)
    try:
        link.bench(0, 8)
    except ProtocolError:
        pass
    else:
        raise AssertionError("expected ProtocolError")


def test_bench_nonzero_reserved_bytes_are_rejected():
    """The last 4 body bytes are reserved-zero by section 8; a device
    that fills them is not speaking this protocol version."""
    body = bytes(12) + b"\x01\x00\x00\x00"
    link = OcaLink(_CannedBenchReply(body), timeout=0.2)
    try:
        link.bench(0, 8)
    except ProtocolError:
        pass
    else:
        raise AssertionError("expected ProtocolError")


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
