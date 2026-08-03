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

    rc = synth(args.top, DESIGNS[args.top], netlist, BUILD / f"{args.top}.yosys.log")
    if rc != 0:
        return rc
    rc = pnr(args.top, netlist, args, report, BUILD / f"{args.top}.nextpnr.log")
    if rc != 0:
        return rc

    summarise(args.top, args, report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
