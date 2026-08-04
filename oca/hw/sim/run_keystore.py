# SPDX-License-Identifier: MIT
"""Run the key store cocotb tests under the project-local Verilator."""

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

SIM_DIR = Path(__file__).resolve().parent


def main() -> int:
    runner = get_runner("verilator")
    runner.build(
        sources=[ROOT / "oca" / "hw" / "rtl" / "oca_keystore.sv"],
        hdl_toplevel="oca_keystore",
        build_dir=SIM_DIR / "sim_build_keystore",
        always=True,
    )
    runner.test(
        hdl_toplevel="oca_keystore",
        test_module="test_keystore",
        test_dir=SIM_DIR,
        build_dir=SIM_DIR / "sim_build_keystore",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
