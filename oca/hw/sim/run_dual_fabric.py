# SPDX-License-Identifier: MIT
"""Run the dual-fabric cocotb tests under the project-local Verilator.

The DUT is oca_dual_harness.sv (this directory): oca_dispatch, two
oca_core, oca_collect, wired as oca_uart_crypto_dual wires them. Exit
code is the contract: runner.test() only inspects results.xml under
pytest and Verilator exits 0 on $finish however the assertions went,
so the count is read back here.

MUTATION RUNS. Every property in test_dual_fabric.py was proved able
to fail against a scratchpad copy of the sources:

    --src-override DIR   for each source file, use DIR's copy when one
                         exists there (the mutated file), the tree's
                         otherwise; builds into a separate build dir so
                         the pristine build is never clobbered
    --divergent          set OCA_DUAL_DIVERGENT=1 for the simulator, so
                         test_divergent_broadcast_fails_closed runs
                         (skipped otherwise: it MUST fail on RTL whose
                         cores cannot diverge)
    --test NAME          run only that testcase

The mutations that were run, one line each, and what went red:
  oca_dispatch.sv  OP_LOAD_KEY 8'h01 -> 8'hFE (load_key routed, not
                   broadcast)         -> broadcast test red
  oca_collect.sv   bcast_bit -> 1'b0 (both broadcast responses
                   forwarded)         -> broadcast test red (two answers)
  oca_collect.sv   C_PASS releases per beat, not at tlast
                                      -> non-interleaving test red
  harness copy     u_core1 NUM_SLOTS=4 (one keystore smaller: the
                   divergence injection) -> divergence test GREEN there,
                   red on the real RTL
  + oca_collect.sv diverged -> 1'b0   -> divergence test red (trouble)
  + oca_collect.sv merged_status -> status0 (fail open)
                                      -> divergence test red (status)
  oca_dispatch.sv  can1 -> 1'b0 (everything on core 0)
                                      -> overlap test red
  oca_dispatch.sv  D_IDLE asserts s_tready while both cores refuse
                   (drop instead of stall) -> stall test red
"""

import argparse
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
from cocotb_tools.check_results import get_results  # noqa: E402

SIM_DIR = Path(__file__).resolve().parent
RTL = ROOT / "oca" / "hw" / "rtl"

RTL_SOURCES = [
    "chacha20.sv", "poly1305.sv", "chacha20_poly1305.sv",
    "oca_keystore.sv", "oca_pktbuf.sv", "oca_proto.sv", "oca_core.sv",
    "oca_fifo.sv", "oca_dispatch.sv", "oca_collect.sv",
]
HARNESS = "oca_dual_harness.sv"


def resolve_sources(override: Path | None) -> list:
    """The tree's sources, each shadowed by the override dir's copy."""
    sources = []
    for name in RTL_SOURCES + [HARNESS]:
        default = SIM_DIR / name if name == HARNESS else RTL / name
        picked = default
        if override is not None and (override / name).is_file():
            picked = override / name
        sources.append(picked)
    return sources


def failed_tests() -> int:
    """Red tests in the run that just finished; no tests at all is an
    error, not a pass."""
    num_tests, num_failed = get_results(SIM_DIR / "results.xml")
    if num_tests == 0:
        raise RuntimeError("results.xml records no tests")
    return num_failed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src-override", type=Path, default=None,
                    help="directory whose files shadow the tree's sources")
    ap.add_argument("--divergent", action="store_true",
                    help="run the divergence test (needs a divergent copy)")
    ap.add_argument("--test", default=None,
                    help="run only this testcase")
    args = ap.parse_args()

    sources = resolve_sources(args.src_override)
    shadowed = [s.name for s in sources if args.src_override is not None
                and s.parent == args.src_override]
    if args.src_override is not None:
        if not shadowed:
            print(f"src-override {args.src_override} shadows no source",
                  file=sys.stderr)
            return 2
        print(f"# shadowed by {args.src_override}: {', '.join(shadowed)}")

    build_dir = SIM_DIR / ("sim_build_dual_fabric_mut" if shadowed
                           else "sim_build_dual_fabric")
    runner = get_runner("verilator")
    runner.build(
        sources=sources,
        hdl_toplevel="oca_dual_harness",
        build_dir=build_dir,
        always=True,
    )
    (SIM_DIR / "results.xml").unlink(missing_ok=True)  # never grade a stale file
    # Set both ways: an exported stale value must not un-skip the
    # divergence test on RTL it is guaranteed to fail on.
    extra_env = {"OCA_DUAL_DIVERGENT": "1" if args.divergent else "0"}
    runner.test(
        hdl_toplevel="oca_dual_harness",
        test_module="test_dual_fabric",
        testcase=args.test,
        test_dir=SIM_DIR,
        build_dir=build_dir,
        extra_env=extra_env,
    )
    return 1 if failed_tests() else 0


if __name__ == "__main__":
    sys.exit(main())
