# SPDX-License-Identifier: MIT
"""Cocotb testbench for the AEAD ChaCha20-Poly1305 RTL wrapper.

Vectors parsed from tests/vectors/sources/rfc8439.txt — the same source
of truth as the software tests:
  - 2.8.2: AEAD encryption example (114-byte plaintext, 12-byte AAD)
  - A.5:   AEAD decryption example (265-byte ciphertext, 12-byte AAD)
"""

import random
import re
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

from aead_model import aead_decrypt, aead_encrypt

SRC = Path(__file__).resolve().parents[2] / "tests" / "vectors" / "sources" / "rfc8439.txt"


def _section(text: str, start: str, end: str) -> str:
    m = re.search(rf"(?ms)^{re.escape(start)}.*?^(?={re.escape(end)})", text)
    assert m, f"section {start!r}..{end!r} not found"
    return m.group(0)


def _colonhex_after(flat: str, marker: str) -> bytes:
    i = flat.index(marker) + len(marker)
    m = re.match(r"[()\s0-9a-f:]+", flat[i:])
    assert m, f"no colon-hex after {marker!r}"
    s = re.sub(r"[^0-9a-f:]", "", m.group(0)).strip(":")
    return bytes(int(b, 16) for b in s.split(":"))


def _hexdump_after(lines: list[str], marker: str) -> bytes:
    # at most 16 bytes per line: the ASCII gutter can start with
    # hex-looking characters and must not be captured
    i = next(idx for idx, l in enumerate(lines) if marker in l)
    out = []
    for line in lines[i + 1:]:
        m = re.match(r"^\s*\d{3}\s+((?:[0-9a-f]{2}\s+){1,16})", line)
        if not m:
            break
        out.extend(int(b, 16) for b in m.group(1).split())
    assert out, f"no hexdump after {marker!r}"
    return bytes(out)


def parse_rfc8439() -> dict:
    text = SRC.read_text()

    # 2.8.2 - AEAD encryption example
    sec = _section(text, "2.8.2.", "3.  Implementation Advice")
    lines = sec.splitlines()
    pt = _hexdump_after(lines, "Plaintext:")
    aad = _hexdump_after(lines, "AAD:")
    key = _hexdump_after(lines, "Key:")
    iv = _hexdump_after(lines, "IV:")
    fixed = _hexdump_after(lines, "32-bit fixed-common part:")
    nonce = fixed + iv
    ct = _hexdump_after(lines, "Ciphertext:")
    tag = _colonhex_after(" ".join(sec.split()), "Tag:")
    assert len(key) == 32 and len(nonce) == 12 and len(tag) == 16
    assert len(pt) == len(ct) == 114 and len(aad) == 12

    # A.5 - AEAD decryption example
    sec = _section(text, "A.5.", "Appendix B.")
    lines = sec.splitlines()
    key5 = _hexdump_after(lines, "The ChaCha20 Key")
    ct5 = _hexdump_after(lines, "Ciphertext:")
    nonce5 = _hexdump_after(lines, "The nonce:")
    aad5 = _hexdump_after(lines, "The AAD:")
    tag5 = _hexdump_after(lines, "Received Tag:")
    pt5 = _hexdump_after(lines, "Plaintext::")
    assert len(key5) == 32 and len(nonce5) == 12 and len(tag5) == 16
    assert len(pt5) == len(ct5) == 265 and len(aad5) == 12

    return {
        "enc": (key, nonce, aad, pt, ct, tag),
        "dec": (key5, nonce5, aad5, pt5, ct5, tag5),
    }


async def run_aead(dut, key: bytes, nonce: bytes, aad: bytes, msg: bytes,
                   dec: bool = False):
    """Feed AAD + msg, return (output_bytes, tag)."""
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
        dut.in_aad.value = 1 if is_aad else 0
        dut.in_last.value = 1 if last else 0
        dut.in_len.value = len(chunk)
        dut.in_data.value = int.from_bytes(chunk.ljust(64, b"\x00"), "little")
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


async def setup(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst_n.value = 0
    dut.start.value = 0
    dut.dec.value = 0
    dut.in_valid.value = 0
    dut.in_aad.value = 0
    dut.in_last.value = 0
    dut.in_len.value = 0
    dut.in_data.value = 0
    dut.key.value = 0
    dut.nonce.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)


@cocotb.test()
async def test_encrypt(dut):
    """RFC 8439 2.8.2: encryption produces the expected ciphertext+tag."""
    await setup(dut)
    key, nonce, aad, pt, ct, tag = VEC["enc"]
    got_ct, got_tag = await run_aead(dut, key, nonce, aad, pt)
    assert got_ct == ct, f"2.8.2 ciphertext mismatch\n got {got_ct.hex()}\nwant {ct.hex()}"
    assert got_tag == tag, f"2.8.2 tag mismatch: got {got_tag.hex()} want {tag.hex()}"
    dut._log.info(f"rfc8439-2.8.2: OK ({len(pt)} bytes pt, {len(aad)} bytes aad)")


@cocotb.test()
async def test_decrypt(dut):
    """RFC 8439 A.5: decryption recovers the plaintext and the tag."""
    await setup(dut)
    key, nonce, aad, pt, ct, tag = VEC["dec"]
    got_pt, got_tag = await run_aead(dut, key, nonce, aad, ct, dec=True)
    assert got_pt == pt, f"A.5 plaintext mismatch\n got {got_pt.hex()}\nwant {pt.hex()}"
    assert got_tag == tag, f"A.5 tag mismatch: got {got_tag.hex()} want {tag.hex()}"
    dut._log.info(f"rfc8439-A.5: OK ({len(ct)} bytes ct, {len(aad)} bytes aad)")


@cocotb.test()
async def test_roundtrip(dut):
    """2.8.2 backwards: decrypting the ciphertext restores the plaintext."""
    await setup(dut)
    key, nonce, aad, pt, ct, tag = VEC["enc"]
    got_pt, got_tag = await run_aead(dut, key, nonce, aad, ct, dec=True)
    assert got_pt == pt, "decrypt(encrypt(x)) != x"
    assert got_tag == tag, "tag mismatch on decrypt direction"
    dut._log.info("rfc8439-2.8.2 round-trip: OK")


@cocotb.test()
async def test_model_matches_official_vectors(dut):
    """The oracle must reproduce both official vectors before it judges
    the RTL."""
    key, nonce, aad, pt, ct, tag = VEC["enc"]
    got_ct, got_tag = aead_encrypt(key, nonce, aad, pt)
    assert got_ct == ct, f"2.8.2 ct: {got_ct.hex()} != {ct.hex()}"
    assert got_tag == tag, f"2.8.2 tag: {got_tag.hex()} != {tag.hex()}"

    key, nonce, aad, pt, ct, tag = VEC["dec"]
    got_pt, got_tag = aead_decrypt(key, nonce, aad, ct)
    assert got_pt == pt, f"A.5 pt: {got_pt.hex()} != {pt.hex()}"
    assert got_tag == tag, f"A.5 tag: {got_tag.hex()} != {tag.hex()}"


@cocotb.test()
async def test_randomised_encrypt(dut):
    """Randomised plaintext/AAD lengths around the 64-byte block and
    16-byte MAC boundaries, checked against the reference model."""
    await setup(dut)
    rng = random.Random(0xA11CE)      # fixed seed: failures replay
    for i in range(40):
        key = bytes(rng.getrandbits(8) for _ in range(32))
        nonce = bytes(rng.getrandbits(8) for _ in range(12))
        # lengths chosen around the 64-byte block and 16-byte MAC
        # boundaries, where padding and partial blocks interact
        alen = rng.choice([0, 1, 12, 15, 16, 17, 64, 65])
        plen = rng.choice([1, 15, 16, 63, 64, 65, 127, 128, 130])
        aad = bytes(rng.getrandbits(8) for _ in range(alen))
        pt = bytes(rng.getrandbits(8) for _ in range(plen))
        want_ct, want_tag = aead_encrypt(key, nonce, aad, pt)
        got_ct, got_tag = await run_aead(dut, key, nonce, aad, pt)
        assert got_ct == want_ct, (
            f"random enc #{i} (aad={alen} pt={plen}): ct mismatch\n"
            f"  got  {got_ct.hex()}\n  want {want_ct.hex()}")
        assert got_tag == want_tag, (
            f"random enc #{i} (aad={alen} pt={plen}): tag "
            f"{got_tag.hex()} != {want_tag.hex()}")


@cocotb.test()
async def test_randomised_decrypt(dut):
    """Randomised ciphertext/AAD lengths, including lengths that are not
    a multiple of 64: the testbench zero-pads short input blocks, which
    makes masking the *input* a no-op on the encrypt path, so only the
    decrypt direction exercises an off-by-one on the input mask."""
    await setup(dut)
    rng = random.Random(0xDEC0DE)
    for i in range(40):
        key = bytes(rng.getrandbits(8) for _ in range(32))
        nonce = bytes(rng.getrandbits(8) for _ in range(12))
        alen = rng.choice([0, 12, 16, 17, 64])
        clen = rng.choice([1, 16, 63, 64, 65, 128, 130])
        aad = bytes(rng.getrandbits(8) for _ in range(alen))
        ct = bytes(rng.getrandbits(8) for _ in range(clen))
        want_pt, want_tag = aead_decrypt(key, nonce, aad, ct)
        got_pt, got_tag = await run_aead(dut, key, nonce, aad, ct, dec=True)
        assert got_pt == want_pt, f"random dec #{i}: plaintext mismatch"
        assert got_tag == want_tag, (
            f"random dec #{i} (aad={alen} ct={clen}): tag "
            f"{got_tag.hex()} != {want_tag.hex()}")


VEC = parse_rfc8439()
