# SPDX-License-Identifier: MIT
"""Cocotb testbench for the Poly1305 RTL core.

Vectors parsed from tests/vectors/sources/rfc8439.txt:
  - 2.5.2: 34-byte message, exercises the partial final block
  - A.3:   full appendix vector set (includes r=0 and s=0 edge cases)
"""

import re
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

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
    # at most 16 bytes per line: the ASCII gutter can start with
    # hex-looking characters (e.g. 'ed an "IETF Cont') and must not
    # be captured
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


def parse_rfc8439() -> list[tuple[str, bytes, bytes, bytes]]:
    """Returns [(name, key32, msg, tag16), ...]."""
    text = SRC.read_text()
    vecs = []

    sec = _section(text, "2.5.2.", "2.6.")
    flat = " ".join(sec.split())
    key = _colonhex_after(flat, "o Key Material:")
    tag = _colonhex_after(flat, "Tag:")
    (msg,) = _hexdumps(sec)
    assert len(key) == 32 and len(tag) == 16 and len(msg) == 34
    vecs.append(("rfc8439-2.5.2", key, msg, tag))

    sec = _section(text, "A.3.", "A.4.")
    parts = re.split(r"Test Vector #(\d+):", sec)
    # parts = [pre, "1", body1, "2", body2, ...]
    for i in range(1, len(parts), 2):
        num, body = parts[i], parts[i + 1]
        if "Text to MAC" not in body:
            continue  # #5-#8 are ChaCha20 key-generation vectors, not MAC
        dumps = _hexdumps(body)
        assert len(dumps) == 3, f"A.3 vector #{num}: expected 3 hexdumps"
        k, m, t = dumps
        assert len(k) == 32 and len(t) == 16, f"A.3 vector #{num}: bad lengths"
        vecs.append((f"rfc8439-A.3-{num}", k, m, t))

    return vecs


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
        for _ in range(20):
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

    for _ in range(20):
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


VECS = parse_rfc8439()
