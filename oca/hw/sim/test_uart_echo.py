# SPDX-License-Identifier: MIT
"""The echo as a whole, with Python on both ends of the wire.

The frames going in are built from the 8N1 definition and the frames
coming out are decoded the same way, so neither side inherits an
assumption from the RTL. That matters more here than in the two
per-module testbenches: this is the first design where our receiver and
our transmitter face each other, and a shared misreading of bit order or
stop-bit position would cancel out perfectly if the testbench used
either of them as its reference.

It is also the only test oca_uart_tx8 has. Its framing is new code -- the
byte comes from a port where oca_uart_tx takes a parameter -- so it is
exercised here rather than trusted for resembling something tested.

WHAT IT CANNOT SHOW. That H18 is the pin, which is the bench's question;
metastability on the input synchroniser; and real clock error between
two oscillators, since one clock drives both ends here.
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

CLK_NS = 40
DIV = 217


async def start_clock(dut):
    cocotb.start_soon(Clock(dut.clk25, CLK_NS, unit="ns").start())


async def send_frame(dut, byte):
    dut.uart_rx.value = 0
    await ClockCycles(dut.clk25, DIV)
    for i in range(8):
        dut.uart_rx.value = (byte >> i) & 1
        await ClockCycles(dut.clk25, DIV)
    dut.uart_rx.value = 1
    await ClockCycles(dut.clk25, DIV)


def decode(dut, out):
    """Collect bytes off uart_tx for as long as this task runs."""
    async def _run():
        while True:
            while dut.uart_tx.value == 1:
                await RisingEdge(dut.clk25)
            await ClockCycles(dut.clk25, DIV // 2)
            if dut.uart_tx.value != 0:
                continue                      # glitch, not a start bit
            byte = 0
            for bit in range(8):
                await ClockCycles(dut.clk25, DIV)
                byte |= int(dut.uart_tx.value) << bit
            await ClockCycles(dut.clk25, DIV)
            assert dut.uart_tx.value == 1, f"stop bit low after 0x{byte:02x}"
            out.append(byte)
    return cocotb.start_soon(_run())


@cocotb.test()
async def test_echoes_what_it_is_given(dut):
    """The operator's own bytes come back, which no wrong decoder fakes."""
    await start_clock(dut)
    dut.uart_rx.value = 1
    await ClockCycles(dut.clk25, DIV * 2)

    out = []
    decode(dut, out)
    payload = list(b"OCA\n")
    for byte in payload:
        await send_frame(dut, byte)
        # One byte in flight at a time: the design drops a byte offered
        # while the transmitter is busy, which the next test covers.
        await ClockCycles(dut.clk25, DIV * 11)

    assert out == payload, f"sent {payload}, echoed {out}"


@cocotb.test()
async def test_every_byte_value_survives_the_round_trip(dut):
    """All 256, because bit order and off-by-one hide in half of them."""
    await start_clock(dut)
    dut.uart_rx.value = 1
    await ClockCycles(dut.clk25, DIV * 2)

    out = []
    decode(dut, out)
    for byte in range(256):
        await send_frame(dut, byte)
        await ClockCycles(dut.clk25, DIV * 11)

    assert out == list(range(256)), (
        f"round trip lost or altered bytes: {len(out)} back, first mismatch "
        f"at {next((i for i, (a, b) in enumerate(zip(out, range(256))) if a != b), None)}")


@cocotb.test()
async def test_a_byte_arriving_mid_echo_is_dropped_not_spliced(dut):
    """A short echo is legible; a corrupted one is not.

    Two bytes back to back at the same baud cannot both be echoed: the
    second arrives while the first is still going out. What must not
    happen is the second being spliced into the first's frame, which
    would put a byte on the wire that neither end sent.
    """
    await start_clock(dut)
    dut.uart_rx.value = 1
    await ClockCycles(dut.clk25, DIV * 2)

    out = []
    decode(dut, out)
    await send_frame(dut, 0x41)
    await send_frame(dut, 0x42)
    await ClockCycles(dut.clk25, DIV * 24)

    assert out, "nothing was echoed at all"
    assert all(b in (0x41, 0x42) for b in out), (
        f"the echo contains a byte neither end sent: {out}")
    assert out[0] == 0x41, f"the first byte echoed was {out[0]:#04x}, not 0x41"
