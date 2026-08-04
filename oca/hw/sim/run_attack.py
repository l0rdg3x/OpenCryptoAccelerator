# SPDX-License-Identifier: MIT
"""Run the adversarial oca_core cocotb tests under the project-local Verilator.

These are the tests written to break the four-stage overlap rather than
to confirm it: the descriptor hand-off, engine ownership, ordering
across every failure class, and the timing residuals. They share the
packet plumbing and the reference cryptography with test_oca_core.py and
nothing else.
"""

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
RTL = ROOT / "oca" / "hw" / "rtl"


def main() -> int:
    runner = get_runner("verilator")
    runner.build(
        sources=[
            RTL / "chacha20.sv",
            RTL / "poly1305.sv",
            RTL / "chacha20_poly1305.sv",
            RTL / "oca_keystore.sv",
            RTL / "oca_pktbuf.sv",
            RTL / "oca_proto.sv",
            RTL / "oca_core.sv",
        ],
        hdl_toplevel="oca_core",
        build_dir=SIM_DIR / "sim_build_attack",
        always=True,
    )
    runner.test(
        hdl_toplevel="oca_core",
        test_module="test_attack",
        test_dir=SIM_DIR,
        build_dir=SIM_DIR / "sim_build_attack",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
