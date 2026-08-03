# SPDX-License-Identifier: MIT
"""End-to-end tests for oca_core: packets in, packets out.

Requests are injected on the 8-bit stream that verilog-ethernet will
later drive, so this runs with no Ethernet in the simulation. Every
expected value comes from aead_model through proto_model.
"""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

from aead_model import aead_encrypt
from proto_model import (HDR_LEN, OP_LOAD_KEY, OP_OPEN, OP_SEAL, ST_BAD_SLOT,
                         ST_OK, build_load_key, build_open, build_seal,
                         parse_response)

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


async def send_packet(dut, pkt: bytes):
    for i, b in enumerate(pkt):
        dut.s_axis_tdata.value = b
        dut.s_axis_tlast.value = 1 if i == len(pkt) - 1 else 0
        dut.s_axis_tvalid.value = 1
        await RisingEdge(dut.clk)
        while dut.s_axis_tready.value == 0:
            await RisingEdge(dut.clk)
    dut.s_axis_tvalid.value = 0
    dut.s_axis_tlast.value = 0


async def recv_packet(dut, budget: int = 20000) -> bytes:
    out = bytearray()
    for _ in range(budget):
        await RisingEdge(dut.clk)
        if dut.m_axis_tvalid.value == 1:
            out.append(int(dut.m_axis_tdata.value))
            if dut.m_axis_tlast.value == 1:
                return bytes(out)
    raise AssertionError(f"no response within {budget} cycles "
                         f"({len(out)} bytes seen)")


async def command(dut, pkt: bytes) -> dict:
    await send_packet(dut, pkt)
    return parse_response(await recv_packet(dut))


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
