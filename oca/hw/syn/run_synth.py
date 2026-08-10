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
    # The placer seed this design is known to close timing on. None means
    # the flow's default. A design that needs a particular seed says so
    # here, because "it closes" and "it closes on seed 6" are different
    # claims and only one of them reproduces.
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
        # Seed 6, and it is not a preference. Over seeds 1 to 13 this
        # design closes all three constraints on this one and no other:
        # rgmii_rx_clk clears 125 MHz on two of the thirteen and clk_tx
        # on six, and the only seed where those overlap is 6. Both
        # clocks swing about 20% across the sweep, so a seed is not a
        # detail here, it is the difference between a bitstream and a
        # failed build.
        #
        # What that means, stated rather than left to be discovered: the
        # design has no timing margin of its own. Any RTL change reshuffles
        # the placement and the seed has to be found again. See
        # hw/syn/README.md for the whole sweep.
        seed=6,
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
    "oca_core": {"oca_keystore.sv": 2313, "oca_proto.sv": 3600},
    "oca_dual": {"oca_keystore.sv": 4626, "oca_proto.sv": 7200},
    # The board build. Same two floors as oca_core, since it contains
    # one, plus the seam: 276 live at this RTL, floored at 270. The seam
    # is worth its own floor because its header queue is the one place
    # in this design where losing storage does not break anything
    # visible -- it just starts addressing replies to the wrong peer.
    "oca_top": {"oca_keystore.sv": 2313, "oca_proto.sv": 3600,
                "oca_udp_seam.sv": 270},
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
# oca_top measures 16840, of which 12043 are attributed to our RTL and
# 4797 to the vendor stack, which read_verilog gives no src attribute
# this census can key on. The total is floored rather than those two
# separately for the same reason as above, and because the vendor's
# share is the part most likely to move if a parameter changes.
NETLIST_FF_TOTAL = {"oca_core": 11900, "oca_dual": 23800, "oca_top": 16700}

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
        for f in sorted(set(re.findall(r"([\w.]+\.sv):", src))) or ["(none)"]:
            census[f] = census.get(f, 0) + 1
    return census, total


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
    floors = NETLIST_FF_FLOOR.get(top, {})
    total_floor = NETLIST_FF_TOTAL.get(top)
    if not floors and total_floor is None:
        print(f"WARNING: {top} has no NETLIST_FF_FLOOR or NETLIST_FF_TOTAL "
              f"entry — this run checked nothing about its netlist storage. "
              f"Not fatal: measure a floor and add one once this top carries "
              f"state worth guarding (see README.md, 'The cmp2lut trap').",
              file=sys.stderr)
        return 0
    census, live_total = live_ff_census(top, netlist)
    print("\nlive flip-flops by source file:")
    for src, n in sorted(census.items(), key=lambda kv: -kv[1]):
        print(f"  {src:<24} {n:>6}")
    rc = 0
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
    # it protects -- on a design that closes on one placer seed of
    # thirteen, retrying is the normal way to work. And nextpnr writes
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
