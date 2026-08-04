# SPDX-License-Identifier: MIT
"""End-to-end tests for oca_core: packets in, packets out.

Requests are injected on the 64-bit stream an axis_adapter will drive
from verilog-ethernet's 8-bit output, so this runs with no Ethernet in
the simulation. Every expected value comes from aead_model through
proto_model: the wire format is unchanged by the width, only the number
of beats it takes to carry it.
"""

import random
import struct

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from aead_model import aead_encrypt
from proto_model import (HDR_LEN, OP_LOAD_KEY, OP_OPEN, OP_SEAL, OP_STATS,
                         ST_AUTH_FAIL, ST_BAD_LENGTH, ST_BAD_MAGIC,
                         ST_BAD_OPCODE, ST_BAD_SLOT, ST_BAD_VERSION, ST_OK,
                         build_load_key, build_open, build_seal, build_stats,
                         parse_response)

KEY = bytes(range(32))
NONCE = bytes(range(12))

# oca_pktbuf drops writes past BYTES and raises wr_full, and oca_proto
# turns wr_full into a length error, so the largest request the device
# accepts is one word short of the buffer: a request of exactly BYTES
# bytes leaves the counter at BYTES, which is already full. Measured
# against the RTL by test_maximum_packet_size_is_not_reduced, which is
# what stops a two-bank buffer halving it in silence.
BYTES = 2048
MAX_REQUEST = BYTES - 8


def counters(rsp: dict) -> dict:
    """Unpack a stats response: four little-endian 32-bit counters."""
    rx, drop, done, auth = struct.unpack("<4I", rsp["body"])
    return {"rx": rx, "drop": drop, "done": done, "auth": auth}


def words_of(pkt: bytes):
    """Split a packet into little-endian beats.

    Only the final beat may be partial, which is the invariant
    oca_pktbuf documents and oca_proto fails closed on: an adapter that
    emitted a short beat mid-packet would desynchronise the word write
    pointer.
    """
    beats = []
    for off in range(0, len(pkt), 8):
        chunk = pkt[off:off + 8]
        beats.append((int.from_bytes(chunk.ljust(8, b"\x00"), "little"),
                      (1 << len(chunk)) - 1))
    return beats


class EngineWatch:
    """Concurrent monitor of the AEAD engine boundary and the drain.

    The properties below are the ones no payload assertion can see. A
    block silently discarded by the engine (in_valid outside S_ACCEPT) or
    fed twice, or a ciphertext block dropped because the drain was busy,
    all produce a *valid* tag over the wrong message: the response looks
    like a success and only the length is off. Watching the handshakes
    themselves is what turns every test in this file into a witness.

    Engine ownership is the other half. chacha20_poly1305 holds one key,
    one nonce, one counter and one accumulator, and honours `start` only
    from S_IDLE, so a start that lands while it is busy is discarded in
    silence. Held as a level instead, it would start a second message on
    the same one-time (r, s) -- Security.md section 4 item 3. Exactly one
    start per message, only while idle, one done per start.

    Not checked here, deliberately: whether a (key, nonce) pair repeats
    across commands. The device cannot know a host's nonce discipline,
    and this file's own seal/open round trips reuse a pair legitimately.
    That obligation lives with the host (Security.md section 4).
    """

    def __init__(self):
        self.failures = []
        self.starts = 0
        self.dones = 0
        self.blocks_in = 0
        self.blocks_out = 0

    def fail(self, cycle, what):
        self.failures.append(f"cycle {cycle}: {what}")

    def check(self):
        assert not self.failures, "engine monitor: " + "; ".join(self.failures)


async def watch_engine(dut, w: EngineWatch):
    aead = dut.u_aead
    proto = dut.u_proto
    prev_start = 0
    prev_in_valid = 0
    prev_err = 0
    owner_open = False      # a message is between its start and its done
    owed = 0                # ciphertext words the drain still has to write
    cycle = 0
    while True:
        await ReadOnly()
        start = int(aead.start.value)
        busy = int(aead.busy.value)
        done = int(aead.done.value)
        err = int(aead.err.value)
        in_valid = int(aead.in_valid.value)
        in_ready = int(aead.in_ready.value)
        out_valid = int(aead.out_valid.value)
        out_len = int(aead.out_len.value) if out_valid else 0
        tx_wr_en = int(proto.tx_wr_en.value)

        if start:
            w.starts += 1
            if busy:
                w.fail(cycle, "eng_start asserted while the engine is busy")
            if prev_start:
                w.fail(cycle, "eng_start held for a second cycle")
            if owner_open:
                w.fail(cycle, "second eng_start with no intervening done")
            owner_open = True
        if in_valid:
            w.blocks_in += 1
            if not in_ready:
                w.fail(cycle, "block presented while in_ready is low "
                              "(the engine discards it silently)")
            if prev_in_valid:
                w.fail(cycle, "in_valid held two cycles (block fed twice)")
            if not owner_open:
                w.fail(cycle, "block fed with no message in flight")
        if out_valid:
            w.blocks_out += 1
            if owed:
                w.fail(cycle, f"ciphertext block arrived with {owed} words of "
                              "the previous one still undrained")
            owed = (out_len + 7) // 8
        elif tx_wr_en and owed:
            owed -= 1
        if done:
            w.dones += 1
            if not owner_open:
                w.fail(cycle, "eng_done with no message in flight")
            if owed:
                w.fail(cycle, f"eng_done with {owed} ciphertext words undrained")
            owner_open = False
        if err and not prev_err:
            owner_open = False

        prev_start, prev_in_valid, prev_err = start, in_valid, err
        await RisingEdge(dut.clk)
        cycle += 1


async def setup(dut, monitor: bool = True):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst_n.value = 0
    dut.s_axis_tdata.value = 0
    dut.s_axis_tkeep.value = 0
    dut.s_axis_tvalid.value = 0
    dut.s_axis_tlast.value = 0
    dut.m_axis_tready.value = 1
    for _ in range(3):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)
    dut._oca_watch = EngineWatch() if monitor else None
    if monitor:
        cocotb.start_soon(watch_engine(dut, dut._oca_watch))
    return dut._oca_watch


async def send_word(dut, data: int, keep: int, last: bool):
    """Offer one beat as an AXI-Stream source.

    tdata, tkeep, tlast and tvalid are held stable until tready is seen
    high in the read-only phase of a cycle; the transfer is the edge that
    ends that cycle. Sampling the handshake before the edge rather than
    after it is what keeps this a source the RTL cannot distinguish from
    the adapter in front of verilog-ethernet.
    """
    dut.s_axis_tdata.value = data
    dut.s_axis_tkeep.value = keep
    dut.s_axis_tlast.value = 1 if last else 0
    dut.s_axis_tvalid.value = 1
    while True:
        await ReadOnly()
        ready = dut.s_axis_tready.value == 1
        await RisingEdge(dut.clk)
        if ready:
            return


async def send_packet(dut, pkt: bytes, rng: random.Random | None = None,
                      max_gap: int = 0):
    beats = words_of(pkt)
    for i, (data, keep) in enumerate(beats):
        if rng is not None:
            gap = rng.randint(0, max_gap)
            if gap:
                dut.s_axis_tvalid.value = 0
                for _ in range(gap):
                    await RisingEdge(dut.clk)
        await send_word(dut, data, keep, i == len(beats) - 1)
    dut.s_axis_tvalid.value = 0
    dut.s_axis_tlast.value = 0


async def recv_packet(dut, budget: int = 20000,
                      rng: random.Random | None = None,
                      stall_p: float = 0.0) -> bytes:
    """Collect one response as an AXI-Stream sink.

    A beat is taken only where tvalid and tready are both high in the
    read-only phase, which is the cycle the transfer edge ends; tkeep
    says how many of its bytes are response and not padding.

    The bytes past tkeep are checked before being discarded, which is
    what makes every test using this a witness that the final beat is
    masked. Reading the response *through* tkeep alone cannot see it:
    the bytes a partial last beat carries past the response length are
    AEAD engine output — keystream over the tail of a partial block —
    and dropping them here silently would leave nothing but the
    downstream MAC honouring tkeep between them and the wire.
    """
    out = bytearray()
    for _ in range(budget):
        if rng is not None:
            dut.m_axis_tready.value = 0 if rng.random() < stall_p else 1
        await ReadOnly()
        taken = (dut.m_axis_tvalid.value == 1
                 and dut.m_axis_tready.value == 1)
        last = taken and dut.m_axis_tlast.value == 1
        if taken:
            keep = int(dut.m_axis_tkeep.value)
            raw = int(dut.m_axis_tdata.value).to_bytes(8, "little")
            nbytes = keep.bit_count()
            assert raw[nbytes:] == bytes(8 - nbytes), (
                f"beat leaks {8 - nbytes} unmasked bytes past tkeep: "
                f"tdata {int(dut.m_axis_tdata.value):#018x}, keep {keep:#04x}")
            out += raw[:nbytes]
        await RisingEdge(dut.clk)
        if last:
            dut.m_axis_tready.value = 1
            watch = getattr(dut, "_oca_watch", None)
            if watch is not None:
                watch.check()
            return bytes(out)
    raise AssertionError(f"no response within {budget} cycles "
                         f"({len(out)} bytes seen)")


async def send_back_to_back(dut, pkts, gap: int = 0):
    """Send several packets without ever lowering tvalid between them.

    This is the source the adapter becomes when a frame is already
    queued behind the one going out: the first beat of the next packet
    is offered in the cycle right after tlast.

    `gap` inserts that many idle cycles between packets instead. Sweeping
    it is what slides a successor's parse across a predecessor's tag
    check rather than landing on one arbitrary alignment.
    """
    for n, pkt in enumerate(pkts):
        if gap and n:
            dut.s_axis_tvalid.value = 0
            dut.s_axis_tlast.value = 0
            for _ in range(gap):
                await RisingEdge(dut.clk)
        beats = words_of(pkt)
        for i, (data, keep) in enumerate(beats):
            await send_word(dut, data, keep, i == len(beats) - 1)
    dut.s_axis_tvalid.value = 0
    dut.s_axis_tlast.value = 0


async def recv_packets(dut, count: int, budget: int = 60000,
                       rng: random.Random | None = None,
                       stall_p: float = 0.0) -> list:
    return [await recv_packet(dut, budget=budget, rng=rng, stall_p=stall_p)
            for _ in range(count)]


async def pipeline(dut, pkts, nresp: int | None = None, gap: int = 0,
                   stall_p: float = 0.0, seed: int = 0x0CA1,
                   raw: bool = False) -> list:
    """Offer every packet back to back and collect the responses.

    The sink runs as its own coroutine so it is draining while the
    source is still offering: a source that waits for each response
    before sending the next drains the very pipeline it is meant to
    stress, which is what makes `command()` useless for these tests.
    """
    if nresp is None:
        nresp = len(pkts)
    sink_rng = random.Random(seed) if stall_p else None
    sink = cocotb.start_soon(
        recv_packets(dut, nresp, rng=sink_rng, stall_p=stall_p))
    await send_back_to_back(dut, pkts, gap=gap)
    got = await sink
    return list(got) if raw else [parse_response(r) for r in got]


async def command(dut, pkt: bytes, rng: random.Random | None = None) -> dict:
    await send_packet(dut, pkt, rng=rng, max_gap=3)
    return parse_response(await recv_packet(dut, rng=rng, stall_p=0.4))


@cocotb.test()
async def test_load_key_then_seal(dut):
    await setup(dut)
    rsp = await command(dut, build_load_key(0x0001, 0, KEY))
    assert rsp["status"] == ST_OK and rsp["req_id"] == 0x0001
    assert rsp["opcode"] == OP_LOAD_KEY

    aad, msg = b"header", b"the quick brown fox"
    rsp = await command(dut, build_seal(0x0002, 0, NONCE, aad, msg))
    assert rsp["status"] == ST_OK, f"seal status {rsp['status']}"
    want_ct, want_tag = aead_encrypt(KEY, NONCE, aad, msg)
    assert rsp["body"][:16] == want_tag, "tag mismatch"
    assert rsp["body"][16:] == want_ct, "ciphertext mismatch"


@cocotb.test()
async def test_seal_then_open_round_trip(dut):
    await setup(dut)
    await command(dut, build_load_key(1, 2, KEY))
    aad, msg = b"", b"round trip"
    sealed = await command(dut, build_seal(2, 2, NONCE, aad, msg))
    tag, ct = sealed["body"][:16], sealed["body"][16:]
    opened = await command(dut, build_open(3, 2, NONCE, aad, ct, tag))
    assert opened["status"] == ST_OK, f"open status {opened['status']}"
    assert opened["body"] == msg, f"{opened['body']!r} != {msg!r}"


@cocotb.test()
async def test_unloaded_slot_is_refused(dut):
    await setup(dut)
    rsp = await command(dut, build_seal(4, 7, NONCE, b"", b"x"))
    assert rsp["status"] == ST_BAD_SLOT, f"status {rsp['status']}"
    assert rsp["body"] == b"", "body returned for a refused command"


@cocotb.test()
async def test_backpressure_is_transparent(dut):
    """A source with gaps and a sink that stalls change nothing.

    Both are legal AXI-Stream behaviour, so every byte must survive them
    unmoved; the seed is fixed so a failure replays.
    """
    rng = random.Random(20260803)
    await setup(dut)

    rsp = await command(dut, build_load_key(0x21, 3, KEY), rng=rng)
    assert rsp["status"] == ST_OK, f"load_key status {rsp['status']}"

    # Two blocks of message with an AAD in front of them, so the gaps
    # and stalls fall inside a multi-block command as well as around it.
    aad = bytes(rng.randrange(256) for _ in range(5))
    msg = bytes(rng.randrange(256) for _ in range(70))
    want_ct, want_tag = aead_encrypt(KEY, NONCE, aad, msg)

    rsp = await command(dut, build_seal(0x22, 3, NONCE, aad, msg), rng=rng)
    assert rsp["status"] == ST_OK, f"seal status {rsp['status']}"
    assert rsp["body"][:16] == want_tag, "tag mismatch under backpressure"
    assert rsp["body"][16:] == want_ct, "ciphertext mismatch under backpressure"

    opened = await command(
        dut, build_open(0x23, 3, NONCE, aad, want_ct, want_tag), rng=rng)
    assert opened["status"] == ST_OK, f"open status {opened['status']}"
    assert opened["body"] == msg, "plaintext mismatch under backpressure"


@cocotb.test()
async def test_next_packet_offered_with_no_gap(dut):
    """The byte after tlast must wait, not be accepted and thrown away.

    A ready that outlives the state which stores what it accepts eats
    exactly one byte here, and the first response still looks perfect:
    it is the second packet that arrives one byte short, so only sending
    two frames with tvalid held high across the boundary can see it.
    """
    await setup(dut)
    rsp = await command(dut, build_load_key(0x31, 4, KEY))
    assert rsp["status"] == ST_OK, f"load_key status {rsp['status']}"

    aad, msg = b"back", b"to back"
    want_ct, want_tag = aead_encrypt(KEY, NONCE, aad, msg)
    packets = [build_seal(req_id, 4, NONCE, aad, msg)
               for req_id in (0x32, 0x33)]

    sink = cocotb.start_soon(recv_packets(dut, len(packets)))
    await send_back_to_back(dut, packets)

    for req_id, raw in zip((0x32, 0x33), await sink):
        rsp = parse_response(raw)
        assert rsp["req_id"] == req_id, \
            f"req_id {rsp['req_id']:#06x}, want {req_id:#06x}"
        assert rsp["status"] == ST_OK, \
            f"status {rsp['status']} for req_id {req_id:#06x}"
        assert rsp["body"] == want_tag + want_ct, \
            f"body mismatch for req_id {req_id:#06x}"


@cocotb.test()
async def test_corrupt_tag_yields_no_plaintext(dut):
    """The security property of the whole design: a failed tag must
    return an error and not one byte of plaintext.

    The leak is asserted before the status on purpose. Break the tag
    comparison in oca_proto.sv and this test has to fail on the
    plaintext coming out, because a failure on the status alone would
    only say the status changed, which is not the property.
    """
    await setup(dut)
    await command(dut, build_load_key(1, 0, KEY))
    msg = b"secret payload that must not leak"
    sealed = await command(dut, build_seal(2, 0, NONCE, b"", msg))
    tag, ct = bytearray(sealed["body"][:16]), sealed["body"][16:]
    tag[0] ^= 0x01
    rsp = await command(dut, build_open(3, 0, NONCE, b"", ct, bytes(tag)))
    assert msg not in rsp["body"], "plaintext present in the response"
    assert rsp["body"] == b"", f"plaintext leaked: {rsp['body']!r}"
    assert rsp["status"] == ST_AUTH_FAIL, f"status {rsp['status']}"


@cocotb.test()
async def test_bad_header_fields(dut):
    await setup(dut)
    bad_magic = b"XX" + build_seal(1, 0, NONCE, b"", b"x")[2:]
    assert (await command(dut, bad_magic))["status"] == ST_BAD_MAGIC

    pkt = bytearray(build_seal(2, 0, NONCE, b"", b"x"))
    pkt[2] = 0x99
    assert (await command(dut, bytes(pkt)))["status"] == ST_BAD_VERSION

    pkt = bytearray(build_seal(3, 0, NONCE, b"", b"x"))
    pkt[3] = 0x7F
    assert (await command(dut, bytes(pkt)))["status"] == ST_BAD_OPCODE


@cocotb.test()
async def test_inconsistent_lengths(dut):
    await setup(dut)
    await command(dut, build_load_key(1, 0, KEY))
    pkt = bytearray(build_seal(2, 0, NONCE, b"aad", b"message"))
    pkt[22] = 0xFF          # msg_len low byte, now far past the packet
    pkt[23] = 0x00
    rsp = await command(dut, bytes(pkt))
    assert rsp["status"] == ST_BAD_LENGTH, f"status {rsp['status']}"


@cocotb.test()
async def test_partial_keep_mid_packet_fails_closed(dut):
    """A short beat before tlast is a length error, not a header drop.

    Only the final beat of a packet may be partial. A short beat
    mid-stream leaves the receive buffer's byte count off a word
    boundary and the next beat lands back on the word just written, so
    nothing read out of the buffer afterwards is what arrived — the
    magic included. oca_proto fails such a packet closed on the keep
    itself, before the header is looked at, which is why the answer is
    05: without that check the magic is read out of a word the packet
    never wrote and the answer is 01 instead.

    The counters say the same thing from the other side. cnt_drop
    counts packets dropped for an *invalid header*, and this packet's
    header was never judged, so it must not move; cnt_rx counts the
    packet like any other, which is also what keeps the assertion on
    cnt_drop from passing merely because the counters stopped moving
    altogether.
    """
    await setup(dut)
    before = counters(await command(dut, build_stats(0x40)))

    pkt = build_seal(0x41, 0, NONCE, b"", b"x")
    beats = [(int.from_bytes(pkt[:4], "little"), 0x0F)] + words_of(pkt[4:])
    for i, (data, keep) in enumerate(beats):
        await send_word(dut, data, keep, i == len(beats) - 1)
    dut.s_axis_tvalid.value = 0
    dut.s_axis_tlast.value = 0
    rsp = parse_response(await recv_packet(dut))
    assert rsp["status"] == ST_BAD_LENGTH, f"status {rsp['status']}"

    after = counters(await command(dut, build_stats(0x42)))
    assert after["rx"] == before["rx"] + 2, \
        f"cnt_rx {before['rx']} -> {after['rx']}, want +2"
    assert after["drop"] == before["drop"], \
        f"cnt_drop moved {before['drop']} -> {after['drop']} on a length error"


@cocotb.test()
async def test_randomised_round_trips(dut):
    await setup(dut)
    rng = random.Random(0x0CA0)
    for i in range(20):
        key = bytes(rng.getrandbits(8) for _ in range(32))
        nonce = bytes(rng.getrandbits(8) for _ in range(12))
        slot = rng.randrange(8)
        alen = rng.choice([0, 1, 16, 63, 64, 65])
        mlen = rng.choice([1, 16, 63, 64, 65, 128, 200])
        aad = bytes(rng.getrandbits(8) for _ in range(alen))
        msg = bytes(rng.getrandbits(8) for _ in range(mlen))
        await command(dut, build_load_key(i, slot, key))
        sealed = await command(dut, build_seal(i, slot, nonce, aad, msg))
        want_ct, want_tag = aead_encrypt(key, nonce, aad, msg)
        assert sealed["status"] == ST_OK, f"#{i} seal status"
        assert sealed["body"][:16] == want_tag, f"#{i} tag"
        assert sealed["body"][16:] == want_ct, f"#{i} ct"
        opened = await command(dut, build_open(
            i, slot, nonce, aad, want_ct, want_tag))
        assert opened["status"] == ST_OK, f"#{i} open status"
        assert opened["body"] == msg, f"#{i} plaintext"


# ---------------------------------------------------------------------------
# Pipelined tests.
#
# Everything above this line drives the DUT through command(), which waits
# for each response before offering the next packet and so drains the very
# pipeline these tests exist to stress. Every test below sends with
# send_back_to_back against a concurrent sink, and sweeps the inter-packet
# gap and the sink's stall probability so the stages slide against each
# other instead of landing on one arbitrary alignment.
# ---------------------------------------------------------------------------

KEY2 = bytes((i * 7 + 3) & 0xFF for i in range(32))
KEY3 = bytes((i * 11 + 5) & 0xFF for i in range(32))
NONCE2 = bytes((i + 100) & 0xFF for i in range(12))
NONCE3 = bytes((i + 200) & 0xFF for i in range(12))


@cocotb.test()
async def test_pipelined_bad_tag_is_not_rescued_by_successor(dut):
    """A successor's parse must not rewrite the tag its predecessor is
    judged on.

    rx_tag was a combinational slice of `args`, the one shift register
    the parser fills for every packet, and it is one of the two operands
    of the comparison that releases plaintext. A successor that parses
    while its predecessor is still computing therefore hands an attacker
    both operands: seal a message, keep the tag T that came back in the
    clear, send open(ct, T xor 1) followed by any packet whose bytes
    24..39 are T, and the comparison becomes T == T. No key knowledge is
    needed. The successor here is a seal with aad_len 0, whose message
    begins at offset 24 and so places T exactly there.

    The leak is asserted before the status: a failure on the status alone
    would only say a label changed, which is not the property.
    """
    await setup(dut)
    await command(dut, build_load_key(0x9F, 0, KEY))

    msg = b"PLAINTEXT-THAT-MUST-NEVER-LEAVE-THE-DEVICE"
    ct, tag = aead_encrypt(KEY, NONCE, b"", msg)
    bad = bytes([tag[0] ^ 0x01]) + tag[1:]

    succ_msg = tag + bytes(range(48))
    succ_ct, succ_tag = aead_encrypt(KEY, NONCE2, b"", succ_msg)
    assert succ_msg[0:16] == tag, "successor must carry T at offsets 24..39"

    for gap in (0, 1, 2, 3, 5, 8, 12, 16, 20, 24):
        for stall_p in (0.0, 0.4, 0.9):
            raws = await pipeline(dut, [
                build_open(0xA0, 0, NONCE, b"", ct, bad),
                build_seal(0xA1, 0, NONCE2, b"", succ_msg),
            ], gap=gap, stall_p=stall_p, seed=0x5EED + gap, raw=True)
            where = f"gap {gap}, stall {stall_p}"
            assert msg not in b"".join(raws), \
                f"plaintext of a rejected ciphertext left the device ({where})"
            bad_rsp, good_rsp = (parse_response(r) for r in raws)
            assert bad_rsp["req_id"] == 0xA0 and good_rsp["req_id"] == 0xA1, \
                f"responses out of order ({where})"
            assert bad_rsp["body"] == b"", \
                f"body returned for a failed tag ({where}): {bad_rsp['body']!r}"
            assert bad_rsp["status"] == ST_AUTH_FAIL, \
                f"status {bad_rsp['status']} for a failed tag ({where})"
            assert good_rsp["status"] == ST_OK, \
                f"successor status {good_rsp['status']} ({where})"
            assert good_rsp["body"] == succ_tag + succ_ct, \
                f"successor body mismatch ({where})"


@cocotb.test()
async def test_pipelined_seal_tags_are_not_swapped(dut):
    """Each response carries the tag of its own message, under its own key.

    resp_tag is written by the engine's done and read by the egress
    stage. With three packets in flight, packet N's done lands while
    N-1 is still streaming: a single instance ships one message's tag
    with another's ciphertext, and the host's own verification then
    fails for no visible reason. The keys differ per slot as well, so a
    key latched from the wrong packet shows up in the ciphertext.
    """
    await setup(dut)
    await command(dut, build_load_key(0x01, 0, KEY))
    await command(dut, build_load_key(0x02, 1, KEY2))

    want = {}
    pkts = []
    for n in range(4):
        slot = n & 1
        key = KEY if slot == 0 else KEY2
        nonce = bytes((i + n * 17) & 0xFF for i in range(12))
        msg = bytes((i * (n + 3) + n) & 0xFF for i in range(64 * (n + 1)))
        ct, tag = aead_encrypt(key, nonce, b"", msg)
        want[0xB0 + n] = (tag, ct)
        pkts.append(build_seal(0xB0 + n, slot, nonce, b"", msg))

    rsps = await pipeline(dut, pkts, stall_p=0.5, seed=0xB0B0)
    for n, rsp in enumerate(rsps):
        req = 0xB0 + n
        tag, ct = want[req]
        assert rsp["req_id"] == req, \
            f"response {n} carries req_id {rsp['req_id']:#06x}, want {req:#06x}"
        assert rsp["status"] == ST_OK, f"{req:#06x} status {rsp['status']}"
        assert rsp["body"][:16] == tag, f"{req:#06x} tag belongs to another message"
        assert rsp["body"][16:] == ct, f"{req:#06x} ciphertext mismatch"
    others = {req: t for req, (t, _) in want.items()}
    for n, rsp in enumerate(rsps):
        for req, tag in others.items():
            if req != 0xB0 + n:
                assert rsp["body"][:16] != tag, \
                    f"response {n} shipped the tag of {req:#06x}"


@cocotb.test()
async def test_open_does_not_reuse_a_stale_done(dut):
    """A stale eng_done_seen would let a packet fall through before its
    own tag exists, comparing against an earlier message's tag -- a way
    to accept a forgery, not merely a correctness bug."""
    await setup(dut)
    await command(dut, build_load_key(0x03, 2, KEY))

    msg = b"three opens in a row, the middle one corrupt"
    ct, tag = aead_encrypt(KEY, NONCE, b"", msg)
    bad = bytes([tag[0] ^ 0x80]) + tag[1:]

    for gap in (0, 2, 5, 9, 14, 21):
        for stall_p in (0.0, 0.6):
            rsps = await pipeline(dut, [
                build_open(0xE0, 2, NONCE, b"", ct, tag),
                build_open(0xE1, 2, NONCE, b"", ct, bad),
                build_open(0xE2, 2, NONCE, b"", ct, tag),
            ], gap=gap, stall_p=stall_p, seed=0xE0 + gap)
            where = f"gap {gap}, stall {stall_p}"
            got = [r["status"] for r in rsps]
            assert [r["req_id"] for r in rsps] == [0xE0, 0xE1, 0xE2], \
                f"responses out of order ({where})"
            assert rsps[1]["body"] == b"", \
                f"plaintext leaked from the corrupt open ({where})"
            assert got == [ST_OK, ST_AUTH_FAIL, ST_OK], \
                f"statuses {got} ({where})"
            assert rsps[0]["body"] == msg and rsps[2]["body"] == msg, \
                f"plaintext mismatch ({where})"


@cocotb.test()
async def test_failed_open_leaks_nothing_into_a_pipelined_neighbour(dut):
    """A rejected packet contributes zero bytes to anything that leaves.

    Two mechanisms make this the sharpest test in the file. A response
    sized from a live write counter its neighbour is moving ships the
    neighbour's bytes; and oca_pktbuf's clear resets the count and never
    the memory, so a failed open's plaintext stays resident with a live
    pointer aimed at it. The marker is searched for in the raw bytes of
    *every* response, not in the parsed bodies, because that is where
    either mechanism would put it.
    """
    await setup(dut)
    await command(dut, build_load_key(0x04, 6, KEY))

    m1 = b"MARKER-ONE-MUST-NOT-ESCAPE-EVER!" + bytes(268)
    m2 = b"MARKER-TWO-IS-THE-GOOD-ONE-HERE!" + bytes(268)
    ct1, tag1 = aead_encrypt(KEY, NONCE, b"", m1)
    ct2, tag2 = aead_encrypt(KEY, NONCE2, b"", m2)
    bad1 = bytes([tag1[0] ^ 0x01]) + tag1[1:]

    before = counters(await command(dut, build_stats(0x50)))

    for order, stall_p in (("bad-first", 0.0), ("bad-first", 0.9),
                           ("good-first", 0.0), ("good-first", 0.9)):
        bad_pkt = build_open(0xF0, 6, NONCE, b"", ct1, bad1)
        good_pkt = build_open(0xF1, 6, NONCE2, b"", ct2, tag2)
        pkts = ([bad_pkt, good_pkt] if order == "bad-first"
                else [good_pkt, bad_pkt])
        pkts.append(build_stats(0xF2))

        raws = await pipeline(dut, pkts, stall_p=stall_p,
                              seed=0xF0F0, raw=True)
        blob = b"".join(raws)
        assert m1[:32] not in blob, \
            f"marker of the rejected packet left the device ({order}, {stall_p})"
        rsps = [parse_response(r) for r in raws]
        by_id = {r["req_id"]: r for r in rsps}
        assert [r["req_id"] for r in rsps] == [p[4] | (p[5] << 8) for p in pkts], \
            f"responses out of order ({order}, {stall_p})"
        assert by_id[0xF0]["body"] == b"", f"body for a failed tag ({order})"
        assert by_id[0xF0]["status"] == ST_AUTH_FAIL, f"status ({order})"
        assert by_id[0xF1]["status"] == ST_OK, f"good open status ({order})"
        assert by_id[0xF1]["body"] == m2, f"good open plaintext ({order})"

    after = counters(await command(dut, build_stats(0x51)))
    assert after["auth"] == before["auth"] + 4, \
        f"cnt_auth_fail {before['auth']} -> {after['auth']}, want +4"


@cocotb.test()
async def test_pipelined_load_key_uses_only_its_own_bytes(dut):
    """A load-key takes its 32 bytes from its own packet and no other.

    args is the source of ks_wr_key. A neighbour parsing into it while
    the load-key is committing writes a key an attacker chose the bytes
    of -- the failure the exact-length check exists to prevent,
    reintroduced from the other side. The packet behind the load-key
    here is 32 bytes of 0xAA for exactly that reason.
    """
    await setup(dut)
    await command(dut, build_load_key(0x05, 0, KEY))

    big = bytes((i * 5) & 0xFF for i in range(2000))
    poison = bytes([0xAA]) * 32
    rsps = await pipeline(dut, [
        build_seal(0xC8, 0, NONCE, b"", big),
        build_load_key(0xC9, 3, KEY3),
        build_seal(0xCA, 0, NONCE2, b"", poison),
    ], stall_p=0.3, seed=0xC8C8)
    assert [r["req_id"] for r in rsps] == [0xC8, 0xC9, 0xCA]
    assert [r["status"] for r in rsps] == [ST_OK, ST_OK, ST_OK], \
        f"statuses {[r['status'] for r in rsps]}"

    probe = b"does slot 3 hold the key its own packet carried?"
    want_ct, want_tag = aead_encrypt(KEY3, NONCE3, b"", probe)
    rsp = await command(dut, build_seal(0xCB, 3, NONCE3, b"", probe))
    assert rsp["status"] == ST_OK, f"probe status {rsp['status']}"
    assert rsp["body"] == want_tag + want_ct, \
        "slot 3 does not hold the key its own load-key packet carried"


@cocotb.test()
async def test_pipelined_seal_reads_only_its_own_payload(dut):
    """A packet's payload reads come from bytes that packet wrote."""
    await setup(dut)
    await command(dut, build_load_key(0x06, 0, KEY))

    zeros = bytes(1000)
    marked = b"UNIQUE-MARKER-IN-THE-SECOND-PACKET-ONLY!"
    z_ct, z_tag = aead_encrypt(KEY, NONCE, b"", zeros)
    m_ct, m_tag = aead_encrypt(KEY, NONCE2, b"", marked)

    rsps = await pipeline(dut, [
        build_seal(0xD8, 0, NONCE, b"", zeros),
        build_seal(0xD9, 0, NONCE2, b"", marked),
    ], stall_p=0.4, seed=0xD8D8)
    assert [r["req_id"] for r in rsps] == [0xD8, 0xD9]
    assert rsps[0]["body"] == z_tag + z_ct, "first seal read a neighbour's bytes"
    assert rsps[1]["body"] == m_tag + m_ct, "second seal mismatch"


@cocotb.test()
async def test_unaligned_packet_lengths_pipeline_cleanly(dut):
    """Eight requests in a row, none a multiple of eight bytes long.

    A scheme that gives each in-flight packet a base pointer rather than
    its own bank loses word alignment here as the normal case, and the
    funnel then produces plausible-looking wrong data instead of failing.
    """
    await setup(dut)
    await command(dut, build_load_key(0x07, 0, KEY))

    pkts, want = [], []
    for n, total in enumerate((41, 43, 47, 53, 59, 61, 67, 71)):
        nonce = bytes((i + n * 5) & 0xFF for i in range(12))
        msg = bytes((i * 3 + n) & 0xFF for i in range(total - 24))
        pkt = build_seal(0x70 + n, 0, nonce, b"", msg)
        assert len(pkt) == total and total % 8, f"{total} is not what it claims"
        pkts.append(pkt)
        ct, tag = aead_encrypt(KEY, nonce, b"", msg)
        want.append(tag + ct)

    rsps = await pipeline(dut, pkts, stall_p=0.35, seed=0x7070)
    for n, (rsp, body) in enumerate(zip(rsps, want)):
        assert rsp["req_id"] == 0x70 + n, f"#{n} out of order"
        assert rsp["status"] == ST_OK, f"#{n} status {rsp['status']}"
        assert rsp["body"] == body, f"#{n} body mismatch"


@cocotb.test()
async def test_load_key_is_ordered_against_a_pipelined_seal(dut):
    """A seal behind a load-key encrypts under the new key.

    ks_rd_slot is driven at the head of a command and ks_rd_key sampled
    several cycles later; ks_wr_en fires deeper in. A seal that issues
    its lookup before the load-key in front of it has committed encrypts
    under K_old with a nonce the host chose for K_new -- and re-keying
    is exactly when a host restarts a nonce counter, so that pair is the
    one most likely to have been used before. The test names the
    dangerous outcome as well as the right one.
    """
    await setup(dut)
    await command(dut, build_load_key(0x08, 5, KEY))

    msg = b"encrypted under whichever key won the race"
    new_ct, new_tag = aead_encrypt(KEY2, NONCE, b"", msg)
    old_ct, old_tag = aead_encrypt(KEY, NONCE, b"", msg)

    for gap in (0, 1, 2, 3, 4, 6, 8, 11, 16):
        for stall_p in (0.0, 0.5):
            rsps = await pipeline(dut, [
                build_load_key(0x80, 5, KEY2),
                build_seal(0x81, 5, NONCE, b"", msg),
            ], gap=gap, stall_p=stall_p, seed=0x8080 + gap)
            where = f"gap {gap}, stall {stall_p}"
            assert [r["req_id"] for r in rsps] == [0x80, 0x81], \
                f"out of order ({where})"
            assert rsps[1]["status"] == ST_OK, \
                f"seal status {rsps[1]['status']} ({where})"
            assert rsps[1]["body"] != old_tag + old_ct, \
                f"seal encrypted under the key the load-key replaced ({where})"
            assert rsps[1]["body"] == new_tag + new_ct, \
                f"seal body mismatch ({where})"
            # put the slot back so the next sweep step starts equal
            await command(dut, build_load_key(0x82, 5, KEY))


@cocotb.test()
async def test_responses_leave_in_arrival_order(dut):
    """Responses leave in the order their requests arrived.

    The cheap paths and the expensive ones differ by three orders of
    magnitude in length and all of them converge on one completion
    point. A malformed packet answering ahead of a large seal in front
    of it is recoverable for a host that matches on req_id, but the wire
    format promises order and no host has been written yet, so shipping
    the reordering silently is the class of change that gets found on a
    board. The assertion is positional, not a set comparison.
    """
    await setup(dut)
    await command(dut, build_load_key(0x09, 0, KEY))

    big = bytes((i * 13) & 0xFF for i in range(1400))
    ct, tag = aead_encrypt(KEY, NONCE, b"", big)
    bad_magic = b"XX" + build_seal(0xC1, 0, NONCE, b"", b"x")[2:]
    bad_opcode = bytearray(build_seal(0xC5, 0, NONCE, b"", b"x"))
    bad_opcode[3] = 0x7F

    order = [0xC0, 0xC1, 0xC2, 0xC3, 0xC4, 0xC5]
    for stall_p in (0.0, 0.7):
        rsps = await pipeline(dut, [
            build_seal(0xC0, 0, NONCE, b"", big),
            bad_magic,
            build_load_key(0xC2, 4, KEY2),
            build_stats(0xC3),
            build_open(0xC4, 0, NONCE, b"", ct, tag),
            bytes(bad_opcode),
        ], stall_p=stall_p, seed=0xC0C0)
        got = [r["req_id"] for r in rsps]
        assert got == order, \
            f"arrival order {[hex(x) for x in order]}, got {[hex(x) for x in got]}"
        assert rsps[0]["body"] == tag + ct, "the big seal came back wrong"
        assert rsps[4]["body"] == big, "the big open came back wrong"
        assert rsps[1]["status"] == ST_BAD_MAGIC
        assert rsps[5]["status"] == ST_BAD_OPCODE


@cocotb.test()
async def test_short_packet_does_not_disturb_neighbours(dut):
    """The one packet that legitimately gets no answer must not move the
    ones around it."""
    await setup(dut)
    await command(dut, build_load_key(0x0A, 0, KEY))
    before = counters(await command(dut, build_stats(0x60)))

    msg = bytes((i * 3) & 0xFF for i in range(512))
    ct, tag = aead_encrypt(KEY, NONCE, b"", msg)
    rsps = await pipeline(dut, [
        build_seal(0x61, 0, NONCE, b"", msg),
        b"\x4f\x43\x01\x02",                 # shorter than a header
        build_seal(0x62, 0, NONCE, b"", msg),
    ], nresp=2, stall_p=0.5, seed=0x6161)

    assert [r["req_id"] for r in rsps] == [0x61, 0x62], "neighbours reordered"
    for rsp in rsps:
        assert rsp["status"] == ST_OK, f"status {rsp['status']}"
        assert rsp["body"] == tag + ct, "neighbour body mismatch"

    after = counters(await command(dut, build_stats(0x63)))
    assert after["drop"] == before["drop"] + 1, \
        f"cnt_drop {before['drop']} -> {after['drop']}, want +1"


@cocotb.test()
async def test_response_headers_survive_a_stalled_sink(dut):
    """(req_id, opcode, slot, status) describe the request they answer.

    resp_hdr was combinational on registers the next packet's dispatch
    overwrites, and it is sampled at the beat rather than when the
    response is sized. Under a sink that leaves every response frozen
    mid-stream, a fail-open mislabel puts 00 on a body that failed
    authentication and a fail-closed one puts 06 on a ciphertext that
    verified. Body and label are checked together, because the shape and
    the label are derived from the same four registers and can disagree.
    """
    await setup(dut)
    await command(dut, build_load_key(0x0B, 0, KEY))

    msg = bytes((i * 9 + 1) & 0xFF for i in range(200))
    ct, tag = aead_encrypt(KEY, NONCE, b"", msg)
    bad = bytes([tag[0] ^ 0x40]) + tag[1:]
    bad_magic = b"XX" + build_seal(0x92, 0, NONCE, b"", b"x")[2:]

    pkts = [
        build_seal(0x90, 0, NONCE, b"", msg),
        build_open(0x91, 0, NONCE, b"", ct, tag),
        bad_magic,
        build_open(0x93, 0, NONCE, b"", ct, bad),
        build_seal(0x94, 7, NONCE, b"", b"x"),     # slot 7 was never loaded
        build_stats(0x95),
    ]
    want = [
        (0x90, OP_SEAL, 0, ST_OK, tag + ct),
        (0x91, OP_OPEN, 0, ST_OK, msg),
        (0x92, OP_SEAL, 0, ST_BAD_MAGIC, b""),
        (0x93, OP_OPEN, 0, ST_AUTH_FAIL, b""),
        (0x94, OP_SEAL, 7, ST_BAD_SLOT, b""),
        (0x95, OP_STATS, 0, ST_OK, None),
    ]

    for seed in (0x9501, 0x9502, 0x9503):
        rsps = await pipeline(dut, pkts, stall_p=0.85, seed=seed)
        for rsp, (req, op, slot, st, body) in zip(rsps, want):
            got = (rsp["req_id"], rsp["opcode"], rsp["slot"], rsp["status"])
            assert got == (req, op, slot, st), \
                f"seed {seed:#x}: header {got}, want {(req, op, slot, st)}"
            if body is None:
                assert len(rsp["body"]) == 16, \
                    f"seed {seed:#x}: stats body {len(rsp['body'])} bytes"
            else:
                assert rsp["body"] == body, \
                    f"seed {seed:#x}: body of {req:#04x} disagrees with its label"


@cocotb.test()
async def test_counters_are_exact_under_a_deep_pipeline(dut):
    """Counters report what happened, with the stats snapshot a prefix of
    the command sequence.

    Every assertion is an equality. An undercount from two stages
    targeting one counter in the same cycle is precisely what an
    inequality would hide, in the one command that exists because silent
    failures are the slowest to diagnose.
    """
    await setup(dut)
    await command(dut, build_load_key(0x0C, 0, KEY))

    msg = bytes((i * 11) & 0xFF for i in range(300))
    ct, tag = aead_encrypt(KEY, NONCE, b"", msg)
    bad = bytes([tag[0] ^ 0x11]) + tag[1:]

    def bad_field(idx, val, req):
        p = bytearray(build_seal(req, 0, NONCE, b"", b"x"))
        p[idx] = val
        return bytes(p)

    pkts = []
    pkts += [build_seal(0x100 + n, 0, NONCE, b"", msg) for n in range(6)]
    pkts += [build_open(0x110 + n, 0, NONCE, b"", ct, tag) for n in range(4)]
    pkts += [build_open(0x120 + n, 0, NONCE, b"", ct, bad) for n in range(4)]
    pkts += [b"XX" + build_seal(0x130 + n, 0, NONCE, b"", b"x")[2:]
             for n in range(3)]
    pkts += [bad_field(2, 0x99, 0x140 + n) for n in range(2)]
    pkts += [bad_field(3, 0x7F, 0x150 + n) for n in range(2)]
    # slots the seals and opens above do not use: a load-key landing on
    # slot 0 would re-key it mid-stream and fail the opens for a reason
    # that has nothing to do with the pipeline
    pkts += [build_load_key(0x160 + n, 6 + n, KEY2) for n in range(2)]
    pkts += [b"\x4f\x43\x01\x02"]
    rng = random.Random(0xC0117)
    rng.shuffle(pkts)

    want_drop = 3 + 2 + 2 + 1           # magic, version, opcode, too short
    want_auth = 4
    want_done = 6 + 4 + 2               # seals, opens, load-keys
    nresp = len(pkts) - 1               # the short packet gets no answer

    before = counters(await command(dut, build_stats(0x170)))
    rsps = await pipeline(dut, pkts, nresp=nresp, stall_p=0.4, seed=0xC017)
    assert len(rsps) == nresp
    got_ok = sum(1 for r in rsps if r["status"] == ST_OK)
    after = counters(await command(dut, build_stats(0x171)))

    assert after["rx"] == before["rx"] + len(pkts) + 1, \
        f"cnt_rx {before['rx']} -> {after['rx']}, want +{len(pkts) + 1}"
    assert after["drop"] == before["drop"] + want_drop, \
        f"cnt_drop {before['drop']} -> {after['drop']}, want +{want_drop}"
    assert after["auth"] == before["auth"] + want_auth, \
        f"cnt_auth_fail {before['auth']} -> {after['auth']}, want +{want_auth}"
    assert got_ok == want_done, f"{got_ok} status-00 responses, want {want_done}"
    # + 1 for the earlier stats command itself: its snapshot is taken
    # before it completes, so it is absent from `before` and present in
    # `after`. That is the "never counts itself" property, seen from the
    # far side.
    assert after["done"] == before["done"] + want_done + 1, \
        f"cnt_done {before['done']} -> {after['done']}, want +{want_done + 1}"


@cocotb.test()
async def test_block_boundary_lengths_under_overlap(dut):
    """The 64/65 and 127/128 boundaries, four packets at a time.

    A prefetch off by one block shows up here and nowhere else: the
    ciphertext is short, the MAC is computed over the blocks that did
    arrive, and the tag is a perfectly valid tag over a truncated
    message.
    """
    await setup(dut)
    await command(dut, build_load_key(0x0D, 0, KEY))

    sizes = [(a, m) for a in (0, 1, 63, 64, 65, 127, 128)
             for m in (0, 1, 63, 64, 65, 127, 128)]
    sizes += [(a, 1408) for a in (0, 63, 64, 65, 127)]

    for base in range(0, len(sizes), 4):
        batch = sizes[base:base + 4]
        pkts, want = [], []
        for n, (alen, mlen) in enumerate(batch):
            req = 0x200 + base + n
            nonce = bytes((i + req) & 0xFF for i in range(12))
            aad = bytes((i * 7 + n) & 0xFF for i in range(alen))
            msg = bytes((i * 5 + n + 1) & 0xFF for i in range(mlen))
            pkts.append(build_seal(req, 0, nonce, aad, msg))
            ct, tag = aead_encrypt(KEY, nonce, aad, msg)
            want.append((req, tag + ct))
        rsps = await pipeline(dut, pkts, stall_p=0.3, seed=0x2000 + base)
        for rsp, (req, body), (alen, mlen) in zip(rsps, want, batch):
            assert rsp["req_id"] == req, f"aad {alen} msg {mlen}: out of order"
            assert rsp["status"] == ST_OK, \
                f"aad {alen} msg {mlen}: status {rsp['status']}"
            assert rsp["body"] == body, \
                f"aad {alen} msg {mlen}: body {len(rsp['body'])} bytes, " \
                f"want {len(body)}"


@cocotb.test()
async def test_maximum_packet_size_is_not_reduced(dut):
    """A change that improves throughput must not shrink what is accepted.

    Splitting each buffer into two regions of half the size would answer
    05 to packets that worked yesterday, including the standard-MTU case
    the design declares supported, and len_bad would announce it on a
    board rather than here. MAX_REQUEST is one word short of BYTES
    because a request of exactly BYTES leaves the write counter at BYTES,
    which oca_pktbuf already calls full.
    """
    await setup(dut)
    await command(dut, build_load_key(0x0E, 0, KEY))

    for total, want_status in ((MAX_REQUEST - 8, ST_OK),
                               (MAX_REQUEST, ST_OK),
                               (MAX_REQUEST + 8, ST_BAD_LENGTH)):
        msg = bytes((i * 3) & 0xFF for i in range(total - 24))
        rsp = await command(dut, build_seal(0x300, 0, NONCE, b"", msg))
        assert rsp["status"] == want_status, \
            f"seal of {total} bytes: status {rsp['status']}, want {want_status}"
        if want_status == ST_OK:
            ct, tag = aead_encrypt(KEY, NONCE, b"", msg)
            assert rsp["body"] == tag + ct, f"seal of {total} bytes: body"

        ctl = total - 40
        pt = bytes((i * 7) & 0xFF for i in range(ctl))
        octt, otag = aead_encrypt(KEY, NONCE2, b"", pt)
        rsp = await command(dut, build_open(0x301, 0, NONCE2, b"", octt, otag))
        assert rsp["status"] == want_status, \
            f"open of {total} bytes: status {rsp['status']}, want {want_status}"
        if want_status == ST_OK:
            assert rsp["body"] == pt, f"open of {total} bytes: body"

    # the case a two-region scheme must still accept and a halved one
    # silently rejects
    msg = bytes((i * 3) & 0xFF for i in range(MAX_REQUEST - 24))
    ct, tag = aead_encrypt(KEY, NONCE, b"", msg)
    rsps = await pipeline(dut, [build_seal(0x310, 0, NONCE, b"", msg),
                                build_seal(0x311, 0, NONCE, b"", msg)])
    for n, rsp in enumerate(rsps):
        assert rsp["status"] == ST_OK, \
            f"back-to-back maximum seal #{n}: status {rsp['status']}"
        assert rsp["body"] == tag + ct, f"back-to-back maximum seal #{n}: body"


@cocotb.test()
async def test_pipelined_backpressure_is_transparent(dut):
    """Three packets back to back with the sink stalling half the time.

    This is what catches a new pipeline stage losing the word the output
    register still owes: oca_pktbuf reads unconditionally every edge, so
    a stage that freezes its address without presenting the stage-2 word
    drops one beat and the response is eight bytes short.
    """
    rng = random.Random(20260804)
    await setup(dut)
    await command(dut, build_load_key(0x0F, 1, KEY))

    want, pkts = [], []
    for n in range(3):
        aad = bytes(rng.randrange(256) for _ in range(n * 31))
        msg = bytes(rng.randrange(256) for _ in range(70 + n * 97))
        pkts.append(build_seal(0x320 + n, 1, NONCE, aad, msg))
        ct, tag = aead_encrypt(KEY, NONCE, aad, msg)
        want.append(tag + ct)

    for seed in (0x3201, 0x3202):
        rsps = await pipeline(dut, pkts, stall_p=0.5, seed=seed)
        for n, (rsp, body) in enumerate(zip(rsps, want)):
            assert rsp["req_id"] == 0x320 + n, f"seed {seed:#x}: #{n} order"
            assert rsp["status"] == ST_OK, f"seed {seed:#x}: #{n} status"
            assert rsp["body"] == body, f"seed {seed:#x}: #{n} body"
