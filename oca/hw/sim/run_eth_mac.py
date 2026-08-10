# SPDX-License-Identifier: MIT
"""Run the 1G MAC cocotb tests under the project-local Verilator.

The device under test is oca_eth_mac_1g_fifo_64, the parameter-fixing
wrapper, and not eth_mac_1g_fifo itself. The wrapper is what the board
instantiates, so it is what the tests have to agree with: its parameters
decide whether a frame the MAC marks bad is dropped or handed on, and a
testbench that elaborated the vendor module with vendor defaults would be
checking a different design from the one that ships.

No generated harness. The wrapper takes no parameters and every port
width is a literal, which is exactly the property it was written for, so
cocotb can take it as the one toplevel directly.

The lint gate is scoped, and says so rather than passing quietly. The
vendor tree is not -Wall clean -- 87 findings at the time of writing, all
of them PINMISSING and PINCONNECTEMPTY on PTP and tid/tdest ports nobody
here connects -- and it is pinned upstream, so it cannot be made clean
without editing it. What the gate refuses is a finding in *our* file, and
it prints the vendor count it let through, because a filter that subtracts
information in silence reads as a clean run.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
VERILATOR_BIN = ROOT / "tools" / "verilator" / "bin"
RTL = ROOT / "oca" / "hw" / "rtl"
sys.path.insert(0, str(ROOT / "oca" / "hw" / "vendor"))
import vendor_patches  # noqa: E402

# The patched vendor tree, never the pinned submodule: unpatched, the
# receive path delivers tkeep = 0 on every beat and this suite would be
# testing a MAC the board will never contain.
#
# The override points it at a copy, which is how a mutation is proved
# without writing to the submodule:
#   OCA_ETH_MAC_VENDOR=/path/to/a/copy .venv/bin/python hw/sim/run_eth_mac.py
_override = os.environ.get("OCA_ETH_MAC_VENDOR")
VENDOR = Path(_override) if _override else vendor_patches.PATCHED
vendor_patches.require(VENDOR)

os.environ["PATH"] = str(VERILATOR_BIN) + os.pathsep + os.environ["PATH"]

try:
    from cocotb.runner import get_runner  # cocotb 1.x  # noqa: E402
except ModuleNotFoundError:
    from cocotb_tools.runner import get_runner  # cocotb 2.x  # noqa: E402
from cocotb_tools.check_results import get_results  # noqa: E402

SIM_DIR = Path(__file__).resolve().parent
BUILD = SIM_DIR / "sim_build_eth_mac"
TOPLEVEL = "oca_eth_mac_1g_fifo_64"

# The same list run_synth.py builds the oca_top_mac target from, in the same
# order, so a file the MAC needs cannot be present for synthesis and missing
# here. lfsr.v is the CRC-32 the receive FCS check is built on and the reason
# axis_gmii_rx cannot be simulated on its own.
SOURCES = [
    VENDOR / "lib" / "axis" / "rtl" / "axis_fifo.v",
    VENDOR / "lib" / "axis" / "rtl" / "axis_adapter.v",
    VENDOR / "lib" / "axis" / "rtl" / "axis_async_fifo.v",
    VENDOR / "lib" / "axis" / "rtl" / "axis_async_fifo_adapter.v",
    VENDOR / "rtl" / "lfsr.v",
    VENDOR / "rtl" / "axis_gmii_rx.v",
    VENDOR / "rtl" / "axis_gmii_tx.v",
    VENDOR / "rtl" / "eth_mac_1g.v",
    VENDOR / "rtl" / "eth_mac_1g_fifo.v",
    RTL / "vendor" / "oca_eth_mac_1g_fifo_64.v",
]

# 1 ns / 1 ps, stated rather than inherited. Every vendor file carries
# `timescale 1ns / 1ps` and so does the wrapper, but the logic clock is
# 20.8 ns -- oca_clkrst's 48.0769 MHz -- and a build that fell back to 1 ns
# precision would round it to 21 and drift the two clock domains apart from
# the ones the board has, silently.
TIMESCALE = "1ns/1ps"

# %Warning-NAME: path:line:col: text, or %Error / %Error-NAME the same way.
# The path is optional: "%Error: Exiting due to N warning(s)" has none.
DIAGNOSTIC = re.compile(
    r"^%(?P<kind>Warning|Error)(?:-(?P<code>[A-Z0-9_]+))?: "
    r"(?:(?P<file>\S+?):\d+:\d+: )?")

# The one finding the wrapper cannot answer for, named rather than folded
# into the vendor count, because it is reported against our file.
#
# SYNCASYNCNET on rx_rst, tx_rst and logic_rst: each reset is used
# asynchronously by eth_mac_1g_fifo's status synchronisers (:174) and
# synchronously inside axis_gmii_rx (:335). Both usages are vendor code; the
# only thing our file contributes is the port the net is declared on, which
# is where Verilator hangs the report. Removing it means editing the pinned
# tree, and the mixed usage is what the wrapper's header already documents.
WAIVED = {"SYNCASYNCNET"}


def failed_tests() -> int:
    """Red tests in the run that just finished.

    runner.test() only inspects results.xml under pytest, and Verilator exits
    0 on $finish even with failing tests: without this check the process exits
    0 however the suite went, and anything driving these runners by exit code
    would call a red suite green.
    """
    num_tests, num_failed = get_results(SIM_DIR / "results.xml")
    if num_tests == 0:
        raise RuntimeError("results.xml records no tests")
    return num_failed


def lint() -> int:
    """-Wall over the whole elaborated MAC. Only our own file is fatal."""
    proc = subprocess.run(
        [str(VERILATOR_BIN / "verilator"), "--lint-only", "-Wall", "-Wno-fatal",
         "--timescale-override", TIMESCALE,
         *[str(src) for src in SOURCES], "--top-module", TOPLEVEL],
        capture_output=True, text=True)

    ours, theirs, waived = [], 0, 0
    for line in (proc.stdout + proc.stderr).splitlines():
        match = DIAGNOSTIC.match(line)
        if not match:
            continue
        path = match.group("file")
        if path is None:
            # Only the "Exiting due to N warning(s)" tail has no file, and
            # -Wno-fatal means it cannot appear. Anything else without one is
            # ours by default: an unattributable error is not a pass.
            if "Exiting due to" not in line:
                ours.append(line)
        elif "verilog-ethernet" in path:
            theirs += 1
        elif match.group("kind") == "Warning" and match.group("code") in WAIVED:
            waived += 1
        else:
            ours.append(line)

    if proc.returncode != 0 and not ours:
        # The design did not elaborate and no diagnostic carried the blame.
        print(proc.stdout + proc.stderr, flush=True)
        print(f"lint: verilator exited {proc.returncode}: FAILED", flush=True)
        return 1
    if ours:
        print("\n".join(ours), flush=True)
        print(f"lint: {len(ours)} finding(s) outside the vendor tree: FAILED",
              flush=True)
        return 1
    print(f"lint: ok — {theirs} finding(s) in oca/hw/vendor/verilog-ethernet "
          f"and {waived} waived {'/'.join(sorted(WAIVED))} on the wrapper's "
          "reset ports let through, nothing else in ours", flush=True)
    return 0


def main() -> int:
    rc = lint()
    if rc != 0:
        return rc

    runner = get_runner("verilator")
    runner.build(
        sources=SOURCES,
        hdl_toplevel=TOPLEVEL,
        build_dir=BUILD,
        # The vendor tree's findings are the lint gate's business, and it has
        # already had it; repeating 87 of them on every build would bury the
        # one line that matters.
        build_args=["-Wno-lint", "-Wno-style", "-Wno-fatal",
                    "--timescale-override", TIMESCALE],
        always=True,
    )
    (SIM_DIR / "results.xml").unlink(missing_ok=True)  # never grade a stale file
    runner.test(
        hdl_toplevel=TOPLEVEL,
        test_module="test_eth_mac",
        test_dir=SIM_DIR,
        build_dir=BUILD,
    )
    return 1 if failed_tests() else 0


if __name__ == "__main__":
    sys.exit(main())
