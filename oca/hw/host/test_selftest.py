# SPDX-License-Identifier: MIT
"""run_selftest against FakeBoard: the sequence itself is exercised
without the board on the bench, and proved able to fail -- a selftest
that always passes is not a selftest, it is decoration.
"""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SIM_DIR = _HERE.parent / "sim"
for _p in (_HERE, _SIM_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from fake_device import FakeBoard
from link import OcaLink
from selftest import SelftestFailure, run_selftest


def test_selftest_passes_against_a_correct_fake_board():
    link = OcaLink(FakeBoard(), timeout=1.0)
    log = []
    run_selftest(link, log=log.append)
    assert any("6/6" in line for line in log)


def test_selftest_fails_when_the_board_seals_under_the_wrong_key():
    """A stand-in that quietly corrupts the key before sealing: the
    comparison against aead_model.aead_encrypt must catch it. Proves
    the selftest's core check is load-bearing rather than vacuous."""

    class BrokenBoard(FakeBoard):
        def _op_seal(self, req_id, slot, body):
            self._slots[slot] = bytes(b ^ 0xFF for b in self._slots[slot])
            return super()._op_seal(req_id, slot, body)

    link = OcaLink(BrokenBoard(), timeout=1.0)
    try:
        run_selftest(link, log=lambda *_: None)
    except SelftestFailure:
        pass
    else:
        raise AssertionError(
            "expected SelftestFailure against a board that seals under "
            "the wrong key")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"test_selftest: OK ({len(tests)} tests)")
