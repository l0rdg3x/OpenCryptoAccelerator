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

sys.path.insert(0, str(ROOT / "oca" / "hw" / "vendor"))
import vendor_patches  # noqa: E402

# Where the pinned submodule sits in a DESIGNS path, and where the file
# yosys actually opens sits instead.
_PINNED_PREFIX = "oca/hw/vendor/verilog-ethernet/"


def source_path(rel: str) -> Path:
    """Absolute path for a DESIGNS entry, redirected to the patched tree.

    The lists name files under the pinned submodule because that is where
    they come from. Nothing reads them there: the submodule is read-only
    and unpatched, and building from it gives a receive path with tkeep
    tied to zero and an FCS comparison on the 125 MHz critical path.
    """
    if rel.startswith(_PINNED_PREFIX):
        return vendor_patches.PATCHED / rel[len(_PINNED_PREFIX):]
    return ROOT / rel


def uses_vendor(top: str) -> bool:
    d = DESIGNS[top]
    return any(p.startswith(_PINNED_PREFIX) for p in d.verilog + d.incdirs)

YOSYS = ROOT / "tools" / "yosys" / "bin" / "yosys"
NEXTPNR = ROOT / "tools" / "nextpnr" / "bin" / "nextpnr-ecp5"
ECPPACK = ROOT / "tools" / "trellis" / "bin" / "ecppack"

# Sources per top module, in dependency order.
#
# sv       SystemVerilog of ours, read by read_slang, relative to hw/rtl/.
# verilog  Verilog read by yosys's own frontend before it, relative to the
#          repository root: the vendored Ethernet stack and the wrappers
#          that fix its parameters.
# incdirs  include paths for that Verilog, relative to the repository root.
# lpf      pin constraints, relative to hw/syn/. A design with one is
#          built with real IO; a design without one is built
#          out-of-context.
class Design(NamedTuple):
    sv: list
    verilog: list = []
    incdirs: list = []
    lpf: str = ""
    # Extra nextpnr arguments, for pinned designs only. Recorded per
    # design rather than passed on the command line so that a figure in
    # a document can be reproduced by naming the target and nothing else.
    pnr_args: list = []
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
    "oca_dual": Design(sv=CORE + ["oca_dual.sv"]),
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
    # The first pinned build this project has. It carries no crypto: it
    # exists to place the clocking and the RGMII pads against the real
    # .lpf and report which clocks nextpnr constrained, which is the one
    # thing that cannot be learned from an out-of-context run and the one
    # thing every later number depends on.
    "oca_top_stub": Design(
        sv=["ecp5_prims.sv", "oca_clkrst.sv", "oca_rgmii.sv",
            "oca_top_stub.sv"],
        lpf="colorlight_i9.lpf",
    ),
    # Clocking, RGMII and the MAC, with the crypto removed. It answers
    # whether oca_top's missed 125 MHz receive constraint is the MAC's
    # own depth or the rest of the design stretching its routing.
    "oca_top_mac": Design(
        sv=["ecp5_prims.sv", "oca_clkrst.sv", "oca_rgmii.sv",
            "oca_top_mac.sv"],
        verilog=[
            "oca/hw/vendor/verilog-ethernet/lib/axis/rtl/axis_fifo.v",
            "oca/hw/vendor/verilog-ethernet/lib/axis/rtl/axis_adapter.v",
            "oca/hw/vendor/verilog-ethernet/lib/axis/rtl/axis_async_fifo.v",
            "oca/hw/vendor/verilog-ethernet/lib/axis/rtl/axis_async_fifo_adapter.v",
            "oca/hw/vendor/verilog-ethernet/rtl/lfsr.v",
            "oca/hw/vendor/verilog-ethernet/rtl/axis_gmii_rx.v",
            "oca/hw/vendor/verilog-ethernet/rtl/axis_gmii_tx.v",
            "oca/hw/vendor/verilog-ethernet/rtl/eth_mac_1g.v",
            "oca/hw/vendor/verilog-ethernet/rtl/eth_mac_1g_fifo.v",
            "oca/hw/rtl/vendor/oca_eth_mac_1g_fifo_64.v",
        ],
        incdirs=[
            "oca/hw/vendor/verilog-ethernet/rtl",
            "oca/hw/vendor/verilog-ethernet/lib/axis/rtl",
        ],
        lpf="colorlight_i9.lpf",
    ),
    # The board. Vendor Verilog is read first, by yosys's own frontend,
    # because a module that reaches read_slang through read_verilog
    # arrives already elaborated -- which is why the three oca_* wrappers
    # exist and why our SystemVerilog can instantiate them with no
    # parameters at all.
    #
    # The file list is the union of the two probe lists plus eth_axis_rx
    # and eth_axis_tx, which nothing in this project needed until there
    # was a top level to join the MAC to the stack. lfsr.v is the only
    # file both probes name; yosys must not read it twice.
    "oca_top": Design(
        sv=["ecp5_prims.sv", "oca_clkrst.sv", "oca_rgmii.sv",
            "chacha20.sv", "poly1305.sv", "chacha20_poly1305.sv",
            "oca_keystore.sv", "oca_pktbuf.sv", "oca_proto.sv",
            "oca_core.sv", "oca_udp_seam.sv", "oca_top.sv"],
        verilog=[
            # AXI-Stream library
            "oca/hw/vendor/verilog-ethernet/lib/axis/rtl/arbiter.v",
            "oca/hw/vendor/verilog-ethernet/lib/axis/rtl/priority_encoder.v",
            "oca/hw/vendor/verilog-ethernet/lib/axis/rtl/axis_fifo.v",
            "oca/hw/vendor/verilog-ethernet/lib/axis/rtl/axis_adapter.v",
            "oca/hw/vendor/verilog-ethernet/lib/axis/rtl/axis_async_fifo.v",
            "oca/hw/vendor/verilog-ethernet/lib/axis/rtl/axis_async_fifo_adapter.v",
            # MAC
            "oca/hw/vendor/verilog-ethernet/rtl/lfsr.v",
            "oca/hw/vendor/verilog-ethernet/rtl/axis_gmii_rx.v",
            "oca/hw/vendor/verilog-ethernet/rtl/axis_gmii_tx.v",
            "oca/hw/vendor/verilog-ethernet/rtl/eth_mac_1g.v",
            "oca/hw/vendor/verilog-ethernet/rtl/eth_mac_1g_fifo.v",
            # Ethernet header parse and build
            "oca/hw/vendor/verilog-ethernet/rtl/eth_axis_rx.v",
            "oca/hw/vendor/verilog-ethernet/rtl/eth_axis_tx.v",
            # ARP, IP, UDP
            "oca/hw/vendor/verilog-ethernet/rtl/arp_cache.v",
            "oca/hw/vendor/verilog-ethernet/rtl/arp_eth_rx.v",
            "oca/hw/vendor/verilog-ethernet/rtl/arp_eth_tx.v",
            "oca/hw/vendor/verilog-ethernet/rtl/arp.v",
            "oca/hw/vendor/verilog-ethernet/rtl/eth_arb_mux.v",
            "oca/hw/vendor/verilog-ethernet/rtl/ip_eth_rx_64.v",
            "oca/hw/vendor/verilog-ethernet/rtl/ip_eth_tx_64.v",
            "oca/hw/vendor/verilog-ethernet/rtl/ip_arb_mux.v",
            "oca/hw/vendor/verilog-ethernet/rtl/ip_64.v",
            "oca/hw/vendor/verilog-ethernet/rtl/ip_complete_64.v",
            "oca/hw/vendor/verilog-ethernet/rtl/udp_checksum_gen_64.v",
            "oca/hw/vendor/verilog-ethernet/rtl/udp_ip_rx_64.v",
            "oca/hw/vendor/verilog-ethernet/rtl/udp_ip_tx_64.v",
            "oca/hw/vendor/verilog-ethernet/rtl/udp_64.v",
            "oca/hw/vendor/verilog-ethernet/rtl/udp_complete_64.v",
            # ours, fixing their parameters
            "oca/hw/rtl/vendor/oca_eth_mac_1g_fifo_64.v",
            "oca/hw/rtl/vendor/oca_eth_axis_64.v",
            "oca/hw/rtl/vendor/oca_udp_complete_64.v",
        ],
        incdirs=[
            "oca/hw/vendor/verilog-ethernet/rtl",
            "oca/hw/vendor/verilog-ethernet/lib/axis/rtl",
        ],
        lpf="colorlight_i9.lpf",
        # Seed 10 is the best of 32, NOT a seed that works. This design
        # does not close timing: rgmii_rx_clk clears 125 MHz on none of
        # the 32 seeds measured, and 10 is the closest at 124.22, short
        # by 0.63%. The next best is 117.32 and the bulk sits between
        # 105 and 117, so the best is a tail and not a near miss.
        #
        # It closed on seed 6 until 54a2df8, at 129.87 / 130.07 / 49.41.
        # Connecting the raw-IP ready pins there -- which the board needs,
        # since without them one non-UDP frame stops reception for good --
        # cost +36 LUTs -- while a third pin in the same commit,
        # clear_arp_cache, brought back +881 LUTs and +400 flip-flops of
        # ARP logic that had been optimised away, around the receive
        # path, which never had margin.
        #
        # Recorded so that the published numbers reproduce by naming the
        # target. See hw/syn/README.md for the sweep and AGENTS.md for
        # what has been ruled out.
        seed=10,
        # Synthesis alone measured 3941 s on this design. 7200 leaves the
        # bound doing its job -- a stage that has stopped progressing
        # still dies -- without killing a build that is simply large.
        timeout=7200,
    ),
}

# oca_rgmii is deliberately not a target of its own. It cannot be built
# out-of-context: nextpnr absorbs a DELAYF or DELAYG into a pin's IOLOGIC
# and refuses one that does not reach a top-level port —
# "DELAYG 'gen_ecp5.u_tx_clk_delay' must be connected directly to top
# level input or output" — and out-of-context inserts no IO at all. It is
# built as part of a pinned top, which is the only place its numbers mean
# anything anyway, since the delay elements cost no fabric.

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
# 3645 live at the RTL of this commit with the toolchain in tools/; the
# floor sits just under that, loose enough to survive the optimiser
# moving a few registers and tight enough to catch storage vanishing
# wholesale, which is what the cmp2lut trap did to 89% of the key store.
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
    "oca_core": {"oca_keystore.sv": 2313, "oca_proto.sv": 3600},
    "oca_dual": {"oca_keystore.sv": 4626, "oca_proto.sv": 7200},
    # The board build. Same two floors as oca_core, since it contains
    # one, plus the seam: 276 live at this RTL, floored at 270. The seam
    # is worth its own floor because its header queue is the one place
    # in this design where losing storage does not break anything
    # visible -- it just starts addressing replies to the wrong peer.
    #
    # And the vendor stack, every module of it that carries state. This
    # is not belt and braces: until 2026-08-11 the census keyed on a .sv
    # filename, so no vendor module could ever produce a bucket, and
    # arp_cache.v sat at ZERO live flip-flops in every netlist this
    # project ever built -- it first reached 130 on 2026-08-11, when
    # clear_arp_cache stopped being undriven. The design that closed
    # timing, packed a bitstream and was published had no ARP cache in
    # it at all: the same shape as the cmp2lut trap, and invisible for
    # the same reason, nothing counted it.
    #
    # Floored 5% under measured, because the failure worth catching is a
    # module vanishing rather than a register moving. This covers the
    # 4281 flip-flops that carry a vendor .v in their src; the remaining
    # 525 of the old unattributed lump carry no design source at all and
    # are still guarded only by NETLIST_FF_TOTAL. Re-measure when the
    # pin, the patches or a parameter changes; the census printed below
    # is where the numbers come from.
    "oca_top": {"oca_keystore.sv": 2313, "oca_proto.sv": 3600,
                "oca_udp_seam.sv": 270,
                "arp.v": 335,                   # 353 measured
                "arp_cache.v": 123,             # 130
                "arp_eth_rx.v": 266,            # 280
                "arp_eth_tx.v": 272,            # 287
                "axis_adapter.v": 166,          # 175
                "axis_async_fifo.v": 355,       # 374
                "axis_fifo.v": 89,              # 94
                "axis_gmii_rx.v": 99,           # 105
                "axis_gmii_tx.v": 67,           # 71
                "eth_arb_mux.v": 194,           # 205
                "eth_axis_rx.v": 229,           # 242
                "eth_axis_tx.v": 249,           # 263
                "ip_64.v": 52,                  # 55
                "ip_arb_mux.v": 238,            # 251
                "ip_eth_rx_64.v": 389,          # 410
                "ip_eth_tx_64.v": 431,          # 454
                "udp_checksum_gen_64.v": 137,   # 145
                "udp_ip_rx_64.v": 295,          # 311
                "udp_ip_tx_64.v": 384},         # 405
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
# oca_core measures 12033 live flip-flops, oca_dual exactly twice that.
#
# oca_top measures 17249, of which 12043 are attributed to our RTL and
# 5206 to the vendor stack, which read_verilog gives no src attribute
# this census can key on. (16849 / 4806 until 2026-08-11 and 16840 /
# 4797 before the vendor patches: the last step is the +400 the raw-IP
# tie-off in 54a2df8 keeps alive, all of it vendor and none of it ours.)
# The total is floored rather than those two
# separately for the same reason as above, and because the vendor's
# share is the part most likely to move if a parameter changes.
NETLIST_FF_TOTAL = {"oca_core": 11900, "oca_dual": 23800, "oca_top": 16700}

# The cells no flip-flop census can see, and nothing else checks either.
#
# live_ff_census skips any cell whose type lacks "FF" or lacks a Q port,
# so IDDRX1F, ODDRX1F, DELAYF, DELAYG and EHXPLLL are all invisible to
# it -- the DDR cells expose Q0/Q1, and the PLL is not storage at all.
# That is the whole physical interface: 22 bits of receive capture and
# transmit launch, their delay lines, and the one PLL every clock in the
# design comes from. A mapper that dropped any of them leaves a netlist
# that passes every check above, places, routes, meets timing and packs
# a bitstream. On the board it is a link that never comes up, and the
# cmp2lut trap is the precedent for treating that as a real risk rather
# than a theoretical one.
#
# Exact counts, not floors, and deliberately: these follow from the pin
# map rather than from logic, so a change in either direction is
# something a person should look at. Adding a second port means editing
# this table, the way adding a design means editing DESIGNS.
#
# Measured on the netlists in hw/syn/build/: identical across all three
# pinned designs, because all three carry one oca_rgmii and one
# oca_clkrst.
NETLIST_PRIM_COUNT = {
    # Bring-up step 3 carries the PLL and nothing else physical: no DDR
    # register and no delay, because it touches no RGMII pad. The entry
    # exists mainly to reach check_pll, which is called from here and
    # cannot run for a top this table does not list.
    "oca_pll": {"EHXPLLL": 1},
    "oca_top": {"EHXPLLL": 1, "IDDRX1F": 5, "ODDRX1F": 6,
                "DELAYF": 5, "DELAYG": 6},
    "oca_top_mac": {"EHXPLLL": 1, "IDDRX1F": 5, "ODDRX1F": 6,
                    "DELAYF": 5, "DELAYG": 6},
    "oca_top_stub": {"EHXPLLL": 1, "IDDRX1F": 5, "ODDRX1F": 6,
                     "DELAYF": 5, "DELAYG": 6},
}

# The PLL exists is not the same claim as the PLL is the one the design
# describes, and nothing downstream can tell the two apart.
#
# nextpnr derives the clk_sys and clk_tx constraints from these very
# parameters as they reach the netlist (ecp5/pack.cc), so a wrong
# divider moves the constraint and the measurement together and
# check_timing still reports ok. colorlight_i9.lpf:195 says as much: the
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
    "oca_top": {"CLKI_DIV": 1, "CLKFB_DIV": 5, "CLKOP_DIV": 5,
                "CLKOS_DIV": 13},
    "oca_top_mac": {"CLKI_DIV": 1, "CLKFB_DIV": 5, "CLKOP_DIV": 5,
                    "CLKOS_DIV": 13},
    "oca_top_stub": {"CLKI_DIV": 1, "CLKFB_DIV": 5, "CLKOP_DIV": 5,
                     "CLKOS_DIV": 13},
}

# Colorlight i9 v7.2 carries an LFE5U-45F-6BG381C (BOM-MVP.md).
DEFAULT_DEVICE = "45k"
DEFAULT_PACKAGE = "CABGA381"
DEFAULT_SPEED = 6

# Hard bound per stage, for a design that does not say otherwise. Finite
# is the point: a build that has produced nothing after this long has not
# produced anything by carrying on either.
#
# It is NOT generous enough for the whole Ethernet top, which this
# comment claimed until 2026-08-10. oca_top's synthesis measured 3941 s
# ("End of script ... time: 3941.43s" in its yosys log), so
# `run_synth.py oca_top` at this bound is killed at 30 minutes with
# nothing measured -- the documented way to reproduce that design's
# figures could not finish. A design that needs longer now records it,
# the way oca_top records its seed, so naming the target is enough.
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

    `synth_ecp5` runs `techmap -map +/cmp2lut.v` unconditionally. Stock
    yosys (still true upstream at 0.67+/41a4b5a03) does not sign-extend
    the constant operand there, so a signed comparison against a
    negative constant becomes a constant-false LUT. `$signed(a) >= -8`
    is a tautology and must map to an all-ones LUT; if it maps to zero
    the key store silently disappears from the netlist. Apply
    patches/yosys-cmp2lut-signed-negative-constant.patch.
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
            "negative constants and will delete the key store.\nApply "
            f"{SYN_DIR / 'patches' / 'yosys-cmp2lut-signed-negative-constant.patch'} "
            "to tools/src/yosys and copy the result over "
            "tools/yosys/share/yosys/cmp2lut.v (it is read at run time; no rebuild needed)."
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


def check_pll(top, params):
    """Fail unless the mapped PLL is the one this design describes.

    See NETLIST_PLL_PARAMS. Also prints the clocks the netlist implies,
    which is the reading colorlight_i9.lpf asks a human to do by hand.
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
    pfd = PLL_INPUT_HZ / got["CLKI_DIV"]
    clk_tx = pfd * got["CLKFB_DIV"]
    vco = clk_tx * got["CLKOP_DIV"]
    print(f"  -> VCO {vco / 1e6:.2f} MHz, clk_tx {clk_tx / 1e6:.4f} MHz, "
          f"clk_sys {vco / got['CLKOS_DIV'] / 1e6:.4f} MHz")
    return 0


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


def synth(top, json_out, log, timeout):
    """Elaborate one design, through one or both yosys frontends.

    read_slang, not read_verilog -sv: the yosys Verilog-2005 frontend
    rejects the SystemVerilog used by the cores (functions with return,
    concatenation assignments).

    A design that also carries Verilog — the vendored Ethernet stack and
    the wrappers that fix its parameters — reads that first. The order is
    not cosmetic: read_slang can see modules already in the design and
    checks its instantiations against them, but a module that arrives
    through read_verilog arrives already elaborated, so a parameter
    override from the SystemVerilog side fails with "parameter 'X' does
    not exist". That is why the vendor modules are instantiated from
    Verilog wrappers and never from our own RTL directly.
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
    return run([YOSYS, "-p", "; ".join(cmds)], log, timeout)


def pnr(top, json_in, args, report, log):
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
        # The text configuration ecppack turns into a bitstream. Written
        # for every build; only a pinned one is packed, since an
        # out-of-context placement has no IO and would produce a
        # bitstream that drives nothing.
        "--textcfg", str(BUILD / f"{top}.config"),
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
        cmd += d.pnr_args + args.pnr_arg
    else:
        # No pins: the wide internal buses have far more signals than the
        # package has balls, so the core is placed as a locked macro. The
        # numbers characterise the core and not a pinned-out design.
        cmd += ["--out-of-context"]
    return run(cmd, log, args.timeout)


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

    # Which vendor tree this netlist was elaborated from, recorded beside
    # it. Without this --pnr-only would happily place and route a netlist
    # built from the pinned tree while printing that the patches are in,
    # and there was a live case of exactly that in build/.
    provenance = BUILD / f"{args.top}.vendor.json"

    if uses_vendor(args.top):
        vendor_patches.require()
        print(vendor_patches.describe(vendor_patches.PATCHED))

    if args.pnr_only:
        # Synthesis is deterministic, so re-running it to try a placer
        # setting spends the same 40 minutes to produce the same netlist.
        # This reuses it. The netlist check still runs: it costs nothing
        # and skipping it is how a mapping defect gets in through a door
        # marked "only placement changed".
        if not netlist.exists():
            sys.exit(f"--pnr-only needs {netlist}, which does not exist; "
                     f"run once without it first")
        if uses_vendor(args.top):
            want = vendor_patches.stamp_now(vendor_patches.PATCHED)
            try:
                have = json.loads(provenance.read_text())
            except (OSError, ValueError):
                sys.exit(f"{netlist} has no record of the vendor tree it was "
                         f"elaborated from. Re-run without --pnr-only.")
            if have != want:
                sys.exit(f"{netlist} was elaborated from a different vendor "
                         f"tree than the one present now. Placing it would "
                         f"report Fmax for a design that is not this one. "
                         f"Re-run without --pnr-only.")
        print(f"reusing {netlist} (--pnr-only)")
    else:
        rc = synth(args.top, netlist, BUILD / f"{args.top}.yosys.log",
                   args.timeout)
        if rc != 0:
            return rc
        if uses_vendor(args.top):
            provenance.write_text(json.dumps(
                vendor_patches.stamp_now(vendor_patches.PATCHED), indent=2))
        elif provenance.exists():
            provenance.unlink()
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
