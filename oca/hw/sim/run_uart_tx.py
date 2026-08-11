# SPDX-License-Identifier: MIT
"""Run the UART transmitter tests under the project-local Verilator.

The MSG parameter is overridden to "PIN=J17\\n" at build time, which is
what oca_uart_probe's first instance carries. The second instance differs
only in that constant, so one elaboration covers both: what could break
between them is the parameter plumbing, and that is a synthesis-time
question the netlist census answers, not a simulation one.

Exit code is the contract. runner.test() only inspects results.xml under
pytest and Verilator exits 0 on $finish however the assertions went, so
the count is read back here.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
VERILATOR_BIN = ROOT / "tools" / "verilator" / "bin"
RTL = ROOT / "oca" / "hw" / "rtl"
SOURCES = [RTL / "oca_uart_tx.sv"]

os.environ["PATH"] = str(VERILATOR_BIN) + os.pathsep + os.environ["PATH"]

try:
    from cocotb.runner import get_runner  # cocotb 1.x  # noqa: E402
except ModuleNotFoundError:
    from cocotb_tools.runner import get_runner  # cocotb 2.x  # noqa: E402
from cocotb_tools.check_results import get_results  # noqa: E402

SIM_DIR = Path(__file__).resolve().parent

# "PIN=J17\n", the payload oca_uart_probe's J17 instance carries.
#
# A SIZED SystemVerilog LITERAL, not a Python int. Passed as an int the
# runner writes -GMSG=5786930903936473354 and Verilator takes it as 32
# bits wide, silently keeping the low half: the transmitter then sends
# "J17\n" preceded by four zero bytes and the testbench catches it. On
# the board there is no testbench, so this would have named the wrong
# pin with a straight face.
MSG_J17 = "64'h50494E3D4A31370A"


def failed_tests() -> int:
    num_tests, num_failed = get_results(SIM_DIR / "results.xml")
    if num_tests == 0:
        raise RuntimeError("results.xml records no tests")
    return num_failed


def main() -> int:
    runner = get_runner("verilator")
    runner.build(
        sources=SOURCES,
        hdl_toplevel="oca_uart_tx",
        parameters={"MSG": MSG_J17},
        build_dir=SIM_DIR / "sim_build_uart_tx",
        always=True,
    )
    (SIM_DIR / "results.xml").unlink(missing_ok=True)  # never grade a stale file
    runner.test(
        hdl_toplevel="oca_uart_tx",
        test_module="test_uart_tx",
        test_dir=SIM_DIR,
        build_dir=SIM_DIR / "sim_build_uart_tx",
    )
    return 1 if failed_tests() else 0


if __name__ == "__main__":
    sys.exit(main())
