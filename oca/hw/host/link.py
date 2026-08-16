# SPDX-License-Identifier: MIT
"""The host side of the OCA host protocol: one command, one SLIP frame,
one reply, judged the way proto_model.py already defines it. The one
deliberate exception is bench_pair, which keeps two commands in flight
to measure the dual-engine device (host-protocol.md section 9) and
matches the replies back by req_id.

This is plumbing, not a second protocol implementation: request and
response bytes come from and go to oca/hw/sim/proto_model.py unchanged
(build_load_key, build_seal, build_open, build_stats, build_bench,
parse_response), and SLIP encoding comes from oca/hw/sim/slip_model.py
unchanged. Wire
format: docs/design/2026-08-03-host-protocol.md. RTL framing this is
judged against: oca/hw/rtl/oca_slip_rx.sv, oca_slip_tx.sv.
"""

from __future__ import annotations

import itertools
import random
import struct
import sys
import time
from pathlib import Path
from typing import Protocol

_SIM_DIR = Path(__file__).resolve().parents[1] / "sim"
if str(_SIM_DIR) not in sys.path:
    sys.path.insert(0, str(_SIM_DIR))

import proto_model  # noqa: E402
from slip_model import encode  # noqa: E402
from slip_stream import SlipDecodeError, SlipReader  # noqa: E402

__all__ = [
    "OcaLink", "OcaLinkError", "SerialTimeout", "FrameRejected",
    "ProtocolError", "StatusError", "RequestIdMismatch", "OpcodeMismatch",
    "FrameTooLarge", "STATUS_NAMES",
]

STATUS_NAMES = {
    proto_model.ST_OK: "OK",
    proto_model.ST_BAD_MAGIC: "bad magic",
    proto_model.ST_BAD_VERSION: "unsupported version",
    proto_model.ST_BAD_OPCODE: "unknown opcode",
    proto_model.ST_BAD_SLOT: "key slot out of range or not loaded",
    proto_model.ST_BAD_LENGTH: "lengths inconsistent or larger than the buffer",
    proto_model.ST_AUTH_FAIL: "authentication failed",
    proto_model.ST_ENGINE_ERR: "engine error",
}


class Transport(Protocol):
    """What OcaLink needs from a byte pipe -- real (transport.RawSerial)
    or fake (fake_device.FakeBoard)."""

    def write(self, data: bytes) -> None: ...
    def read(self, timeout: float) -> bytes: ...


class OcaLinkError(Exception):
    """Base of everything this module raises on purpose. A caller that
    wants to fail loudly on any link problem can catch just this."""


class SerialTimeout(OcaLinkError):
    """No complete frame arrived before the deadline."""


class FrameRejected(OcaLinkError):
    """The reply violated SLIP framing and could not be decoded at all.

    This is the same rule oca_slip_rx.sv applies to host-to-board
    traffic (cnt_esc: a dangling or bad escape), applied here to the
    board-to-host direction. The RTL is silent about its own refusal --
    a request this malformed on the way up never gets an answer, which
    is why a real board's equivalent case surfaces as SerialTimeout, not
    this exception. This one fires when the corruption is on a reply
    the host actually received and could see was garbage.
    """


class ProtocolError(OcaLinkError):
    """The reply decoded as a valid SLIP frame but is not a well-formed
    OCA response: shorter than a header, a bad magic, a body length that
    does not fit what the opcode promises, or plaintext accompanying an
    authentication failure that must have carried none.
    """


class StatusError(OcaLinkError):
    """The response's status byte was not 0x00 (proto_model.ST_OK)."""

    def __init__(self, status: int, response: dict):
        self.status = status
        self.response = response
        name = STATUS_NAMES.get(status, f"unrecognised status 0x{status:02x}")
        super().__init__(f"status 0x{status:02x} ({name})")


class RequestIdMismatch(OcaLinkError):
    """The response's request id does not echo the request's."""

    def __init__(self, sent: int, got: int):
        self.sent = sent
        self.got = got
        super().__init__(
            f"request id 0x{sent:04x} sent, 0x{got:04x} echoed back")


class OpcodeMismatch(OcaLinkError):
    """The response's opcode does not echo the request's.

    Request id alone does not prove a reply belongs to this request: a
    reply queued from an earlier, unrelated command can echo the same id
    (every SLIP refusal is answered with silence, so a caller that gave
    up on a timeout and moved on leaves its eventual reply sitting
    unread), and this catches it when the two commands differ.
    """

    def __init__(self, sent: int, got: int):
        self.sent = sent
        self.got = got
        super().__init__(f"opcode 0x{sent:02x} sent, 0x{got:02x} echoed back")


class FrameTooLarge(OcaLinkError):
    """The request frame would not fit the board's receive buffer.

    Refused here rather than sent: over MAX_FRAME_BYTES, oca_slip_rx.sv
    refuses the frame before it ever reaches oca_proto (cnt_long) and
    answers nothing at all, so sending it would only ever surface as a
    SerialTimeout -- a dead-link message for a size this module already
    knows in advance.
    """


class OcaLink:
    """One OCA host-protocol command at a time, over a Transport.

    Every round trip validates, in order: the request frame is not too
    large to ever be answered (else FrameTooLarge, raised before the
    transport is touched at all), the reply decodes as SLIP (else
    FrameRejected), it is at least a header and its magic is right (else
    ProtocolError), its opcode echoes what was sent (else
    OpcodeMismatch), its request id echoes what was sent (else
    RequestIdMismatch), and its status is 0x00 (else StatusError) -- so
    a caller that only checks the return value of load_key/seal/open/
    stats for "did it work" is still exposed to every one of these as an
    exception, never as a silently wrong answer, and never as a reply
    left over from a different process's request on the same tty (the
    opcode check, together with RawSerial flushing on open and the
    request id no longer starting at a fixed value every process, is
    what makes that last one true across a process boundary too).
    """

    # oca_pktbuf.sv / oca_slip_rx.sv BYTES default. A frame this size or
    # larger can never complete: exactly MAX_FRAME_BYTES fills
    # oca_pktbuf's bank (rd_full) and always answers ST_BAD_LENGTH
    # regardless of the header's own declared lengths; over it,
    # oca_slip_rx.sv refuses the frame (cnt_long) before oca_proto ever
    # sees it, and nothing answers at all.
    MAX_FRAME_BYTES = 2048

    def __init__(self, transport: Transport, timeout: float = 2.0):
        self.transport = transport
        self.timeout = timeout
        # Seeded per instance, not fixed at 1: a reply queued by an
        # earlier process's request 1 -- left unread because every SLIP
        # refusal is answered with silence, so a timed-out caller has no
        # way to know a reply is still coming -- must not collide with
        # this process's own first request id.
        self._ids = itertools.count(random.randint(0, 0xFFFF))
        # Wire bytes actually written and read -- SLIP overhead
        # included -- so a caller can report the SERIAL LINK's rate.
        # This says nothing about the accelerator: see selftest.py.
        self.bytes_written = 0
        self.bytes_read = 0
        # Bytes read off the transport but not yet consumed into a
        # frame. One transport.read can carry more than one reply once
        # two commands are in flight (bench_pair), and before this
        # buffer existed _recv_frame dropped everything in the chunk
        # past the first frame's END -- invisible with one command out,
        # a lost reply with two.
        self._rx_pending = bytearray()

    def _next_req_id(self) -> int:
        return next(self._ids) & 0xFFFF

    def _recv_frame(self) -> bytes:
        deadline = time.monotonic() + self.timeout
        reader = SlipReader()
        buf = self._rx_pending
        while True:
            for i, b in enumerate(buf):
                try:
                    frame = reader.feed(b)
                except SlipDecodeError as exc:
                    # The tail stays buffered: SLIP resynchronises on
                    # the next END, and dropping it would lose a reply
                    # still in flight along with the garbled one.
                    del buf[:i + 1]
                    raise FrameRejected(str(exc)) from exc
                if frame is not None:
                    del buf[:i + 1]
                    return frame
            # Every buffered byte fed the reader, which now holds any
            # partial frame; the buffer only ever grows by whole chunks.
            buf.clear()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SerialTimeout(
                    f"no complete frame within {self.timeout:.3f}s")
            chunk = self.transport.read(remaining)
            self.bytes_read += len(chunk)
            buf += chunk

    def _send_frame(self, req_frame: bytes) -> None:
        if len(req_frame) > self.MAX_FRAME_BYTES:
            raise FrameTooLarge(
                f"request frame is {len(req_frame)} bytes, over the "
                f"board's {self.MAX_FRAME_BYTES}-byte limit -- not sent")
        wire = encode(req_frame)
        self.transport.write(wire)
        self.bytes_written += len(wire)

    def _recv_response(self, req_opcode: int) -> dict:
        """One frame off the wire, validated up to but not including the
        request id: header length, magic, opcode echo. The id check
        differs between the one-command path (exactly this id) and the
        pipelined pair (one of the outstanding ids, each once), so it
        stays with the callers; both keep the same order -- opcode, then
        id, then status."""
        raw = self._recv_frame()
        if len(raw) < proto_model.HDR_LEN:
            raise ProtocolError(
                f"response shorter than a header: {len(raw)} bytes")
        resp = proto_model.parse_response(raw)
        if not resp["magic_ok"]:
            raise ProtocolError(f"bad magic in response: {raw[0:2].hex()}")
        if resp["opcode"] != req_opcode:
            raise OpcodeMismatch(req_opcode, resp["opcode"])
        return resp

    def _roundtrip(self, req_frame: bytes, req_id: int) -> dict:
        self._send_frame(req_frame)
        resp = self._recv_response(req_frame[3])
        if resp["req_id"] != req_id:
            raise RequestIdMismatch(req_id, resp["req_id"])
        if resp["status"] != proto_model.ST_OK:
            raise StatusError(resp["status"], resp)
        return resp

    def load_key(self, slot: int, key: bytes) -> None:
        req_id = self._next_req_id()
        self._roundtrip(proto_model.build_load_key(req_id, slot, key), req_id)

    def seal(self, slot: int, nonce: bytes, aad: bytes,
              msg: bytes) -> tuple[bytes, bytes]:
        """Returns (tag, ciphertext), tag first -- the wire order."""
        req_id = self._next_req_id()
        resp = self._roundtrip(
            proto_model.build_seal(req_id, slot, nonce, aad, msg), req_id)
        body = resp["body"]
        if len(body) < 16:
            raise ProtocolError(
                f"seal response too short for a tag: {len(body)} bytes")
        return body[:16], body[16:]

    def open(self, slot: int, nonce: bytes, aad: bytes, ct: bytes,
              tag: bytes, *, allow_auth_fail: bool = False) -> bytes | None:
        """Returns plaintext. If allow_auth_fail and the board answers
        status 06, returns None instead of raising -- and still raises
        ProtocolError if that answer carried any body at all, because a
        failed tag must produce zero bytes of plaintext
        (docs/design/2026-08-03-host-protocol.md, section 5/6)."""
        req_id = self._next_req_id()
        try:
            resp = self._roundtrip(
                proto_model.build_open(req_id, slot, nonce, aad, ct, tag),
                req_id)
        except StatusError as exc:
            if allow_auth_fail and exc.status == proto_model.ST_AUTH_FAIL:
                if exc.response["body"]:
                    raise ProtocolError(
                        "authentication failure carried plaintext: "
                        f"{len(exc.response['body'])} bytes") from exc
                return None
            raise
        return resp["body"]

    def stats(self) -> dict[str, int]:
        req_id = self._next_req_id()
        resp = self._roundtrip(proto_model.build_stats(req_id), req_id)
        body = resp["body"]
        if len(body) != 16:
            raise ProtocolError(
                f"stats response is {len(body)} bytes, want 16 (4 x uint32)")
        received, dropped_header, completed, auth_failures = \
            struct.unpack("<4I", body)
        return {
            "received": received,
            "dropped_header": dropped_header,
            "completed": completed,
            "auth_failures": auth_failures,
        }

    def bench(self, slot: int, nblocks: int, *, nonce: bytes = bytes(12),
               block: bytes = bytes(64)) -> dict[str, int]:
        """Run the on-chip benchmark: seal one 64-byte block nblocks
        times under the slot's key, returning {"duration", "timestamp"}.

        Both values are cycles of the DEVICE's clock, whose frequency
        this module cannot know and does not guess -- converting to
        time is the caller's business, with a clock the caller can
        vouch for. The nonce and block defaults are fixed zeros: the
        wire layout is the seal's (host-protocol.md section 8), but the
        product of a bench is the count, not the ciphertext.
        """
        req_id = self._next_req_id()
        resp = self._roundtrip(
            proto_model.build_bench(req_id, slot, nonce, nblocks, block),
            req_id)
        return self._parse_bench_body(resp["body"])

    @staticmethod
    def _parse_bench_body(body: bytes) -> dict[str, int]:
        if len(body) != 16:
            raise ProtocolError(
                f"bench response body is {len(body)} bytes, want 16")
        duration, timestamp, reserved = struct.unpack("<IQ4s", body)
        if reserved != b"\x00" * 4:
            raise ProtocolError(
                f"bench response reserved bytes not zero: {reserved.hex()}")
        return {"duration": duration, "timestamp": timestamp}

    def bench_pair(self, slot: int, nblocks: int, *,
                    nonce: bytes = bytes(12),
                    block: bytes = bytes(64)) -> list[dict[str, int]]:
        """Two bench requests written back to back, both on the wire
        before any reply is read -- the pipelined pair the dual-engine
        device needs to show its two engines running at once
        (host-protocol.md section 9). On a single-core device both
        requests still answer; the windows simply serialise.

        Responses arrive in the device's COMPLETION order, which on the
        dual need not be request order, so they are matched back by the
        echoed req_id: each reply must echo one of the two outstanding
        ids, each exactly once, or RequestIdMismatch. Every other check
        is bench()'s, in the same order -- opcode, id, status -- and a
        non-OK status on either reply raises StatusError just as a lone
        bench would.

        Returns the two results in REQUEST order regardless of arrival
        order, each {"req_id", "duration", "timestamp"} with the cycle
        semantics of bench(). Whether the two windows
        [timestamp - duration, timestamp] overlap is the caller's
        question to ask; both timestamps come from the device's one
        free-running timebase, so the comparison is meaningful.
        """
        req_ids = [self._next_req_id(), self._next_req_id()]
        frames = [proto_model.build_bench(rid, slot, nonce, nblocks, block)
                  for rid in req_ids]
        # Both frames judged against the board's buffer before either is
        # sent: refusing between the writes would leave one command in
        # flight with nobody reading its reply.
        for frame in frames:
            if len(frame) > self.MAX_FRAME_BYTES:
                raise FrameTooLarge(
                    f"request frame is {len(frame)} bytes, over the "
                    f"board's {self.MAX_FRAME_BYTES}-byte limit -- not sent")
        for frame in frames:
            self._send_frame(frame)
        matched: dict[int, dict[str, int]] = {}
        for _ in req_ids:
            resp = self._recv_response(proto_model.OP_BENCH)
            rid = resp["req_id"]
            if rid not in req_ids or rid in matched:
                pending = [r for r in req_ids if r not in matched]
                raise RequestIdMismatch(pending[0], rid)
            if resp["status"] != proto_model.ST_OK:
                raise StatusError(resp["status"], resp)
            matched[rid] = self._parse_bench_body(resp["body"])
        return [{"req_id": rid, **matched[rid]} for rid in req_ids]
