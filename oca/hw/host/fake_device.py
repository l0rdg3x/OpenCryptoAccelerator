# SPDX-License-Identifier: MIT
"""A software stand-in for the board's serial link, for testing the host
tool with nothing plugged in.

It speaks the same two layers link.py does -- SLIP framing (RFC 1055)
carrying the OCA host protocol -- and enforces the refusals both RTL
modules document, deliberately no more and no less, because a permissive
fake cannot catch a protocol bug: reusing SlipReader means the two
refusals oca_slip_rx.sv counts as cnt_esc (a bad or dangling escape) are
silent here exactly as the RTL is silent about them -- no response at
all, so the caller sees a timeout -- a frame under MIN_BYTES is dropped
the same way (cnt_short, oca_proto's P_DROP), and a frame over BYTES gets
no response at all either: oca_slip_rx.sv's cnt_long never reaches
S_PRIME (:312-313), so oca_proto never sees it. A frame of exactly BYTES
bytes IS delivered, and for seal/open is always answered ST_BAD_LENGTH
regardless of what its own aad_len/msg_len claim: oca_pktbuf.sv's
rd_full triggers on a full bank (:151), which oca_proto.sv:372 folds
into len_bad ahead of comparing the declared lengths to what was
received. Above that layer this is a minimal oca_proto plus
oca_keystore: every status code the design defines, and
chacha20-poly1305 by way of aead_model.py -- the same oracle the RTL is
judged against, so this checks the wire protocol and the host tool's
use of it, not a second cryptographic implementation. Slot is checked
before length on every command that carries both (oca_proto.sv:818-824,
:831-836), the same order oca_proto uses.

ST_ENGINE_ERR (07) is the one status this cannot reach through any
request the host protocol can express: chacha20_poly1305.sv's `err`
only fires when a fed block's in_len exceeds 64 bytes, and oca_proto's
own block splitter caps every block it ever presents at 64 by
construction (oca_proto.sv:390, :406) -- reaching it would need a bug in
oca_proto itself, which this fake does not model. force_engine_err below
exists so the host tool's handling of the status can still be tested,
the same way break_req_id and break_response_slip test detection of
link-level misbehaviour no legitimate traffic can trigger either.

What this deliberately does not model: oca_proto's own stats counters
(this keeps a simplified count of its own, documented where it is
built, not a verified replica of RTL this task does not touch) and
"busy" -- being synchronous, a load-key command here can never arrive
mid-block the way it can against real hardware.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

_SIM_DIR = Path(__file__).resolve().parents[1] / "sim"
if str(_SIM_DIR) not in sys.path:
    sys.path.insert(0, str(_SIM_DIR))

import proto_model  # noqa: E402
from aead_model import aead_decrypt, aead_encrypt  # noqa: E402
from slip_model import encode  # noqa: E402
from slip_stream import SlipDecodeError, SlipReader  # noqa: E402

__all__ = ["FakeBoard"]


class FakeBoard:
    """Transport-shaped (write()/read()), so it drops into OcaLink in
    place of transport.RawSerial with no other change on the host side.
    """

    NUM_SLOTS = 8                        # oca_keystore.sv's default
    BYTES = 2048                         # oca_pktbuf.sv / oca_slip_rx.sv default
    MIN_BYTES = proto_model.HDR_LEN      # oca_slip_rx.sv's MIN_BYTES default

    def __init__(self) -> None:
        self._slots: dict[int, bytes] = {}
        self._reader = SlipReader()
        self._out = bytearray()
        self._stats = {
            "received": 0, "dropped_header": 0,
            "completed": 0, "auth_failures": 0,
        }
        # Test-only misbehaviour hooks, off by default: a fake that can
        # never misbehave cannot prove the host tool detects it when the
        # real link does.
        self.break_req_id = False
        self.break_response_slip = False
        # Forces ST_ENGINE_ERR on the next seal/open, in place of running
        # the crypto -- see the module docstring for why no request built
        # from this protocol can reach that status any other way.
        self.force_engine_err = False

    # -- Transport interface --------------------------------------------

    def write(self, data: bytes) -> None:
        for b in data:
            try:
                frame = self._reader.feed(b)
            except SlipDecodeError:
                continue  # cnt_esc: silent, as oca_slip_rx.sv is
            if frame is None:
                continue  # no frame yet, or an empty one: also silent
            self._accept(bytes(frame))

    def read(self, timeout: float) -> bytes:
        if not self._out:
            return b""
        out, self._out = bytes(self._out), bytearray()
        return out

    def close(self) -> None:
        pass

    # -- Frame-level refusals, matching oca_slip_rx.sv -------------------

    def _accept(self, frame: bytes) -> None:
        if len(frame) < self.MIN_BYTES:
            return  # cnt_short: P_DROP, no response at all
        if len(frame) > self.BYTES:
            return  # cnt_long: oca_slip_rx.sv never reaches S_PRIME, no response
        self._stats["received"] += 1
        response = self._handle(frame)
        if response is None:
            return
        if self.break_response_slip:
            # Deliberately invalid SLIP on the way back -- an ESC
            # followed by neither ESC_END nor ESC_ESC -- to prove the
            # host's own decoder refuses it instead of accepting it.
            self._out += bytes([0xDB, 0x00, 0xC0])
            return
        self._out += encode(response)

    # -- OCA protocol ------------------------------------------------------

    def _header_out(self, opcode: int, req_id: int, slot: int,
                     status: int) -> bytes:
        if self.break_req_id:
            req_id ^= 0xFFFF
        return (proto_model.MAGIC + bytes([proto_model.VERSION, opcode])
                + struct.pack("<H", req_id) + bytes([slot, status]))

    def _handle(self, frame: bytes) -> bytes | None:
        magic = frame[0:2]
        version = frame[2]
        opcode = frame[3]
        req_id = struct.unpack("<H", frame[4:6])[0]
        slot = frame[6]
        body = frame[proto_model.HDR_LEN:]

        if magic != proto_model.MAGIC:
            self._stats["dropped_header"] += 1
            return self._header_out(opcode, req_id, slot,
                                      proto_model.ST_BAD_MAGIC)
        if version != proto_model.VERSION:
            self._stats["dropped_header"] += 1
            return self._header_out(opcode, req_id, slot,
                                      proto_model.ST_BAD_VERSION)

        handler = {
            proto_model.OP_LOAD_KEY: self._op_load_key,
            proto_model.OP_SEAL: self._op_seal,
            proto_model.OP_OPEN: self._op_open,
            proto_model.OP_STATS: self._op_stats,
        }.get(opcode)
        if handler is None:
            self._stats["dropped_header"] += 1
            return self._header_out(opcode, req_id, slot,
                                      proto_model.ST_BAD_OPCODE)
        return handler(req_id, slot, body)

    def _slot_key(self, slot: int) -> bytes | None:
        if slot < 0 or slot >= self.NUM_SLOTS:
            return None
        return self._slots.get(slot)

    def _op_load_key(self, req_id: int, slot: int, body: bytes) -> bytes:
        if slot >= self.NUM_SLOTS:
            return self._header_out(proto_model.OP_LOAD_KEY, req_id, slot,
                                      proto_model.ST_BAD_SLOT)
        if len(body) != 32:
            return self._header_out(proto_model.OP_LOAD_KEY, req_id, slot,
                                      proto_model.ST_BAD_LENGTH)
        self._slots[slot] = bytes(body)
        self._stats["completed"] += 1
        return self._header_out(proto_model.OP_LOAD_KEY, req_id, slot,
                                  proto_model.ST_OK)

    def _op_seal(self, req_id: int, slot: int, body: bytes) -> bytes:
        key = self._slot_key(slot)
        if key is None:
            return self._header_out(proto_model.OP_SEAL, req_id, slot,
                                      proto_model.ST_BAD_SLOT)
        if len(body) < 16:
            return self._header_out(proto_model.OP_SEAL, req_id, slot,
                                      proto_model.ST_BAD_LENGTH)
        nonce = body[0:12]
        aad_len, msg_len = struct.unpack("<HH", body[12:16])
        frame_len = proto_model.HDR_LEN + len(body)
        if (frame_len >= self.BYTES  # oca_pktbuf.sv rd_full: a full bank
                or len(body) != 16 + aad_len + msg_len):
            return self._header_out(proto_model.OP_SEAL, req_id, slot,
                                      proto_model.ST_BAD_LENGTH)
        if self.force_engine_err:
            return self._header_out(proto_model.OP_SEAL, req_id, slot,
                                      proto_model.ST_ENGINE_ERR)
        aad = body[16:16 + aad_len]
        msg = body[16 + aad_len:16 + aad_len + msg_len]
        ct, tag = aead_encrypt(key, nonce, aad, msg)
        self._stats["completed"] += 1
        return (self._header_out(proto_model.OP_SEAL, req_id, slot,
                                   proto_model.ST_OK) + tag + ct)

    def _op_open(self, req_id: int, slot: int, body: bytes) -> bytes:
        key = self._slot_key(slot)
        if key is None:
            return self._header_out(proto_model.OP_OPEN, req_id, slot,
                                      proto_model.ST_BAD_SLOT)
        if len(body) < 32:
            return self._header_out(proto_model.OP_OPEN, req_id, slot,
                                      proto_model.ST_BAD_LENGTH)
        nonce = body[0:12]
        aad_len, ct_len = struct.unpack("<HH", body[12:16])
        frame_len = proto_model.HDR_LEN + len(body)
        if (frame_len >= self.BYTES  # oca_pktbuf.sv rd_full: a full bank
                or len(body) != 32 + aad_len + ct_len):
            return self._header_out(proto_model.OP_OPEN, req_id, slot,
                                      proto_model.ST_BAD_LENGTH)
        if self.force_engine_err:
            return self._header_out(proto_model.OP_OPEN, req_id, slot,
                                      proto_model.ST_ENGINE_ERR)
        tag = body[16:32]
        aad = body[32:32 + aad_len]
        ct = body[32 + aad_len:32 + aad_len + ct_len]
        pt, expect_tag = aead_decrypt(key, nonce, aad, ct)
        if tag != expect_tag:
            self._stats["auth_failures"] += 1
            return self._header_out(proto_model.OP_OPEN, req_id, slot,
                                      proto_model.ST_AUTH_FAIL)
        self._stats["completed"] += 1
        return (self._header_out(proto_model.OP_OPEN, req_id, slot,
                                   proto_model.ST_OK) + pt)

    def _op_stats(self, req_id: int, slot: int, body: bytes) -> bytes:
        self._stats["completed"] += 1
        counters = struct.pack(
            "<4I", self._stats["received"], self._stats["dropped_header"],
            self._stats["completed"], self._stats["auth_failures"])
        return (self._header_out(proto_model.OP_STATS, req_id, slot,
                                   proto_model.ST_OK) + counters)
