# SPDX-License-Identifier: MIT
"""Clocks and resets: the reset synchronisers, and what no simulation can show.

WHAT DRIVES THE CLOCKS HERE. EHXPLLL reaches a simulator only as the port
list ecp5_prims.sv declares -- a blackbox with no body -- so clk_sys,
clk_tx and pll_locked, all three of them driven by that instance, are
dead nets in any simulation of this module. Writing to them from the
testbench does not stick: measured, not assumed, the port assignment from
the blackbox output runs again on the next evaluation and puts the old
value back. What does stick is a write to the instance's own output pins,
dut.u_pll.CLKOS, .CLKOP and .LOCK, which the --public-flat-rw the cocotb
runner passes to Verilator makes reachable. So this testbench is the PLL:
it drives the two clocks and asserts lock by hand, and what it can hold
the module to is everything downstream of lock.

WHAT IS THEREFORE OUT OF REACH, and stays out until the board:

  * The PLL. No divider, no CPHASE, no VCO frequency, no FEEDBK_PATH and
    none of the four analogue attributes has any effect on a blackbox, so
    nothing here shows that 25 MHz in gives 125 MHz and 48.08 MHz out.
    The periods below are transcribed from the ecppll run quoted in
    oca_clkrst.sv's header: they are this testbench's assumption about
    the PLL, not a measurement of it. The elaboration guards that do
    check that arithmetic are exercised by run_clkrst.py, outside the
    simulator, because that is where they fire.
  * Metastability, which is the whole reason the synchroniser has two
    flops. No simulation shows it and the module's own header says so.
    What is pinned below is the structure that buys the resolving time --
    release costs exactly RST_SYNC_STAGES edges of the domain's own clock
    -- so a chain shortened to one stage fails a test instead of quietly
    trading away the mean time between failures.
  * The recovery and removal windows of the real ECP5 fabric flop, and
    everything about how the reset tree is routed.

Every test drives its own edges rather than starting a free-running
clock, apart from the two that have to count 10 ms of them. That is what
lets a test stop one domain's clock while the others keep running, which
is the case this module exists for and the one a free-running testbench
cannot reach.
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, SimTimeoutError, Timer, with_timeout
from cocotb.utils import get_sim_time

# The module's default, which run_clkrst.py builds at. Its elaboration
# guard refuses anything below 2; run_clkrst.py is what proves that.
RST_SYNC_STAGES = 2

# Half periods, in whole picoseconds because that is the resolution the
# runner leaves Verilator on and a float nanosecond does not land on it.
# clk_sys is 625/13 MHz = 48.076923 MHz, a 20.8 ns period; clk_tx is the
# 125 MHz RGMII transmit clock; clk_rx is the recovered receive clock,
# 125 MHz while the link is at 1000BASE-T. All three from the header.
HALF_PS = {"sys": 10_400, "tx": 4_000, "rx": 4_000}
DOMAINS = ("sys", "tx", "rx")

# B50612D-DS100-R Table 86, as oca_clkrst.sv's header transcribes it:
# RESET_PU >= 10 ms and RESET_WAIT >= 20 us. These are the requirement,
# not the module's cycle counts -- reproducing its arithmetic here would
# only assert that a multiplication is itself.
PHY_RST_MIN_PS = 10_000_000_000
PHY_WAIT_MIN_PS = 20_000_000

# Enough to reach both thresholds with room to spare, and short enough
# that a module that never releases fails instead of running until the
# runner's own timeout kills it.
PHY_RST_LIMIT_MS = 12
PHY_WAIT_LIMIT_MS = 1

# The grid the complement test samples on. Fine enough that a pair
# derived from two different flops of one synchroniser -- one clk_sys
# period of disagreement, 20.8 ns -- is caught fifty times over.
SAMPLE_STEP_PS = 400

# Long enough that a release that needed a clock edge has had none, and
# that an asynchronous assert has settled. Neither depends on the number.
SETTLE_PS = 1_000


def clk(dut, domain):
    """The handle that drives one domain's clock.

    clk_sys and clk_tx are outputs of the PLL blackbox and only reachable
    at its pins (see the module docstring); clk_rx is a real input port.
    """
    return {"sys": dut.u_pll.CLKOS,
            "tx": dut.u_pll.CLKOP,
            "rx": dut.clk_rx}[domain]


def lock(dut):
    """The PLL's LOCK, driven here because the blackbox never asserts it."""
    return dut.u_pll.LOCK


def resets(dut):
    """Every domain's reset, as 1 for released and 0 for asserted.

    The complement check lives here rather than in one test, so that both
    polarities are held to an inverter's worth of agreement at every
    point any test in this file looks at either of them.
    """
    state = {}
    for domain in DOMAINS:
        low = int(getattr(dut, f"rst_n_{domain}").value)
        high = int(getattr(dut, f"rst_{domain}").value)
        assert low != high, \
            f"rst_n_{domain}={low} and rst_{domain}={high} are not " \
            f"complements at {get_sim_time('ns')} ns"
        state[domain] = low
    return state


ALL_ASSERTED = {"sys": 0, "tx": 0, "rx": 0}
ALL_RELEASED = {"sys": 1, "tx": 1, "rx": 1}


async def setup(dut):
    """Both reset roots low, every clock parked low: in reset, with no edges."""
    dut.ext_rst_n.value = 0
    lock(dut).value = 0
    for domain in DOMAINS:
        clk(dut, domain).value = 0
    await Timer(SETTLE_PS, unit="ps")
    assert resets(dut) == ALL_ASSERTED, \
        f"the DUT did not enter reset at all: {resets(dut)}"


async def edge(dut, domain, count=1):
    """One rising edge on one domain's clock, leaving it parked low.

    Parking low is what makes the next call an edge and a bare Timer not
    one, which is the distinction every test below rests on.
    """
    handle = clk(dut, domain)
    half = HALF_PS[domain]
    for _ in range(count):
        handle.value = 1
        await Timer(half, unit="ps")
        handle.value = 0
        await Timer(half, unit="ps")


async def release_root(dut):
    """Both reset roots high. No clock moves, so no domain releases yet."""
    dut.ext_rst_n.value = 1
    lock(dut).value = 1
    await Timer(SETTLE_PS, unit="ps")


async def release_all(dut):
    """Bring all three domains out of reset."""
    await release_root(dut)
    for domain in DOMAINS:
        await edge(dut, domain, RST_SYNC_STAGES)
    assert resets(dut) == ALL_RELEASED, \
        f"the domains did not leave reset: {resets(dut)}"


@cocotb.test()
async def test_each_domain_asserts_its_reset_with_every_clock_stopped(dut):
    """Both reset roots assert all six outputs with no clock edge anywhere.

    This is the half of the design that needs no clock, and the half a
    synchronous reset would silently fail: with the clocks parked there
    is no edge for a synchronous branch to act on, so a module that
    reset itself on a clock instead of on the level would leave every
    domain running here. Held across twenty clk_sys periods afterwards,
    so what is measured is a level and not a pulse.
    """
    for root in ("ext_rst_n", "pll_locked"):
        await setup(dut)
        await release_all(dut)

        handle = dut.ext_rst_n if root == "ext_rst_n" else lock(dut)
        handle.value = 0
        await Timer(SETTLE_PS, unit="ps")
        assert resets(dut) == ALL_ASSERTED, \
            f"{root} low left {resets(dut)} with no clock edge to act on"

        await Timer(20 * 2 * HALF_PS["sys"], unit="ps")
        assert resets(dut) == ALL_ASSERTED, \
            f"{root} low did not hold: {resets(dut)}"


@cocotb.test()
async def test_each_domain_releases_only_on_a_rising_edge_of_its_own_clock(dut):
    """Release is synchronous, to that domain's clock, and costs RST_SYNC_STAGES edges.

    Three separate claims, and each of them has its own way of being
    wrong. A combinational release needs no edge at all, so the first
    step waits with every clock stopped. A release on the wrong edge is
    invisible to any test that only counts edges, so the second step
    parks the clocks high before the root goes away and then drops them,
    giving every domain a falling edge and nothing else. And a domain
    released by another domain's clock passes any test that runs all
    three together, so the walk below hands out edges one domain at a
    time and asserts the other two are still held.
    """
    await setup(dut)
    for domain in DOMAINS:
        clk(dut, domain).value = 1        # while the root is still low
    await Timer(SETTLE_PS, unit="ps")
    await release_root(dut)

    await Timer(50 * 2 * HALF_PS["sys"], unit="ps")
    assert resets(dut) == ALL_ASSERTED, \
        f"a reset released with no clock edge at all: {resets(dut)}"

    for domain in DOMAINS:                # one falling edge each, no rising
        clk(dut, domain).value = 0
    await Timer(SETTLE_PS, unit="ps")
    assert resets(dut) == ALL_ASSERTED, \
        f"a reset released on a falling edge: {resets(dut)}"

    released = []
    for domain in DOMAINS:
        for n in range(1, RST_SYNC_STAGES + 1):
            await edge(dut, domain)
            state = resets(dut)
            want = 1 if n == RST_SYNC_STAGES else 0
            assert state[domain] == want, \
                f"rst_n_{domain} is {state[domain]} after {n} edge(s) of " \
                f"clk_{domain}; RST_SYNC_STAGES is {RST_SYNC_STAGES}"
            for other in DOMAINS:
                if other != domain and other not in released:
                    assert state[other] == 0, \
                        f"clk_{domain} released rst_n_{other}"
        released.append(domain)

        # And the domains still held stay held however long the released
        # ones run: a crossed clock that needs a few edges to show is
        # still a crossed clock.
        for _ in range(20):
            for done in released:
                await edge(dut, done)
        state = resets(dut)
        for other in DOMAINS:
            if other not in released:
                assert state[other] == 0, \
                    f"rst_n_{other} released on 20 edges of {released}"

    assert resets(dut) == ALL_RELEASED, f"nothing released at all: {resets(dut)}"


@cocotb.test()
async def test_the_receive_domain_stays_in_reset_while_its_clock_is_absent(dut):
    """clk_rx has no edges before the link is up, and rst_n_rx must not release.

    The header's own bench warning, as a test: the receive clock is
    recovered from the PHY, so it does not exist until the link does.
    Two hundred edges of the two clocks that do exist are what a
    plausible mistake -- resetting the receive domain from clk_sys, which
    is where everything it feeds lives -- would release it on.
    """
    await setup(dut)
    await release_root(dut)

    for _ in range(200):
        await edge(dut, "sys")
        await edge(dut, "tx")
        assert resets(dut)["rx"] == 0, \
            "rst_n_rx released with clk_rx stopped, on edges of clk_sys/clk_tx"

    state = resets(dut)
    assert (state["sys"], state["tx"]) == (1, 1), \
        f"the stimulus released nothing, so it could not have released rx: {state}"

    await edge(dut, "rx", RST_SYNC_STAGES)
    assert resets(dut)["rx"] == 1, \
        "rst_n_rx did not release once clk_rx returned: the test above was vacuous"


@cocotb.test()
async def test_no_domain_leaves_reset_while_the_pll_never_locks(dut):
    """ext_rst_n high is not enough: arst_n is the AND of it and LOCK.

    A PLL that never locks is a clock that is not the frequency anything
    was constrained at, so every domain has to stay in reset. Two hundred
    edges of all three clocks, then the same stimulus with LOCK high, so
    the negative half cannot pass by having driven nothing.
    """
    await setup(dut)
    dut.ext_rst_n.value = 1               # LOCK stays low
    await Timer(SETTLE_PS, unit="ps")

    for _ in range(200):
        for domain in DOMAINS:
            await edge(dut, domain)
        assert resets(dut) == ALL_ASSERTED, \
            f"a domain left reset with the PLL unlocked: {resets(dut)}"

    lock(dut).value = 1
    await Timer(SETTLE_PS, unit="ps")
    for domain in DOMAINS:
        await edge(dut, domain, RST_SYNC_STAGES)
    assert resets(dut) == ALL_RELEASED, \
        f"the same stimulus with LOCK high released nothing: {resets(dut)}"


@cocotb.test()
async def test_both_polarities_of_each_reset_are_exact_complements(dut):
    """rst_x is ~rst_n_x at every point sampled, across every transition there is.

    resets() checks the pair on every read in this file; what this test
    adds is density and coverage of the moments that matter. It samples
    every SAMPLE_STEP_PS through both halves of every clock period it
    drives, so the window a pair built from two separate synchronisers
    would disagree over -- one clock period, 20.8 ns on clk_sys -- is
    sampled twenty-six times rather than jumped over.

    The transitions it walks are all of them: the synchronous release of
    each domain, an asynchronous assert from ext_rst_n and another from
    LOCK, with the release repeated after each. The tally at the end is
    what stops the whole thing passing on a DUT that never moved.
    """
    seen = {domain: set() for domain in DOMAINS}

    def record():
        for domain, value in resets(dut).items():
            seen[domain].add(value)

    async def sweep(domain, edges):
        """Drive edges on one clock, sampling right through them."""
        handle = clk(dut, domain)
        steps = HALF_PS[domain] // SAMPLE_STEP_PS
        for _ in range(edges):
            for level in (1, 0):
                handle.value = level
                for _ in range(steps):
                    await Timer(SAMPLE_STEP_PS, unit="ps")
                    record()

    async def sweep_idle(count):
        """Sample with every clock stopped."""
        for _ in range(count):
            await Timer(SAMPLE_STEP_PS, unit="ps")
            record()

    for root in ("ext_rst_n", "pll_locked"):
        await setup(dut)
        record()
        await release_root(dut)
        await sweep_idle(4)
        for domain in DOMAINS:
            await sweep(domain, RST_SYNC_STAGES + 1)

        handle = dut.ext_rst_n if root == "ext_rst_n" else lock(dut)
        handle.value = 0
        await sweep_idle(8)
        for domain in DOMAINS:            # asserted, so these are no-ops
            await sweep(domain, 2)

    for domain in DOMAINS:
        assert seen[domain] == {0, 1}, \
            f"rst_n_{domain} was only ever {seen[domain]} while sampling, so " \
            "no transition of it was under the complement check"


async def time_the_phy_reset(dut):
    """Lock the PLL with clk_sys free running; return the three instants, in ps.

    The only two tests that need a real clock rather than hand-driven
    edges: 10 ms at 48.08 MHz is 480770 of them.
    """
    await setup(dut)
    assert (int(dut.phy_rst_n.value), int(dut.phy_ready.value)) == (0, 0), \
        "the PHY is not held in reset while the system domain is"

    await release_root(dut)
    t_lock = get_sim_time("ps")
    clock = Clock(clk(dut, "sys"), 2 * HALF_PS["sys"], unit="ps", impl="gpi")
    clock.start(start_high=False)
    try:
        try:
            await with_timeout(RisingEdge(dut.phy_rst_n), PHY_RST_LIMIT_MS, "ms")
        except SimTimeoutError:
            raise AssertionError(
                f"phy_rst_n never released in {PHY_RST_LIMIT_MS} ms") from None
        t_rst = get_sim_time("ps")
        # Read after the edge that moved phy_rst_n, not the one that moved
        # phy_ready: an out-of-order design would have raised phy_ready on
        # an earlier edge, so this read is not the stale one.
        assert int(dut.phy_ready.value) == 0, \
            "phy_ready was already high when phy_rst_n released"
        try:
            await with_timeout(RisingEdge(dut.phy_ready), PHY_WAIT_LIMIT_MS, "ms")
        except SimTimeoutError:
            raise AssertionError(
                f"phy_ready never rose in the {PHY_WAIT_LIMIT_MS} ms after "
                "phy_rst_n released") from None
        t_ready = get_sim_time("ps")
    finally:
        clock.stop()
        clk(dut, "sys").value = 0         # a falling edge clocks nothing
        await Timer(SETTLE_PS, unit="ps")
    return t_lock, t_rst, t_ready


@cocotb.test()
async def test_the_phy_reset_holds_the_datasheet_minimums_in_the_datasheet_order(dut):
    """RESET_PU >= 10 ms, then RESET_WAIT >= 20 us, both measured in time.

    Measured from PLL lock, which is later than the power-up the
    datasheet counts RESET_PU from, so the whole configuration time is
    margin this test does not have to know about. Asserted in
    nanoseconds rather than in cycles because the requirement is a
    duration: a truncating cycle count that lands the reset tens of
    nanoseconds under 10 ms satisfies every count-based assertion and
    still misses the datasheet.
    """
    t_lock, t_rst, t_ready = await time_the_phy_reset(dut)

    hold = t_rst - t_lock
    wait = t_ready - t_rst
    assert hold >= PHY_RST_MIN_PS, \
        f"phy_rst_n released {hold / 1e6:.4f} us after lock; RESET_PU needs " \
        f"{PHY_RST_MIN_PS / 1e6:.0f} us"
    assert wait >= PHY_WAIT_MIN_PS, \
        f"phy_ready rose {wait / 1e6:.4f} us after phy_rst_n; RESET_WAIT needs " \
        f"{PHY_WAIT_MIN_PS / 1e6:.0f} us"


@cocotb.test()
async def test_the_phy_reset_returns_asynchronously_when_the_system_domain_does(dut):
    """The counter's reset is rst_n_sys, asynchronously, so P4 follows with no edge.

    The one output of this module that leaves the FPGA. Once phy_ready is
    up the counter is parked at its terminal value, and a reset of the
    system domain has to send both outputs back down and start the 10 ms
    again -- with clk_sys stopped, since a design being reset is a design
    whose clock may be about to stop.
    """
    await time_the_phy_reset(dut)
    assert (int(dut.phy_rst_n.value), int(dut.phy_ready.value)) == (1, 1), \
        "the PHY reset did not complete, so there is nothing to withdraw"

    dut.ext_rst_n.value = 0               # clk_sys is parked low
    await Timer(SETTLE_PS, unit="ps")
    assert (int(dut.phy_rst_n.value), int(dut.phy_ready.value)) == (0, 0), \
        f"phy_rst_n/phy_ready are {int(dut.phy_rst_n.value)}/" \
        f"{int(dut.phy_ready.value)} with the system domain reset and clk_sys " \
        "stopped"

    await Timer(20 * 2 * HALF_PS["sys"], unit="ps")
    assert (int(dut.phy_rst_n.value), int(dut.phy_ready.value)) == (0, 0), \
        "the PHY reset did not hold"
