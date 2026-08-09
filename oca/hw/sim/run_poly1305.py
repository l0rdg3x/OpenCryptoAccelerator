# SPDX-License-Identifier: MIT
"""Run the Poly1305 cocotb tests under the project-local Verilator."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
VERILATOR_BIN = ROOT / "tools" / "verilator" / "bin"

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


def run_at(rows: int, tag: str) -> int:
    print(f"=== poly1305 at ROWS_PER_CYCLE={rows}", flush=True)
    runner = get_runner("verilator")
    runner.build(
        sources=[ROOT / "oca" / "hw" / "rtl" / "poly1305.sv"],
        hdl_toplevel="poly1305",
        build_dir=SIM_DIR / f"sim_build_poly{tag}",
        parameters={"ROWS_PER_CYCLE": rows},
        always=True,
    )
    (SIM_DIR / "results.xml").unlink(missing_ok=True)  # never grade a stale file
    runner.test(
        hdl_toplevel="poly1305",
        test_module="test_poly1305",
        test_dir=SIM_DIR,
        build_dir=SIM_DIR / f"sim_build_poly{tag}",
    )
    return failed_tests()


# A cocotb run only ever sees the ROWS_PER_CYCLE its build was elaborated
# with, and the parameter reshapes the multiply stage rather than sizing
# it: MCYC accumulation cycles, the rotation of the row accumulator by
# ROWS_PER_CYCLE per cycle and the CSH offset S_C1 reads through are all
# derived from it. 5 is the end of the range oca/README.md documents
# (5 cycles and 25 multiply operators against 9 and 5), and it is the
# degenerate one: MCYC collapses to 1, so a single cycle covers every row
# and CSH is 0, which is the arithmetic the default -- MCYC 5, CSH 1 --
# never reaches.
DEFAULT_ROWS = 1
OTHER_ROWS = 5


def main() -> int:
    failed = run_at(DEFAULT_ROWS, "")
    failed += run_at(OTHER_ROWS, f"_r{OTHER_ROWS}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
