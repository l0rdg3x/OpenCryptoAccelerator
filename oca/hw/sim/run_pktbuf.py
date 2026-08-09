# SPDX-License-Identifier: MIT
"""Run the packet buffer cocotb tests under the project-local Verilator.

The cocotb tests can only ever exercise the BYTES the build was
elaborated with, so the parameter check below runs first, outside the
simulator: a BYTES the module cannot implement must fail to elaborate
rather than corrupt the upper bank in silence.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
VERILATOR_BIN = ROOT / "tools" / "verilator" / "bin"
RTL = ROOT / "oca" / "hw" / "rtl" / "oca_pktbuf.sv"

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

# BYTES the module implements, and BYTES it must refuse. 1536 is the one
# that matters: eight times 192 is not a power of two, so the bank base
# 2**ADDR_W puts half of bank 1 past the end of a 2*WORDS array. Writes
# there are dropped and reads come back empty, and oca_core answers status
# 00 over the result — measured before the guard, three of six back-to-back
# 1392-byte seals came back with the right length and the wrong bytes. 4096
# truncates 12'(BYTES) to zero and jams both full flags high; 2044 is not a
# whole number of words.
LEGAL_BYTES = [16, 1024, 2048]
ILLEGAL_BYTES = [8, 1536, 2044, 4096, 8192]


def elaborates(nbytes: int) -> bool:
    """Elaborate oca_pktbuf alone at BYTES=nbytes. True if it builds."""
    proc = subprocess.run(
        [VERILATOR_BIN / "verilator", "--lint-only", "-Wall",
         f"-GBYTES={nbytes}", str(RTL), "--top-module", "oca_pktbuf"],
        capture_output=True, text=True)
    return proc.returncode == 0


def check_bytes_guard() -> int:
    rc = 0
    for nbytes in LEGAL_BYTES:
        if not elaborates(nbytes):
            print(f"BYTES={nbytes} is legal but does not elaborate", flush=True)
            rc = 1
    for nbytes in ILLEGAL_BYTES:
        if elaborates(nbytes):
            print(f"BYTES={nbytes} elaborated: the guard is gone and the "
                  "buffer answers over memory it never wrote", flush=True)
            rc = 1
    print(f"BYTES guard: {len(LEGAL_BYTES)} legal accepted, "
          f"{len(ILLEGAL_BYTES)} illegal refused — "
          f"{'ok' if rc == 0 else 'FAILED'}", flush=True)
    return rc


# A cocotb run only ever sees the BYTES its build was elaborated with, so
# every assertion above this line speaks for BYTES=2048 alone. The clear
# derives its counter width from the parameter — CLR_W = ADDR_W + 1, and
# it stops on the all-ones address — and the tests for it therefore run
# again at the smallest legal size, where ADDR_W is 1 and the whole array
# is four words. This is parameter coverage, not a second chance at the
# same defect: reading every word back would catch a missed one at either
# size. What only the second run can catch is a width or a bound that
# happens to work out at 256 words and not at 2.
CLEAR_TESTS = [
    "test_reset_zeroises_both_banks",
    "test_clear_busy_spans_one_cycle_per_word",
    "test_writes_during_the_clear_do_not_land",
]
SMALL_BYTES = 16


def run_at(nbytes: int, tag: str, testcase=None) -> int:
    os.environ["OCA_PKTBUF_BYTES"] = str(nbytes)
    runner = get_runner("verilator")
    runner.build(
        sources=[ROOT / "oca" / "hw" / "rtl" / "oca_pktbuf.sv"],
        hdl_toplevel="oca_pktbuf",
        build_dir=SIM_DIR / f"sim_build_pktbuf{tag}",
        parameters={"BYTES": nbytes},
        always=True,
    )
    (SIM_DIR / "results.xml").unlink(missing_ok=True)  # never grade a stale file
    runner.test(
        hdl_toplevel="oca_pktbuf",
        test_module="test_pktbuf",
        test_dir=SIM_DIR,
        build_dir=SIM_DIR / f"sim_build_pktbuf{tag}",
        testcase=testcase,
    )
    return failed_tests()


def main() -> int:
    rc = check_bytes_guard()
    if rc != 0:
        return rc

    failed = run_at(2048, "")
    failed += run_at(SMALL_BYTES, "_small", testcase=CLEAR_TESTS)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
