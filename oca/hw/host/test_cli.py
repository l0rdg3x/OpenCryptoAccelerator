# SPDX-License-Identifier: MIT
"""cli.py's exit code is the contract, and this file holds it to that:
0 only on a clean success, 1 on any link or protocol failure, 2 on a
bad invocation before anything touches the wire. Driven through
main() with --fake, capturing stdout/stderr the fixture-free way so
the direct-run mode at the bottom keeps working.
"""

import contextlib
import io
import itertools
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SIM_DIR = _HERE.parent / "sim"
for _p in (_HERE, _SIM_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import cli
from fake_device import FakeBoard
from link import OcaLink


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


def _run_with_loaded_slot(argv):
    """A real board's keystore outlives a host process, so load-key and
    bench are two invocations; FakeBoard's dies with the process and
    main() builds a fresh one per call. Loading the slot on the very
    board main() will use is the fake's stand-in for that persistence.
    """
    board = FakeBoard()
    OcaLink(board).load_key(0, bytes(range(32)))
    orig = cli.FakeBoard
    cli.FakeBoard = lambda: board
    try:
        return _run(argv)
    finally:
        cli.FakeBoard = orig


def test_bench_happy_path_exits_zero_and_stays_in_cycles():
    """No --clock-hz: the output must be cycles only, with the refusal
    to guess stated, and no derived rate anywhere -- the host never
    guesses the clock."""
    rc, out, _ = _run_with_loaded_slot(
        ["--fake", "bench", "--slot", "0", "--blocks", "8"])
    assert rc == 0
    expected = FakeBoard.BENCH_BASE + 8 * FakeBoard.BENCH_PER_BLOCK
    assert f"duration_cycles={expected}" in out
    assert "timestamp_cycles=" in out
    assert "cycles only" in out
    assert "blocks_per_second" not in out


def test_bench_with_user_clock_derives_blocks_per_second():
    """With the user vouching for a clock, the rate is derived from it
    and labeled with it. blocks=8 gives duration 1800 fake cycles, so a
    1800 Hz clock makes the arithmetic exact: 8 blocks in one second."""
    rc, out, _ = _run_with_loaded_slot(
        ["--fake", "bench", "--slot", "0", "--blocks", "8",
         "--clock-hz", "1800"])
    assert rc == 0
    assert "blocks_per_second=8.000" in out
    assert "--clock-hz 1800" in out
    assert "cycles only" not in out


def test_bench_unloaded_slot_exits_one():
    """A fresh fake has no key in any slot; the board answers status 04
    and the tool must exit 1, not 0 with an error only in the text."""
    rc, out, err = _run(["--fake", "bench", "--slot", "0", "--blocks", "8"])
    assert rc == 1
    assert "error:" in err
    assert "blocks_per_second" not in out


def test_bench_zero_blocks_is_a_bad_invocation():
    """N of zero has no cycle count to mean; argparse refuses it with
    exit 2 before anything touches the wire."""
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            cli.main(["--fake", "bench", "--slot", "0", "--blocks", "0"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("expected SystemExit(2) from argparse")


def test_bench_oversize_blocks_is_a_bad_invocation():
    """N rides a 16-bit field; 65536 cannot be sent, only mangled, so
    it must die in argparse with exit 2, never reach the wire."""
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            cli.main(["--fake", "bench", "--slot", "0", "--blocks", "65536"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("expected SystemExit(2) from argparse")


def test_windows_overlap_predicate_both_verdicts():
    """The pure function the bench-pair verdict rides on, held at its
    edges: overlapping and contained windows are yes; abutting and
    disjoint windows are no. Abutting is the case the one-engine fake
    always produces, so getting it wrong would misreport every offline
    run."""
    assert cli._windows_overlap((0, 10), (5, 15))
    assert cli._windows_overlap((5, 15), (0, 10))
    assert cli._windows_overlap((0, 100), (40, 60))
    assert not cli._windows_overlap((0, 10), (10, 20))   # abutting
    assert not cli._windows_overlap((10, 20), (0, 10))
    assert not cli._windows_overlap((0, 10), (30, 40))


def test_bench_pair_happy_path_reports_two_windows_and_non_overlap():
    """Against the one-engine fake the two runs serialise, and the tool
    must say so -- non-overlap reported as such, not as a failure: exit
    0, both windows printed, verdict no, and cycles only without
    --clock-hz."""
    rc, out, _ = _run_with_loaded_slot(
        ["--fake", "bench-pair", "--slot", "0", "--blocks", "8"])
    assert rc == 0
    expected = FakeBoard.BENCH_BASE + 8 * FakeBoard.BENCH_PER_BLOCK
    assert f"window_cycles=[0, {expected}]" in out
    assert f"window_cycles=[{expected}, {2 * expected}]" in out
    assert "overlap=no" in out
    assert "overlap=yes" not in out
    assert "cycles only" in out
    assert "blocks_per_second" not in out


def test_bench_pair_with_user_clock_derives_aggregate_rate():
    """blocks=8 gives two abutting 1800-cycle windows, a 3600-cycle
    span; at --clock-hz 3600 the aggregate is exactly 16 blocks in one
    second, credited over the span so serialised runs earn no phantom
    concurrency."""
    rc, out, _ = _run_with_loaded_slot(
        ["--fake", "bench-pair", "--slot", "0", "--blocks", "8",
         "--clock-hz", "3600"])
    assert rc == 0
    assert "aggregate_blocks_per_second=16.000" in out
    assert "3600-cycle span" in out
    assert "cycles only" not in out


class _PinnedIdLink(OcaLink):
    """OcaLink with the request-id counter pinned away from 0x7FFF:
    break_req_id XORs the echo with 0xFFFF, and at exactly id0 = 0x7FFF
    the two mangled ids land back on the pair's own ids in swapped
    order -- a 1-in-65536 flake if the seed stays random."""

    def __init__(self, transport, timeout=2.0):
        super().__init__(transport, timeout=timeout)
        self._ids = itertools.count(0x0010)


def test_bench_pair_req_id_mismatch_exits_one():
    """A device that mangles the echoed ids must surface as exit 1 with
    the error on stderr, never as a report matched by guesswork."""
    board = FakeBoard()
    OcaLink(board).load_key(0, bytes(range(32)))
    board.break_req_id = True
    orig_board, orig_link = cli.FakeBoard, cli.OcaLink
    cli.FakeBoard = lambda: board
    cli.OcaLink = _PinnedIdLink
    try:
        rc, out, err = _run(
            ["--fake", "bench-pair", "--slot", "0", "--blocks", "8"])
    finally:
        cli.FakeBoard, cli.OcaLink = orig_board, orig_link
    assert rc == 1
    assert "error:" in err
    assert "request id" in err
    assert "overlap" not in out


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"test_cli: OK ({len(tests)} tests)")
