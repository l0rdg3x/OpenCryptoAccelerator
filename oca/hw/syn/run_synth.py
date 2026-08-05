# SPDX-License-Identifier: MIT
"""ECP5 synthesis and place & route for the OCA cores (yosys + nextpnr-ecp5).

Runs the project-local open toolchain (`tools/yosys`, `tools/nextpnr`,
`tools/trellis`) on one of the RTL cores and reports area and Fmax.

The cores expose wide internal buses (512-bit data blocks), far more
signals than the package has pins, so nextpnr runs with
`--out-of-context`: no IO buffers are inserted and the design is placed
as a locked macro. The numbers therefore characterise the core itself,
not a pinned-out design; a top level with a real host interface will add
its own IO and routing pressure.

Usage:
    ../.venv/bin/python hw/syn/run_synth.py chacha20_poly1305
    ../.venv/bin/python hw/syn/run_synth.py --freq 150 chacha20
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RTL = ROOT / "oca" / "hw" / "rtl"
SYN_DIR = Path(__file__).resolve().parent
BUILD = SYN_DIR / "build"

YOSYS = ROOT / "tools" / "yosys" / "bin" / "yosys"
NEXTPNR = ROOT / "tools" / "nextpnr" / "bin" / "nextpnr-ecp5"

# Sources per top module, in dependency order.
DESIGNS = {
    "chacha20": ["chacha20.sv"],
    "poly1305": ["poly1305.sv"],
    "chacha20_poly1305": ["chacha20.sv", "poly1305.sv", "chacha20_poly1305.sv"],
    "oca_core": ["chacha20.sv", "poly1305.sv", "chacha20_poly1305.sv",
                 "oca_keystore.sv", "oca_pktbuf.sv", "oca_proto.sv",
                 "oca_core.sv"],
    "oca_dual": ["chacha20.sv", "poly1305.sv", "chacha20_poly1305.sv",
                 "oca_keystore.sv", "oca_pktbuf.sv", "oca_proto.sv",
                 "oca_core.sv", "oca_dual.sv"],
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
}

# Colorlight i9 v7.2 carries an LFE5U-45F-6BG381C (BOM-MVP.md).
DEFAULT_DEVICE = "45k"
DEFAULT_PACKAGE = "CABGA381"
DEFAULT_SPEED = 6


def run(cmd, log_path):
    """Run cmd, tee-ing its output to log_path. Returns the exit code."""
    print("+ " + " ".join(str(c) for c in cmd), flush=True)
    with open(log_path, "w") as log:
        proc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)
    if proc.returncode != 0:
        print(f"FAILED (rc={proc.returncode}), see {log_path}", file=sys.stderr)
        sys.stderr.write(log_path.read_text()[-4000:])
    return proc.returncode


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
    """
    design = json.loads(netlist.read_text())
    census = {}
    for c in design["modules"][top]["cells"].values():
        if "FF" not in c["type"] or "Q" not in c["connections"]:
            continue
        if c["connections"].get("DI") == c["connections"]["Q"]:
            continue
        src = c.get("attributes", {}).get("src", "")
        for f in sorted(set(re.findall(r"([\w.]+\.sv):", src))) or ["(none)"]:
            census[f] = census.get(f, 0) + 1
    return census


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
    floors = NETLIST_FF_FLOOR.get(top)
    if not floors:
        return 0
    census = live_ff_census(top, netlist)
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
    if rc:
        print("\nStorage is missing from the netlist: the design would build "
              "but not work.\nSee hw/syn/README.md, 'The cmp2lut trap'.",
              file=sys.stderr)
    return rc


def synth(top, sources, json_out, log):
    # read_slang, not read_verilog -sv: the yosys Verilog-2005 frontend
    # rejects the SystemVerilog used by the cores (functions with return,
    # concatenation assignments).
    script = "; ".join(
        [
            f"read_slang --top {top} " + " ".join(str(RTL / s) for s in sources),
            f"synth_ecp5 -top {top} -json {json_out}",
            "stat",
        ]
    )
    return run([YOSYS, "-p", script], log)


def pnr(top, json_in, args, report, log):
    cmd = [
        NEXTPNR,
        f"--{args.device}",
        "--package", args.package,
        "--speed", str(args.speed),
        "--json", str(json_in),
        "--out-of-context",
        "--freq", str(args.freq),
        "--seed", str(args.seed),
        # Characterisation run: a missed target must be reported, not fatal.
        "--timing-allow-fail",
        "--report", str(report),
        "--write", str(BUILD / f"{top}_pnr.json"),
    ]
    return run(cmd, log)


def summarise(top, args, report_path):
    report = json.loads(report_path.read_text())
    util = report.get("utilization", {})
    fmax = report.get("fmax", {})

    print(f"\n=== {top} — LFE5U-{args.device.upper()} "
          f"{args.package} speed {args.speed} (out-of-context) ===")
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
    ap.add_argument("--seed", type=int, default=1,
                    help="nextpnr placer seed, fixed for reproducibility")
    args = ap.parse_args()

    for tool in (YOSYS, NEXTPNR):
        if not tool.exists():
            sys.exit(f"missing tool: {tool} (see AGENTS.md for the build steps)")

    BUILD.mkdir(exist_ok=True)
    netlist = BUILD / f"{args.top}.json"
    report = BUILD / f"{args.top}.report.json"

    check_cmp2lut()

    rc = synth(args.top, DESIGNS[args.top], netlist, BUILD / f"{args.top}.yosys.log")
    if rc != 0:
        return rc
    rc = check_netlist(args.top, netlist)
    if rc != 0:
        return rc
    rc = pnr(args.top, netlist, args, report, BUILD / f"{args.top}.nextpnr.log")
    if rc != 0:
        return rc

    summarise(args.top, args, report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
