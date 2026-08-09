# SPDX-License-Identifier: MIT
"""Run the ChaCha20 cocotb tests under the project-local Verilator."""

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


def run_at(rounds: int, tag: str) -> int:
    print(f"=== chacha20 at ROUNDS_PER_CYCLE={rounds}", flush=True)
    runner = get_runner("verilator")
    runner.build(
        sources=[ROOT / "oca" / "hw" / "rtl" / "chacha20.sv"],
        hdl_toplevel="chacha20",
        build_dir=SIM_DIR / f"sim_build{tag}",
        parameters={"ROUNDS_PER_CYCLE": rounds},
        always=True,
    )
    (SIM_DIR / "results.xml").unlink(missing_ok=True)  # never grade a stale file
    runner.test(
        hdl_toplevel="chacha20",
        test_module="test_chacha20",
        test_dir=SIM_DIR,
        build_dir=SIM_DIR / f"sim_build{tag}",
    )
    return failed_tests()


# A cocotb run only ever sees the ROUNDS_PER_CYCLE its build was
# elaborated with, and the parameter selects the datapath rather than a
# capacity: at 1 the state register alternates between the plain and the
# diagonalised frame and the round counter picks the direction, at 2 one
# cycle composes both and the counter never selects anything. The whole
# keystream at 2 therefore comes out of logic the default run never
# evaluates. The set is closed at these two values -- chacha20.sv
# $fatal()s on anything else, verified: -GROUNDS_PER_CYCLE=4 fails to
# elaborate even though 4 divides the 20 rounds.
DEFAULT_ROUNDS = 1
OTHER_ROUNDS = 2


def main() -> int:
    failed = run_at(DEFAULT_ROUNDS, "")
    failed += run_at(OTHER_ROUNDS, f"_r{OTHER_ROUNDS}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
