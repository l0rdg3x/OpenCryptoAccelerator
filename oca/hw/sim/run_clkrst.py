# SPDX-License-Identifier: MIT
"""Run the clock and reset cocotb tests under the project-local Verilator.

No SIMULATION parameter, because oca_clkrst.sv has no SIMULATION branch:
its header says why, and test_clkrst.py says what the testbench does
instead. The PLL is a blackbox with no behaviour in either case.

The elaboration guards run first, outside the simulator, because a
cocotb run only ever sees the parameters its build elaborated with: a
value the module is supposed to refuse has to fail to elaborate rather
than come up as a design nobody asked for. That is the module's own
argument for the guards -- nothing downstream of them makes these
checks -- and the guards are the only part of the PLL arithmetic a
simulation of a blackbox can reach at all.

All six guards are reachable from here since 2026-08-16, when the
divider set became parameters: VCO_HZ, PFD_HZ and CLK_TX_HZ now
recompute from CLKI_DIV, CLKFB_DIV, CLKOP_DIV and CLKOS_DIV, so the
three guards on them are exercised below with divider sets rather than
single values -- the guards check derived quantities, and one divider
cannot always move one while the others hold.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
VERILATOR_BIN = ROOT / "tools" / "verilator" / "bin"
RTL = ROOT / "oca" / "hw" / "rtl"
SOURCES = [RTL / "ecp5_prims.sv", RTL / "oca_clkrst.sv"]

os.environ["PATH"] = str(VERILATOR_BIN) + os.pathsep + os.environ["PATH"]

try:
    from cocotb.runner import get_runner  # cocotb 1.x  # noqa: E402
except ModuleNotFoundError:
    from cocotb_tools.runner import get_runner  # cocotb 2.x  # noqa: E402
from cocotb_tools.check_results import get_results  # noqa: E402

SIM_DIR = Path(__file__).resolve().parent


def failed_tests() -> int:
    """Red tests in the run that just finished.

    runner.test() only inspects results.xml under pytest, and Verilator
    exits 0 on $finish even with failing tests: without this check the
    process exits 0 however the suite went, and anything driving these
    runners by exit code would call a red suite green.
    """
    num_tests, num_failed = get_results(SIM_DIR / "results.xml")
    if num_tests == 0:
        raise RuntimeError("results.xml records no tests")
    return num_failed


# Values each guard must accept, and values it must refuse. The bounds are
# oca_clkrst.sv's: RST_SYNC_STAGES >= 2 because one flop synchronises
# nothing, PHY_RST_MS 10..1000 and PHY_WAIT_US 20..100000 because below
# the lower bound is a datasheet violation and above the upper one the
# cycle count wraps a 32-bit int and the PHY gets a shorter reset than
# was asked for, silently. Both ends of each range are exercised: an
# upper bound is the half that is easy to leave out.
GUARDS = {
    "RST_SYNC_STAGES": ([2, 3, 5], [1, 0, -1]),
    "PHY_RST_MS":      ([10, 100, 1000], [9, 0, 1001]),
    "PHY_WAIT_US":     ([20, 1000, 100_000], [19, 0, 100_001]),
}

# The three guards on derived quantities, exercised as whole divider
# sets. Accepted sets are the rungs of the module's own ladder: the
# shipping 625 MHz VCO (the defaults), the 50.00 MHz rung's 500, and a
# 750 for the band's last legal VCO. Refused sets each isolate one
# guard -- the other two derived values stay legal, so the refusal has
# to come from the guard named by its message fragment. `Refused` alone
# is not enough, per the message check below: a deleted guard can leave
# a set that fails for another reason entirely.
DIVIDER_ACCEPT = [
    {},                                  # defaults: VCO 625, clk_sys 48.08
    {"CLKOP_DIV": 4, "CLKOS_DIV": 10},   # VCO 500, clk_sys exactly 50.00
    {"CLKOP_DIV": 6, "CLKOS_DIV": 12},   # VCO 750, clk_sys 62.50
]
DIVIDER_REFUSE = [
    # VCO 375 MHz, under the band; clk_tx stays 125, PFD stays 25.
    ({"CLKOP_DIV": 3}, "VCO"),
    # VCO 875 MHz, over the band; clk_tx stays 125, PFD stays 25.
    ({"CLKOP_DIV": 7}, "VCO"),
    # PFD 5 MHz, under the 10 MHz minimum, with VCO 500 and clk_tx 125
    # both legal: the CLKFB_DIV of 25 is what keeps them so.
    ({"CLKI_DIV": 5, "CLKFB_DIV": 25, "CLKOP_DIV": 4, "CLKOS_DIV": 10},
     "phase detector"),
    # clk_tx 100 MHz, not the 125 RGMII needs, with VCO 500 legal.
    ({"CLKFB_DIV": 4}, "clk_tx"),
]


def elaborate(overrides: dict[str, int]) -> tuple[bool, str]:
    """Elaborate oca_clkrst with the given parameters overridden.

    -Wall because that is what makes a failure legible, and it is clean
    at every legal value: the blackbox ports are waived inside
    ecp5_prims.sv and PINMISSING over the PLL instance alone.
    """
    proc = subprocess.run(
        [VERILATOR_BIN / "verilator", "--lint-only", "-Wall",
         *[f"-G{name}={value}" for name, value in overrides.items()],
         *[str(src) for src in SOURCES], "--top-module", "oca_clkrst"],
        capture_output=True, text=True)
    return proc.returncode == 0, proc.stdout + proc.stderr


def diagnostics(output: str) -> str:
    """Verilator's own first words on it, so a refusal names its own cause."""
    lines = [line for line in output.splitlines() if line.startswith("%")]
    return "\n".join("    " + line for line in lines[:4])


def check_elaboration_guards() -> int:
    rc = 0
    legal = illegal = 0
    for name, (accept, refuse) in GUARDS.items():
        for value in accept:
            legal += 1
            ok, out = elaborate({name: value})
            if not ok:
                print(f"{name}={value} is legal but does not elaborate\n"
                      f"{diagnostics(out)}", flush=True)
                rc = 1
        for value in refuse:
            illegal += 1
            ok, out = elaborate({name: value})
            if ok:
                print(f"{name}={value} elaborated: the guard is gone and the "
                      "design comes up on a value the module refuses",
                      flush=True)
                rc = 1
            # Refused is not enough: it has to be this guard that refused
            # it. RST_SYNC_STAGES below 2 also makes sync_sys[-1:0] a slice
            # -Wall rejects on its own, so a check that only read the exit
            # code would report the guard working with the guard deleted.
            elif f"oca_clkrst: {name} must" not in out:
                print(f"{name}={value} was refused, but not by the module's own "
                      f"guard\n{diagnostics(out)}", flush=True)
                rc = 1
    for overrides in DIVIDER_ACCEPT:
        legal += 1
        ok, out = elaborate(overrides)
        if not ok:
            print(f"divider set {overrides or 'defaults'} is legal but does "
                  f"not elaborate\n{diagnostics(out)}", flush=True)
            rc = 1
    for overrides, fragment in DIVIDER_REFUSE:
        illegal += 1
        ok, out = elaborate(overrides)
        if ok:
            print(f"divider set {overrides} elaborated: the guard is gone "
                  "and the design comes up on a set the module refuses",
                  flush=True)
            rc = 1
        # Same discipline as above: the refusal has to name the derived
        # quantity whose guard this set isolates.
        elif "oca_clkrst:" not in out or fragment not in out:
            print(f"divider set {overrides} was refused, but not by the "
                  f"guard on {fragment}\n{diagnostics(out)}", flush=True)
            rc = 1
    print(f"elaboration guards: {legal} legal accepted, {illegal} illegal "
          f"refused by their own guard — {'ok' if rc == 0 else 'FAILED'}",
          flush=True)
    return rc


def main() -> int:
    rc = check_elaboration_guards()
    if rc != 0:
        return rc

    runner = get_runner("verilator")
    runner.build(
        sources=SOURCES,
        hdl_toplevel="oca_clkrst",
        build_dir=SIM_DIR / "sim_build_clkrst",
        always=True,
    )
    (SIM_DIR / "results.xml").unlink(missing_ok=True)  # never grade a stale file
    runner.test(
        hdl_toplevel="oca_clkrst",
        test_module="test_clkrst",
        test_dir=SIM_DIR,
        build_dir=SIM_DIR / "sim_build_clkrst",
    )
    return 1 if failed_tests() else 0


if __name__ == "__main__":
    sys.exit(main())
