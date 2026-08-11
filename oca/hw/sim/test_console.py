# SPDX-License-Identifier: MIT
"""The console's command logic, driven at its own handshakes.

Unit level rather than through the UART, because at 217 cycles a bit a
single command costs about 6000 cycles of simulation and the interesting
part is what the answers say, not that 8N1 works -- which oca_uart_rx and
oca_uart_tx8 already have their own tests for. One integration test runs
through the real UART in test_uart_console.py to prove the wiring.

The counters get the most attention here because they are what the
channel reports about itself, and a counter that is wrong is worse than
no counter: it is a wrong answer to the only question the operator can
ask when nothing else works.
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

CLK_NS = 40


async def setup(dut):
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    dut.rst_n.value = 0
    dut.rx_data.value = 0
    dut.rx_valid.value = 0
    dut.frame_error.value = 0
    dut.rx_overflow.value = 0
    dut.tx_ready.value = 1
    await ClockCycles(dut.clk, 4)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)


def collect(dut, out, illegal=None):
    """Record every push, and separately every push made while not ready.

    The first version filtered on `tx_push and tx_ready`, which is what a
    correct FIFO would accept -- and that is exactly why it was wrong. A
    console that raised tx_push while tx_ready was low had its illegal
    pushes discarded by the collector and passed the test written to
    catch them. Measured: the mutation survived 5 of 5. A test double
    must refuse what the real thing refuses, not tidy up after it.
    """
    async def _run():
        while True:
            await RisingEdge(dut.clk)
            if dut.tx_push.value == 1:
                if dut.tx_ready.value == 1:
                    out.append(int(dut.tx_data.value))
                elif illegal is not None:
                    illegal.append(int(dut.tx_data.value))
    return cocotb.start_soon(_run())


async def command(dut, char, settle=200):
    """Offer one character and wait for the response to finish."""
    dut.rx_data.value = ord(char)
    dut.rx_valid.value = 1
    await RisingEdge(dut.clk)
    while dut.rx_pop.value != 1:
        await RisingEdge(dut.clk)
    dut.rx_valid.value = 0
    await ClockCycles(dut.clk, settle)


async def strobe(dut, name, times=1):
    for _ in range(times):
        getattr(dut, name).value = 1
        await RisingEdge(dut.clk)
        getattr(dut, name).value = 0
        await RisingEdge(dut.clk)


@cocotb.test()
async def test_ping_and_help_and_unknown(dut):
    await setup(dut)
    out = []
    collect(dut, out)

    await command(dut, "p")
    assert bytes(out) == b"OCA\n", f"p answered {bytes(out)!r}"

    out.clear()
    await command(dut, "?")
    assert bytes(out) == b"psz?\n", f"? answered {bytes(out)!r}"

    out.clear()
    await command(dut, "q")
    assert bytes(out) == b"?\n", f"an unknown command answered {bytes(out)!r}"


@cocotb.test()
async def test_counters_report_what_happened(dut):
    """R counts delivered bytes, E refused frames, O lost bytes, C commands."""
    await setup(dut)
    out = []
    collect(dut, out)

    await strobe(dut, "frame_error", 3)
    await strobe(dut, "rx_overflow", 2)
    await command(dut, "p")          # R=1 C=1 after this
    out.clear()
    await command(dut, "s")          # R=2 C=2 by the time it answers

    line = bytes(out).decode()
    assert line == "R=0002 E=0003 O=0002 C=0002\n", f"s answered {line!r}"


@cocotb.test()
async def test_zero_clears_and_is_not_counted_into_what_it_cleared(dut):
    await setup(dut)
    out = []
    collect(dut, out)

    await strobe(dut, "frame_error", 5)
    await command(dut, "p")
    out.clear()
    await command(dut, "z")
    assert bytes(out) == b"ok\n", f"z answered {bytes(out)!r}"

    out.clear()
    await command(dut, "s")
    line = bytes(out).decode()
    assert line == "R=0001 E=0000 O=0000 C=0001\n", (
        f"after z the counters read {line!r}; the s that reported them is the "
        f"only thing that should be in there")


@cocotb.test()
async def test_counters_saturate_rather_than_wrap(dut):
    """0xFFFF is a stuck counter; 0x0000 after a long run is a lie.

    Driven on frame_error because it is the one this testbench can pulse
    65536 times without also moving the others.
    """
    await setup(dut)
    out = []
    collect(dut, out)

    dut.frame_error.value = 1
    await ClockCycles(dut.clk, 70000)
    dut.frame_error.value = 0
    await ClockCycles(dut.clk, 4)

    await command(dut, "s")
    line = bytes(out).decode()
    assert "E=FFFF" in line, f"E did not saturate: {line!r}"


@cocotb.test()
async def test_a_full_transmit_side_stalls_without_losing_bytes(dut):
    """tx_ready low must pause the response, not drop part of it."""
    await setup(dut)
    out = []
    illegal = []
    collect(dut, out, illegal)

    dut.tx_ready.value = 0
    dut.rx_data.value = ord("p")
    dut.rx_valid.value = 1
    await ClockCycles(dut.clk, 40)
    assert illegal == [], (
        f"tx_push was raised {len(illegal)} times while tx_ready was low; a "
        f"FIFO would have refused those bytes and the response would be short")
    assert out == [], "bytes were accepted while tx_ready was low"

    dut.tx_ready.value = 1
    await ClockCycles(dut.clk, 4)
    dut.rx_valid.value = 0
    await ClockCycles(dut.clk, 40)
    assert bytes(out) == b"OCA\n", f"after the stall the response was {bytes(out)!r}"
