# SPDX-License-Identifier: MIT
"""The FIFO, held to the two things a console depends on.

Order, and that a full FIFO refuses rather than absorbs. Everything else
about a FIFO is arithmetic that either works for every depth or fails on
the first wrap, so the tests drive past the wrap deliberately: a pointer
scheme that cannot tell full from empty passes any test that never fills
it, and that is the bug this module's extra pointer bit exists to
prevent.
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

CLK_NS = 40
DEPTH = 16


async def setup(dut):
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    dut.rst_n.value = 0
    dut.push.value = 0
    dut.pop.value = 0
    dut.wr_data.value = 0
    await ClockCycles(dut.clk, 4)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)


async def push(dut, value):
    dut.wr_data.value = value
    dut.push.value = 1
    await RisingEdge(dut.clk)
    dut.push.value = 0
    await ClockCycles(dut.clk, 1)


async def pop(dut):
    value = int(dut.rd_data.value)
    dut.pop.value = 1
    await RisingEdge(dut.clk)
    dut.pop.value = 0
    await ClockCycles(dut.clk, 1)
    return value


@cocotb.test()
async def test_empty_after_reset(dut):
    await setup(dut)
    assert dut.empty.value == 1, "not empty after reset"
    assert dut.full.value == 0, "full after reset"
    assert int(dut.level.value) == 0, "level is not zero after reset"


@cocotb.test()
async def test_first_in_first_out_across_a_wrap(dut):
    """Three full passes, so the pointers wrap twice with data in flight."""
    await setup(dut)
    expected = []
    for i in range(DEPTH * 3):
        await push(dut, i & 0xFF)
        expected.append(i & 0xFF)
        if len(expected) > DEPTH // 2:
            got = await pop(dut)
            want = expected.pop(0)
            assert got == want, f"expected 0x{want:02x}, got 0x{got:02x}"
    while expected:
        got = await pop(dut)
        want = expected.pop(0)
        assert got == want, f"draining: expected 0x{want:02x}, got 0x{got:02x}"
    assert dut.empty.value == 1, "not empty after draining everything"


@cocotb.test()
async def test_full_refuses_and_counts(dut):
    """A full FIFO must not absorb, and must say it did not."""
    await setup(dut)
    for i in range(DEPTH):
        await push(dut, i)
    assert dut.full.value == 1, f"not full after {DEPTH} pushes"
    assert int(dut.level.value) == DEPTH, f"level is {int(dut.level.value)}"

    saw_overflow = False

    async def watch():
        nonlocal saw_overflow
        while True:
            await RisingEdge(dut.clk)
            if dut.overflow.value == 1:
                saw_overflow = True

    cocotb.start_soon(watch())
    await push(dut, 0xEE)
    # overflow is registered, so it appears on the edge AFTER the refused
    # push and lasts one cycle. Asserting immediately races the watcher
    # on that same edge, and two coroutines resuming on one edge have no
    # defined order: the first version of this test failed for that and
    # not for anything the FIFO did.
    await ClockCycles(dut.clk, 3)
    assert saw_overflow, "a push into a full FIFO raised no overflow"

    # And the refused byte must not have displaced anything.
    got = [await pop(dut) for _ in range(DEPTH)]
    assert got == list(range(DEPTH)), f"contents changed by a refused push: {got}"


@cocotb.test()
async def test_pop_on_empty_is_harmless(dut):
    """A reader that outruns the writer must not move the pointer."""
    await setup(dut)
    dut.pop.value = 1
    await ClockCycles(dut.clk, 5)
    dut.pop.value = 0
    await ClockCycles(dut.clk, 1)
    assert dut.empty.value == 1, "popping an empty FIFO left it non-empty"
    await push(dut, 0xA5)
    got = await pop(dut)
    assert got == 0xA5, f"after empty pops the next byte was 0x{got:02x}"
