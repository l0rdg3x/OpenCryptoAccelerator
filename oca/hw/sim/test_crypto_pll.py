# SPDX-License-Identifier: MIT
"""D2 on the board top: the three readings, and which of them a simulation can reach.

The heartbeat left oca_uart_crypto with the PLL. It is counted on clk25
so that a PLL which never locks still beats -- static is the reading
oca_crypto_pll.sv reserves for no bitstream and no clk25 -- and that is
the claim this file exists to hold: a design whose PLL never locks shows
the FAST rate, not a dead LED.

THE SAME RATE HAS TO SURVIVE A LOCK THAT MEANS NOTHING, which is the
second test: LOCK closes through CLKFB from CLKOP and can be high over a
CLKOS that never runs, and that board is mute. So the slow rate is
asserted in one place only, the test that clocks CLKOS.

THE PLL IS THIS TESTBENCH. EHXPLLL reaches a simulator only as the port
list ecp5_prims.sv declares, a blackbox with no body, so clk_sys and
pll_locked are dead nets: nothing here makes 25 MHz into 48.0769 MHz and
nothing here shows that it locks. Writing to the module-level net does
not stick -- the port assignment from the blackbox output runs again on
the next evaluation and puts the old value back -- and a write to the
instance's own output pin does. test_clkrst.py established that one
level up, on dut.u_pll; the same pins are reachable here at
dut.u_clkrst.u_pll through the --public-flat-rw the cocotb runner passes
to Verilator. So this file drives LOCK and CLKOS by hand and holds the
module to everything downstream of them.

WHAT IS THEREFORE OUT OF REACH, and stays out until the board: that the
PLL locks at all, that its output is 48.0769 MHz, and that D2's two live
rates are 0.75 Hz and 6 Hz rather than a ratio of eight. LED_BITS is 8
here and 25 there; what a simulation can hold is the ratio and which
condition selects which rate, and nothing about the seconds.

THE ORDER OF THE FIRST TEST MATTERS. LOCK is driven by this file and
nothing resets it between tests, so the one test that asserts what an
undriven LOCK does has to run before anything writes to it. It says so
itself.
"""

import os

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer

CLK25_NS = 40                     # 25 MHz, the board's oscillator on P3

# clk_sys is 625/13 MHz = 48.076923 MHz, a 20.8 ns period, in whole
# picoseconds because that is the resolution the runner leaves Verilator
# on and a float nanosecond does not land on it. Transcribed from
# oca_clkrst.sv's header, like test_clkrst.py's: this testbench's
# assumption about the PLL, not a measurement of it.
CLK_SYS_HALF_PS = 10_400

# oca_uart_crypto's divisor at that frequency: 48_076_923 / 115200 = 417.
DIV = 417

# Long enough that the receiver samples a stop bit inside it: it waits
# DIV/2 for the middle of the start bit, then one sample per DIV for
# eight data bits and the stop, which is 9.5 bit times.
BREAK_CYCLES = 11 * DIV

# The build decides LED_BITS and this file cannot read it back, so
# run_crypto_pll.py passes it. No default: a default is a second number
# free to disagree with the parameter the build elaborated, and every
# count below is computed from this one.
LED_BITS = int(os.environ["OCA_LED_BITS"])
SLOW = LED_BITS - 1
FAST = LED_BITS - 4

# A whole multiple of both periods, so both counts are exact and neither
# assertion is a range that cannot be violated.
LED_WINDOW = 4096

SLOW_EDGES = LED_WINDOW // (2 ** SLOW)
FAST_EDGES = LED_WINDOW // (2 ** FAST)

# Enough clk_sys edges for oca_clkrst's two-stage synchroniser and
# oca_uart_crypto's four-bit power-on counter, which run in parallel from
# the first edge. Sixteen is what the counter needs; this is margin.
RESET_CYCLES = 32

# clk25 edges between `trouble` rising and led_n carrying the new rate:
# two synchroniser flops and the register led_n itself is.
SYNC_EDGES = 4

SETTLE_PS = 1_000


def pll(dut):
    """The PLL instance, which is where a blackbox can be driven at all.

    See the module docstring: dut.clk_sys and dut.pll_locked are outputs
    of a body-less instance and a write to either does not stick.
    """
    return dut.u_clkrst.u_pll


def start_clk25(dut):
    """Restart the 25 MHz clock.

    Per test, and not redundant: cocotb cancels the tasks a test started
    when that test ends, so a clock started in the first test stops with
    it and every test after it dies on the spot.
    """
    return Clock(dut.clk25, CLK25_NS, unit="ns").start()


def start_clk_sys(dut):
    """Run the PLL's CLKOS by hand, which is the only way it runs at all."""
    clock = Clock(pll(dut).CLKOS, 2 * CLK_SYS_HALF_PS, unit="ps", impl="gpi")
    clock.start(start_high=False)
    return clock


async def led_edges(dut, cycles: int) -> int:
    """Transitions of led_n over a window of exactly `cycles` clk25 edges.

    Exact rather than approximate: the heartbeat is a free-running
    counter bit, so over a window that is a whole multiple of its period
    the count does not depend on where the window starts.
    """
    prev = int(dut.led_n.value)
    edges = 0
    for _ in range(cycles):
        await RisingEdge(dut.clk25)
        now = int(dut.led_n.value)
        if now != prev:
            edges += 1
        prev = now
    return edges


def rate_report(edges: int) -> str:
    """The three readings, so a failure says which one the LED gave."""
    return (f"{edges} transitions in {LED_WINDOW} cycles of clk25: at "
            f"LED_BITS={LED_BITS} the clean rate is bit {SLOW} and gives "
            f"{SLOW_EDGES}, the trouble rate is bit {FAST} and gives "
            f"{FAST_EDGES}, and 0 is the static LED that means no bitstream "
            f"or no clk25")


@cocotb.test()
async def test_the_heartbeat_is_fast_while_the_pll_has_never_locked(dut):
    """FIRST IN THE FILE, and it has to be: nothing has written LOCK yet.

    This is the reading the LED moved domains for. EHXPLLL has no body,
    so pll_locked is exactly what a PLL that never locks gives the design
    -- low for ever, with clk_sys dead behind it -- and D2 has to beat
    anyway, at the fast rate, because a lock that never arrives is
    something the operator has to be told about.

    Counted from clk_sys instead of clk25 this test reads exactly 0 --
    run as a mutation on a copy of the RTL rather than argued -- and 0 is
    the reading reserved for a board with no bitstream at all. That is
    the failure this file is here to catch, and the one the assertion
    below names first.
    """
    assert int(pll(dut).LOCK.value) == 0, \
        "LOCK is already high before any test drove it, so this run says " \
        "nothing about a PLL that never locks; this test must run first"
    start_clk25(dut)

    edges = await led_edges(dut, LED_WINDOW)
    assert edges == FAST_EDGES, \
        f"with the PLL unlocked D2 gave {rate_report(edges)}"


@cocotb.test()
async def test_a_lock_with_no_clk_sys_behind_it_keeps_the_heartbeat_fast(dut):
    """LOCK high over a datapath that has never been clocked: the mute board.

    EHXPLLL closes LOCK through CLKFB from CLKOP (oca_clkrst.sv:60-62)
    and says nothing whatever about CLKOS, which is the output this
    design's datapath runs on, so a build in which CLKOS never runs locks
    all the same. That build is what this test stands in for, and in it
    the other two terms of the fast rate are both unreachable: the PLL
    did lock, so !locked_sync is 0, and `trouble` is a register in the
    clk_sys domain, so with no edge it cannot be raised. rst_n_core is
    the only term left, which is why the state is established by reading
    it: oca_uart_crypto releases it after sixteen edges of clk_sys and
    there have been none.

    Read slow, this LED would be telling the operator "alive, PLL
    locked, the datapath out of reset, nothing refused or lost" at a
    board that answers nobody. This file asserted exactly that until
    2026-08-15, which is the defect the rst_n_core term was added for and
    the reason this test replaced the one that made the claim.

    THE SLOW RATE IS NOT LOST WITH IT, and is not repeated here. The
    third test drives LOCK, clocks CLKOS through oca_clkrst's release and
    the power-on counter, reads rst_n_core high, and only then counts
    SLOW_EDGES as the control it needs before its break. That is the
    clean reading held against a datapath demonstrably running -- the
    only state this module is supposed to give it -- so a second slow
    assertion here would be an assertion about a state that must not
    produce one.

    LOCK IS DRIVEN BEFORE clk25 STARTS so that the selector never
    changes inside the window: fast on the lock term while locked_sync
    fills, fast on rst_n_core from then on, one rate for the whole
    count. uart_rx is driven for the same reason the assertion below
    reads `trouble` -- a line left undriven is a frame error waiting for
    a receiver to sample it, and a fast rate read off a latched
    `trouble` would not be the fast rate this test names.
    """
    dut.uart_rx.value = 1
    pll(dut).LOCK.value = 1
    start_clk25(dut)

    assert int(dut.trouble.value) == 0, \
        "the datapath reported trouble before anything drove it"
    assert int(dut.u_crypto.rst_n_core.value) == 0, \
        "the datapath left reset without a single edge of clk_sys, so " \
        "this run is not the CLKOS-dead fault this test reproduces"

    edges = await led_edges(dut, LED_WINDOW)
    assert edges == FAST_EDGES, \
        f"with the PLL locked but clk_sys dead D2 gave {rate_report(edges)}"


@cocotb.test()
async def test_a_malformed_byte_on_the_line_takes_the_heartbeat_back_to_fast(dut):
    """The sixth source of `trouble`, end to end, into the LED.

    The rate is the whole of what the operator gets: the three SLIP
    refusal counters are not reachable through opcode 04 and never can
    be, so if `trouble` did not reach D2 nothing on the board would
    report a link that lost something.

    A break on the line is the cheapest of the six to produce and the one
    oca_crypto_pll.sv warns about by name: the receiver samples a stop
    bit that is not high, raises frame_error, and the latch in
    oca_uart_crypto keeps it. Held low for eleven bit times, which is
    more than the 9.5 the receiver needs to reach that sample.

    The slow rate is measured first, in this same test, so the fast one
    below cannot pass on a DUT that was already fast -- which is exactly
    what a `trouble` stuck high, or a lock term that never cleared, would
    look like.

    IT IS ALSO WHERE rst_n IS READ, because getting there needs the core
    running and that is the one thing the reset decides. clk_sys is this
    testbench's, so its edges arrive whether the PLL locked or not --
    something no board can do, and what makes the gating visible: with
    LOCK low the datapath has to stay held through those edges, and come
    out only once LOCK rises. A reset tied high instead of taken from
    oca_clkrst passes every other assertion in this file, which is how
    that mutation was found.
    """
    start_clk25(dut)
    clk_sys = start_clk_sys(dut)
    try:
        pll(dut).LOCK.value = 0           # test 2 left it high
        dut.uart_rx.value = 1
        for _ in range(RESET_CYCLES):
            await RisingEdge(pll(dut).CLKOS)
        assert int(dut.u_crypto.rst_n_core.value) == 0, \
            "the core left reset on a clock whose PLL has not locked: " \
            "rst_n is oca_clkrst's release and it is gated on LOCK"

        pll(dut).LOCK.value = 1
        for _ in range(RESET_CYCLES):
            await RisingEdge(pll(dut).CLKOS)
        assert int(dut.u_crypto.rst_n_core.value) == 1, \
            "the core is still in reset after LOCK rose, so the latch " \
            "below cannot be set and the fast rate would not mean what " \
            "this test reads it as"

        control = await led_edges(dut, LED_WINDOW)
        assert control == SLOW_EDGES, \
            f"before the break D2 already gave {rate_report(control)}"

        dut.uart_rx.value = 0
        for _ in range(BREAK_CYCLES):
            await RisingEdge(pll(dut).CLKOS)
        dut.uart_rx.value = 1
        # Read, not waited for: the stop bit is sampled 9.5 bit times into
        # a break that lasted eleven, so the latch is set before the line
        # comes back. Waiting for a RisingEdge of it here would wait for
        # an edge that has already happened, and time out on a working
        # DUT -- which is what this test did until the edge was measured.
        assert int(dut.trouble.value) == 1, \
            "eleven bit times of a line held low did not reach the trouble " \
            "latch: the receiver samples the stop bit at 9.5 of them and " \
            "frame_error is one of the latch's six sources"

        # The bit crosses into clk25 through two flops and led_n is
        # registered after them, so the rate does not change on the edge
        # that set it. A window counted through that change is neither
        # rate.
        for _ in range(SYNC_EDGES):
            await RisingEdge(dut.clk25)

        edges = await led_edges(dut, LED_WINDOW)
        assert edges == FAST_EDGES, \
            f"after a malformed byte D2 gave {rate_report(edges)}"
    finally:
        clk_sys.stop()
        pll(dut).CLKOS.value = 0          # a falling edge clocks nothing
        await Timer(SETTLE_PS, unit="ps")
