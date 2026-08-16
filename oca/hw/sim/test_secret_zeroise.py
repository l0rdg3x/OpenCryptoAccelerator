# SPDX-License-Identifier: MIT
"""Reset must clear the secrets, not only the control state.

Security.md records the gap this suite closes: "Secrets are not cleared
on reset... Reset restores control state, not confidentiality." The
three AEAD engine files each keep message secrets in registers that the
`if (!rst_n)` branch never touched -- the ChaCha20 working state and its
key/nonce snapshot, the Poly1305 one-time key and accumulator, the
buffered plaintext and ciphertext.

A test that only asserted "zero after reset" would pass on a DUT that
was never loaded, so every check here is paired with its own proof that
the register held something first: a real AEAD_CHACHA20_POLY1305
operation is driven to completion against the RFC 8439 2.8.2 vector,
and the reset is taken at an instant where every listed register is
observed non-zero.
"""

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge, Timer

from test_chacha20_poly1305 import VEC, run_aead, setup

# poly1305 localparams: NL digits, ROWS_PER_CYCLE multiply rows in flight
NL = 5
ROWS = 1

# Poly1305 registers held as unpacked arrays of NL digits.
POLY_DIGIT_ARRAYS = ("r_d", "r5_d", "a_d", "sum_d", "t", "c1", "f")

# `fold` carries the bit-130 overflow of the final add, and the digits
# reaching it are bounded such that the overflow needs a[4] == 2^26-1
# together with a carry in: roughly one message in 2^26, so no message
# this suite can drive leaves anything in it. Its value is deposited
# instead, which keeps the check on it non-vacuous -- what is under test
# is the reset, not the arithmetic that would have written the register.
FOLD = "u_poly.fold"
FOLD_MARKER = 0x5EC5E7_5EC5E7


def secret_regs(dut) -> list[tuple[str, object]]:
    """(name, handle) for every register that holds message secrets.

    Named individually so a failure says which register leaked.
    """
    c, p = dut.u_chacha, dut.u_poly
    regs = [
        # chacha20.sv: working state, the key/nonce snapshot it is added
        # to, and the last block of keystream-xored data
        ("u_chacha.st", c.st),
        ("u_chacha.st_init", c.st_init),
        ("u_chacha.data_out", c.data_out),
        # poly1305.sv: scalars
        ("u_poly.s", p.s),
        ("u_poly.fold", p.fold),
        ("u_poly.a_flat", p.a_flat),
        ("u_poly.tag", p.tag),
        # chacha20_poly1305.sv: the key, the derived one-time key and the
        # plaintext/ciphertext in flight. p_data_in is not here because
        # it is no longer a register: since the p_blk handshake bubble
        # was removed it is combinational from mac_buf, which is listed.
        ("key_r", dut.key_r),
        ("nonce_r", dut.nonce_r),
        ("p_key", dut.p_key),
        ("c_data_in", dut.c_data_in),
        ("mac_buf", dut.mac_buf),
        ("out_data", dut.out_data),
    ]
    for name in POLY_DIGIT_ARRAYS:
        arr = getattr(p, name)
        regs += [(f"u_poly.{name}[{i}]", arr[i]) for i in range(NL)]
    # prod is indexed [ROWS_PER_CYCLE][NL]
    regs += [(f"u_poly.prod[{sl}][{i}]", p.prod[sl][i])
             for sl in range(ROWS) for i in range(NL)]
    return regs


def snapshot(regs) -> dict[str, int]:
    return {name: int(handle.value) for name, handle in regs}


async def loaded_instant(dut, regs, task, limit: int = 4000) -> dict[str, int]:
    """Run until every listed register is non-zero at the same instant.

    Simultaneity is the point: a register that was loaded at some other
    cycle proves nothing about the reset taken here. Sampled mid-cycle,
    so the values read are the ones the reset that follows has to clear
    -- no clock edge separates the observation from it.
    """
    watched = [(name, h) for name, h in regs if name != FOLD]
    never = {name for name, _ in watched}
    for _ in range(limit):
        await RisingEdge(dut.clk)
        await Timer(2, unit="ns")
        snap = snapshot(watched)
        never -= {name for name, v in snap.items() if v != 0}
        if all(v != 0 for v in snap.values()):
            dut.u_poly.fold.value = FOLD_MARKER
            await Timer(1, unit="ns")
            return snapshot(regs)
    task.cancel()
    raise AssertionError(
        "no instant with every secret register loaded; never non-zero: "
        + ", ".join(sorted(never)))


async def assert_cleared(dut, regs, before: dict[str, int]) -> None:
    """Drop rst_n, hold it, and require every register to read zero."""
    dut.rst_n.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    await ReadOnly()
    after = snapshot(regs)
    leaked = [f"{name}=0x{after[name]:x} (was 0x{before[name]:x})"
              for name, _ in regs if after[name] != 0]
    assert not leaked, (
        f"{len(leaked)} register(s) still hold secrets after reset:\n  "
        + "\n  ".join(leaked))


@cocotb.test()
async def test_reset_clears_secrets_mid_message(dut):
    """Reset taken while a message is in flight must wipe every secret.

    The first message runs to completion against the official vector, so
    the finalisation registers (tag, f, c1, a_flat, fold) hold real
    values; the reset is then taken during the second message, where the
    multiply pipeline and the accumulator are live as well.
    """
    await setup(dut)
    regs = secret_regs(dut)

    key, nonce, aad, pt, ct, tag = VEC["enc"]
    got_ct, got_tag = await run_aead(dut, key, nonce, aad, pt)
    assert got_ct == ct and got_tag == tag, (
        "engine did not reproduce RFC 8439 2.8.2; the registers under "
        "test would not be holding what this suite claims")

    task = cocotb.start_soon(run_aead(dut, key, nonce, aad, pt))
    before = await loaded_instant(dut, regs, task)
    task.cancel()
    dut._log.info("all %d secret registers loaded: %s", len(before),
                  ", ".join(f"{n}=0x{v:x}" for n, v in before.items()))

    await assert_cleared(dut, regs, before)


@cocotb.test()
async def test_reset_clears_secrets_after_message(dut):
    """The realistic case: the message is done, the engine is idle, and
    the key, the one-time key and the last block are still in there."""
    await setup(dut)
    regs = secret_regs(dut)

    key, nonce, aad, pt, ct, tag = VEC["enc"]
    got_ct, got_tag = await run_aead(dut, key, nonce, aad, pt)
    assert got_ct == ct and got_tag == tag, "RFC 8439 2.8.2 mismatch"

    await RisingEdge(dut.clk)
    await Timer(2, unit="ns")
    before = snapshot(regs)
    # Not every register carries a value once the engine is idle -- the
    # multiply pipeline drains to zero by construction, and `fold` needs
    # an overflow that does not happen -- so the non-vacuity claim here
    # is over the registers that do; the other test covers the rest.
    keyed = ("u_chacha.st", "u_chacha.st_init", "u_chacha.data_out",
             "u_poly.s", "u_poly.tag", "u_poly.a_flat", "key_r", "nonce_r",
             "p_key", "c_data_in", "mac_buf", "out_data")
    empty = [n for n in keyed if before[n] == 0]
    assert not empty, f"idle engine holds no secret in: {', '.join(empty)}"

    await assert_cleared(dut, regs, before)
