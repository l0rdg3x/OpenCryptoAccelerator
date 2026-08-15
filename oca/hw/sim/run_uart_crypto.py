# SPDX-License-Identifier: MIT
"""Run the crypto-console end-to-end tests under the project-local Verilator.

Exit code is the contract: runner.test() only inspects results.xml under
pytest and Verilator exits 0 on $finish however the assertions went, so
the count is read back here.

ONE BUILD, AT THE DEFAULT PARAMETERS. It ran twice at two LED_BITS until
the heartbeat left this module for oca_crypto_pll: the counter was the
only thing here that could not be simulated at the width the board gets,
and the two tests that watched it are hw/sim/test_crypto_pll.py's now.
What is left elaborates at CLK_HZ's default, 25_000_000, which is the
frequency the standalone build of this module runs at and the one every
bit time in test_uart_crypto.py is counted from.

THE PLL BUILD IS NOT COVERED HERE. oca_crypto_pll hands this module
48.0769 MHz, where the divisor is 417 rather than 217; run_crypto_pll.py
elaborates that combination and so exercises the sampler-error guard at
it, but nothing shifts a byte through the module at that frequency in
any suite. The bit timing is proved at 25 MHz and carried across by the
guard, which is an argument and not a measurement.
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


def failed_tests() -> int:
    num_tests, num_failed = get_results(SIM_DIR / "results.xml")
    if num_tests == 0:
        raise RuntimeError("results.xml records no tests")
    return num_failed


def main() -> int:
    build_dir = SIM_DIR / "sim_build_uart_crypto"
    runner = get_runner("verilator")
    runner.build(
        sources=SOURCES,
        hdl_toplevel="oca_uart_crypto",
        build_dir=build_dir,
        always=True,
    )
    (SIM_DIR / "results.xml").unlink(missing_ok=True)  # never grade a stale file
    runner.test(
        hdl_toplevel="oca_uart_crypto",
        test_module="test_uart_crypto",
        test_dir=SIM_DIR,
        build_dir=build_dir,
    )
    return 1 if failed_tests() else 0


if __name__ == "__main__":
    sys.exit(main())
