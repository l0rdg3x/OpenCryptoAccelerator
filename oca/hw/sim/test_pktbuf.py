# SPDX-License-Identifier: MIT
"""Packet buffer: words come back at the word address they went in, the
counter tracks the write position in bytes, and the full flag fires at
BYTES."""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

BYTES = 2048
WORDS = BYTES // 8


async def setup(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst_n.value = 0
    dut.wr_en.value = 0
    dut.wr_data.value = 0
    dut.wr_bytes.value = 0
    dut.wr_clear.value = 0
    dut.rd_addr.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)


async def write_words(dut, words):
    """Write back to back, one (value, valid bytes) pair per cycle."""
    for value, nbytes in words:
        dut.wr_data.value = value
        dut.wr_bytes.value = nbytes
        dut.wr_en.value = 1
        await RisingEdge(dut.clk)
    dut.wr_en.value = 0
    await RisingEdge(dut.clk)


async def write_word(dut, value: int, nbytes: int = 8):
    await write_words(dut, [(value, nbytes)])


async def write_bytes(dut, data: bytes):
    """Split into little-endian words; only the last one may be partial."""
    words = []
    for off in range(0, len(data), 8):
        chunk = data[off:off + 8]
        words.append((int.from_bytes(chunk.ljust(8, b"\x00"), "little"), len(chunk)))
    await write_words(dut, words)


async def read_word(dut, addr: int) -> int:
    dut.rd_addr.value = addr
    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)
    return int(dut.rd_data.value)


@cocotb.test()
async def test_write_then_read_back(dut):
    await setup(dut)
    rng = random.Random(0xB0FF)
    payload = bytes(rng.getrandbits(8) for _ in range(304))
    await write_bytes(dut, payload)
    assert int(dut.wr_count.value) == len(payload)
    for addr in (0, 1, 7, 8, 9, 37):
        got = await read_word(dut, addr)
        want = payload[addr * 8:addr * 8 + 8]
        assert got.to_bytes(8, "little") == want, f"word {addr}: {got:#018x}"


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
    w0 = await read_word(dut, 0)
    assert w0.to_bytes(8, "little")[:6] == b"second"


@cocotb.test()
async def test_full_flag_and_no_overrun(dut):
    await setup(dut)
    await write_bytes(dut, bytes(BYTES))
    assert int(dut.wr_full.value) == 1, "full not asserted at capacity"
    before = int(dut.wr_count.value)
    await write_word(dut, 0xFFFFFFFFFFFFFFFF, 8)
    assert int(dut.wr_count.value) == before, "counter moved past capacity"
    assert await read_word(dut, 0) == 0, "overrun wrapped and corrupted word 0"


@cocotb.test()
async def test_partial_final_word(dut):
    """A final word with 3 valid bytes advances wr_count by 3, not 8."""
    await setup(dut)
    await write_word(dut, int.from_bytes(b"abcdefgh", "little"), 8)
    await write_word(dut, int.from_bytes(b"xyz00000", "little"), 3)
    assert int(dut.wr_count.value) == 11, f"count {int(dut.wr_count.value)}"
    w0 = await read_word(dut, 0)
    assert w0.to_bytes(8, "little") == b"abcdefgh"
    w1 = await read_word(dut, 1)
    assert w1.to_bytes(8, "little")[:3] == b"xyz"


@cocotb.test()
async def test_read_past_capacity_is_clamped(dut):
    """An out-of-range word address reads word 0, never wraps into data.

    WORDS + 1 truncates to word 1 if the range check is dropped, which is
    what makes this assertion able to fail."""
    await setup(dut)
    await write_bytes(dut, b"headword" + b"\x11" * 8)
    got = await read_word(dut, WORDS + 1)
    assert got == int.from_bytes(b"headword", "little"), f"{got:#018x}"
