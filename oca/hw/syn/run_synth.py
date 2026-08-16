# SPDX-License-Identifier: MIT
"""ECP5 synthesis and place & route for the OCA cores (yosys + nextpnr-ecp5).

Runs the project-local open toolchain (`tools/yosys`, `tools/nextpnr`,
`tools/trellis`) on one of the RTL cores and reports area and Fmax.

The cores expose wide internal buses (512-bit data blocks), far more
signals than the package has pins, so a design with no pin constraints
runs with `--out-of-context`: no IO buffers are inserted and it is
placed as a locked macro. Those numbers characterise the core itself and
not a pinned-out design. A design that carries an `.lpf` is built the
other way, with real IO and the routing pressure that comes with it, and
the two are not comparable.

Usage, from oca/:
    .venv/bin/python hw/syn/run_synth.py chacha20_poly1305
    .venv/bin/python hw/syn/run_synth.py --freq 150 chacha20
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[3]
RTL = ROOT / "oca" / "hw" / "rtl"
SYN_DIR = Path(__file__).resolve().parent
BUILD = SYN_DIR / "build"


def source_path(rel: str) -> Path:
    """Absolute path for a DESIGNS entry."""
    return ROOT / rel


YOSYS = ROOT / "tools" / "yosys" / "bin" / "yosys"
NEXTPNR = ROOT / "tools" / "nextpnr" / "bin" / "nextpnr-ecp5"
ECPPACK = ROOT / "tools" / "trellis" / "bin" / "ecppack"

# Sources per top module, in dependency order.
#
# sv       SystemVerilog of ours, read by read_slang, relative to hw/rtl/.
# verilog  Verilog read by yosys's own frontend before it, relative to the
#          repository root. No design carries any since the Ethernet
#          transport was retired; the field stays because the two-frontend
#          order below is what makes mixing them possible at all.
# incdirs  include paths for that Verilog, relative to the repository root.
# lpf      pin constraints, relative to hw/syn/. A design with one is
#          built with real IO; a design without one is built
#          out-of-context.
#
# verilog, incdirs and pnr_args default to () rather than []: a
# NamedTuple stores the literal default object, so every DESIGNS entry
# that takes the default would otherwise alias the SAME list, and
# `d.pnr_args.append(...)` on one design would leak onto all fourteen.
# A tuple default turns that mutation into an AttributeError instead.
class Design(NamedTuple):
    sv: list
    verilog: tuple = ()
    incdirs: tuple = ()
    lpf: str = ""
    # Extra nextpnr arguments, for pinned designs only. Recorded per
    # design rather than passed on the command line so that a figure in
    # a document can be reproduced by naming the target and nothing else.
    pnr_args: tuple = ()
    # The placer seed a design's published figures were measured on.
    # None means the flow's default. Recorded here because "it closes"
    # and "it closes on seed 6" are different claims and only one of
    # them reproduces -- and for the same reason a design that closes on
    # nothing still records the seed its numbers came from.
    seed: int | None = None
    # Per-stage wall-clock bound, when DEFAULT_TIMEOUT is not enough.
    # Recorded here rather than left to the caller for the same reason as
    # the seed: the documented way to reproduce a design's figures is to
    # name the target, and a bound that kills it half way makes that
    # false.
    timeout: int | None = None


ENGINE = ["chacha20.sv", "poly1305.sv", "chacha20_poly1305.sv"]
CORE = ENGINE + ["oca_keystore.sv", "oca_pktbuf.sv", "oca_proto.sv",
                 "oca_core.sv"]

DESIGNS = {
    "chacha20": Design(sv=["chacha20.sv"]),
    "poly1305": Design(sv=["poly1305.sv"]),
    "chacha20_poly1305": Design(sv=ENGINE),
    "oca_core": Design(sv=CORE),
    "oca_dual": Design(
        sv=CORE + ["oca_dual.sv"],
        # The densest design here at 56% of the device, and the only one
        # the default has killed. Router1 time varies enormously with the
        # placer seed, so the bound has to cover the slow seeds and not
        # the median.
        #
        # The figures behind that are observations, not artefacts: in one
        # four-seed sweep on 2026-08-15 router time ran from 950 s to
        # over 3000 s, and one build died at rc=124 on the 1800 s
        # default. run() opens each stage log with "w", so a sweep leaves
        # only its last seed in build/ and only that seed's "Router1
        # time" line survives; nothing in the tree records the other
        # seeds or the killed build, and the message of the commit that
        # raised this bound is where they are written down.
        timeout=3600,
    ),
    # The two halves of the serial bridge, measured apart because they
    # are what the top level adds to a core whose cost is already known,
    # and because the decoder's store-and-forward buffer is the one
    # figure the design decision turns on.
    "oca_slip_rx": Design(sv=["oca_slip_rx.sv"]),
    "oca_slip_tx": Design(sv=["oca_slip_tx.sv"]),
    # Bring-up step 2, and the first design here whose purpose is to be
    # loaded rather than measured. It said "only" until 2026-08-11, and
    # oca_vccio, oca_pll and oca_uart_probe were added to the same
    # list afterwards. Its own .lpf, not the seventeen-pin
    # one: a design is required to constrain every IO it has, but an
    # .lpf naming pins the design does not have is skipped without a
    # word, so building this against colorlight_i9.lpf would pass while
    # proving nothing about the fifteen lines it silently ignored.
    "oca_blink": Design(
        sv=["oca_blink.sv"],
        lpf="colorlight_i9_blink.lpf",
    ),
    # The diagnostic console: UART both ways, a FIFO on each side so a
    # byte arriving during a response has somewhere to wait, and
    # single-character commands with counters for the two ways this
    # channel loses input.
    "oca_uart_console": Design(
        sv=["oca_uart_rx.sv", "oca_uart_tx8.sv", "oca_fifo.sv",
            "oca_console.sv", "oca_uart_console.sv"],
        lpf="colorlight_i9_console.lpf",
    ),
    # The AEAD datapath on the serial line: the console's five modules
    # with oca_console replaced by the two SLIP halves and the whole of
    # oca_core. Built at its default CLK_HZ of 25 MHz.
    #
    # OUT OF CONTEXT SINCE THE PLL ARRIVED, and dropping the .lpf is not
    # a preference. This module stopped being a board top when
    # oca_crypto_pll took the clocking and the LED: its ports are now
    # clk, rst_n, uart_tx, uart_rx, rst_n_core and trouble, while
    # colorlight_i9_crypto.lpf constrains clk25 and led_n, which no
    # longer exist here. nextpnr skips a LOCATE naming a cell that is
    # not there without a word (ecp5/lpf.cc:144) and then fails on "IO
    # clk is unconstrained in LPF" (ecp5/main.cc:287) — so keeping the
    # line would not be a stale comment, it would be a target that
    # cannot build. What this one measures now is the datapath alone,
    # the way oca_core does; the pinned build is oca_crypto_pll below.
    #
    # It follows that this target no longer packs a bitstream and no
    # longer has its timing checked: check_timing and pack both return
    # early for a design with no .lpf. Both of those moved to
    # oca_crypto_pll with the pins.
    "oca_uart_crypto": Design(
        sv=["oca_uart_rx.sv", "oca_uart_tx8.sv", "oca_fifo.sv",
            "oca_slip_rx.sv", "oca_slip_tx.sv",
            "chacha20.sv", "poly1305.sv", "chacha20_poly1305.sv",
            "oca_keystore.sv", "oca_pktbuf.sv", "oca_proto.sv",
            "oca_core.sv", "oca_uart_crypto.sv"],
        # oca_core alone is 4-10 minutes and its router time is the part
        # that varies; this carries the same engine and the serial
        # bridge on top of it. Kept at 3600 across the move out of
        # context: the bound is here to stop a stage that has stopped
        # progressing, not to predict this one.
        timeout=3600,
    ),
    # The crypto console on the PLL, and the board top of that pair:
    # oca_clkrst and the whole of oca_uart_crypto on the console's four
    # pins, with the datapath on clk_sys at 48.0769 MHz instead of on the
    # 25 MHz pin. The heartbeat stays on clk25 and the reasons are
    # oca_crypto_pll.sv's.
    #
    # ecp5_prims.sv and oca_clkrst.sv lead, in oca_pll's order and for
    # oca_pll's reason: EHXPLLL is declared in the first of those so that
    # all 36 of its parameters are in front of the frontend that
    # elaborates the override (oca_clkrst.sv:40-50).
    #
    # THIS LIST AND hw/sim/run_crypto_pll.py's SOURCES ARE THE SAME LIST,
    # which that file says in its own words: a suite that elaborates a
    # different set of files from the one the bitstream is built from is
    # a suite for a design nobody loads. Nothing enforces it, so the two
    # move together by hand.
    #
    # colorlight_i9_crypto.lpf, shared rather than copied. This top's
    # four ports are exactly the four that file constrains, on the same
    # balls: the argument that earned oca_blink an .lpf of its own is
    # about a design whose port set differs from the file's, which is not
    # the case here, and two copies of one pin map drift apart while one
    # file cannot. What that file must NOT gain is a FREQUENCY line for
    # clk_sys. nextpnr derives that constraint from the PLL's own
    # dividers (ecp5/pack.cc:2824), and where a net already carries a
    # user constraint the derived one is discarded rather than applied
    # (ecp5/pack.cc:2815-2822) — so an .lpf line naming clk_sys would
    # replace the one number NETLIST_PLL_PARAMS below exists to make
    # load-bearing with a number typed by hand.
    "oca_crypto_pll": Design(
        sv=["ecp5_prims.sv", "oca_clkrst.sv",
            "oca_uart_rx.sv", "oca_uart_tx8.sv", "oca_fifo.sv",
            "oca_slip_rx.sv", "oca_slip_tx.sv",
            "chacha20.sv", "poly1305.sv", "chacha20_poly1305.sv",
            "oca_keystore.sv", "oca_pktbuf.sv", "oca_proto.sv",
            "oca_core.sv", "oca_uart_crypto.sv", "oca_crypto_pll.sv"],
        lpf="colorlight_i9_crypto.lpf",
        # 7200, and what separates it from oca_uart_crypto's 3600 is the
        # constraint and not the size — the two netlists differ by 34
        # registers. That 3600 was chosen for a build constrained at
        # 25 MHz, a target its netlist cleared by 99%, so nothing in
        # place or route was under any timing pressure at all; this one
        # asks the same fabric for 48.0769 and puts that pressure on.
        #
        # Three figures sized this bound and NOT ONE of them is a
        # measurement of this build. Router1 spent 1231 s on the pinned
        # 25 MHz build of the RTL before the split — on congestion
        # alone, which is worth stating because it means the slack
        # margin there did NOT make that build cheap (seed 1,
        # 2026-08-15; that log is overwritten by the next run of any
        # target, so here is where it is written down). oca_dual's
        # four-seed sweep put the spread between seeds at roughly 3.2x,
        # which on 1231 s reaches 3900 before timing pressure is added.
        # And how much that pressure adds is unknown. So this is a bound
        # and not a prediction: what the build costs is measured by
        # building it.
        #
        # It has been built since: four seeds on 2026-08-15, and all four
        # finished -- sweep.sh stops on the first non-zero exit and a
        # bound hit here returns 124, so four figures is four builds
        # inside 7200. What any of them actually cost is known for one:
        # only one log survives, run() and sweep.sh each reopening
        # theirs, and the one in build/ is the seed-1 build taken after
        # the sweep: Router1 time 108.11 s -- under a tenth of the
        # 1231 s that sized this bound rather than the
        # multiple that was feared. One seed is not the spread, and the
        # spread is the part that varies, so the bound stays where it is.
        timeout=7200,
    ),
    # The receive half of the console. J17 is settled; H18 is litex's
    # pairing and nothing more until a byte travels it, which is what an
    # echo makes the operator prove by supplying the expected value.
    "oca_uart_echo": Design(
        sv=["oca_uart_rx.sv", "oca_uart_tx8.sv", "oca_uart_echo.sv"],
        lpf="colorlight_i9_echo.lpf",
    ),
    # Which pin is the DAPLink's UART on. Two transmitters, each naming
    # its own pin, because litex offers two candidates and offering two
    # is the evidence that neither is certain.
    "oca_uart_probe": Design(
        sv=["oca_uart_tx.sv", "oca_uart_probe.sv"],
        lpf="colorlight_i9_uart.lpf",
    ),
    # Bring-up step 3. oca_clkrst as the real design instantiates it, and
    # a counter sized to turn 125 MHz into a 1 Hz blink, so the reading
    # is a frequency a stopwatch can check rather than a lock flag.
    "oca_pll": Design(
        sv=["ecp5_prims.sv", "oca_clkrst.sv", "oca_pll.sv"],
        lpf="colorlight_i9_pll.lpf",
    ),
    # Eight free bank 6 pins toggling in step with D2, so a meter on a
    # header hole reads VCCIO6 off a driven pad instead of off a
    # capacitor nobody can identify, and a reading that swings with a
    # visible light identifies which hole. Its own .lpf for the reason
    # above. This entry described four pins at fixed levels until
    # 2026-08-11, which was the design before the probes were made to
    # announce themselves.
    "oca_vccio": Design(
        sv=["oca_vccio.sv"],
        lpf="colorlight_i9_vccio.lpf",
    ),
}

# Minimum live flip-flops a netlist must contain, keyed by the RTL file
# the cells are attributed to. Simulation cannot see synthesis, so these
# are the only checks standing between a mapper bug and a silently
# non-functional bitstream (see README.md, "The cmp2lut trap").
#
# oca_keystore.sv: NUM_SLOTS*256 keys + NUM_SLOTS loaded + 256 rd_key
# + 1 rd_valid = 2313 for the default NUM_SLOTS = 8. Derived, so it is
# exact and only NUM_SLOTS moves it.
#
# oca_proto.sv: measured, not derived. The registers the module declares
# add up to more than the netlist keeps — yosys folds fields it can prove
# equal — so a figure computed from the RTL would fail a healthy build.
# 3837 live at the RTL of this commit with the toolchain in tools/,
# measured 2026-08-16: 3645 before the bench counter, plus its 192 (a
# 64-bit tick and two 64-bit captures). The floor stays at 3600 — under
# the pre-bench figure on purpose, since what it exists to catch is
# storage vanishing wholesale, which is what the cmp2lut trap did to
# 89% of the key store, and a floor chasing the census upward buys
# nothing against that while failing the next healthy refactor.
# It has to be re-measured whenever oca_proto's state changes, and the
# census check_netlist prints below is where the new number comes from.
#
# oca_dual instantiates oca_core twice and shares nothing, so both floors
# double. Stated as its own entry rather than computed from oca_core's:
# if one core's key store vanished and the other survived, a doubled
# figure would still be met by 2313 live registers and the check would
# pass over a half-empty netlist.
NETLIST_FF_FLOOR = {
    # All 25 bits of the blink counter, floored at exactly 25 because
    # that is a whole design and there is nothing in it to move. This is
    # the one build here whose result an operator reads by eye, and a
    # counter one bit short does not fail -- it blinks twice as fast,
    # which at the bench is indistinguishable from the oscillator not
    # being the 25 MHz we think it is. That reading is the entire purpose
    # of the step, so the thing it depends on is checked.
    "oca_blink": {"oca_blink.sv": 25},
    # 27 for the probe: a 25-bit tick counter, the send pulse and led_n.
    # 52 for the two transmitters, 26 each, so both instances have to
    # keep their storage.
    #
    # It does NOT check that the two carry different payloads, which is
    # the failure that would actually matter: giving both the same
    # message still counts 52, measured rather than assumed. That one is
    # an elaboration guard in oca_uart_probe.sv instead.
    "oca_uart_probe": {"oca_uart_probe.sv": 27, "oca_uart_tx.sv": 52},
    # 32 for the receiver: 8-bit divisor, 3-bit index, 8-bit shifter, the
    # state, the outputs, and the TWO SYNCHRONISER FLOPS on rx. Those two
    # are why the floor is here at all. Nothing downstream notices if a
    # mapper folds a synchroniser to one stage: the design still receives
    # in every simulation, and what it loses is mean time between
    # failures on a boundary no test reaches.
    # 22 for the transmitter, 1 for the top's LED toggle.
    "oca_uart_echo": {"oca_uart_rx.sv": 32, "oca_uart_tx8.sv": 22,
                      "oca_uart_echo.sv": 1},
    # 81 for the console. The declared state is 83 bits -- four 16-bit
    # counters, the latched command, the response index and length, the
    # sending flag -- and two do not survive the mapper. Which two is not
    # established: ABC renames everything it touches and the netlist
    # cannot be read back per signal, so the floor is the measured
    # figure rather than a decomposition. The load-bearing part is not
    # in doubt: 64 of the 83 are the counters, which is what the channel
    # reports about itself.
    # 33 for the receiver, one more than in oca_uart_echo. NOT because
    # of a reset: oca_uart_rx has no reset port and no reset branch. The
    # extra flop is frame_error, which oca_uart_echo leaves unconnected
    # and yosys therefore drops, and which the console counts.
    #
    # 23 for the FIFO, and that is BOTH instances together: the pointers
    # and the overflow flag only. The storage is not in flops at all --
    # yosys puts it in distributed RAM, which is the TRELLIS_RAMW in the
    # resource table. So this floor guards the pointer arithmetic and
    # says nothing about the bytes, and a mapper that lost the RAM would
    # pass it. What catches that is test_fifo's order-across-a-wrap.
    #
    # 22 for the transmitter, 5 for the top's power-on counter and LED.
    # oca_console.sv was 81 until the status line became a snapshot: the
    # four counters are now sampled into four registers of their own on
    # the accept, which is exactly +64.
    "oca_uart_console": {"oca_uart_rx.sv": 33, "oca_uart_tx8.sv": 22,
                         "oca_fifo.sv": 23, "oca_console.sv": 145,
                         "oca_uart_console.sv": 5},
    # The tx counter has to reach 62_499_999, which takes 26 bits. At 25
    # bits that compare is unreachable and therefore constant false, so
    # tx_beat never toggles and yosys folds the counter and the toggle
    # out together: the census drops to 23, not 49, and the LED sits
    # static while the PLL is locked. Static is the reading this design
    # reserves for no bitstream and no clock, so the failure would not
    # merely be a wrong rate, it would be the wrong diagnosis. 26
    # counter, 1 toggle, 23 for the clk25 beat that reports a PLL which
    # never locked.
    # oca_clkrst contributes 2, not the thirty-odd it holds: oca_pll
    # consumes pll_locked and rst_n_tx and nothing else, so the PHY reset
    # timer and the sys and rx synchronisers are optimised away. Those
    # belong to step 4. The 2 that remain are the tx reset synchroniser,
    # and they are worth a floor of their own: if rst_n_tx ever came out
    # constant high the tx counter would run before lock, and the three
    # readings this design exists to separate would collapse into two.
    "oca_pll": {"oca_pll.sv": 50, "oca_clkrst.sv": 2},
    # 27 bits, two more than oca_blink, because led_n has to alternate
    # slowly enough for a meter to settle on each level: bit 26 gives
    # 2.684 s per state. Two bits short and it is 0.67 s, which a digital
    # meter cannot resolve into two clean readings, and the whole method
    # is reading two clean levels off one pad.
    "oca_vccio": {"oca_vccio.sv": 27},
    # The crypto console. oca_core's two floors unchanged -- it contains
    # one core and the same mapping defect would delete the same key
    # store -- plus every module the serial bridge adds, because this is
    # the first design here that packs a bitstream with crypto in it and
    # a floor that covers the engine and not the path to it guards a
    # board that answers nobody.
    #
    # 33 for the receiver, the same as the console's and one more than
    # oca_uart_echo's: the extra flop is frame_error, which the echo
    # leaves unconnected and which this design latches into `trouble`.
    # 22 for the transmitter. 22 for BOTH oca_fifo instances together --
    # two 5-bit pointer pairs and two overflow flags -- and, as in the
    # console, the bytes are not in flip-flops at all but in the four
    # TRELLIS_RAMW of the resource table, so this floor guards the
    # pointer arithmetic and says nothing about the storage.
    #
    # 6 for the top: the 4-bit power-on counter, the sticky `trouble`
    # latch and the registered `rst_n_core`, which is everything this
    # file still holds. rst_n_core is a register rather than a decode
    # because it drives asynchronous resets, where a combinational
    # all-ones aperture is a spurious reset release. It read 31 until
    # the heartbeat moved to oca_crypto_pll.sv -- 25 bits of counter and
    # led_n -- and with those went this entry's second job, holding
    # LED_BITS at the board's 25. That duty did not lapse: it is the
    # "oca_crypto_pll.sv" floor below, and it has to live somewhere,
    # because hw/sim/run_crypto_pll.py elaborates the counter at 8 so
    # that both rates fit in a run and no simulation anywhere sees the
    # 25.
    #
    # 5 is derived, 31 was derived and measured together; the 26 between
    # them is exactly what left. It has not been re-measured. Where a
    # floor here is wrong it is wrong upwards, and that costs the
    # synthesis and not the build: check_netlist runs before place &
    # route and prints the census the correction comes from.
    #
    # 160 for the decoder, which is what the per-file census reads --
    # and it reads 160 in the standalone build too. THE 302 IN
    # NETLIST_FF_TOTAL ABOVE IS NOT THE SAME MEASUREMENT: that is the
    # whole netlist of a design whose only module is this one, and the
    # 142 between them is the census's own "(none)" bucket, cells yosys
    # attributes to no design file. This comment read "160 here and not
    # the 302 the same RTL gives as a top of its own" until 2026-08-12
    # and then explained the gap as deleted counters plus block-RAM
    # emulation registers. There is no gap to explain: comparing a
    # per-file census against a whole-netlist total is an error of the
    # exact kind these tables exist to catch, and it was made in the
    # table itself.
    #
    # What IS true, and separately measured: the three 16-bit saturating
    # counters cnt_short, cnt_long and cnt_esc are deleted here. Nothing
    # reads their value -- only the OR that drives `trouble` -- so yosys
    # keeps the disjunction and drops the counters, and the crypto
    # netlist holds zero nets matching cnt_*, against 33 in the
    # standalone one. That is the price of the blind spot
    # oca_uart_crypto.sv records: the refusal counts do not exist in the
    # bitstream, only the fact that something was refused. It does not
    # move this floor, because those cells were never in this bucket.
    #
    # 75 for the encoder, and here the two measurements do coincide,
    # its standalone total being 75 as well.
    "oca_uart_crypto": {"oca_keystore.sv": 2313, "oca_proto.sv": 3600,
                        "oca_uart_rx.sv": 33, "oca_uart_tx8.sv": 22,
                        "oca_fifo.sv": 22, "oca_slip_rx.sv": 160,
                        "oca_slip_tx.sv": 75, "oca_uart_crypto.sv": 5},
    # The same datapath under the PLL, and the differences from the entry
    # above are the whole point of reading them side by side.
    #
    # THE TWO UART FLOORS EACH GAIN ONE BIT AND ONLY HERE. Both modules
    # size their divisor counter as $clog2(DIV) (oca_uart_rx.sv:40,
    # oca_uart_tx8.sv:32), and DIV is CLK_HZ/115200: 217 at the entry
    # above, where $clog2 is 8, and 417 here, where it is 9. So the
    # receiver goes 33 -> 34 and the transmitter 22 -> 23. Derived from
    # the measured 33 and 22 rather than measured again, which is what
    # makes them worth stating: a build of this top that censuses 33 and
    # 22 has elaborated oca_uart_crypto at 25 MHz, and a UART whose
    # divisor belongs to the wrong clock is a mute serial line that
    # builds, packs and loads.
    #
    # 5 for oca_uart_crypto.sv, unchanged from the standalone build:
    # neither the power-on counter nor `trouble` moves with CLK_HZ.
    #
    # 2 for oca_clkrst.sv, which is the system reset synchroniser and
    # nothing else. That module holds thirty-odd registers and this top
    # reads three of its outputs -- clk_sys, pll_locked, rst_n_sys -- so
    # the transmit and receive synchronisers and the whole PHY reset
    # timer are optimised away, exactly as they are in oca_pll, where the
    # surviving pair is the transmit one instead and the census measured
    # 2. It earns a floor for oca_pll's reason with the domains swapped:
    # if rst_n_sys came out constant high the datapath would leave reset
    # before the PLL had locked, on the first edges of a clock that is
    # not yet at frequency.
    #
    # 32 for the top: 25 bits of heartbeat, led_n, and the three
    # two-flop synchronisers that bring `trouble`, pll_locked and
    # rst_n_core into clk25. The third one is what keeps the LED from
    # reading "alive and clean" at a board whose CLKOS never ran, so a
    # floor of 30 would pass the netlist this design exists to refuse.
    # THIS IS THE FLOOR THAT HOLDS LED_BITS AT 25, inherited from
    # oca_uart_crypto.sv's old 31. hw/sim/run_crypto_pll.py builds the
    # counter at 8 because at 25 a half-period is 16.8 million clocks and
    # no run can watch one, so nothing in simulation ever asserts the
    # board's width. One bit short is not a dead LED at the bench, it is
    # a heartbeat at twice the rate -- which is the reading this design
    # reserves for a link that has lost something, or for a PLL that
    # never locked.
    #
    # Every figure in this entry was derived before the target had ever
    # been built. Two four-seed sweeps built it on 2026-08-15, the
    # second after the reset and heartbeat corrections added three
    # registers, and the census of that netlist confirmed all ten:
    # 2313, 3645, 34, 23, 22, 160, 75, 6, 2, 32.
    # The 2026-08-16 sweep on the bubble-and-bench netlist reads the
    # same ten with one change, oca_proto.sv at 3837 -- the bench
    # counter's 192, as its entry above records. Nine sit exactly on
    # their floor and stay there, which is what a derived figure earns
    # here -- a legitimate reduction should fail and be re-measured.
    # oca_proto.sv is the one with slack, now the bench counter's
    # registers on top of the old 45, for the reason its entry gives.
    #
    # THE PAIR WORTH READING TWICE IS 34 AND 23. Predicted from
    # $clog2(417) = 9 where the standalone build has $clog2(217) = 8, and
    # the census returns exactly that, so this entry does catch a top
    # elaborated at 25 MHz. It does NOT catch 48.08 MHz confused with
    # 56.82: $clog2 is 9 for both divisors, these floors read 34 and 23
    # either way, and what separates them is check_clk_sys_const.
    "oca_crypto_pll": {"oca_keystore.sv": 2313, "oca_proto.sv": 3600,
                       "oca_uart_rx.sv": 34, "oca_uart_tx8.sv": 23,
                       "oca_fifo.sv": 22, "oca_slip_rx.sv": 160,
                       "oca_slip_tx.sv": 75, "oca_uart_crypto.sv": 6,
                       "oca_clkrst.sv": 2, "oca_crypto_pll.sv": 32},
    "oca_core": {"oca_keystore.sv": 2313, "oca_proto.sv": 3600},
    "oca_dual": {"oca_keystore.sv": 4626, "oca_proto.sv": 7200},
}

# The AEAD engine's own storage — ChaCha20's block state, Poly1305's
# accumulator, the 512-bit staging registers between them — is guarded
# by a floor on the whole netlist rather than per file, because yosys's
# per-file attribution is not stable enough to floor tightly. Measured
# on this toolchain, oca_core across the secret-zeroisation merge:
# poly1305.sv 391 -> 1789 live registers while the unattributed bucket
# fell 1753 -> 324 and the whole netlist lost ten. Over that delta
# poly1305.sv gained reset branches, not 1398 registers of new state, so
# what moved was the label and not the storage. (The merge did add state
# elsewhere — oca_pktbuf's memory-clearing walk, +20 registers — which
# is why the total moved at all.) A per-file floor tight enough to catch
# the accumulator vanishing would have failed that healthy build; a
# floor loose enough to survive it would not catch the accumulator.
#
# A total is immune to that migration and still tight: the smallest
# thing worth catching here is a few hundred registers, and the margin
# below is ~1%, the same discipline as oca_proto's floor. It has to be
# re-measured whenever any of the RTL changes, and the census
# check_netlist prints is where the new number comes from. Adding
# storage is free — this only ever fails downwards.
#
# oca_core measured 12033 live flip-flops on the netlist before the
# 2026-08-16 bubble-and-bench change, oca_dual exactly twice that;
# neither target has been rebuilt on it, so neither floor has been
# exercised there. The two pinned tops below have, and moved by +61
# and +60.
NETLIST_FF_TOTAL = {"oca_core": 11900, "oca_dual": 23800,
                    # Floored ~1% under for the same reason as oca_core:
                    # chacha20.sv, poly1305.sv and chacha20_poly1305.sv
                    # carry 5683 of these registers between them and
                    # yosys's attribution moves between the three, so a
                    # per-file floor tight enough to catch an accumulator
                    # vanishing would fail a healthy build.
                    #
                    # 12518 was the measurement on the RTL before the
                    # heartbeat left this module; losing the LED's 26
                    # made 12492 the derived expectation, and the build
                    # of 2026-08-16 measured 12553 -- the derivation
                    # plus the bubble-and-bench commit's net +61 (the
                    # bubble removed ~131 registers, the bench counter
                    # added 192; the finer split was not chased). The
                    # floor stays at 12400: 153 registers of margin,
                    # 1.2%, the same discipline as oca_core's, and the
                    # smallest loss worth catching is still hundreds.
                    "oca_uart_crypto": 12400,
                    # The same datapath plus 37 registers: 32 in the
                    # new top, 2 surviving in oca_clkrst.sv, one bit each
                    # on the two UART divisor counters and the core's
                    # registered rst_n_core. 12526 was the prediction,
                    # 12529 the 2026-08-15 measurement (the reset and
                    # heartbeat corrections added three registers after
                    # the prediction was written), and 12589 the
                    # 2026-08-16 one -- the bubble-and-bench commit's
                    # net +60 here, against +61 on the top above; the
                    # one-register disagreement between the two tops was
                    # not chased, and the census, the yosys log and the
                    # nextpnr log agree on 12589.
                    #
                    # The floor stays at 12400, a margin under a
                    # measurement: 189 registers, 1.5%, the same
                    # discipline as oca_core's 11900 under 12033. Loose
                    # enough that the optimiser moving a few registers
                    # does not fail a healthy build, tight enough that
                    # the smallest thing worth catching -- a few hundred
                    # registers, a key store, an accumulator -- cannot
                    # hide under it.
                    "oca_crypto_pll": 12400,
                    # Exact rather than a few percent under, because
                    # these two are new and small enough that every
                    # register in them is accounted for: the decoder's
                    # 302 and the encoder's 75 as measured on 2026-08-12.
                    # A legitimate reduction should fail this and be
                    # re-measured, which on a module this size is cheap.
                    "oca_slip_rx": 302, "oca_slip_tx": 75}

# The cell no flip-flop census can see, and nothing else checks either.
#
# live_ff_census skips any cell whose type lacks "FF" or lacks a Q port,
# so EHXPLLL is invisible to it -- the PLL is not storage at all. It is
# where every clock in the design comes from. A mapper that dropped it
# leaves a netlist that passes every check above, places, routes, meets
# timing and packs a bitstream, and the cmp2lut trap is the precedent for
# treating that as a real risk rather than a theoretical one.
#
# Exact counts, not floors, and deliberately: these follow from the pin
# map rather than from logic, so a change in either direction is
# something a person should look at. Adding a second port means editing
# this table, the way adding a design means editing DESIGNS.
#
# Measured on the netlist in hw/syn/build/.
NETLIST_PRIM_COUNT = {
    # Bring-up step 3 carries the PLL and nothing else physical. The entry
    # exists mainly to reach check_pll, which is called from here and
    # cannot run for a top this table does not list.
    "oca_pll": {"EHXPLLL": 1},
    # The board top of the crypto pair: the same one PLL, on a design
    # where every register outside the heartbeat is clocked from it.
    #
    # The entry is what reaches check_pll and that is most of its value
    # here. Without it check_prims prints a warning and returns 0, and
    # check_prims is the only caller of check_pll — so a missing line in
    # this table does not merely skip a cell census, it leaves the four
    # dividers below unchecked, and with them the 48.0769 MHz that every
    # throughput figure for this design divides into a cycle count.
    "oca_crypto_pll": {"EHXPLLL": 1},
}

# The PLL exists is not the same claim as the PLL is the one the design
# describes, and nothing downstream can tell the two apart.
#
# nextpnr derives the clk_sys and clk_tx constraints from these very
# parameters as they reach the netlist (ecp5/pack.cc), so a wrong
# divider moves the constraint and the measurement together and
# check_timing still reports ok. colorlight_i9.lpf:212 says as much: the
# check on the PLL "is to read those two log lines" -- by hand, every
# time, or never. This is that reading, done by the flow.
#
# It also pins the frequency the published throughput figures divide
# into a cycle count. clk_sys is 625/13 = 48.0769 MHz here; editing
# CLKOS_DIV without editing this table now fails the build instead of
# quietly making every Gbps figure in the documents wrong.
PLL_INPUT_HZ = 25_000_000
NETLIST_PLL_PARAMS = {
    # Bring-up step 3 checks the same four, and it is the one build where
    # they carry a bench consequence rather than a synthesis one. The
    # LED measures CLKOP at 125 MHz to stopwatch precision, which fixes
    # the VCO at 625 MHz; CLKOS is that same VCO over CLKOS_DIV, so a
    # checked 13 here is what makes 48.0769 MHz a conclusion rather than
    # an untested second output nothing in this design consumes.
    "oca_pll": {"CLKI_DIV": 1, "CLKFB_DIV": 5, "CLKOP_DIV": 5,
                "CLKOS_DIV": 13},
    # The same four, from the same instance: oca_crypto_pll instantiates
    # the same oca_clkrst, whose localparams are CLKI_DIV 1, CLKFB_DIV 5,
    # CLKOP_DIV 5, CLKOS_DIV 13 (oca_clkrst.sv:259-263, ecppll's own
    # output transcribed in that file's header).
    #
    # WHY 48.0769 CAN ONLY BE CLKOS, which is what makes a checked
    # CLKOS_DIV of 13 the thing this design turns on rather than a
    # divider nobody reads. FEEDBK_PATH is "CLKOP", so the loop closes on
    # CLKOP and pins it at the phase detector rate times CLKFB_DIV — an
    # integer multiple of 25 MHz here, and oca_clkrst's own guard refuses
    # anything but exactly 125. The VCO is that times CLKOP_DIV and the
    # legal 400-800 MHz band leaves 500, 625 or 750. 48.0769 is not an
    # integer multiple of 25 and cannot be CLKOP at all; it is 625/13 on
    # the secondary output, and nothing else on that ladder is nearer.
    #
    # The consequence for this build in particular: nextpnr derives the
    # clk_sys constraint from these parameters as they reach the netlist,
    # so this is the target where a wrong CLKOS_DIV would move the
    # constraint and the measurement together and check_timing would
    # report a clean 48.08 against a clock that is not 48.08.
    "oca_crypto_pll": {"CLKI_DIV": 1, "CLKFB_DIV": 5, "CLKOP_DIV": 5,
                       "CLKOS_DIV": 13},
}

# The dividers being the ones this table names is still not the claim
# that the design knows what frequency they produce.
#
# oca_clkrst keeps CLK_SYS_HZ as a localparam and exports no parameter to
# read it from, so oca_crypto_pll.sv redoes that arithmetic by hand and
# passes the result to oca_uart_crypto as CLK_HZ -- which is where the
# UART divisor comes from, and the only thing in the design that decides
# what a bit time is. Change the divider the supported way, editing
# oca_clkrst.sv and NETLIST_PLL_PARAMS above together, and nothing
# anywhere compares the new clock against that copy: EHXPLLL has no body,
# so no simulation runs at the real frequency; the flip-flop floors see
# the divisor only through $clog2(DIV), which does not move between
# 48.08 MHz and 56.82 MHz; and the elaboration guard in
# oca_uart_crypto.sv checks the copy against itself. The result builds,
# meets timing, packs, loads, and answers nobody.
#
# So the copy is checked here, against the frequency the netlist's own
# dividers give. Read out of the RTL because it is not in the netlist to
# read: it is a localparam, synth_ecp5 flattens the hierarchy, and the
# top module's parameter_default_values in build/oca_crypto_pll.json is
# empty.
#
# A top mapped to None declares no such constant and needs none: oca_pll
# blinks an LED off CLKOP and consumes clk_sys nowhere, so it holds no
# frequency that can disagree. Listed rather than left out, so that a top
# which grows a PLL and is forgotten here gets the warning below instead
# of a silent pass -- the same hole that was closed one level up when a
# missing NETLIST_PLL_PARAMS entry stopped meaning "checked".
TOP_CLK_SYS_CONST = {
    "oca_pll": None,
    "oca_crypto_pll": ("oca_crypto_pll.sv", "CLK_SYS_HZ"),
}

# Colorlight i9 v7.2 carries an LFE5U-45F-6BG381C (BOM-MVP.md).
DEFAULT_DEVICE = "45k"
DEFAULT_PACKAGE = "CABGA381"
DEFAULT_SPEED = 6

# Hard bound per stage, for a design that does not say otherwise. Finite
# is the point: a build that has produced nothing after this long has not
# produced anything by carrying on either.
#
# It is not generous enough for every design here: oca_dual,
# oca_uart_crypto and oca_crypto_pll each record a bound of their own. A
# design that needs longer states it in DESIGNS, the way it states its
# seed, so naming the target is enough to reproduce its figures.
DEFAULT_TIMEOUT = 1800


def run(cmd, log_path, timeout):
    """Run cmd under a hard wall-clock bound. Returns the exit code.

    The bound lives here, in the only thing that starts yosys and
    nextpnr, and not in whoever calls it. Twice a build in this project
    ran for tens of minutes to no result: once a stalled yosys that
    outlived the agent that started it, once a caller that used a two
    hour timeout where it had been told fifteen minutes. An instruction
    to bound a command is a request; this is a control.

    The child is started in its own session, so the kill can take the
    whole process group. That is the part that failed before: yosys
    spawns helpers, and killing the parent alone leaves them holding a
    core each with nobody watching.
    """
    print(f"+ [<= {timeout}s] " + " ".join(str(c) for c in cmd), flush=True)
    started = time.monotonic()
    with open(log_path, "w") as log:
        # start_new_session: the child leads its own process group.
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True)
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            rc = kill_group(proc, timeout, log_path)
    if rc != 0:
        elapsed = time.monotonic() - started
        print(f"FAILED (rc={rc}) after {elapsed:.0f}s, see {log_path}",
              file=sys.stderr)
        sys.stderr.write(log_path.read_text()[-4000:])
    return rc


def kill_group(proc, timeout, log_path):
    """Take down a timed-out child and everything it started."""
    pgid = os.getpgid(proc.pid)
    print(f"\nTIMEOUT after {timeout}s — killing process group {pgid}.\n"
          f"Nothing was measured. The log so far is {log_path}.\n"
          f"If this build legitimately needs longer, say so with --timeout "
          f"rather than removing the bound.", file=sys.stderr)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            break
        try:
            proc.wait(timeout=10)
            break
        except subprocess.TimeoutExpired:
            continue
    # Report what, if anything, outlived the kill: a silent survivor is
    # exactly the failure this function exists to end.
    try:
        os.killpg(pgid, 0)
        print(f"WARNING: process group {pgid} still exists after SIGKILL; "
              f"check with `ps -eo pid,pgid,etime,comm | grep {pgid}`",
              file=sys.stderr)
    except ProcessLookupError:
        pass
    return 124


def check_cmp2lut():
    """Refuse to run on a yosys whose cmp2lut.v mis-maps signed comparisons.

    `synth_ecp5` runs `techmap -map +/cmp2lut.v` unconditionally. A yosys
    older than f77ddfb87 does not sign-extend the constant operand
    there, so a signed comparison against a negative constant becomes a
    constant-false LUT. `$signed(a) >= -8` is a tautology and must map
    to an all-ones LUT; if it maps to zero the key store silently
    disappears from the netlist.

    Reported as YosysHQ/yosys#6085 and fixed upstream by PR #6114,
    merged 2026-08-14. This project carried the fix as a local patch
    until the pin moved to f77ddfb87 on 2026-08-15. The probe stays
    because it tests the behaviour, not the pin: it is what catches a
    toolchain built from anything older.
    """
    probe = BUILD / "cmp2lut_probe.il"
    probe.write_text(
        "module \\top\n"
        "  wire width 4 input 1 \\a\n"
        "  wire output 2 \\y\n"
        "  cell $ge \\c\n"
        "    parameter \\A_SIGNED 1\n"
        "    parameter \\B_SIGNED 1\n"
        "    parameter \\A_WIDTH 4\n"
        "    parameter \\B_WIDTH 4\n"
        "    parameter \\Y_WIDTH 1\n"
        "    connect \\A \\a\n"
        "    connect \\B 4'1000\n"
        "    connect \\Y \\y\n"
        "  end\n"
        "end\n"
    )
    out = subprocess.run(
        [YOSYS, "-q", "-p", f"read_rtlil {probe}; "
                            "techmap -map +/cmp2lut.v -D LUT_WIDTH=4; write_rtlil"],
        capture_output=True, text=True).stdout
    lut = re.search(r"parameter \\LUT 16'([01]{16})", out)
    if lut is None:
        sys.exit("cmp2lut probe: yosys did not map the comparison; "
                 "cannot vouch for this toolchain")
    if lut.group(1) != "1" * 16:
        sys.exit(
            f"cmp2lut probe FAILED: $signed(a) >= -8 mapped to LUT 16'b{lut.group(1)}, "
            "expected all ones.\nThis yosys mis-synthesises signed comparisons against "
            "negative constants and will delete the key store.\nThe fix is upstream in "
            "yosys f77ddfb87 (PR #6114, 2026-08-14), so this toolchain predates it: "
            "check that tools/src/yosys is at the YOSYS_REV in "
            "scripts/build-toolchain.sh, then move tools/yosys aside and re-run that "
            "script — it refuses to install over a yosys at another revision rather "
            "than leave the old binary standing.\nIf both are already at the pin, "
            "tools/yosys/share/yosys/cmp2lut.v is stale — it is read at run time, so "
            "copying the source file over it is enough."
        )


def live_ff_census(top, netlist):
    """Live flip-flops per RTL file, as yosys attributes them.

    A cell whose DI is its own Q is storage in name only: that is the
    signature the cmp2lut defect left behind, 2056 of the key store's
    2313 registers still present and each holding itself.

    Returns the per-file census and the total counted on cells, not on
    the census buckets: a cell whose src attribute cites two files lands
    in both buckets, and a total summed over the buckets would count it
    twice and could hide a real loss by the size of the overlap.
    """
    design = json.loads(netlist.read_text())
    census = {}
    total = 0
    for c in design["modules"][top]["cells"].values():
        if "FF" not in c["type"] or "Q" not in c["connections"]:
            continue
        if c["connections"].get("DI") == c["connections"]["Q"]:
            continue
        total += 1
        src = c.get("attributes", {}).get("src", "")
        # A mapped cell cites its own techmap rule as well as the RTL it
        # came from, so every cell in the design names cells_map_trellis.v
        # and it would swamp the census with the total.
        design_srcs = [part for part in src.split("|")
                       if "/share/yosys/" not in part]
        keys = sorted(set(re.findall(r"([\w.-]+\.s?v):", "|".join(design_srcs))))
        for f in keys or ["(none)"]:
            census[f] = census.get(f, 0) + 1
    return census, total


def check_clk_sys_const(top, clk_sys):
    """Fail unless the top's copy of clk_sys is the clock the PLL makes.

    See TOP_CLK_SYS_CONST. clk_sys comes from the netlist's dividers;
    this is the only reader of the number the RTL was elaborated with.
    """
    if top not in TOP_CLK_SYS_CONST:
        print(f"WARNING: {top} has checked PLL dividers and no "
              f"TOP_CLK_SYS_CONST entry — nothing compared the clock they "
              f"produce against the frequency this design believes it runs "
              f"at. Not fatal: add an entry, or None if this top derives "
              f"nothing from clk_sys.", file=sys.stderr)
        return 0
    entry = TOP_CLK_SYS_CONST[top]
    if entry is None:
        return 0
    src, name = entry
    path = RTL / src
    m = re.search(rf"^\s*localparam\s+int\s+{name}\s*=\s*([\d_]+)\s*;",
                  path.read_text(), re.M)
    if m is None:
        # Not a skip. This table names the constant, so failing to find
        # it means it was renamed, moved or made an expression, and
        # returning "no opinion" would retire the check without a word.
        sys.exit(f"{path}: no `localparam int {name} = <literal>;` for "
                 f"run_synth.py to check the PLL against. If it moved, move "
                 f"TOP_CLK_SYS_CONST with it.")
    declared = int(m.group(1).replace("_", ""))
    status = "ok" if declared == clk_sys else "FAILED"
    print(f"  {name:<10} {declared} Hz in {src} "
          f"(netlist gives {clk_sys}) — {status}")
    if declared != clk_sys:
        print(f"\n{src} believes clk_sys is {declared} Hz and the netlist's "
              f"PLL makes {clk_sys} Hz. That constant is what reaches "
              f"oca_uart_crypto as CLK_HZ, so the UART divisor belongs to a "
              f"clock this board does not run: a bitstream that builds, "
              f"meets timing, packs, loads and answers nobody.\nEdit "
              f"whichever of the two is wrong — the divider in "
              f"oca_clkrst.sv with NETLIST_PLL_PARAMS, or the copy in "
              f"{src}.", file=sys.stderr)
        return 1
    return 0


def check_pll(top, params):
    """Fail unless the mapped PLL is the one this design describes.

    See NETLIST_PLL_PARAMS. Also prints the clocks the netlist implies,
    which is the reading colorlight_i9.lpf asks a human to do by hand,
    and hands clk_sys to check_clk_sys_const.
    """
    want = NETLIST_PLL_PARAMS.get(top)
    if not want:
        # The same silent skip that was removed from check_prims, one
        # level down: a top listed there but not here reaches this
        # function with a real EHXPLLL in hand and returns ok without
        # looking at it. No top is in that state today, which is exactly
        # when a hole is cheap to close.
        if params is not None:
            print(f"WARNING: {top} has an EHXPLLL in its netlist and no "
                  f"NETLIST_PLL_PARAMS entry — its dividers, and therefore "
                  f"every clock derived from them, went unchecked.",
                  file=sys.stderr)
        return 0
    if params is None:
        print("\nPLL: no EHXPLLL in the netlist", file=sys.stderr)
        return 1
    rc = 0
    got = {}
    print("PLL parameters:")
    for k, n in sorted(want.items()):
        raw = params.get(k)
        # yosys writes an integer parameter as a 32-bit binary string.
        value = int(raw, 2) if isinstance(raw, str) else raw
        got[k] = value
        status = "ok" if value == n else "FAILED"
        print(f"  {k:<10} {value} (want {n}) — {status}")
        if value != n:
            rc = 1
    if rc:
        print("\nThe PLL is not configured as this design describes. nextpnr "
              "derives the clk_sys and clk_tx constraints from these very "
              "parameters, so timing would still report ok against the wrong "
              "clock.", file=sys.stderr)
        return rc
    # Integer division throughout, mirroring oca_clkrst.sv:267-270: those
    # localparams are `int`, so the frequency the RTL computes -- and the
    # constant hand-copied from it -- is truncated and not rounded. The
    # print keeps the full quotient, which is the reading a person does.
    pfd = PLL_INPUT_HZ // got["CLKI_DIV"]
    clk_tx = pfd * got["CLKFB_DIV"]
    vco = clk_tx * got["CLKOP_DIV"]
    print(f"  -> VCO {vco / 1e6:.2f} MHz, clk_tx {clk_tx / 1e6:.4f} MHz, "
          f"clk_sys {vco / got['CLKOS_DIV'] / 1e6:.4f} MHz")
    return check_clk_sys_const(top, vco // got["CLKOS_DIV"])


def check_prims(top, netlist):
    """Fail if a physical-interface primitive is missing or multiplied.

    See NETLIST_PRIM_COUNT for why these need a check of their own: the
    flip-flop census cannot see any of them, and a design that lost its
    PLL or a DDR register still builds, still meets timing and still
    packs.
    """
    want = NETLIST_PRIM_COUNT.get(top)
    if not want:
        # Silence here would have been indistinguishable from a pass, and
        # was: oca_pll built once with no entry in this table, so its PLL
        # parameters went unchecked and check_pll never ran at all, since
        # this is the only place that calls it. The flip-flop census says
        # so when it has no floor; this now says so too.
        print(f"WARNING: {top} has no NETLIST_PRIM_COUNT entry — this run "
              f"checked no physical-interface primitive and did not check "
              f"the PLL parameters either, since check_pll is reached only "
              f"from here. Not fatal: add an entry once this top carries a "
              f"PLL, a DDR register or a delay.", file=sys.stderr)
        return 0
    cells = json.loads(netlist.read_text())["modules"][top]["cells"]
    have = {}
    pll = None
    for cell in cells.values():
        t = cell["type"]
        if t in want:
            have[t] = have.get(t, 0) + 1
        if t == "EHXPLLL" and pll is None:
            pll = cell.get("parameters", {})
    rc = 0
    print("\nphysical-interface primitives:")
    for t, n in sorted(want.items()):
        got = have.get(t, 0)
        status = "ok" if got == n else "FAILED"
        print(f"  {t:<10} {got:>3} (want {n}) — {status}")
        if got != n:
            rc = 1
    rc |= check_pll(top, pll)
    if rc:
        print("\nThe physical interface is not what this design describes: "
              "a link that never comes up builds and packs exactly like one "
              "that does.\nSee run_synth.py, NETLIST_PRIM_COUNT.",
              file=sys.stderr)
    return rc


def check_netlist(top, netlist):
    """Fail if storage the design depends on has vanished from the netlist.

    A mapper bug that folds a memory to a constant leaves a netlist that
    passes every simulation — Verilator never runs yosys — and answers
    'bad slot' to every request on hardware. Count the flip-flops yosys
    attributes to each guarded RTL file and require the full complement.

    This covers storage and nothing else. The comparison that decides
    whether plaintext leaves is combinational, so no census here can see
    it; hw/sim/run_proto_gate.py replays it on the mapped netlist for
    that reason.
    """
    rc = check_prims(top, netlist)
    floors = NETLIST_FF_FLOOR.get(top, {})
    total_floor = NETLIST_FF_TOTAL.get(top)
    if not floors and total_floor is None:
        print(f"WARNING: {top} has no NETLIST_FF_FLOOR or NETLIST_FF_TOTAL "
              f"entry — this run checked nothing about its netlist storage. "
              f"Not fatal: measure a floor and add one once this top carries "
              f"state worth guarding (see README.md, 'The cmp2lut trap').",
              file=sys.stderr)
        return rc
    census, live_total = live_ff_census(top, netlist)
    print("\nlive flip-flops by source file:")
    for src, n in sorted(census.items(), key=lambda kv: -kv[1]):
        print(f"  {src:<24} {n:>6}")
    for src, want in sorted(floors.items()):
        live = census.get(src, 0)
        status = "ok" if live >= want else "FAILED"
        print(f"netlist check {src}: {live} live flip-flops "
              f"(>= {want} required) — {status}")
        if live < want:
            rc = 1
    if total_floor is not None:
        live = live_total
        status = "ok" if live >= total_floor else "FAILED"
        print(f"netlist check (whole netlist): {live} live flip-flops "
              f"(>= {total_floor} required) — {status}")
        if live < total_floor:
            rc = 1
    if rc:
        print("\nStorage is missing from the netlist: the design would build "
              "but not work.\nSee hw/syn/README.md, 'The cmp2lut trap'.",
              file=sys.stderr)
    return rc


def synth_command(top, json_out):
    """The yosys argv for one design's elaboration. Pure, so the two-
    frontend order below can be tested without running yosys at all —
    the same reason pnr_command is split from pnr further down.

    read_slang, not read_verilog -sv: the yosys Verilog-2005 frontend
    rejects the SystemVerilog used by the cores (functions with return,
    concatenation assignments).

    A design that also carries Verilog reads that first. The order is
    not cosmetic: read_slang can see modules already in the design and
    checks its instantiations against them, but a module that arrives
    through read_verilog arrives already elaborated, so a parameter
    override from the SystemVerilog side fails with "parameter 'X' does
    not exist". Any Verilog added here has to be instantiated from
    Verilog wrappers that fix its parameters, never from our own RTL
    directly.
    """
    d = DESIGNS[top]
    cmds = []
    if d.verilog:
        incs = " ".join(f"-I{source_path(i)}" for i in d.incdirs)
        cmds.append(f"read_verilog {incs} "
                    + " ".join(str(source_path(v)) for v in d.verilog))
    cmds.append(f"read_slang --top {top} " + " ".join(str(RTL / s) for s in d.sv))
    cmds.append(f"synth_ecp5 -top {top} -json {json_out}")
    cmds.append("stat")
    return [YOSYS, "-p", "; ".join(cmds)]


def synth(top, json_out, log, timeout):
    return run(synth_command(top, json_out), log, timeout)


def pnr_command(top, json_in, args, report):
    """The nextpnr argv for one build. Pure, so the two paths can be
    tested without running nextpnr at all.
    """
    d = DESIGNS[top]
    cmd = [
        NEXTPNR,
        f"--{args.device}",
        "--package", args.package,
        "--speed", str(args.speed),
        "--json", str(json_in),
        "--freq", str(args.freq),
        "--seed", str(args.seed),
        # Always on, for both paths, so nextpnr runs to completion and
        # writes its report either way. For --out-of-context that is the
        # end of it: a missed target is characterisation, not failure.
        # For a design with real pins, check_timing() below re-reads the
        # same report and log and turns a missed *real* constraint into a
        # build failure; this flag only stops nextpnr failing the build
        # itself, which it would do for every clock it could not
        # otherwise constrain too (common/kernel/timing_log.cc:230-235).
        "--timing-allow-fail",
        "--report", str(report),
        "--write", str(BUILD / f"{top}_pnr.json"),
    ]
    if d.lpf:
        # A design with real pins. Every IO must be constrained: nextpnr
        # skips a LOCATE naming a cell that does not exist without a word,
        # so the unconstrained-IO check is the only thing that catches a
        # misspelled port, and passing --lpf-allow-unconstrained would
        # disable exactly that.
        cmd += ["--lpf", str(SYN_DIR / d.lpf)]
        # Placer and router settings this design asks for. A pinned build
        # is the only place they mean anything: out-of-context placement
        # has no pads to be pulled towards and no congestion to resolve.
        # list(): d.pnr_args is a tuple (see Design above), args.pnr_arg
        # a list built by argparse's action="append", and + needs both
        # sides to agree.
        cmd += list(d.pnr_args) + args.pnr_arg
        # The text configuration ecppack turns into a bitstream. Only for
        # a pinned build: nextpnr refuses --textcfg together with
        # --out-of-context ("bitstream generation is not available in
        # out-of-context mode"), and it fails late, after placement and
        # routing have already run and printed their Fmax, so the report
        # and the routed netlist never get written either.
        cmd += ["--textcfg", str(BUILD / f"{top}.config")]
    else:
        # No pins: the wide internal buses have far more signals than the
        # package has balls, so the core is placed as a locked macro. The
        # numbers characterise the core and not a pinned-out design.
        cmd += ["--out-of-context"]
    return cmd


def pnr(top, json_in, args, report, log):
    return run(pnr_command(top, json_in, args, report), log, args.timeout)


# Matches nextpnr's own log wording for a constraint that came from this
# design rather than from --freq: a FREQUENCY line in the .lpf
# (ecp5/lpf.cc:121, logged by BaseCtx::addClock as "constraining clock
# net '%s' to %.02f MHz") and a constraint nextpnr propagated from one,
# typically a PLL or clock-divider output (ecp5/pack.cc:2824/2853,
# "Derived frequency constraint of %.1f MHz for net %s"). Every other
# clock in the report still gets a "constraint" field — nextpnr applies
# --freq to any net without a ClockConstraint
# (common/kernel/timing.cc:1111) — but that number is nextpnr's
# fallback, not a target this design set for itself.
REAL_CONSTRAINT_PATTERNS = [
    re.compile(r"constraining clock net '([^']+)' to [\d.]+ MHz"),
    re.compile(r"Derived frequency constraint of [\d.]+ MHz for net (\S+)"),
]


def bare_clock_name(clock):
    """The net name nextpnr constrained, from the name it reports.

    A clock is renamed twice on its way into the report and both have to
    come off before it can be matched against the log. Promotion to a
    global buffer prefixes '$glbnet$' (ecp5/globals.cc:465), and a clock
    that arrives on a pad is reported on the input buffer's own net, with
    '$TRELLIS_IO_IN' appended. Measured on the first pinned build: the
    .lpf constrains 'clk25' and 'rgmii_rx_clk', and the report calls them
    'clk25$TRELLIS_IO_IN' and '$glbnet$rgmii_rx_clk$TRELLIS_IO_IN'.

    Stripping only the prefix — which is what this did until the first
    pinned build was run — leaves both of those unmatched, so a clock
    that carries a real .lpf constraint is reported as unchecked and a
    miss on it passes. That is the exact failure check_timing exists to
    prevent, so it is worth being blunt: this function is load-bearing.
    """
    if clock.startswith("$glbnet$"):
        clock = clock[len("$glbnet$"):]
    cut = clock.find("$")
    return clock[:cut] if cut > 0 else clock


def real_clock_constraints(nextpnr_log):
    """Net names nextpnr constrained for real, read back from its own log."""
    text = nextpnr_log.read_text()
    names = set()
    for pattern in REAL_CONSTRAINT_PATTERNS:
        names |= set(pattern.findall(text))
    return names


def check_timing(top, report_path, nextpnr_log):
    """Fail a pinned build that misses a constraint it actually carries.

    A design with no `.lpf` is characterisation (see the module
    docstring): there is no real constraint to check, so this returns 0
    without reading anything, same as before this function existed.

    A design with real pins gets checked: a clock nextpnr promotes to a
    global buffer is renamed '$glbnet$<net>' in the report
    (ecp5/globals.cc:465), which is why the match below tries both
    forms. A clock nextpnr could not otherwise constrain still gets
    --freq applied and printed by summarise() above; that is reported,
    same as always, not failed here, because it is not a target this
    design set for itself — see REAL_CONSTRAINT_PATTERNS.
    """
    if not DESIGNS[top].lpf:
        return 0
    real = real_clock_constraints(nextpnr_log)
    fmax = json.loads(report_path.read_text()).get("fmax", {})
    rc = 0
    fallback = []
    print("\ntiming check (pinned build, constraints this design carries):")
    for clock, data in sorted(fmax.items()):
        if clock not in real and bare_clock_name(clock) not in real:
            fallback.append(clock)
            continue
        achieved = data.get("achieved", 0.0)
        constraint = data.get("constraint", 0.0)
        status = "ok" if achieved >= constraint else "FAILED"
        print(f"  {clock:<24} {achieved:>8.2f} MHz achieved "
              f"(>= {constraint:.2f} MHz required) — {status}")
        if achieved < constraint:
            rc = 1
    if fallback:
        print(f"  no .lpf constraint reached these clocks, nextpnr's --freq "
              f"default applied instead, not checked: {', '.join(fallback)}",
              file=sys.stderr)
    # A pinned build where nothing was checked is the failure this
    # function exists to remove, not a quiet pass: it means every
    # FREQUENCY line in the .lpf failed to reach a clock -- a misspelled
    # net, a clock nextpnr renamed, or an .lpf that carries none at all
    # -- and the fmax figures printed above are all --freq's default.
    # Reporting that on stderr and returning 0 would be the same silent
    # green this whole check was written to end.
    if not any(c in real or bare_clock_name(c) in real for c in fmax):
        print("\nTiming NOT CHECKED: this design carries an .lpf, but not one "
              "of its FREQUENCY constraints reached a clock in the report. "
              "Every figure above is nextpnr's --freq default. Fix the .lpf "
              "or the net names before trusting this build.", file=sys.stderr)
        return 1
    if rc:
        print("\nTiming FAILED: a constraint this design's .lpf carries was "
              "missed. This is a pinned build, not characterisation — see "
              "hw/syn/README.md.", file=sys.stderr)
    return rc


def pack(top, args):
    """Turn a routed pinned design into a bitstream. Nothing else packs.

    An --out-of-context build has no IO buffers and is placed as a locked
    macro, so a bitstream from one would configure a device that drives
    no pin. Those builds stop at the report, which is all they were for.

    This runs only after check_timing has passed. A bitstream that misses
    its clock is a bitstream that will be loaded, will appear to work,
    and will corrupt one frame in some number nobody measures -- packing
    it would turn a build failure into a bench mystery.
    """
    if not DESIGNS[top].lpf:
        return 0
    config = BUILD / f"{top}.config"
    if not config.is_file():
        sys.exit(f"{config} was not written; nextpnr cannot have routed")
    bitstream = BUILD / f"{top}.bit"
    rc = run([ECPPACK, str(config), str(bitstream), "--compress"],
             BUILD / f"{top}.ecppack.log", args.timeout)
    if rc != 0:
        # ecppack having failed says nothing about how much it wrote, and
        # a truncated bitstream is one a programmer will load.
        bitstream.unlink(missing_ok=True)
        return rc
    size = bitstream.stat().st_size
    print(f"\nbitstream: {bitstream} ({size} bytes)")
    print(f"load it with: tools/openFPGALoader/bin/openFPGALoader "
          f"-b colorlight-i9 {bitstream}")
    return 0


def summarise(top, args, report_path):
    report = json.loads(report_path.read_text())
    util = report.get("utilization", {})
    fmax = report.get("fmax", {})

    # Say which of the two builds this was: they are not comparable, and
    # this line said "out-of-context" for both until the first pinned
    # build printed it over a design with 17 real pads.
    kind = "pinned, real IO" if DESIGNS[top].lpf else "out-of-context"
    print(f"\n=== {top} — LFE5U-{args.device.upper()} "
          f"{args.package} speed {args.speed} ({kind}) ===")
    print(f"{'resource':<16} {'used':>8} {'available':>10} {'%':>7}")
    for name in sorted(util):
        used = util[name].get("used", 0)
        avail = util[name].get("available", 0)
        if used == 0:
            continue
        pct = 100.0 * used / avail if avail else 0.0
        print(f"{name:<16} {used:>8} {avail:>10} {pct:>6.1f}%")

    print()
    missed = False
    for clock, data in fmax.items():
        achieved = data.get("achieved", 0.0)
        constraint = data.get("constraint", 0.0)
        flag = ""
        if constraint and achieved < constraint:
            flag = f"  <-- below the {constraint:.1f} MHz target"
            missed = True
        print(f"Fmax {clock}: {achieved:.2f} MHz{flag}")
    if not fmax:
        print("Fmax: not reported by nextpnr")
    if missed:
        print("\nTiming target not met: the numbers above are the achieved "
              "frequency, not a signed-off timing closure.")
    print(f"\nseed={args.seed}  report={report_path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("top", choices=sorted(DESIGNS), help="top module to build")
    ap.add_argument("--device", default=DEFAULT_DEVICE,
                    choices=["25k", "45k", "85k"])
    ap.add_argument("--package", default=DEFAULT_PACKAGE)
    ap.add_argument("--speed", type=int, default=DEFAULT_SPEED, choices=[6, 7, 8])
    ap.add_argument("--freq", type=float, default=100.0,
                    help="clock constraint in MHz (default 100)")
    ap.add_argument("--seed", type=int, default=None,
                    help="nextpnr placer seed. Defaults to the seed the "
                         "design records, or 1 if it records none.")
    ap.add_argument("--timeout", type=int, default=None,
                    help=f"hard wall-clock bound per stage in seconds. "
                         f"Defaults to what the design records, or "
                         f"{DEFAULT_TIMEOUT}. A stage that hits it is killed "
                         f"with its whole process group and nothing is "
                         f"measured.")
    ap.add_argument("--pnr-only", action="store_true",
                    help="skip synthesis and place & route the netlist "
                         "already in build/. For trying placer settings "
                         "without re-running a deterministic 40-minute "
                         "yosys pass. The netlist check still runs.")
    ap.add_argument("--pnr-arg", action="append", default=[], metavar="ARG",
                    help="extra argument for nextpnr, repeatable, pinned "
                         "designs only. For trying a placer or router "
                         "setting before deciding whether it belongs in the "
                         "design's own pnr_args. A figure measured with this "
                         "is not reproducible from the target name alone, so "
                         "do not record one without saying what was passed.")
    args = ap.parse_args()
    if args.seed is None:
        args.seed = DESIGNS[args.top].seed or 1
    if args.timeout is None:
        args.timeout = DESIGNS[args.top].timeout or DEFAULT_TIMEOUT

    for tool in (YOSYS, NEXTPNR):
        if not tool.exists():
            sys.exit(f"missing tool: {tool} (see AGENTS.md for the build steps)")

    BUILD.mkdir(exist_ok=True)
    netlist = BUILD / f"{args.top}.json"
    report = BUILD / f"{args.top}.report.json"

    check_cmp2lut()

    if args.pnr_only:
        # Synthesis is deterministic, so re-running it to try a placer
        # setting spends the same 40 minutes to produce the same netlist.
        # This reuses it. The netlist check still runs: it costs nothing
        # and skipping it is how a mapping defect gets in through a door
        # marked "only placement changed".
        if not netlist.exists():
            sys.exit(f"--pnr-only needs {netlist}, which does not exist; "
                     f"run once without it first")
        print(f"reusing {netlist} (--pnr-only)")
    else:
        rc = synth(args.top, netlist, BUILD / f"{args.top}.yosys.log",
                   args.timeout)
        if rc != 0:
            return rc
    rc = check_netlist(args.top, netlist)
    if rc != 0:
        return rc
    nextpnr_log = BUILD / f"{args.top}.nextpnr.log"
    rc = pnr(args.top, netlist, args, report, nextpnr_log)
    if rc != 0:
        return rc

    # The report has just been replaced, so the bitstream an earlier run
    # left no longer has one. build/ is where a programmer looks and a
    # .bit records nothing about its own origin, so one outliving its
    # report is one that gets loaded against numbers describing a
    # different placement.
    #
    # Here and not earlier, twice over. A run that fails before this
    # point has changed nothing, and destroying the last good artefact
    # over a missing tool or a failed netlist check would cost more than
    # it protects -- on a design this sensitive to placement, retrying
    # with a different seed is the normal way to work. And nextpnr writes
    # neither the report nor the configuration unless place and route
    # both succeed, so a routing failure or a timeout leaves the old
    # report standing: deleting the bitstream that matches it would
    # break the pairing rather than keep it.
    stale = BUILD / f"{args.top}.bit"
    if stale.is_file():
        stale.unlink()

    summarise(args.top, args, report)
    rc = check_timing(args.top, report, nextpnr_log)
    if rc != 0:
        return rc
    return pack(args.top, args)


if __name__ == "__main__":
    sys.exit(main())
