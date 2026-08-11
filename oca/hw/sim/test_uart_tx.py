# SPDX-License-Identifier: MIT
"""The 8N1 transmitter, decoded rather than watched.

WHY THIS EXISTS AT ALL. oca_uart_probe goes on the board to answer which
pin the carrier's DAPLink listens to, and its whole method is that a
string arriving names the pin it came from. Nothing arriving has to mean
one thing -- wrong pin -- and it only means that if the transmitter is
known to work. An untested transmitter turns a clean bench answer into
two hypotheses that a bench cannot separate, which is the failure this
project keeps paying for elsewhere.

So the testbench is a receiver. It does not look at the line and check it
wiggles; it samples the middle of each bit, decodes the frame, assembles
the bytes and compares them to the payload. A transmitter that shifted
the wrong way, framed the wrong number of bits or ran at the wrong
divisor produces a wiggling line and fails every assertion here.

WHAT IT CANNOT SHOW. Whether either candidate pin is wired to anything,
which is the bench's question and not a simulation's; and the real
divisor error against a real host clock, since both ends here are exact.
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

# oca_uart_probe's own constants, restated rather than imported because a
# testbench that reads its expectations from the design under test cannot
# fail. 25 MHz over 115200 baud.
CLK_NS = 40
DIV = 217
LEN = 8

MSG_J17 = b"PIN=J17\n"
MSG_E5 = b"PIN=E5 \n"


async def start_clock(dut):
    """Per test, because cocotb cancels a test's tasks when it ends.

    Hoisting this to a once-only guard leaves every test after the first
    with no clock at all, which the simulator reports as running out of
    events rather than as a hang.
    """
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())


async def pulse_send(dut):
    """Wait for idle, then start a message.

    THE WAIT IS LOAD BEARING. cocotb runs all five tests in one
    simulation without resetting the DUT between them, and receive()
    returns having sampled the last stop bit at its midpoint, with half a
    bit period still to run. A test that sends immediately after sends
    into a busy transmitter, which drops it by design -- and then waits
    forever for a start bit that will never come. That is a hang, not a
    failure, and it is what test 3 did before this wait existed.
    """
    while dut.busy.value == 1:
        await RisingEdge(dut.clk)
    await pulse_send_raw(dut)


async def pulse_send_raw(dut):
    """A send with no regard for whether the transmitter is busy."""
    dut.send.value = 1
    await RisingEdge(dut.clk)
    dut.send.value = 0


async def receive(dut, count):
    """Decode `count` 8N1 frames off dut.tx, sampling mid-bit.

    Mid-bit and not edge: sampling on the transition is what a receiver
    with no margin does, and it would pass on a design whose bit period
    is off by nearly half a bit. Half a period in means a divisor error
    of more than 50% is needed before this reads the wrong bit, and the
    period is asserted separately below anyway.
    """
    out = bytearray()
    for _ in range(count):
        # Start bit: wait for the line to leave idle.
        while dut.tx.value == 1:
            await RisingEdge(dut.clk)
        # Into the middle of the start bit, then one period per bit.
        await ClockCycles(dut.clk, DIV // 2)
        assert dut.tx.value == 0, "start bit is not low at its midpoint"
        byte = 0
        for bit in range(8):
            await ClockCycles(dut.clk, DIV)
            byte |= int(dut.tx.value) << bit
        await ClockCycles(dut.clk, DIV)
        assert dut.tx.value == 1, f"stop bit is not high after 0x{byte:02x}"
        out.append(byte)
    return bytes(out)


@cocotb.test()
async def test_idles_high_before_anything(dut):
    """A line that idles low has no start bit for a receiver to find."""
    await start_clock(dut)
    dut.send.value = 0
    await ClockCycles(dut.clk, 50)
    assert dut.tx.value == 1, "tx does not idle high"
    assert dut.busy.value == 0, "busy is asserted before any send"


@cocotb.test()
async def test_transmits_the_whole_payload(dut):
    """Every byte, in order, LSB first inside each frame."""
    await start_clock(dut)
    dut.send.value = 0
    await ClockCycles(dut.clk, 10)
    await pulse_send(dut)
    got = await receive(dut, LEN)
    assert got == MSG_J17, f"payload is {got!r}, expected {MSG_J17!r}"


@cocotb.test()
async def test_bit_period_is_the_divisor(dut):
    """The baud rate, measured off the line instead of trusted.

    A transmitter one bit-time out still spells the message to a
    testbench that samples by counting frames; it spells nothing to a
    host. So the start bit is timed edge to edge.
    """
    await start_clock(dut)
    dut.send.value = 0
    await ClockCycles(dut.clk, 10)
    await pulse_send(dut)
    while dut.tx.value == 1:
        await RisingEdge(dut.clk)
    # 'P' is 0x50 = 0b0101_0000. Sent LSB first that is 0,0,0,0,1,0,1,0,
    # so the start bit and bits 0 through 3 are all low: the line stays
    # down for FIVE bit periods and rises at bit 4. Counting to the first
    # rise therefore measures 5 * DIV, and getting that arithmetic wrong
    # is how a period assertion passes on a transmitter running at the
    # wrong baud.
    low = 0
    while dut.tx.value == 0:
        await RisingEdge(dut.clk)
        low += 1
    assert abs(low - 5 * DIV) <= 2, (
        f"start bit plus bits 0-3 lasted {low} cycles, expected {5 * DIV}")


@cocotb.test()
async def test_returns_to_idle_and_sends_again(dut):
    """Two messages, because a transmitter that never rearms sends one.

    oca_uart_probe pulses send once a second forever, so a state machine
    that leaves `active` set after the last stop bit would put one
    message on the wire and go quiet, which at the bench reads exactly
    like the wrong pin.
    """
    await start_clock(dut)
    dut.send.value = 0
    await ClockCycles(dut.clk, 10)
    await pulse_send(dut)
    first = await receive(dut, LEN)
    assert first == MSG_J17, f"first message is {first!r}"
    await ClockCycles(dut.clk, DIV * 4)
    assert dut.busy.value == 0, "still busy after the last stop bit"
    assert dut.tx.value == 1, "tx is not back at idle"
    await pulse_send(dut)
    second = await receive(dut, LEN)
    assert second == MSG_J17, f"second message is {second!r}"


@cocotb.test()
async def test_send_during_a_frame_is_ignored(dut):
    """A dropped message beats a corrupted one.

    The probe's send tick is asynchronous to nothing, so this cannot
    happen there; it is asserted because the alternative implementation,
    restarting mid-frame, produces a stream that decodes to garbage
    rather than to nothing, and garbage on the bench would be read as the
    wrong pin rather than as a bug here.
    """
    await start_clock(dut)
    dut.send.value = 0
    await ClockCycles(dut.clk, 10)

    # The receiver has to be listening before the frame starts, because a
    # decoder that joins mid-byte cannot tell a dropped send from a
    # corrupted one -- which is the entire distinction under test.
    while dut.busy.value == 1:
        await RisingEdge(dut.clk)
    listener = cocotb.start_soon(receive(dut, LEN))
    await pulse_send_raw(dut)
    await ClockCycles(dut.clk, DIV * 3)
    await pulse_send_raw(dut)
    got = await listener
    assert got == MSG_J17, (
        f"a send during a frame disturbed the message: got {got!r}")
