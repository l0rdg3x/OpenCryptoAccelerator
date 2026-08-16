# SPDX-License-Identifier: MIT
"""Run the board top's heartbeat tests under the project-local Verilator.

Exit code is the contract: runner.test() only inspects results.xml under
pytest and Verilator exits 0 on $finish however the assertions went, so
the count is read back here.

ONE BUILD, AND IT IS A SMALL ONE. LED_BITS is the width of the heartbeat
counter, and at the board's 25 a half-period is 0.671 s of simulated time
-- 16.8 million clocks -- which no run can watch. Every test in this file
is about D2, so a build at the default would be a build where the whole
suite skips. It builds at 8 instead, and nothing here holds the default:
that is a netlist census's job, run_synth.py's NETLIST_FF_FLOOR entry for
oca_crypto_pll.sv, the way oca_uart_crypto.sv's carried the counter
before it moved.

THE BUILD ALSO ELABORATES oca_uart_crypto AT 48_076_923 Hz, which no
other suite does: run_uart_crypto.py builds the core standalone at its
default 25 MHz. So the divisor guard in oca_uart_crypto.sv -- the one
that refuses a CLK_HZ whose stop-bit sample lands outside the budget --
is exercised here at the frequency the top says the board runs at.

THAT IS THE COPY AND NOT THE CLOCK, and the difference is the whole of
what this suite cannot see. 48_076_923 is oca_crypto_pll's CLK_SYS_HZ
parameter default, since 2026-08-16 guarded at elaboration against the
divider parameters it forwards to oca_clkrst -- a CLK_SYS_HZ overridden
apart from its dividers refuses to elaborate, which the old hand-copied
localparam could not do. What no guard here reaches is the CLOCK:
EHXPLLL has no body, so no simulation turns CLKOS_DIV into a frequency,
and a coherent override of the whole set -- a real 56.82 MHz clock
carrying its own matching divisor -- leaves all three tests green. On
the pre-parameter RTL that was measured, not assumed, by setting the
divider alone to 11. What compares the RTL's constant with the dividers
in the built netlist is hw/syn/run_synth.py's check_clk_sys_const,
before place & route.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
VERILATOR_BIN = ROOT / "tools" / "verilator" / "bin"
RTL = ROOT / "oca" / "hw" / "rtl"

# oca_clkrst and the PLL primitive ahead of run_uart_crypto.py's list,
# plus this top. It has to stay identical to run_synth.py's DESIGNS entry
# for oca_crypto_pll: a test that elaborates a different set of files from
# the one the bitstream is built from is a test of a design nobody loads.
SOURCES = [RTL / f for f in (
    "ecp5_prims.sv", "oca_clkrst.sv",
    "oca_uart_rx.sv", "oca_uart_tx8.sv", "oca_fifo.sv",
    "oca_slip_rx.sv", "oca_slip_tx.sv",
    "chacha20.sv", "poly1305.sv", "chacha20_poly1305.sv",
    "oca_keystore.sv", "oca_pktbuf.sv", "oca_proto.sv",
    "oca_core.sv", "oca_uart_crypto.sv", "oca_crypto_pll.sv")]

os.environ["PATH"] = str(VERILATOR_BIN) + os.pathsep + os.environ["PATH"]

try:
    from cocotb.runner import get_runner  # cocotb 1.x  # noqa: E402
except ModuleNotFoundError:
    from cocotb_tools.runner import get_runner  # cocotb 2.x  # noqa: E402
from cocotb_tools.check_results import get_results  # noqa: E402

SIM_DIR = Path(__file__).resolve().parent

# Small enough that both rates fit in a run, and the same value the
# heartbeat pair used while it lived in run_uart_crypto.py. The module's
# own guard refuses anything under 5.
LED_BITS = 8


def failed_tests() -> int:
    num_tests, num_failed = get_results(SIM_DIR / "results.xml")
    if num_tests == 0:
        raise RuntimeError("results.xml records no tests")
    return num_failed


def main() -> int:
    # The testbench has to agree with the parameter this build elaborated,
    # and it cannot read it back: a default in the testbench would be a
    # second number free to disagree with this one, so it reads this.
    os.environ["OCA_LED_BITS"] = str(LED_BITS)
    build_dir = SIM_DIR / "sim_build_crypto_pll"
    runner = get_runner("verilator")
    runner.build(
        sources=SOURCES,
        hdl_toplevel="oca_crypto_pll",
        build_dir=build_dir,
        parameters={"LED_BITS": LED_BITS},
        always=True,
    )
    (SIM_DIR / "results.xml").unlink(missing_ok=True)  # never grade a stale file
    runner.test(
        hdl_toplevel="oca_crypto_pll",
        test_module="test_crypto_pll",
        test_dir=SIM_DIR,
        build_dir=build_dir,
    )
    return 1 if failed_tests() else 0


if __name__ == "__main__":
    sys.exit(main())
