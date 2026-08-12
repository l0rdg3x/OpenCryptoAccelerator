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

from run_synth import pnr_command


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


if __name__ == "__main__":
    test_out_of_context_design_gets_no_textcfg_or_lpf()
    test_pinned_design_gets_textcfg_and_lpf_not_out_of_context()
    print("run_synth: OK")
