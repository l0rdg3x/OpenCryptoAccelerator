# SPDX-License-Identifier: MIT
"""Cocotb testbench for the Poly1305 RTL core.

Vectors parsed from tests/vectors/sources/rfc8439.txt:
  - 2.5.2: 34-byte message, exercises the partial final block
  - A.3:   full appendix vector set (includes r=0 and s=0 edge cases)
"""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

from poly1305_model import parse_rfc8439, poly1305_tag

VECS = parse_rfc8439()


async def run_mac(dut, key: bytes, msg: bytes) -> bytes:
    dut.key.value = int.from_bytes(key, "little")
    dut.start.value = 1
    await RisingEdge(dut.clk)
    dut.start.value = 0
    await RisingEdge(dut.clk)

    assert len(msg) > 0
    for off in range(0, len(msg), 16):
        chunk = msg[off:off + 16]
        is_last = off + 16 >= len(msg)
        for _ in range(64):
            if dut.blk_ready.value == 1:
                break
            await RisingEdge(dut.clk)
        assert dut.blk_ready.value == 1, "timeout: blk_ready never asserted"
        dut.data_in.value = int.from_bytes(chunk.ljust(16, b"\x00"), "little")
        dut.data_len.value = len(chunk)
        dut.last.value = 1 if is_last else 0
        dut.blk.value = 1
        await RisingEdge(dut.clk)
        dut.blk.value = 0
        dut.last.value = 0
        await RisingEdge(dut.clk)

    for _ in range(64):
        if dut.done.value == 1:
            return int(dut.tag.value).to_bytes(16, "little")
        await RisingEdge(dut.clk)
    raise AssertionError("timeout: done never asserted")


async def setup(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst_n.value = 0
    dut.start.value = 0
    dut.blk.value = 0
    dut.last.value = 0
    dut.key.value = 0
    dut.data_in.value = 0
    dut.data_len.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)


@cocotb.test()
async def test_all_vectors(dut):
    await setup(dut)
    for name, key, msg, tag in VECS:
        got = await run_mac(dut, key, msg)
        assert got == tag, f"{name}: got {got.hex()} want {tag.hex()}"
        dut._log.info(f"{name}: OK ({len(msg)} bytes)")


@cocotb.test()
async def test_model_matches_official_vectors(dut):
    """The oracle must reproduce every official vector before it is
    trusted to judge the RTL."""
    for name, key, msg, tag in VECS:
        got = poly1305_tag(key, msg)
        assert got == tag, f"{name}: model got {got.hex()} want {tag.hex()}"
    assert len(VECS) == 5, f"expected 5 official vectors, parsed {len(VECS)}"


def edge_case_messages() -> list[tuple[str, bytes, bytes]]:
    """(name, key, msg) triples aimed at 26-bit digit carries."""
    k_ff = b"\xff" * 32
    k_mix = bytes(range(32))
    cases = [
        ("all-ff-1blk", k_ff, b"\xff" * 16),
        ("all-ff-4blk", k_ff, b"\xff" * 64),
        ("all-ff-partial", k_ff, b"\xff" * 17),
        ("one-byte", k_mix, b"\x01"),
        ("fifteen-bytes", k_mix, b"\xff" * 15),
        ("digit-boundary", k_mix, (1 << 26).to_bytes(16, "little")),
        ("high-bit", k_mix, (1 << 127).to_bytes(16, "little")),
        ("zeros", k_mix, bytes(16)),
        ("r-zero", bytes(16) + b"\xaa" * 16, b"\xff" * 32),
        ("s-zero", b"\xaa" * 16 + bytes(16), b"\xff" * 32),
    ]
    return cases


@cocotb.test()
async def test_edge_cases(dut):
    await setup(dut)
    for name, key, msg in edge_case_messages():
        want = poly1305_tag(key, msg)
        got = await run_mac(dut, key, msg)
        assert got == want, f"{name}: got {got.hex()} want {want.hex()}"
        dut._log.info(f"{name}: OK ({len(msg)} bytes)")


@cocotb.test()
async def test_randomised(dut):
    await setup(dut)
    rng = random.Random(0xC0FFEE)   # fixed seed: failures are reproducible
    for i in range(200):
        key = bytes(rng.getrandbits(8) for _ in range(32))
        n = rng.choice([1, 15, 16, 17, 31, 32, 33, 64, 129])
        msg = bytes(rng.getrandbits(8) for _ in range(n))
        want = poly1305_tag(key, msg)
        got = await run_mac(dut, key, msg)
        assert got == want, (
            f"random #{i} ({n} bytes): got {got.hex()} want {want.hex()}\n"
            f"  key={key.hex()}\n  msg={msg.hex()}")
