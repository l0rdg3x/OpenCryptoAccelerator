# SPDX-License-Identifier: MIT
"""Replay the tag comparison on a SYNTHESISED oca_proto, inside oca_core.

run_keystore_gate.py does this for oca_keystore, which is where the
cmp2lut defect happened to land. oca_proto is 1241 lines and holds the
comparison that decides whether plaintext leaves, and until this runner
existed nothing between the RTL simulation and the board looked at it: a
mapper defect there would build, pass all 73 RTL tests, pass the
flip-flop floors in hw/syn/run_synth.py — a 128-bit equality is
combinational and no cell census can see it — and answer wrongly on
hardware.

oca_proto is mapped to ECP5 primitives and instantiated in place of the
RTL module; oca_core.sv and the other five sources are the RTL, so the
packets on the wire and the cryptography behind them are the ones the
usual suites use. Verilator takes the netlist's `oca_proto` because the
RTL one is not in the source list.

Why not the whole core: yosys ships no simulation model for MULT18X18D
or PDPW16KD, only blackbox declarations, so a netlist containing
poly1305's multipliers or the packet buffers' block RAM cannot be
simulated at all (Verilator: "Cannot find file containing module
'MULT18X18D'"). oca_proto infers neither, which is exactly why it can be
replayed like this and the core around it cannot.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
VERILATOR_BIN = ROOT / "tools" / "verilator" / "bin"
YOSYS = ROOT / "tools" / "yosys" / "bin" / "yosys"
ECP5_SIM = ROOT / "tools" / "yosys" / "share" / "yosys" / "ecp5"
RTL = ROOT / "oca" / "hw" / "rtl"

os.environ["PATH"] = str(VERILATOR_BIN) + os.pathsep + os.environ["PATH"]

try:
    from cocotb.runner import get_runner  # cocotb 1.x  # noqa: E402
except ModuleNotFoundError:
    from cocotb_tools.runner import get_runner  # cocotb 2.x  # noqa: E402

SIM_DIR = Path(__file__).resolve().parent
BUILD = SIM_DIR / "sim_build_proto_gate"

# Everything except oca_proto.sv, which arrives as cells.
RTL_SOURCES = ["chacha20.sv", "poly1305.sv", "chacha20_poly1305.sv",
               "oca_keystore.sv", "oca_pktbuf.sv", "oca_core.sv"]


def synth(netlist: Path) -> None:
    """Map oca_proto to ECP5 primitives, the same way run_synth.py does."""
    rtl = RTL / "oca_proto.sv"
    script = (f"read_slang --top oca_proto {rtl}; "
              f"synth_ecp5 -top oca_proto; "
              f"write_verilog -noattr {netlist}")
    subprocess.run([YOSYS, "-q", "-p", script], check=True)

    # Synthesis resolves parameters, so the netlist module takes none and
    # oca_core.sv's `oca_proto #(.NUM_SLOTS, .BYTES)` has nothing to bind
    # to. Declare them again, unused: the netlist is already elaborated at
    # the values oca_core passes, since both take them from the same RTL
    # defaults. A netlist built at other values would fail these tests
    # rather than pass them quietly -- it would answer 04 to slot 3.
    text = netlist.read_text()
    header = "module oca_proto("
    assert text.count(header) == 1, "netlist module header not found"
    netlist.write_text(text.replace(
        header,
        "module oca_proto #(parameter NUM_SLOTS = 8, parameter BYTES = 2048)(",
        1))


def main() -> int:
    BUILD.mkdir(parents=True, exist_ok=True)
    netlist = BUILD / "oca_proto_ecp5.v"

    t0 = time.monotonic()
    synth(netlist)
    t1 = time.monotonic()

    runner = get_runner("verilator")
    runner.build(
        # The vendor primitive models are not lint-clean and carry
        # several top modules; neither is our problem here.
        verilog_sources=[ECP5_SIM / "cells_sim.v", netlist],
        sources=[RTL / s for s in RTL_SOURCES],
        hdl_toplevel="oca_core",
        build_dir=BUILD,
        # PINNOTFOUND: synthesis resolves NUM_SLOTS and BYTES, so the
        # netlist module takes no parameters and oca_core.sv's override
        # has nothing to bind to. Both were synthesised at the defaults
        # oca_core passes, so ignoring the override is what is wanted --
        # and a netlist built at other values would fail these tests.
        build_args=["--timing", f"-I{ECP5_SIM}", "-Wno-PINNOTFOUND",
                    "-Wno-lint", "-Wno-style", "-Wno-MULTITOP", "-Wno-fatal"],
        always=True,
    )
    t2 = time.monotonic()
    runner.test(
        hdl_toplevel="oca_core",
        # Netlist plus RTL is a mixed-language build as far as the runner
        # is concerned, and it will not guess which side the top is on.
        hdl_toplevel_lang="verilog",
        test_module="test_proto_gate",
        test_dir=SIM_DIR,
        build_dir=BUILD,
    )
    t3 = time.monotonic()
    print(f"gate-level oca_proto: yosys {t1 - t0:.0f} s, "
          f"verilator build {t2 - t1:.0f} s, tests {t3 - t2:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
