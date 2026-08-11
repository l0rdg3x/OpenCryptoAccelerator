# SPDX-License-Identifier: MIT
"""The console through the real UART, to prove the wiring.

test_console.py holds the command logic at its own handshakes, which is
where the interesting assertions are and where they run in thousands of
cycles instead of tens of thousands. What it cannot show is that the five
modules are connected to each other correctly, so this file sends real
frames into the real receiver and decodes real frames off the real
transmitter.

THE BACK-TO-BACK TEST IS THE POINT. oca_uart_echo dropped alternate bytes
because a byte arriving mid-response had nowhere to wait, and "OCA" typed
at speed came back "OA". That is the defect the FIFOs exist to fix, so it
is the thing asserted here: two commands with no gap must produce two
complete answers.
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

CLK_NS = 40
DIV = 217


async def start(dut):
    cocotb.start_soon(Clock(dut.clk25, CLK_NS, unit="ns").start())
    dut.uart_rx.value = 1
    await ClockCycles(dut.clk25, DIV * 2)


async def send_byte(dut, byte):
    dut.uart_rx.value = 0
    await ClockCycles(dut.clk25, DIV)
    for i in range(8):
        dut.uart_rx.value = (byte >> i) & 1
        await ClockCycles(dut.clk25, DIV)
    dut.uart_rx.value = 1
    await ClockCycles(dut.clk25, DIV)


def decode(dut, out):
    async def _run():
        while True:
            while dut.uart_tx.value == 1:
                await RisingEdge(dut.clk25)
            await ClockCycles(dut.clk25, DIV // 2)
            if dut.uart_tx.value != 0:
                continue
            byte = 0
            for bit in range(8):
                await ClockCycles(dut.clk25, DIV)
                byte |= int(dut.uart_tx.value) << bit
            await ClockCycles(dut.clk25, DIV)
            assert dut.uart_tx.value == 1, f"stop bit low after 0x{byte:02x}"
            out.append(byte)
    return cocotb.start_soon(_run())


@cocotb.test()
async def test_ping_through_the_wire(dut):
    await start(dut)
    out = []
    decode(dut, out)
    await send_byte(dut, ord("p"))
    await ClockCycles(dut.clk25, DIV * 60)
    assert bytes(out) == b"OCA\n", f"p through the UART answered {bytes(out)!r}"


@cocotb.test()
async def test_two_commands_back_to_back_both_answer(dut):
    """The failure oca_uart_echo had, and the reason for the FIFOs."""
    await start(dut)
    out = []
    decode(dut, out)
    await send_byte(dut, ord("p"))
    await send_byte(dut, ord("p"))
    await ClockCycles(dut.clk25, DIV * 120)
    assert bytes(out) == b"OCA\nOCA\n", (
        f"two commands with no gap answered {bytes(out)!r}; the echo before "
        f"the FIFOs would have dropped one")


@cocotb.test()
async def test_status_counts_the_bytes_that_arrived(dut):
    """Zeroed first, because the tests share one simulation.

    cocotb does not reset the DUT between tests, so the counters arrive
    here holding everything the two tests above sent: without the z this
    read R=0007 for four bytes, and the arithmetic was the testbench's
    fault rather than the console's.
    """
    await start(dut)
    out = []
    decode(dut, out)
    await send_byte(dut, ord("z"))
    await ClockCycles(dut.clk25, DIV * 60)
    for _ in range(3):
        await send_byte(dut, ord("p"))
    await ClockCycles(dut.clk25, DIV * 200)
    out.clear()
    await send_byte(dut, ord("s"))
    await ClockCycles(dut.clk25, DIV * 400)
    line = bytes(out).decode()
    assert line == "R=0004 E=0000 O=0000 C=0004\n", (
        f"after three p and one s the status reads {line!r}")
