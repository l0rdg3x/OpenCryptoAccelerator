# SPDX-License-Identifier: MIT
"""Run the FIFO tests under the project-local Verilator.

Exit code is the contract: runner.test() only inspects results.xml under
pytest and Verilator exits 0 on $finish however the assertions went, so
the count is read back here.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
VERILATOR_BIN = ROOT / "tools" / "verilator" / "bin"
RTL = ROOT / "oca" / "hw" / "rtl"
SOURCES = [RTL / "oca_console.sv"]

os.environ["PATH"] = str(VERILATOR_BIN) + os.pathsep + os.environ["PATH"]

try:
    from cocotb.runner import get_runner  # cocotb 1.x  # noqa: E402
except ModuleNotFoundError:
    from cocotb_tools.runner import get_runner  # cocotb 2.x  # noqa: E402
from cocotb_tools.check_results import get_results  # noqa: E402

SIM_DIR = Path(__file__).resolve().parent


def failed_tests() -> int:
    num_tests, num_failed = get_results(SIM_DIR / "results.xml")
    if num_tests == 0:
        raise RuntimeError("results.xml records no tests")
    return num_failed


def main() -> int:
    runner = get_runner("verilator")
    runner.build(
        sources=SOURCES,
        hdl_toplevel="oca_console",
        build_dir=SIM_DIR / "sim_build_console",
        always=True,
    )
    (SIM_DIR / "results.xml").unlink(missing_ok=True)  # never grade a stale file
    runner.test(
        hdl_toplevel="oca_console",
        test_module="test_console",
        test_dir=SIM_DIR,
        build_dir=SIM_DIR / "sim_build_console",
    )
    return 1 if failed_tests() else 0


if __name__ == "__main__":
    sys.exit(main())
