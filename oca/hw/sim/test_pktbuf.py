# SPDX-License-Identifier: MIT
"""Packet buffer: bytes come back at the offset they went in, the
counter tracks the write position, and the full flag fires at BYTES."""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

BYTES = 2048


async def setup(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst_n.value = 0
    dut.wr_en.value = 0
    dut.wr_data.value = 0
    dut.wr_clear.value = 0
    dut.rd_addr.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)


async def write_bytes(dut, data: bytes):
    for b in data:
        dut.wr_data.value = b
        dut.wr_en.value = 1
        await RisingEdge(dut.clk)
    dut.wr_en.value = 0
    await RisingEdge(dut.clk)


async def read_byte(dut, addr: int) -> int:
    dut.rd_addr.value = addr
    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)
    return int(dut.rd_data.value)


@cocotb.test()
async def test_write_then_read_back(dut):
    await setup(dut)
    rng = random.Random(0xB0FF)
    payload = bytes(rng.getrandbits(8) for _ in range(300))
    await write_bytes(dut, payload)
    assert int(dut.wr_count.value) == len(payload)
    for addr in (0, 1, 63, 64, 65, 299):
        got = await read_byte(dut, addr)
        assert got == payload[addr], f"offset {addr}: {got} != {payload[addr]}"


@cocotb.test()
async def test_clear_restarts_at_zero(dut):
    await setup(dut)
    await write_bytes(dut, b"first")
    dut.wr_clear.value = 1
    await RisingEdge(dut.clk)
    dut.wr_clear.value = 0
    await RisingEdge(dut.clk)
    assert int(dut.wr_count.value) == 0
    await write_bytes(dut, b"second")
    assert int(dut.wr_count.value) == 6
    assert await read_byte(dut, 0) == ord("s")


@cocotb.test()
async def test_full_flag_and_no_overrun(dut):
    await setup(dut)
    await write_bytes(dut, bytes(BYTES))
    assert int(dut.wr_full.value) == 1, "full not asserted at capacity"
    before = int(dut.wr_count.value)
    await write_bytes(dut, b"\xff" * 4)
    assert int(dut.wr_count.value) == before, "counter moved past capacity"
    assert await read_byte(dut, 0) == 0, "overrun wrapped and corrupted offset 0"
