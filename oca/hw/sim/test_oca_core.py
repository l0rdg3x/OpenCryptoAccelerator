# SPDX-License-Identifier: MIT
"""End-to-end tests for oca_core: packets in, packets out.

Requests are injected on the 8-bit stream that verilog-ethernet will
later drive, so this runs with no Ethernet in the simulation. Every
expected value comes from aead_model through proto_model.
"""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from aead_model import aead_encrypt
from proto_model import (HDR_LEN, OP_LOAD_KEY, OP_OPEN, OP_SEAL, ST_AUTH_FAIL,
                         ST_BAD_LENGTH, ST_BAD_MAGIC, ST_BAD_OPCODE,
                         ST_BAD_SLOT, ST_BAD_VERSION, ST_OK, build_load_key,
                         build_open, build_seal, parse_response)

KEY = bytes(range(32))
NONCE = bytes(range(12))


async def setup(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst_n.value = 0
    dut.s_axis_tdata.value = 0
    dut.s_axis_tvalid.value = 0
    dut.s_axis_tlast.value = 0
    dut.m_axis_tready.value = 1
    for _ in range(3):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)


async def send_byte(dut, b: int, last: bool):
    """Offer one byte as an AXI-Stream source.

    tdata, tlast and tvalid are held stable until tready is seen high in
    the read-only phase of a cycle; the transfer is the edge that ends
    that cycle. Sampling the handshake before the edge rather than after
    it is what keeps this a source the RTL cannot distinguish from
    verilog-ethernet.
    """
    dut.s_axis_tdata.value = b
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
    for i, b in enumerate(pkt):
        if rng is not None:
            gap = rng.randint(0, max_gap)
            if gap:
                dut.s_axis_tvalid.value = 0
                for _ in range(gap):
                    await RisingEdge(dut.clk)
        await send_byte(dut, b, i == len(pkt) - 1)
    dut.s_axis_tvalid.value = 0
    dut.s_axis_tlast.value = 0


async def recv_packet(dut, budget: int = 20000,
                      rng: random.Random | None = None,
                      stall_p: float = 0.0) -> bytes:
    """Collect one response as an AXI-Stream sink.

    A byte is taken only where tvalid and tready are both high in the
    read-only phase, which is the cycle the transfer edge ends.
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
            out.append(int(dut.m_axis_tdata.value))
        await RisingEdge(dut.clk)
        if last:
            dut.m_axis_tready.value = 1
            return bytes(out)
    raise AssertionError(f"no response within {budget} cycles "
                         f"({len(out)} bytes seen)")


async def send_back_to_back(dut, pkts):
    """Send several packets without ever lowering tvalid between them.

    This is the source verilog-ethernet becomes when a frame is already
    queued behind the one going out: the first byte of the next packet
    is offered in the cycle right after tlast.
    """
    for pkt in pkts:
        for i, b in enumerate(pkt):
            await send_byte(dut, b, i == len(pkt) - 1)
    dut.s_axis_tvalid.value = 0
    dut.s_axis_tlast.value = 0


async def recv_packets(dut, count: int) -> list:
    return [await recv_packet(dut) for _ in range(count)]


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
