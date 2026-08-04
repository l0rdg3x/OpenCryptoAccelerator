# SPDX-License-Identifier: MIT
"""Key slots: a slot reads back what was written, an unwritten or
out-of-range slot reports invalid, and reset clears everything."""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

NUM_SLOTS = 8


async def setup(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst_n.value = 0
    dut.wr_en.value = 0
    dut.wr_slot.value = 0
    dut.wr_key.value = 0
    dut.rd_slot.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)


async def write_slot(dut, slot: int, key: bytes):
    dut.wr_slot.value = slot
    dut.wr_key.value = int.from_bytes(key, "little")
    dut.wr_en.value = 1
    await RisingEdge(dut.clk)
    dut.wr_en.value = 0
    await RisingEdge(dut.clk)


async def read_slot(dut, slot: int):
    dut.rd_slot.value = slot
    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)
    return int(dut.rd_key.value).to_bytes(32, "little"), int(dut.rd_valid.value)


@cocotb.test()
async def test_unwritten_slots_are_invalid(dut):
    await setup(dut)
    for slot in range(NUM_SLOTS):
        _, valid = await read_slot(dut, slot)
        assert valid == 0, f"slot {slot} valid before any write"


@cocotb.test()
async def test_write_then_read(dut):
    await setup(dut)
    rng = random.Random(0x5107)
    keys = {}
    for slot in range(NUM_SLOTS):
        keys[slot] = bytes(rng.getrandbits(8) for _ in range(32))
        await write_slot(dut, slot, keys[slot])
    for slot in range(NUM_SLOTS):
        got, valid = await read_slot(dut, slot)
        assert valid == 1, f"slot {slot} invalid after write"
        assert got == keys[slot], f"slot {slot}: {got.hex()} != {keys[slot].hex()}"


@cocotb.test()
async def test_out_of_range_slot_is_invalid(dut):
    await setup(dut)
    await write_slot(dut, 0, b"\xaa" * 32)
    for slot in (NUM_SLOTS, NUM_SLOTS + 1, 255):
        _, valid = await read_slot(dut, slot)
        assert valid == 0, f"out-of-range slot {slot} reported valid"


@cocotb.test()
async def test_reset_clears(dut):
    await setup(dut)
    await write_slot(dut, 3, b"\x5a" * 32)
    _, valid = await read_slot(dut, 3)
    assert valid == 1
    dut.rst_n.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)
    key, valid = await read_slot(dut, 3)
    assert valid == 0, "slot still valid after reset"
    assert key == bytes(32), "key material survived reset"
