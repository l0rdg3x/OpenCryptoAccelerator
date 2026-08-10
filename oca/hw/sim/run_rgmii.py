# SPDX-License-Identifier: MIT
"""Run the RGMII front end cocotb tests under the project-local Verilator.

The build sets SIMULATION=1. The other branch instantiates IDDRX1F and
ODDRX1F, which reach a simulator only as the blackboxes ecp5_prims.sv
declares -- no behaviour at all -- so a testbench on that branch
elaborates, runs green and captures nothing.

The delay guard runs first, outside the simulator, for the reason
run_pktbuf.py checks BYTES there: a cocotb run only ever sees the tap
count its build was elaborated with, so a DEL_VALUE the ECP5 cannot hold
has to fail to elaborate rather than wrap to a design with no delay in it
and no diagnostic.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
VERILATOR_BIN = ROOT / "tools" / "verilator" / "bin"
RTL = ROOT / "oca" / "hw" / "rtl"
SOURCES = [RTL / "ecp5_prims.sv", RTL / "oca_rgmii.sv"]

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

# Tap counts the module implements, and tap counts it must refuse.
# DEL_VALUE is the 7-bit field oca_rgmii.sv documents against nextpnr's
# bitstream writer: 128 wraps to 0, so a design asking for it comes up
# with the delay element at no taps at all and says nothing. 127 is the
# last legal one; -1 is the other end, which a parameter typed `int`
# accepts happily.
LEGAL_DELAYS = [0, 80, 127]
ILLEGAL_DELAYS = [128, 200, -1]


def elaborates(rx_delay: int, tx_delay: int) -> bool:
    """Elaborate oca_rgmii alone at these tap counts. True if it builds.

    On SIMULATION=1 because -Wall is what makes a failure legible here,
    and on the ECP5 branch -Wall fails for a reason that has nothing to do
    with the guard: the blackbox ports ecp5_prims.sv declares are neither
    driven nor read, so every value would be reported illegal.
    """
    proc = subprocess.run(
        [VERILATOR_BIN / "verilator", "--lint-only", "-Wall", "-GSIMULATION=1",
         f"-GRX_DEL_VALUE={rx_delay}", f"-GTX_DEL_VALUE={tx_delay}",
         *[str(src) for src in SOURCES], "--top-module", "oca_rgmii"],
        capture_output=True, text=True)
    return proc.returncode == 0


def check_delay_guard() -> int:
    rc = 0
    for value in LEGAL_DELAYS:
        if not elaborates(value, 0):
            print(f"RX_DEL_VALUE={value} is legal but does not elaborate",
                  flush=True)
            rc = 1
        if not elaborates(80, value):
            print(f"TX_DEL_VALUE={value} is legal but does not elaborate",
                  flush=True)
            rc = 1
    for value in ILLEGAL_DELAYS:
        if elaborates(value, 0):
            print(f"RX_DEL_VALUE={value} elaborated: the guard is gone and the "
                  "delay lands on whatever seven bits of it survive", flush=True)
            rc = 1
        if elaborates(80, value):
            print(f"TX_DEL_VALUE={value} elaborated: the guard is gone and the "
                  "delay lands on whatever seven bits of it survive", flush=True)
            rc = 1
    print(f"delay guard: {2 * len(LEGAL_DELAYS)} legal accepted, "
          f"{2 * len(ILLEGAL_DELAYS)} illegal refused — "
          f"{'ok' if rc == 0 else 'FAILED'}", flush=True)
    return rc


def main() -> int:
    rc = check_delay_guard()
    if rc != 0:
        return rc

    runner = get_runner("verilator")
    runner.build(
        sources=SOURCES,
        hdl_toplevel="oca_rgmii",
        build_dir=SIM_DIR / "sim_build_rgmii",
        parameters={"SIMULATION": 1},
        always=True,
    )
    (SIM_DIR / "results.xml").unlink(missing_ok=True)  # never grade a stale file
    runner.test(
        hdl_toplevel="oca_rgmii",
        test_module="test_rgmii",
        test_dir=SIM_DIR,
        build_dir=SIM_DIR / "sim_build_rgmii",
    )
    return 1 if failed_tests() else 0


if __name__ == "__main__":
    sys.exit(main())
