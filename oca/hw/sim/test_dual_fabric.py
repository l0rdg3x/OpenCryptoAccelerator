# SPDX-License-Identifier: MIT
"""The dual-core fabric: dispatcher, two oca_core, collector.

The DUT is oca_dual_harness.sv, which is oca_uart_crypto_dual's middle
with the serial front end cut away, so requests are injected on the
64-bit stream oca_slip_rx drives in the assembled design and responses
are read where oca_slip_tx reads them. Everything oca_core does with a
packet is proved in test_oca_core.py; this file proves only what the
pair adds — broadcast, routing, collection — and every expected value
still comes from aead_model through proto_model.

What is asserted here, and by which test:

- BROADCAST (test_broadcast_load_key_reaches_both_cores): one load_key
  in, exactly one response out, and the key afterwards exists in BOTH
  private keystores — proved by occupying core 0 with a long bench and
  then sealing on core 1, against the model's ciphertext. The dispatch
  monitor asserts where each frame actually went, so the test cannot
  pass by both commands landing on the same core.
- NON-INTERLEAVING (test_responses_never_interleave): two responses in
  flight forward whole, never mixed. The premise is witnessed at run
  time: a cycle must be seen where one core's response is mid-forward
  while the other's stands complete and waiting, or the test fails as
  vacuous rather than passing as green.
- FAIL-CLOSED (test_clean_broadcast_keeps_trouble_low on this RTL;
  test_divergent_broadcast_fails_closed on a scratchpad copy whose
  core 1 carries a smaller keystore, run with OCA_DUAL_DIVERGENT=1):
  a broadcast the cores answer identically leaves `trouble` low; one
  they answer differently forwards the ERROR status of the two and
  latches `trouble` sticky.
- OVERLAP (test_bench_windows_overlap): two pipelined benches land on
  different cores and their measured windows overlap on the shared
  timebase — the aggregate-throughput fact the dual exists for.
- STALL (test_both_cores_busy_stalls_cleanly): with both cores refusing
  input the dispatcher holds the stream, and every stalled frame later
  completes intact, in per-core order, with nothing lost or corrupted.

Every one of those assertions has been shown to fail by a mutation on
a scratchpad copy of the RTL; run_dual_fabric.py --src-override is the
mechanism, and the runner's docstring records the mutations.
"""

import os
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from aead_model import aead_encrypt
from proto_model import (OP_BENCH, OP_LOAD_KEY, ST_BAD_SLOT, ST_OK,
                         build_bench, build_load_key, build_open, build_seal,
                         build_stats, parse_bench, parse_response)

KEY = bytes(range(32))
KEY2 = bytes(range(32, 64))
NONCE = bytes(range(12))
NONCE2 = bytes(range(100, 112))

# Divergence is a fault this RTL cannot produce (identical cores,
# identical input), so the test that expects it runs only against a
# deliberately divergent scratchpad copy, under this variable.
DIVERGENT = os.environ.get("OCA_DUAL_DIVERGENT") == "1"

# Both cores' packet buffers zero themselves out of reset in parallel,
# one word per cycle over two banks each; the pair is no slower than
# one core (test_oca_core.py).
BYTES = 2048
CLEAR_CYCLES = 2 * (BYTES // 8) + 32

# The engine's marginal cost per 64-byte block, asserted exactly in
# test_aead_cycles.py; a bench of N blocks owns its engine for at
# least 36*N cycles, which is what makes it the fill command here.
ENGINE_FLOOR = 36


def words_of(pkt: bytes):
    beats = []
    for off in range(0, len(pkt), 8):
        chunk = pkt[off:off + 8]
        beats.append((int.from_bytes(chunk.ljust(8, b"\x00"), "little"),
                      (1 << len(chunk)) - 1))
    return beats


class FabricWatch:
    """Where every frame went, and whether the dispatcher ever stalled.

    push0/push1 pulse once per frame per core in dispatch order, both
    together for a broadcast: the record list is therefore the routing
    decision itself, read from the RTL rather than inferred from which
    responses came back. The stall counter runs only while the
    dispatcher sits in D_IDLE with a frame offered and not taken —
    the one state where "both cores busy" is the reason — so the
    one dead decision cycle per frame cannot masquerade as a stall.
    """

    def __init__(self):
        self.records = []       # 'b', '0' or '1' per frame, dispatch order
        self.max_stall = 0
        self.overlap_seen = 0   # collector forwarding one core while the
        #                         other core's response stands waiting

    def core_of(self, index: int) -> int:
        rec = self.records[index]
        assert rec in ("0", "1"), f"frame {index} was a broadcast"
        return int(rec)


async def watch_fabric(dut, w: FabricWatch):
    stall_run = 0
    while True:
        await ReadOnly()
        p0 = int(dut.push0.value)
        p1 = int(dut.push1.value)
        if p0 and p1:
            w.records.append("b")
        elif p0:
            w.records.append("0")
        elif p1:
            w.records.append("1")

        # oca_dispatch's state_e: D_IDLE = 0.
        idle = int(dut.u_dispatch.state.value) == 0
        if idle and int(dut.s_tvalid.value) and not int(dut.s_tready.value):
            stall_run += 1
            w.max_stall = max(w.max_stall, stall_run)
        else:
            stall_run = 0

        # oca_collect's state_e: C_PASS = 1; src names the locked source.
        if int(dut.u_collect.state.value) == 1:
            src = int(dut.u_collect.src.value)
            other_valid = int(dut.u_collect.s1_tvalid.value) if src == 0 \
                else int(dut.u_collect.s0_tvalid.value)
            if other_valid:
                w.overlap_seen += 1

        await RisingEdge(dut.clk)


async def setup(dut) -> FabricWatch:
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst_n.value = 0
    dut.s_tdata.value = 0
    dut.s_tkeep.value = 0
    dut.s_tvalid.value = 0
    dut.s_tlast.value = 0
    dut.m_tready.value = 1
    for _ in range(3):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    watch = FabricWatch()
    cocotb.start_soon(watch_fabric(dut, watch))
    for _ in range(CLEAR_CYCLES):
        await RisingEdge(dut.clk)
    return watch


async def send_word(dut, data: int, keep: int, last: bool):
    """One beat, held stable until tready is seen high in ReadOnly."""
    dut.s_tdata.value = data
    dut.s_tkeep.value = keep
    dut.s_tlast.value = 1 if last else 0
    dut.s_tvalid.value = 1
    while True:
        await ReadOnly()
        ready = dut.s_tready.value == 1
        await RisingEdge(dut.clk)
        if ready:
            return


async def send_packets(dut, pkts):
    """Back to back: the next frame's first beat right after tlast."""
    for pkt in pkts:
        beats = words_of(pkt)
        for i, (data, keep) in enumerate(beats):
            await send_word(dut, data, keep, i == len(beats) - 1)
    dut.s_tvalid.value = 0
    dut.s_tlast.value = 0


async def recv_packet(dut, budget: int = 60000,
                      rng: random.Random | None = None,
                      stall_p: float = 0.0) -> bytes:
    """One whole response frame, cut at tlast, tail bytes checked masked."""
    out = bytearray()
    for _ in range(budget):
        if rng is not None:
            dut.m_tready.value = 0 if rng.random() < stall_p else 1
        await ReadOnly()
        taken = (dut.m_tvalid.value == 1 and dut.m_tready.value == 1)
        last = taken and dut.m_tlast.value == 1
        if taken:
            keep = int(dut.m_tkeep.value)
            raw = int(dut.m_tdata.value).to_bytes(8, "little")
            nbytes = keep.bit_count()
            assert raw[nbytes:] == bytes(8 - nbytes), (
                f"beat leaks {8 - nbytes} unmasked bytes past tkeep")
            out += raw[:nbytes]
        await RisingEdge(dut.clk)
        if last:
            dut.m_tready.value = 1
            return bytes(out)
    raise AssertionError(f"no response within {budget} cycles "
                         f"({len(out)} bytes seen)")


async def recv_packets(dut, count: int, budget: int = 60000,
                       rng: random.Random | None = None,
                       stall_p: float = 0.0) -> list:
    return [await recv_packet(dut, budget=budget, rng=rng, stall_p=stall_p)
            for _ in range(count)]


async def expect_quiet(dut, cycles: int, why: str):
    """No beat may appear on the response stream for `cycles` cycles."""
    dut.m_tready.value = 1
    for n in range(cycles):
        await ReadOnly()
        assert dut.m_tvalid.value == 0, \
            f"unexpected response beat after {n} quiet cycles: {why}"
        await RisingEdge(dut.clk)


def by_req_id(raws: list) -> dict:
    rsps = [parse_response(r) for r in raws]
    got = [r["req_id"] for r in rsps]
    assert len(set(got)) == len(got), f"duplicate req_ids in {got}"
    return {r["req_id"]: r for r in rsps}


@cocotb.test()
async def test_round_trip_through_the_fabric(dut):
    """One load_key, a seal, and an open of its output — on different
    cores, which is the fact that makes it a fabric test: the open
    verifies on a core that never saw the seal, so the broadcast key
    and the collected ciphertext both crossed the fabric intact."""
    watch = await setup(dut)

    await send_packets(dut, [build_load_key(0x0001, 2, KEY)])
    rsp = parse_response(await recv_packet(dut))
    assert rsp["status"] == ST_OK and rsp["req_id"] == 0x0001

    aad, msg = b"header", b"the quick brown fox crosses the fabric"
    await send_packets(dut, [build_seal(0x0002, 2, NONCE, aad, msg)])
    sealed = parse_response(await recv_packet(dut))
    want_ct, want_tag = aead_encrypt(KEY, NONCE, aad, msg)
    assert sealed["status"] == ST_OK, f"seal status {sealed['status']}"
    assert sealed["body"] == want_tag + want_ct, "seal disagrees with model"

    await send_packets(dut, [build_open(0x0003, 2, NONCE, aad,
                                        want_ct, want_tag)])
    opened = parse_response(await recv_packet(dut))
    assert opened["status"] == ST_OK, f"open status {opened['status']}"
    assert opened["body"] == msg, "plaintext did not round-trip"

    assert watch.records == ["b", "0", "1"], (
        f"dispatch went {watch.records}, want ['b', '0', '1']: the seal "
        f"and the open were meant to land on different cores")
    assert int(dut.trouble.value) == 0, "trouble raised by clean traffic"


@cocotb.test()
async def test_broadcast_load_key_reaches_both_cores(dut):
    """One load_key in, one response out, and the key is in BOTH cores.

    The second half is the teeth: core 0 is occupied by a long bench
    (36 cycles a block, 64 blocks), the seal behind it lands on core 1
    — asserted from the dispatch record, not assumed from the policy —
    and the ciphertext that comes back is the model's under the
    broadcast key. A load_key that reached only one core fails here on
    the seal's status or body; one answered twice fails the quiet
    window after its response.
    """
    watch = await setup(dut)

    await send_packets(dut, [build_load_key(0x0011, 0, KEY)])
    rsp = parse_response(await recv_packet(dut))
    assert rsp["opcode"] == OP_LOAD_KEY and rsp["req_id"] == 0x0011
    assert rsp["status"] == ST_OK, f"load_key status {rsp['status']}"
    assert rsp["body"] == b"", f"load_key returned a body: {rsp['body']!r}"
    await expect_quiet(dut, 300,
                       "a broadcast load_key must be answered exactly once")
    assert watch.records == ["b"], (
        f"load_key dispatched as {watch.records}, want a broadcast")

    nblocks = 64
    aad, msg = b"fabric", b"sealed by the core the broadcast had to reach"
    want_ct, want_tag = aead_encrypt(KEY, NONCE2, aad, msg)
    await send_packets(dut, [
        build_bench(0x0012, 0, NONCE, nblocks, bytes(range(64))),
        build_seal(0x0013, 0, NONCE2, aad, msg),
    ])
    arrived = await recv_packets(dut, 2)
    # Completion order beats arrival order: the seal was dispatched
    # second but runs on the idle core while the 64-block bench holds
    # core 0, so its response must come back FIRST. Flip this pair and
    # the assertion reads a collector that restored arrival order.
    arrival_ids = [parse_response(raw)["req_id"] for raw in arrived]
    assert arrival_ids == [0x0013, 0x0012], (
        f"responses arrived {[hex(i) for i in arrival_ids]}: "
        f"completion order should put the short seal before the long "
        f"bench")
    rsps = by_req_id(arrived)

    assert watch.records == ["b", "0", "1"], (
        f"dispatch went {watch.records}: the bench was meant to occupy "
        f"core 0 and force the seal onto core 1")
    seal = rsps[0x0013]
    assert seal["status"] == ST_OK, (
        f"seal on core 1 answered {seal['status']}: the broadcast key "
        f"is not in core 1's keystore")
    assert seal["body"] == want_tag + want_ct, \
        "core 1 sealed with something other than the broadcast key"
    bench = rsps[0x0012]
    assert bench["status"] == ST_OK, f"bench status {bench['status']}"
    assert parse_bench(bench)["duration"] >= ENGINE_FLOOR * nblocks

    assert int(dut.trouble.value) == 0, "trouble raised by clean traffic"


@cocotb.test()
async def test_responses_never_interleave(dut):
    """Two responses in flight forward whole, byte order intact.

    Both cores carry a large seal each and the sink stalls, so one
    core's response is mid-forward while the other's stands complete —
    witnessed by the monitor, and the test fails as vacuous if the
    witness never fires. Integrity is the whole frame: each response
    parses, and each body equals the model's output for its own
    request, which no interleaving of beats can survive.
    """
    watch = await setup(dut)
    await send_packets(dut, [build_load_key(0x0021, 1, KEY)])
    assert parse_response(await recv_packet(dut))["status"] == ST_OK

    for round_no, (alen, mlen, stall_p) in enumerate(
            [(16, 800, 0.5), (0, 640, 0.8), (48, 512, 0.9)]):
        rng = random.Random(0x1EAF + round_no)
        aad = bytes(rng.getrandbits(8) for _ in range(alen))
        msg_a = bytes(rng.getrandbits(8) for _ in range(mlen))
        msg_b = bytes(rng.getrandbits(8) for _ in range(mlen))
        ct_a, tag_a = aead_encrypt(KEY, NONCE, aad, msg_a)
        ct_b, tag_b = aead_encrypt(KEY, NONCE2, aad, msg_b)
        req_a, req_b = 0x0100 + 2 * round_no, 0x0101 + 2 * round_no

        sink = cocotb.start_soon(
            recv_packets(dut, 2, rng=rng, stall_p=stall_p))
        await send_packets(dut, [
            build_seal(req_a, 1, NONCE, aad, msg_a),
            build_seal(req_b, 1, NONCE2, aad, msg_b),
        ])
        rsps = by_req_id(await sink)

        where = f"round {round_no}, stall {stall_p}"
        for req, tag, ct in ((req_a, tag_a, ct_a), (req_b, tag_b, ct_b)):
            rsp = rsps[req]
            assert rsp["magic_ok"], f"garbled header ({where})"
            assert rsp["status"] == ST_OK, \
                f"{req:#06x} status {rsp['status']} ({where})"
            assert rsp["body"] == tag + ct, (
                f"{req:#06x} body differs from the model ({where}): "
                f"beats of the two responses were mixed")

    assert watch.overlap_seen > 0, (
        "no cycle ever had one response mid-forward with the other "
        "waiting: this run proved nothing about interleaving — raise "
        "the sizes or the stall probability")
    assert int(dut.trouble.value) == 0, "trouble raised by clean traffic"


@cocotb.test()
async def test_clean_broadcast_keeps_trouble_low(dut):
    """Identical cores answering one broadcast identically: no fault.

    Slot 6 on purpose — the very slot the divergent scratchpad copy
    refuses on one side — so this test and its mutation twin
    (test_divergent_broadcast_fails_closed) differ only in the RTL
    under them.
    """
    watch = await setup(dut)
    await send_packets(dut, [build_load_key(0x0061, 6, KEY2)])
    rsp = parse_response(await recv_packet(dut))
    assert rsp["status"] == ST_OK, f"load_key status {rsp['status']}"
    await expect_quiet(dut, 300, "one broadcast, one answer")
    assert watch.records == ["b"]
    assert int(dut.trouble.value) == 0, (
        "trouble latched on a broadcast both cores answered identically")

    await send_packets(dut, [build_stats(0x0062)])
    assert parse_response(await recv_packet(dut))["status"] == ST_OK
    assert int(dut.trouble.value) == 0


@cocotb.test(skip=not DIVERGENT)
async def test_divergent_broadcast_fails_closed(dut):
    """The cores disagree on a broadcast: error forwarded, fault latched.

    Runs only under OCA_DUAL_DIVERGENT=1 against a scratchpad copy
    whose core 1 keystore has 4 slots, so a broadcast load_key to
    slot 6 answers ST_OK from core 0 and ST_BAD_SLOT from core 1. The
    collector must forward the ERROR status of the two — reporting the
    success would report a device state that does not exist — raise
    `trouble`, hold it sticky, and keep serving routed traffic. On
    unmutated RTL this test fails, which is its non-vacuity proof.
    """
    watch = await setup(dut)
    await send_packets(dut, [build_load_key(0x0066, 6, KEY2)])
    rsp = parse_response(await recv_packet(dut))
    assert rsp["opcode"] == OP_LOAD_KEY and rsp["req_id"] == 0x0066
    assert rsp["status"] == ST_BAD_SLOT, (
        f"the cores diverged (00 vs 04) and the collector forwarded "
        f"{rsp['status']:#04x}: fail closed means the error wins")
    assert rsp["body"] == b"", "a refused load_key returned a body"
    await expect_quiet(dut, 300, "a diverged broadcast is still one answer")
    assert watch.records == ["b"]
    assert int(dut.trouble.value) == 1, (
        "statuses diverged and the fault latch stayed low: a divergence "
        "that flashes past the LED is a fault nobody catches")

    await send_packets(dut, [build_stats(0x0067)])
    assert parse_response(await recv_packet(dut))["status"] == ST_OK, \
        "the fabric stopped serving routed traffic after a divergence"
    assert int(dut.trouble.value) == 1, "the fault latch is not sticky"


@cocotb.test()
async def test_bench_windows_overlap(dut):
    """Two pipelined benches run on different cores AT THE SAME TIME.

    This is the aggregate-throughput fact the dual exists for, and it
    is asserted as window arithmetic, not as both-answered: each bench
    reports [timestamp - duration, timestamp] on a timebase that
    free-runs from the shared reset in both cores, so the two windows
    are comparable, and each must open strictly before the other
    closes. Serialise the benches — one core, or a dispatcher that
    stopped alternating — and the second window opens at or after the
    first one's close.
    """
    watch = await setup(dut)
    await send_packets(dut, [build_load_key(0x0031, 1, KEY)])
    assert parse_response(await recv_packet(dut))["status"] == ST_OK

    nblocks = 100
    block = bytes(range(64))
    await send_packets(dut, [
        build_bench(0x0032, 1, NONCE, nblocks, block),
        build_bench(0x0033, 1, NONCE2, nblocks, block),
    ])
    rsps = by_req_id(await recv_packets(dut, 2))

    # The windows are judged before the routing record on purpose: a
    # dispatcher that serialised the pair must fail on the overlap
    # arithmetic itself, not only on where the frames went.
    windows = {}
    for req in (0x0032, 0x0033):
        rsp = rsps[req]
        assert rsp["status"] == ST_OK, f"{req:#06x} status {rsp['status']}"
        b = parse_bench(rsp)
        assert b["duration"] >= ENGINE_FLOOR * nblocks, (
            f"{req:#06x} duration {b['duration']} under the engine floor "
            f"{ENGINE_FLOOR * nblocks}")
        windows[req] = (b["timestamp"] - b["duration"], b["timestamp"])

    (a0, a1), (b0, b1) = windows[0x0032], windows[0x0033]
    assert a0 < b1 and b0 < a1, (
        f"the bench windows [{a0}, {a1}] and [{b0}, {b1}] do not "
        f"overlap: the two engines never ran concurrently and the dual "
        f"delivered single-core throughput")
    assert watch.records == ["b", "0", "1"], (
        f"dispatch went {watch.records}: the two benches were meant to "
        f"land on different cores")
    assert int(dut.trouble.value) == 0, "trouble raised by clean traffic"


@cocotb.test()
async def test_both_cores_busy_stalls_cleanly(dut):
    """Backpressure, not loss: a frame no core can take simply waits.

    The sink is held closed while twelve seals pour in, so both cores
    wedge with responses they cannot drain and the dispatcher is left
    holding frames neither core will accept. The stall itself is
    witnessed (D_IDLE, frame offered, not taken, for hundreds of
    cycles — the one-cycle decision gap cannot satisfy it); then the
    sink opens and every frame must complete: all twelve responses,
    each body the model's for its own request, per-core completion
    order matching per-core dispatch order.
    """
    watch = await setup(dut)
    await send_packets(dut, [build_load_key(0x0041, 3, KEY)])
    assert parse_response(await recv_packet(dut))["status"] == ST_OK

    count = 12
    want = {}
    pkts = []
    for n in range(count):
        req = 0x0200 + n
        nonce = bytes((i + n * 7) & 0xFF for i in range(12))
        msg = bytes((i * 13 + n) & 0xFF for i in range(40 + n))
        ct, tag = aead_encrypt(KEY, nonce, b"", msg)
        want[req] = tag + ct
        pkts.append(build_seal(req, 3, nonce, b"", msg))

    dut.m_tready.value = 0
    source = cocotb.start_soon(send_packets(dut, pkts))

    for _ in range(30000):
        await RisingEdge(dut.clk)
        if watch.max_stall >= 200:
            break
    assert watch.max_stall >= 200, (
        f"the dispatcher never stalled (longest wait {watch.max_stall} "
        f"cycles) with the sink closed and {count} seals offered: both "
        f"cores were meant to fill and refuse")

    dut.m_tready.value = 1
    raws = await recv_packets(dut, count, budget=120000)
    await source

    rsps = by_req_id(raws)
    assert sorted(rsps) == sorted(want), (
        f"sent {sorted(want)}, answered {sorted(rsps)}: a stalled frame "
        f"vanished or was invented")
    for req, body in want.items():
        assert rsps[req]["status"] == ST_OK, \
            f"{req:#06x} status {rsps[req]['status']}"
        assert rsps[req]["body"] == body, (
            f"{req:#06x} body differs from the model: a stalled frame "
            f"was corrupted on its way through")

    # Per-core completion order is per-core dispatch order: frames on
    # one core cannot overtake each other whatever the neighbour does.
    seal_records = watch.records[1:]
    assert len(seal_records) == count and set(seal_records) <= {"0", "1"}, \
        f"dispatch records {watch.records}"
    answered = [parse_response(r)["req_id"] for r in raws]
    for core in ("0", "1"):
        dispatched = [0x0200 + n for n, rec in enumerate(seal_records)
                      if rec == core]
        completed = [req for req in answered if req in set(dispatched)]
        assert completed == dispatched, (
            f"core {core} answered {completed}, dispatched {dispatched}: "
            f"responses of one core left out of order")
    assert int(dut.trouble.value) == 0, "trouble raised by clean traffic"
