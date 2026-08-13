# SPDX-License-Identifier: MIT
"""Self-consistency checks for run_synth's nextpnr argv construction, run
without any RTL or toolchain.

nextpnr rejects --textcfg together with --out-of-context ("bitstream
generation is not available in out-of-context mode"), and it fails late,
after place & route has already run and logged Fmax, so the report and
the routed netlist never get written. b9f68ea added --textcfg
unconditionally and broke every out-of-context target this way; these
checks pin the two argv shapes so that regression is caught here rather
than at the end of a 30-minute place & route.
"""

from argparse import Namespace

import run_synth
from run_synth import DESIGNS, pnr_command


def _args(**overrides):
    base = dict(device="45k", package="CABGA381", speed=6, freq=100.0,
                seed=1, pnr_arg=[])
    base.update(overrides)
    return Namespace(**base)


def test_out_of_context_design_gets_no_textcfg_or_lpf():
    cmd = pnr_command("oca_core", "netlist.json", _args(), "report.json")
    assert "--out-of-context" in cmd
    assert "--textcfg" not in cmd
    assert "--lpf" not in cmd


def test_pinned_design_gets_textcfg_and_lpf_not_out_of_context():
    cmd = pnr_command("oca_uart_console", "netlist.json", _args(),
                       "report.json")
    assert "--textcfg" in cmd
    assert "--lpf" in cmd
    assert "--out-of-context" not in cmd


def test_design_list_default_does_not_leak_across_entries():
    """Design's list-typed fields (verilog, incdirs, pnr_args) default to
    a literal in the class body; a NamedTuple stores that one object, so
    every DESIGNS entry that takes the default shares the SAME list. No
    call site mutates one today, but `d.pnr_args.append(...)` is the
    natural way to add a per-design nextpnr flag, and it would silently
    apply to every design that never asked for it.

    The fix turns the defaults into tuples, so the in-place mutation this
    test attempts raises AttributeError instead of succeeding and
    leaking: refusing the mutation is the fix, sharing it silently is the
    defect, so an AttributeError here counts as a pass.
    """
    victim, other = sorted(DESIGNS)[:2]
    assert DESIGNS[victim].pnr_args is DESIGNS[other].pnr_args, (
        "test assumption broken: these two entries no longer share a "
        "default pnr_args, pick two that do")

    leaked = False
    try:
        DESIGNS[victim].pnr_args.append("--mutated-by-test")
    except AttributeError:
        pass
    else:
        leaked = "--mutated-by-test" in DESIGNS[other].pnr_args

    assert not leaked, (
        f"DESIGNS[{victim!r}].pnr_args.append() leaked into "
        f"DESIGNS[{other!r}].pnr_args: the two entries alias the same "
        f"list object because Design's [] default is shared by every "
        f"NamedTuple built without an explicit value for that field")


def test_verilog_frontend_reads_before_slang_frontend():
    """A design that also carries `verilog` must reach `read_verilog`
    before `read_slang`: read_slang can see modules already in the
    design and checks its instantiations against them, but a module that
    arrives through read_verilog arrives already elaborated, so a
    parameter override from the SystemVerilog side would fail with
    "parameter 'X' does not exist" if the order were reversed.

    No DESIGNS entry sets `verilog` today, so this branch is otherwise
    unreached; a synthetic entry exercises it without invoking yosys.
    """
    synthetic = run_synth.Design(sv=["dummy.sv"], verilog=["legacy.v"])
    DESIGNS["synthetic_verilog_test"] = synthetic
    try:
        cmd = run_synth.synth_command("synthetic_verilog_test", "out.json")
    finally:
        del DESIGNS["synthetic_verilog_test"]

    script = cmd[2]
    assert "read_verilog" in script
    assert "read_slang" in script
    assert script.index("read_verilog") < script.index("read_slang")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"run_synth: OK ({len(tests)} tests)")
