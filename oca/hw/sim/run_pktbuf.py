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

SIM_DIR = Path(__file__).resolve().parent

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


def main() -> int:
    rc = check_bytes_guard()
    if rc != 0:
        return rc

    runner = get_runner("verilator")
    runner.build(
        sources=[ROOT / "oca" / "hw" / "rtl" / "oca_pktbuf.sv"],
        hdl_toplevel="oca_pktbuf",
        build_dir=SIM_DIR / "sim_build_pktbuf",
        always=True,
    )
    runner.test(
        hdl_toplevel="oca_pktbuf",
        test_module="test_pktbuf",
        test_dir=SIM_DIR,
        build_dir=SIM_DIR / "sim_build_pktbuf",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
