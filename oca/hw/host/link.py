# SPDX-License-Identifier: MIT
"""The host side of the OCA host protocol: one command, one SLIP frame,
one reply, judged the way proto_model.py already defines it.

This is plumbing, not a second protocol implementation: request and
response bytes come from and go to oca/hw/sim/proto_model.py unchanged
(build_load_key, build_seal, build_open, build_stats, parse_response),
and SLIP encoding comes from oca/hw/sim/slip_model.py unchanged. Wire
format: docs/design/2026-08-03-host-protocol.md. RTL framing this is
judged against: oca/hw/rtl/oca_slip_rx.sv, oca_slip_tx.sv.
"""

from __future__ import annotations

import itertools
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
    "ProtocolError", "StatusError", "RequestIdMismatch", "STATUS_NAMES",
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


class OcaLink:
    """One OCA host-protocol command at a time, over a Transport.

    Every round trip validates, in order: the reply decodes as SLIP
    (else FrameRejected), it is at least a header and its magic is
    right (else ProtocolError), its request id echoes what was sent
    (else RequestIdMismatch), and its status is 0x00 (else StatusError)
    -- so a caller that only checks the return value of load_key/seal/
    open/stats for "did it work" is still exposed to every one of these
    as an exception, never as a silently wrong answer.
    """

    def __init__(self, transport: Transport, timeout: float = 2.0):
        self.transport = transport
        self.timeout = timeout
        self._ids = itertools.count(1)
        # Wire bytes actually written and read -- SLIP overhead
        # included -- so a caller can report the SERIAL LINK's rate.
        # This says nothing about the accelerator: see selftest.py.
        self.bytes_written = 0
        self.bytes_read = 0

    def _next_req_id(self) -> int:
        return next(self._ids) & 0xFFFF

    def _recv_frame(self) -> bytes:
        deadline = time.monotonic() + self.timeout
        reader = SlipReader()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SerialTimeout(
                    f"no complete frame within {self.timeout:.3f}s")
            chunk = self.transport.read(remaining)
            self.bytes_read += len(chunk)
            for b in chunk:
                try:
                    frame = reader.feed(b)
                except SlipDecodeError as exc:
                    raise FrameRejected(str(exc)) from exc
                if frame is not None:
                    return frame

    def _roundtrip(self, req_frame: bytes, req_id: int) -> dict:
        wire = encode(req_frame)
        self.transport.write(wire)
        self.bytes_written += len(wire)

        raw = self._recv_frame()
        if len(raw) < proto_model.HDR_LEN:
            raise ProtocolError(
                f"response shorter than a header: {len(raw)} bytes")
        resp = proto_model.parse_response(raw)
        if not resp["magic_ok"]:
            raise ProtocolError(f"bad magic in response: {raw[0:2].hex()}")
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
