# SPDX-License-Identifier: MIT
"""Run the crypto-console end-to-end tests under the project-local Verilator.

Exit code is the contract: runner.test() only inspects results.xml under
pytest and Verilator exits 0 on $finish however the assertions went, so
the count is read back here.

TWICE, AT TWO LED_BITS, and the second run is not a second chance at the
same defect. LED_BITS is the width of the heartbeat counter, and at the
board's 25 a half-period is 0.671 s of simulated time -- 16.8 million
clocks -- which no run can watch. So the suite runs once as the bitstream
is built, where the two heartbeat tests are skipped and everything else
is exercised on exactly the design that ships, and once at 8, where the
heartbeat tests run and the rest runs again against a parameter it does
not touch. Neither run alone covers the module: the default is the one
the board gets, the small one is the only one that can see D2.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
VERILATOR_BIN = ROOT / "tools" / "verilator" / "bin"
RTL = ROOT / "oca" / "hw" / "rtl"

# The same list as run_synth.py's DESIGNS entry, and it has to stay that
# way: a test that elaborates a different set of files from the one the
# bitstream is built from is a test of a design nobody loads.
SOURCES = [RTL / f for f in (
    "oca_uart_rx.sv", "oca_uart_tx8.sv", "oca_fifo.sv",
    "oca_slip_rx.sv", "oca_slip_tx.sv",
    "chacha20.sv", "poly1305.sv", "chacha20_poly1305.sv",
    "oca_keystore.sv", "oca_pktbuf.sv", "oca_proto.sv",
    "oca_core.sv", "oca_uart_crypto.sv")]

os.environ["PATH"] = str(VERILATOR_BIN) + os.pathsep + os.environ["PATH"]

try:
    from cocotb.runner import get_runner  # cocotb 1.x  # noqa: E402
except ModuleNotFoundError:
    from cocotb_tools.runner import get_runner  # cocotb 2.x  # noqa: E402
from cocotb_tools.check_results import get_results  # noqa: E402

SIM_DIR = Path(__file__).resolve().parent

DEFAULT_LED_BITS = 25
SMALL_LED_BITS = 8


def failed_tests() -> int:
    num_tests, num_failed = get_results(SIM_DIR / "results.xml")
    if num_tests == 0:
        raise RuntimeError("results.xml records no tests")
    return num_failed


def run_at(led_bits: int, tag: str) -> int:
    os.environ["OCA_LED_BITS"] = str(led_bits)
    build_dir = SIM_DIR / f"sim_build_uart_crypto{tag}"
    runner = get_runner("verilator")
    runner.build(
        sources=SOURCES,
        hdl_toplevel="oca_uart_crypto",
        build_dir=build_dir,
        parameters={"LED_BITS": led_bits},
        always=True,
    )
    (SIM_DIR / "results.xml").unlink(missing_ok=True)  # never grade a stale file
    runner.test(
        hdl_toplevel="oca_uart_crypto",
        test_module="test_uart_crypto",
        test_dir=SIM_DIR,
        build_dir=build_dir,
    )
    return failed_tests()


def main() -> int:
    failed = run_at(DEFAULT_LED_BITS, "")
    failed += run_at(SMALL_LED_BITS, "_led")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
