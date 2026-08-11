# SPDX-License-Identifier: MIT
"""The 8N1 receiver, driven by frames this file builds itself.

WHY NOT LOOP IT BACK THROUGH oca_uart_tx. A receiver tested against our
own transmitter agrees with it about everything both get wrong: bit
order, stop-bit position, where the midpoint is. The frames here are
generated from the 8N1 definition in Python, so a shared misreading of
the standard fails instead of cancelling out.

WHAT IT CANNOT SHOW. Metastability on the input synchroniser, which is
the reason the two flops are there and which no simulation reaches; and
the real clock error between two independent oscillators, since the
testbench's bit period is exactly the receiver's.
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

CLK_NS = 40
DIV = 217


async def start_clock(dut):
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())


async def idle(dut, bits=2):
    dut.rx.value = 1
    await ClockCycles(dut.clk, DIV * bits)


async def send_frame(dut, byte, stop=1):
    """One 8N1 frame: start low, eight bits LSB first, then `stop`.

    `stop` is a parameter so the framing-error path can be driven with a
    frame that is correct in every other respect.
    """
    dut.rx.value = 0
    await ClockCycles(dut.clk, DIV)
    for i in range(8):
        dut.rx.value = (byte >> i) & 1
        await ClockCycles(dut.clk, DIV)
    dut.rx.value = stop
    await ClockCycles(dut.clk, DIV)
    dut.rx.value = 1


def collect(dut, got):
    """Gather every delivered byte into `got` for as long as it runs.

    A collector rather than a wait-and-return, because valid is one cycle
    wide and the sender must not be interrupted to look for it. The first
    version of these tests started each frame with start_soon and then
    awaited the strobe, which returns while the sender still has a stop
    bit to drive: the next frame's coroutine then began writing dut.rx
    alongside the one still finishing, and alternate bytes came back
    corrupted. Two drivers on one signal, in the testbench, not the DUT.
    """
    async def _run():
        while True:
            await RisingEdge(dut.clk)
            if dut.valid.value == 1:
                got.append(int(dut.data.value))
    return cocotb.start_soon(_run())


@cocotb.test()
async def test_receives_every_byte_value(dut):
    """All 256, because bit order and off-by-one hide in half of them.

    0x00 and 0xFF pass a receiver that has lost the shifter entirely;
    0x01 and 0x80 are the pair that a reversed shift swaps.
    """
    await start_clock(dut)
    await idle(dut)
    got = []
    collect(dut, got)
    for byte in range(256):
        await send_frame(dut, byte)
        await idle(dut, 1)
    await ClockCycles(dut.clk, DIV * 2)
    assert got == list(range(256)), (
        f"received {len(got)} bytes; first mismatch at "
        f"{next((i for i, (a, b) in enumerate(zip(got, range(256))) if a != b), None)}")


@cocotb.test()
async def test_a_low_stop_bit_is_a_framing_error(dut):
    """A misframed byte must not be delivered as if it were fine."""
    await start_clock(dut)
    await idle(dut)
    saw_valid = False
    saw_error = False

    async def watch():
        nonlocal saw_valid, saw_error
        while True:
            await RisingEdge(dut.clk)
            if dut.valid.value == 1:
                saw_valid = True
            if dut.frame_error.value == 1:
                saw_error = True

    cocotb.start_soon(watch())
    await send_frame(dut, 0x5A, stop=0)
    await ClockCycles(dut.clk, DIV * 3)
    assert not saw_valid, "a frame with a low stop bit was delivered as valid"
    assert saw_error, "a frame with a low stop bit raised no frame_error"


@cocotb.test()
async def test_a_glitch_is_not_a_byte(dut):
    """A brief dip on an idle line must not start a frame.

    The line is taken low for a tenth of a bit and released. A receiver
    that commits on the edge instead of rechecking at the midpoint
    delivers whatever the idle line looks like for the next nine bit
    times, which is 0xFF, and does it every time the cable is touched.
    """
    await start_clock(dut)
    await idle(dut)
    dut.rx.value = 0
    await ClockCycles(dut.clk, DIV // 10)
    dut.rx.value = 1
    for _ in range(DIV * 12):
        await RisingEdge(dut.clk)
        assert dut.valid.value == 0, "a glitch on an idle line produced a byte"


@cocotb.test()
async def test_back_to_back_frames(dut):
    """No idle between frames, which is what a host sending a line does."""
    await start_clock(dut)
    await idle(dut)
    payload = [0x4F, 0x43, 0x41, 0x0A]
    got = []
    collect(dut, got)
    # Sequential and with no idle between them: send_frame leaves the
    # line high for nothing, so each start bit follows the previous stop
    # bit immediately, which is what a host sending a line does.
    for byte in payload:
        await send_frame(dut, byte)
    await ClockCycles(dut.clk, DIV * 3)
    assert got == payload, f"expected {payload}, received {got}"
