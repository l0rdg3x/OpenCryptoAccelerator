# SPDX-License-Identifier: MIT
"""Adversarial companion to test_chacha20_poly1305.py.

The project testbench zero-pads every short block
(`chunk.ljust(64, b"\\x00")`), so masked and unmasked `in_data` are the
same stimulus and the suite cannot tell whether the engine masks the
input at all. This module drives the padding bytes with random garbage
instead: the ciphertext and the tag must not move.
"""

import random

import cocotb
from cocotb.triggers import RisingEdge

from aead_model import aead_decrypt, aead_encrypt
from test_chacha20_poly1305 import setup


async def run_aead(dut, key: bytes, nonce: bytes, aad: bytes, msg: bytes,
                   dec: bool, rng: random.Random):
    """Like test_chacha20_poly1305.run_aead, but the bytes past in_len
    are `rng` garbage instead of zeros."""
    dut.key.value = int.from_bytes(key, "little")
    dut.nonce.value = int.from_bytes(nonce, "little")
    dut.dec.value = 1 if dec else 0
    dut.start.value = 1
    await RisingEdge(dut.clk)
    dut.start.value = 0

    blocks = [(True, aad[o:o + 64]) for o in range(0, len(aad), 64)]
    blocks += [(False, msg[o:o + 64]) for o in range(0, len(msg), 64)]
    if not blocks:
        blocks = [(False, b"")]

    out = b""
    for i, (is_aad, chunk) in enumerate(blocks):
        last = i == len(blocks) - 1
        # await before checking: reading in_ready right after the edge
        # that consumed the previous block returns the stale value
        for _ in range(200):
            await RisingEdge(dut.clk)
            if dut.in_ready.value == 1:
                break
        else:
            raise AssertionError("timeout: in_ready never asserted")
        pad = bytes(rng.getrandbits(8) for _ in range(64 - len(chunk)))
        dut.in_aad.value = 1 if is_aad else 0
        dut.in_last.value = 1 if last else 0
        dut.in_len.value = len(chunk)
        dut.in_data.value = int.from_bytes(chunk + pad, "little")
        dut.in_valid.value = 1
        await RisingEdge(dut.clk)
        dut.in_valid.value = 0
        dut.in_last.value = 0
        if not is_aad and len(chunk) > 0:
            for _ in range(200):
                await RisingEdge(dut.clk)
                if dut.out_valid.value == 1:
                    n = int(dut.out_len.value)
                    out += int(dut.out_data.value).to_bytes(64, "little")[:n]
                    break
            else:
                raise AssertionError("timeout: out_valid never asserted")

    for _ in range(200):
        await RisingEdge(dut.clk)
        if dut.done.value == 1:
            return out, int(dut.tag.value).to_bytes(16, "little")
    raise AssertionError("timeout: done never asserted")


@cocotb.test()
async def test_dirty_padding_encrypt(dut):
    """Random garbage past in_len must not change ciphertext or tag."""
    await setup(dut)
    rng = random.Random(0x0BADF00D)
    for i in range(30):
        key = bytes(rng.getrandbits(8) for _ in range(32))
        nonce = bytes(rng.getrandbits(8) for _ in range(12))
        alen = rng.choice([0, 1, 12, 15, 16, 17, 63, 64, 65])
        plen = rng.choice([1, 15, 16, 17, 31, 63, 64, 65, 127, 130])
        aad = bytes(rng.getrandbits(8) for _ in range(alen))
        pt = bytes(rng.getrandbits(8) for _ in range(plen))
        want_ct, want_tag = aead_encrypt(key, nonce, aad, pt)
        got_ct, got_tag = await run_aead(dut, key, nonce, aad, pt,
                                         dec=False, rng=rng)
        assert got_ct == want_ct, (
            f"dirty enc #{i} (aad={alen} pt={plen}): ct mismatch\n"
            f"  got  {got_ct.hex()}\n  want {want_ct.hex()}")
        assert got_tag == want_tag, (
            f"dirty enc #{i} (aad={alen} pt={plen}): tag "
            f"{got_tag.hex()} != {want_tag.hex()}")


@cocotb.test()
async def test_dirty_padding_decrypt(dut):
    """Same, on the decrypt direction, where the MAC is taken over the
    input block rather than over the ChaCha20 output."""
    await setup(dut)
    rng = random.Random(0xC0FFEE)
    for i in range(30):
        key = bytes(rng.getrandbits(8) for _ in range(32))
        nonce = bytes(rng.getrandbits(8) for _ in range(12))
        alen = rng.choice([0, 1, 12, 16, 17, 63, 64, 65])
        clen = rng.choice([1, 15, 16, 17, 63, 64, 65, 128, 130])
        aad = bytes(rng.getrandbits(8) for _ in range(alen))
        ct = bytes(rng.getrandbits(8) for _ in range(clen))
        want_pt, want_tag = aead_decrypt(key, nonce, aad, ct)
        got_pt, got_tag = await run_aead(dut, key, nonce, aad, ct,
                                         dec=True, rng=rng)
        assert got_pt == want_pt, f"dirty dec #{i}: plaintext mismatch"
        assert got_tag == want_tag, (
            f"dirty dec #{i} (aad={alen} ct={clen}): tag "
            f"{got_tag.hex()} != {want_tag.hex()}")
