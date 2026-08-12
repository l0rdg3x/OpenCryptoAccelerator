# SPDX-License-Identifier: MIT
"""Run the SLIP decoder tests under the project-local Verilator.

Exit code is the contract: runner.test() only inspects results.xml under
pytest and Verilator exits 0 on $finish however the assertions went, so
the count is read back here.

A cocotb run only ever sees the BYTES its build was elaborated with, so
the suite runs a second time at the smallest legal size. That is not a
second chance at the same defect: BYTES sets the width of the byte
counter, the width of the word address and the length of the clear walk,
and what only the small run can catch is a bound that happens to work
out at 256 words and not at 2.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
VERILATOR_BIN = ROOT / "tools" / "verilator" / "bin"
RTL = ROOT / "oca" / "hw" / "rtl" / "oca_slip_rx.sv"

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


# BYTES the module implements and BYTES it must refuse, for the reasons
# oca_pktbuf refuses the same set: a BYTES that is not eight times a
# power of two puts the word address off the end of the array, and above
# 2048 it stops being the size oca_core's buffer can hold.
LEGAL_BYTES = [16, 64, 1024, 2048]
ILLEGAL_BYTES = [8, 1536, 2044, 4096]
SMALL_BYTES = 64


def elaborates(params: list) -> bool:
    proc = subprocess.run(
        [VERILATOR_BIN / "verilator", "--lint-only", "-Wall", *params,
         str(RTL), "--top-module", "oca_slip_rx"],
        capture_output=True, text=True)
    return proc.returncode == 0


def check_parameter_guards() -> int:
    rc = 0
    for nbytes in LEGAL_BYTES:
        if not elaborates([f"-GBYTES={nbytes}"]):
            print(f"BYTES={nbytes} is legal but does not elaborate", flush=True)
            rc = 1
    for nbytes in ILLEGAL_BYTES:
        if elaborates([f"-GBYTES={nbytes}"]):
            print(f"BYTES={nbytes} elaborated: the word address runs off the "
                  f"array and frames are decoded over words never written",
                  flush=True)
            rc = 1
    # MIN_BYTES above BYTES refuses every frame the buffer can hold, in
    # silence: the counter moves and no request is ever answered.
    if elaborates(["-GBYTES=64", "-GMIN_BYTES=65"]):
        print("MIN_BYTES > BYTES elaborated: no frame can ever be delivered",
              flush=True)
        rc = 1
    if elaborates(["-GMIN_BYTES=0"]):
        print("MIN_BYTES=0 elaborated: an empty frame becomes a request",
              flush=True)
        rc = 1
    print(f"parameter guards: {len(LEGAL_BYTES)} legal accepted, "
          f"{len(ILLEGAL_BYTES) + 2} illegal refused — "
          f"{'ok' if rc == 0 else 'FAILED'}", flush=True)
    return rc


def run_at(nbytes: int, tag: str) -> int:
    os.environ["OCA_SLIP_BYTES"] = str(nbytes)
    runner = get_runner("verilator")
    runner.build(
        sources=[RTL],
        hdl_toplevel="oca_slip_rx",
        build_dir=SIM_DIR / f"sim_build_slip_rx{tag}",
        parameters={"BYTES": nbytes},
        always=True,
    )
    (SIM_DIR / "results.xml").unlink(missing_ok=True)  # never grade a stale file
    runner.test(
        hdl_toplevel="oca_slip_rx",
        test_module="test_slip_rx",
        test_dir=SIM_DIR,
        build_dir=SIM_DIR / f"sim_build_slip_rx{tag}",
    )
    return failed_tests()


def main() -> int:
    rc = check_parameter_guards()
    if rc != 0:
        return rc

    failed = run_at(2048, "")
    failed += run_at(SMALL_BYTES, "_small")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
