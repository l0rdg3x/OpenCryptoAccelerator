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


def status_fields(line):
    """The four counters out of a status line, as integers."""
    return {name: int(value, 16)
            for name, value in (f.split("=") for f in line.split(" "))}


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


@cocotb.test()
async def test_a_z_typed_into_a_burst_keeps_the_counters_consistent(dut):
    """`s s s z s` with no gaps, which is what a script sends.

    The tests above leave a DIV*60 gap after every byte -- forty times
    longer than the console needs to answer -- so the input FIFO is
    always empty when a command is accepted and nothing is ever waiting
    behind a z. At speed it is: three status lines are 84 bytes into a
    32-byte output FIFO, so the console stalls with the z and the s
    behind it already counted into R and still queued.

    What the burst proves is R >= C across the clear, which neither of
    the spaced-out tests can see. It does NOT prove the status line is a
    snapshot, and it misses that by one byte. R climbs from 2 to 5 while
    the second line is stalled, but the six bytes of "R=0002" are the
    six the console gets away before the output FIFO fills -- the last
    of them lands in the last free slot -- and the very next byte, the
    space after the field, goes out with R already at 3. Measured, by
    putting the counters back on the live reads: this test stays green
    either way. A torn field needs the event placed between two named
    beats, which is test_console's
    test_the_status_line_is_one_instant_and_not_sixteen.
    """
    await start(dut)
    out = []
    decode(dut, out)

    await send_byte(dut, ord("z"))
    await ClockCycles(dut.clk25, DIV * 60)
    out.clear()

    for char in "ssszs":
        await send_byte(dut, ord(char))
    await ClockCycles(dut.clk25, DIV * 1400)

    lines = bytes(out).decode().split("\n")
    assert lines[-1] == "", f"the console stopped mid-line: {bytes(out)!r}"
    lines = lines[:-1]
    for line in lines:
        if line.startswith("R="):
            field = status_fields(line)
            assert field["R"] >= field["C"], (
                f"{line!r}: C is above R, and R - C - O is "
                f"{field['R'] - field['C'] - field['O']}")
    assert lines == ["R=0001 E=0000 O=0000 C=0001",
                     "R=0002 E=0000 O=0000 C=0002",
                     "R=0005 E=0000 O=0000 C=0003",
                     "ok",
                     "R=0001 E=0000 O=0000 C=0001"], (
        f"the burst answered {lines!r}; the third line is taken while all "
        f"five bytes have arrived and only three have run, and the last one "
        f"reports the s that was still queued behind the z")
