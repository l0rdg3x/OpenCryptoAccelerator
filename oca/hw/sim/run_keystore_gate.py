# SPDX-License-Identifier: MIT
"""Run the key store cocotb tests against the SYNTHESISED ECP5 netlist.

Every other suite in this directory elaborates the SystemVerilog and
never runs yosys, so none of them can see a synthesis bug. That blind
spot cost this project its key store: a yosys older than the pinned
revision mis-maps the index bounds check in `oca_keystore.sv` and
deletes all 2048 key bits, while `run_keystore.py` stays green (see
`../syn/README.md`, "The cmp2lut trap").

This runner synthesises `oca_keystore` to ECP5 primitives and replays
the same tests on the mapped netlist. On a netlist built with the
unpatched mapper, `test_write_then_read` and `test_reset_clears` fail.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
VERILATOR_BIN = ROOT / "tools" / "verilator" / "bin"
YOSYS = ROOT / "tools" / "yosys" / "bin" / "yosys"
ECP5_SIM = ROOT / "tools" / "yosys" / "share" / "yosys" / "ecp5"

os.environ["PATH"] = str(VERILATOR_BIN) + os.pathsep + os.environ["PATH"]

try:
    from cocotb.runner import get_runner  # cocotb 1.x  # noqa: E402
except ModuleNotFoundError:
    from cocotb_tools.runner import get_runner  # cocotb 2.x  # noqa: E402
from cocotb_tools.check_results import get_results  # noqa: E402

SIM_DIR = Path(__file__).resolve().parent
BUILD = SIM_DIR / "sim_build_keystore_gate"


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


def synth(netlist: Path) -> None:
    rtl = ROOT / "oca" / "hw" / "rtl" / "oca_keystore.sv"
    script = (f"read_slang --top oca_keystore {rtl}; "
              f"synth_ecp5 -top oca_keystore; "
              f"write_verilog -noattr {netlist}")
    subprocess.run([YOSYS, "-q", "-p", script], check=True)


def main() -> int:
    BUILD.mkdir(parents=True, exist_ok=True)
    netlist = BUILD / "oca_keystore_ecp5.v"
    synth(netlist)

    runner = get_runner("verilator")
    runner.build(
        # The vendor primitive models are not lint-clean and carry
        # several top modules; neither is our problem here.
        verilog_sources=[ECP5_SIM / "cells_sim.v", netlist],
        hdl_toplevel="oca_keystore",
        build_dir=BUILD,
        build_args=["--timing", f"-I{ECP5_SIM}",
                    "-Wno-lint", "-Wno-style", "-Wno-MULTITOP", "-Wno-fatal"],
        always=True,
    )
    (SIM_DIR / "results.xml").unlink(missing_ok=True)  # never grade a stale file
    runner.test(
        hdl_toplevel="oca_keystore",
        test_module="test_keystore",
        test_dir=SIM_DIR,
        build_dir=BUILD,
    )
    return 1 if failed_tests() else 0


if __name__ == "__main__":
    sys.exit(main())
