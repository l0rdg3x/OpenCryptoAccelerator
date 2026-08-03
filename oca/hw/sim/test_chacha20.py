# SPDX-License-Identifier: MIT
"""Cocotb testbench for the ChaCha20 RTL core.

Vectors are parsed directly from tests/vectors/sources/rfc8439.txt —
the same source of truth as the software tests:
  - 2.3.2: ChaCha20 block function (keystream block)
  - 2.4.2: ChaCha20 encryption, 114-byte message (2 blocks)
"""

import random
import re
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

from chacha20_model import chacha20_block, chacha20_xor

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


def _hexdumps(sec: str) -> list[bytes]:
    """Byte strings from hexdump runs ('  000  xx xx ...' lines).

    At most 16 bytes per line: the ASCII gutter can start with
    hex-looking characters and must not be captured.
    """
    runs, cur = [], []
    for line in sec.splitlines():
        m = re.match(r"^\s*\d{3}\s+((?:[0-9a-f]{2}\s+){1,16})", line)
        if m:
            cur.extend(int(b, 16) for b in m.group(1).split())
        elif cur:
            runs.append(bytes(cur))
            cur = []
    if cur:
        runs.append(bytes(cur))
    return runs


def parse_rfc8439() -> dict:
    text = SRC.read_text()

    # 2.3.2 - block function
    sec = _section(text, "2.3.2.", "2.4.")
    flat = " ".join(sec.split())
    key = _colonhex_after(flat, "o Key =")
    nonce = _colonhex_after(flat, "o Nonce =")
    ctr = int(re.search(r"Block Count = (\d+)", flat).group(1))
    (keystream,) = _hexdumps(sec)  # the "Serialized Block" dump
    assert len(keystream) == 64
    assert len(key) == 32 and len(nonce) == 12

    # 2.4.2 - encryption example
    sec = _section(text, "2.4.2.", "2.5.")
    flat = " ".join(sec.split())
    key2 = _colonhex_after(flat, "o Key =")
    nonce2 = _colonhex_after(flat, "o Nonce =")
    ctr2 = int(re.search(r"Initial Counter = (\d+)", flat).group(1))
    pt, ct = _hexdumps(sec)[:2]
    assert len(key2) == 32 and len(nonce2) == 12 and len(pt) == len(ct) > 64

    return {
        "block": (key, nonce, ctr, keystream),
        "enc": (key2, nonce2, ctr2, pt, ct),
    }


async def run_block(dut, key: bytes, nonce: bytes, ctr: int, data: bytes) -> bytes:
    assert len(data) <= 64
    dut.key.value = int.from_bytes(key, "little")
    dut.nonce.value = int.from_bytes(nonce, "little")
    dut.counter.value = ctr
    dut.data_in.value = int.from_bytes(data.ljust(64, b"\x00"), "little")
    dut.start.value = 1
    await RisingEdge(dut.clk)
    dut.start.value = 0
    for _ in range(20):
        await RisingEdge(dut.clk)
        if dut.done.value == 1:
            return int(dut.data_out.value).to_bytes(64, "little")
    raise AssertionError("timeout: done never asserted")


async def setup(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst_n.value = 0
    dut.start.value = 0
    dut.key.value = 0
    dut.nonce.value = 0
    dut.counter.value = 0
    dut.data_in.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)


@cocotb.test()
async def test_block_function(dut):
    """RFC 8439 2.3.2: block function output (data_in = 0 => raw keystream)."""
    await setup(dut)
    key, nonce, ctr, keystream = VEC["block"]
    got = await run_block(dut, key, nonce, ctr, b"")
    assert got == keystream, (
        f"block function mismatch\n got {got.hex()}\nwant {keystream.hex()}"
    )


@cocotb.test()
async def test_encryption(dut):
    """RFC 8439 2.4.2: two-block encryption."""
    await setup(dut)
    key, nonce, ctr, pt, ct = VEC["enc"]
    out = b""
    for blk in range(0, len(pt), 64):
        out += await run_block(dut, key, nonce, ctr + blk // 64, pt[blk:blk + 64])
    assert out[: len(pt)] == ct, (
        f"encryption mismatch\n got {out[:len(pt)].hex()}\nwant {ct.hex()}"
    )


@cocotb.test()
async def test_decrypt_roundtrip(dut):
    """Decryption is encryption with the same keystream (XOR symmetry)."""
    await setup(dut)
    key, nonce, ctr, pt, ct = VEC["enc"]
    out = b""
    for blk in range(0, len(ct), 64):
        out += await run_block(dut, key, nonce, ctr + blk // 64, ct[blk:blk + 64])
    assert out[: len(ct)] == pt, "decrypt(encrypt(x)) != x"


@cocotb.test()
async def test_model_matches_official_vectors(dut):
    """The oracle must reproduce the official vectors before it judges
    the RTL."""
    key, nonce, ctr, keystream = VEC["block"]
    got = chacha20_block(key, ctr, nonce)
    assert got == keystream, f"2.3.2: model got {got.hex()} want {keystream.hex()}"

    key2, nonce2, ctr2, pt, ct = VEC["enc"]
    out = b""
    for blk in range(0, len(pt), 64):
        chunk = pt[blk:blk + 64]
        out += chacha20_xor(key2, ctr2 + blk // 64, nonce2, chunk)
    assert out[: len(pt)] == ct, f"2.4.2: model got {out.hex()} want {ct.hex()}"


@cocotb.test()
async def test_randomised_blocks(dut):
    await setup(dut)
    rng = random.Random(0x5EED)      # fixed seed: failures replay
    for i in range(100):
        key = bytes(rng.getrandbits(8) for _ in range(32))
        nonce = bytes(rng.getrandbits(8) for _ in range(12))
        counter = rng.getrandbits(32)
        data = bytes(rng.getrandbits(8) for _ in range(64))
        want = chacha20_xor(key, counter, nonce, data)
        got = await run_block(dut, key, nonce, counter, data)
        assert got == want, (
            f"random #{i}: got {got.hex()} want {want.hex()}\n"
            f"  key={key.hex()} nonce={nonce.hex()} counter={counter}")


VEC = parse_rfc8439()
