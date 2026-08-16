# SPDX-License-Identifier: MIT
"""Differential cycle cost of a 64-byte block through oca_core.

This is the measurement apparatus behind the recorded per-block figures
(hw/syn/README.md, docs/RECORD.md): seal commands of 4/8/12/16 blocks
are pipelined back to back and the steady-state spacing between their
responses is read off the wire. The spacing is the engine's occupancy
per command, so differencing two spacings cancels everything a command
pays once — start-up, key derivation, the length block, the tag — and
the quotient is the marginal cost of one 64-byte block, exact or the
assertion fails. The 40-cycle figure (231/391/551/711 at 4/8/12/16
blocks) was measured this way; with the p_blk handshake bubble removed
the marginal cost is 36.00.

Every response is still checked against aead_model: a cycle count over
wrong bytes would be a benchmark of a broken device.
"""

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge

from aead_model import aead_encrypt
from proto_model import (ST_OK, build_bench, build_load_key, build_seal,
                         parse_bench, parse_response)
from test_oca_core import KEY, NONCE, command, send_back_to_back, setup

# Commands per measurement: the first spans absorb the pipeline filling,
# the last WARMUP.. spacings must agree exactly before one is trusted.
NCMDS = 6
WARMUP = 2

MARGINAL = 36  # cycles per 64-byte block: 4 sub-blocks x 9 Poly1305 cycles


def nonce_for(i: int) -> bytes:
    # a fresh nonce per command: reusing (key, nonce) across seals is the
    # host-side misuse Security.md forbids, and nothing here needs it
    return bytes([i]) + NONCE[1:]


async def timed_responses(dut, count: int, budget: int) -> list:
    """Collect `count` responses, stamping the cycle of each tlast beat.

    The sink never stalls: a stalled sink would put the wire, not the
    engine, in charge of the spacing being measured.
    """
    out = []
    cur = bytearray()
    cycle = 0
    while len(out) < count:
        assert cycle < budget, (
            f"no response within {budget} cycles ({len(out)} of {count} "
            f"collected, {len(cur)} bytes partial)")
        await ReadOnly()
        taken = (dut.m_axis_tvalid.value == 1
                 and dut.m_axis_tready.value == 1)
        if taken:
            keep = int(dut.m_axis_tkeep.value)
            raw = int(dut.m_axis_tdata.value).to_bytes(8, "little")
            cur += raw[:keep.bit_count()]
            if dut.m_axis_tlast.value == 1:
                out.append((bytes(cur), cycle))
                cur = bytearray()
        await RisingEdge(dut.clk)
        cycle += 1
    return out


async def seal_spacing(dut, aad: bytes, msg: bytes, base_id: int) -> int:
    """Steady-state cycles per seal command of this shape.

    NCMDS identical seals go in back to back; the spacing between
    consecutive response tails is asserted constant after WARMUP (the
    linearity the record claims) and returned.
    """
    pkts = [build_seal(base_id + i, 0, nonce_for(i), aad, msg)
            for i in range(NCMDS)]
    budget = NCMDS * (60 * (len(aad) + len(msg)) // 64 + 400) + 4000
    sink = cocotb.start_soon(timed_responses(dut, NCMDS, budget))
    await send_back_to_back(dut, pkts)
    got = await sink

    for i, (raw, _) in enumerate(got):
        rsp = parse_response(raw)
        ct, tag = aead_encrypt(KEY, nonce_for(i), aad, msg)
        assert rsp["status"] == ST_OK, f"seal #{i}: status {rsp['status']}"
        assert rsp["req_id"] == base_id + i, f"seal #{i}: wrong req_id"
        assert rsp["body"] == tag + ct, f"seal #{i}: body mismatch"

    times = [t for _, t in got]
    spacings = [b - a for a, b in zip(times, times[1:])]
    steady = spacings[WARMUP:]
    assert len(set(steady)) == 1, (
        f"spacing never settled: {spacings} (aad={len(aad)} msg={len(msg)})")
    return steady[0]


@cocotb.test()
async def test_marginal_block_cost_is_36(dut):
    """One 64-byte block costs exactly MARGINAL cycles at the margin.

    Measured over seals of 4/8/12/16 blocks, every span independently:
    a non-linearity would show as spans disagreeing, not as an average.
    """
    watch = await setup(dut)
    await load_key(dut)

    spacing = {}
    for nblk in (4, 8, 12, 16):
        msg = bytes((i * 5 + nblk) & 0xFF for i in range(64 * nblk))
        spacing[nblk] = await seal_spacing(dut, b"", msg, 0x1000 + nblk)
    dut._log.info(
        "per-command cycles: " + ", ".join(
            f"{n} blocks = {c}" for n, c in spacing.items()))

    for a, b in ((4, 8), (8, 12), (12, 16)):
        delta = spacing[b] - spacing[a]
        assert delta == (b - a) * MARGINAL, (
            f"marginal block cost {delta / (b - a):.2f} cycles over "
            f"{a}->{b} blocks, want {MARGINAL}.00 "
            f"(spacings: {spacing})")
    watch.check()


@cocotb.test()
async def test_aad_block_costs_the_same_as_a_data_block(dut):
    """An AAD-only block and a data block cost the same at the margin.

    An AAD block never runs ChaCha20, so if the schedule were paying for
    the encryption phase a data block would be dearer. Equality is the
    assertion — not any absolute figure — because it is what proves the
    ChaCha20 phase (22 cycles) still hides under the MAC FSM's pace per
    block. Removing the handshake bubble cut that pace from 40 to 36,
    so the slack this equality rides on is now 14 cycles, not 18.

    The AAD seals carry a one-byte plaintext tail, the same shape as the
    recorded measurement (hw/syn/README.md, "Confirmed by a falsifiable
    measurement").
    """
    watch = await setup(dut)
    await load_key(dut)

    data = {}
    aad_only = {}
    for nblk in (4, 8):
        msg = bytes((i * 3 + 1) & 0xFF for i in range(64 * nblk))
        data[nblk] = await seal_spacing(dut, b"", msg, 0x2000 + nblk)
        aad = bytes((i * 7 + 2) & 0xFF for i in range(64 * nblk))
        aad_only[nblk] = await seal_spacing(dut, aad, b"\x5a", 0x3000 + nblk)
    dut._log.info(f"data spacings {data}, aad spacings {aad_only}")

    data_span = data[8] - data[4]
    aad_span = aad_only[8] - aad_only[4]
    assert aad_span == data_span, (
        f"4 AAD-only blocks cost {aad_span} cycles at the margin, 4 data "
        f"blocks {data_span}: the ChaCha20 phase is no longer hidden "
        f"(data={data}, aad={aad_only})")
    watch.check()


@cocotb.test()
async def test_bench_opcode_reports_the_differential_cost(dut):
    """The on-chip counter and this file's model agree exactly.

    Three benches of 4, 8 and 16 blocks: the returned durations must
    differ by exactly MARGINAL per block, and the intercept —
    everything a command pays once: key derivation, the length block,
    the tag — must be the same for all three. The engine monitor pins
    N, because the response never says how many blocks ran: an
    off-by-one repeat counter shifts every duration by one block and
    leaves both the margin and the intercept self-consistent, so only
    counting the engine's own handshakes can catch it.

    The timestamps must describe the same experiment: back-to-back
    windows [timestamp - duration, timestamp] on one engine, strictly
    ordered on the shared free-running timebase.
    """
    watch = await setup(dut)
    await load_key(dut)

    block = bytes((i * 13 + 7) & 0xFF for i in range(64))
    results = {}
    for n in (4, 8, 16):
        fed0 = watch.blocks_in
        rsp = await command(
            dut, build_bench(0x4000 + n, 0, nonce_for(n), n, block))
        assert rsp["status"] == ST_OK, f"bench {n}: status {rsp['status']}"
        fed = watch.blocks_in - fed0
        assert fed == n, f"bench of {n} blocks fed {fed} to the engine"
        results[n] = parse_bench(rsp)

    durs = {n: r["duration"] for n, r in results.items()}
    dut._log.info("bench durations: " + ", ".join(
        f"{n} blocks = {d}" for n, d in durs.items()))

    for a, b in ((4, 8), (8, 16)):
        delta = durs[b] - durs[a]
        assert delta == (b - a) * MARGINAL, (
            f"on-chip marginal cost {delta / (b - a):.2f} cycles over "
            f"{a}->{b} blocks, want {MARGINAL}.00 (durations: {durs})")

    intercepts = {n: durs[n] - n * MARGINAL for n in durs}
    assert len(set(intercepts.values())) == 1, \
        f"intercept moved with N: {intercepts}"
    dut._log.info(f"bench intercept: {intercepts[4]} cycles")

    windows = [(results[n]["timestamp"] - results[n]["duration"],
                results[n]["timestamp"]) for n in (4, 8, 16)]
    for (s0, e0), (s1, e1) in zip(windows, windows[1:]):
        assert e0 < s1 <= e1, (
            f"windows out of order on one engine: ({s0}, {e0}) "
            f"then ({s1}, {e1})")
    watch.check()


async def load_key(dut):
    rsp = await command(dut, build_load_key(0x0001, 0, KEY))
    assert rsp["status"] == ST_OK, f"load_key status {rsp['status']}"
