# SPDX-License-Identifier: MIT
"""Adversarial tests against the four-stage oca_proto.

Written to break the design, not to confirm it. Everything here is
independent of oca/hw/sim/test_oca_core.py except for the packet
plumbing (send/recv/pipeline) and the reference cryptography.
"""

import random
import struct

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge

from aead_model import aead_encrypt
from proto_model import (OP_SEAL, ST_AUTH_FAIL, ST_BAD_LENGTH, ST_BAD_MAGIC,
                         ST_BAD_OPCODE, ST_BAD_SLOT, ST_BAD_VERSION, ST_OK,
                         build_load_key, build_open, build_seal, build_stats,
                         parse_response)
from test_oca_core import (KEY, KEY2, KEY3, NONCE, NONCE2, NONCE3, command,
                           counters, pipeline, recv_packet, recv_packets,
                           send_back_to_back, send_packet, send_word, setup,
                           words_of)


# ===========================================================================
# Structural monitors: properties no payload assertion can see.
# ===========================================================================

class Guard:
    def __init__(self):
        self.hits = []

    def fail(self, cycle, what):
        if len(self.hits) < 40:
            self.hits.append(f"cycle {cycle}: {what}")

    def check(self, label=""):
        assert not self.hits, f"{label}: " + " | ".join(self.hits)


async def watch_descriptor(dut, g: Guard, stats=None):
    """The descriptor a packet is judged on must not move while pending.

    oca_proto's header claims every field DRAIN judges a packet on is
    copied at the stage boundary and that "no other packet's stage can
    write it". pd_opcode/req_id/slot/status/crypto/engine are indeed
    written only in P_ENDREQ, which is guarded by !pd_valid. pd_tag is
    not: it is written in P_ARGS, by the *next* packet's parse, with no
    guard at all. This watches the register file itself, so it fires on
    any alignment the traffic happens to produce rather than on the one
    a hand-written packet sequence reaches.
    """
    p = dut.u_proto
    prev = None
    prev_valid = 0
    cycle = 0
    while True:
        await ReadOnly()
        v = int(p.pd_valid.value)
        cur = (int(p.pd_tag.value), int(p.pd_opcode.value),
               int(p.pd_status.value), int(p.pd_req_id.value),
               int(p.pd_slot.value), int(p.pd_crypto.value),
               int(p.pd_engine.value))
        if prev_valid and v and prev is not None and cur != prev:
            names = ("pd_tag", "pd_opcode", "pd_status", "pd_req_id",
                     "pd_slot", "pd_crypto", "pd_engine")
            moved = [n for n, a, b in zip(names, prev, cur) if a != b]
            g.fail(cycle, "descriptor field(s) " + ",".join(moved)
                   + " changed while pd_valid was still high")
        if stats is not None and v and not prev_valid:
            stats["offered"] = stats.get("offered", 0) + 1
        prev, prev_valid = cur, v
        await RisingEdge(dut.clk)
        cycle += 1


async def watch_pd_tag_margin(dut, margin: dict):
    """Record how many cycles of slack the pd_tag hazard actually has.

    Slack = (cycle PROC rewrites pd_tag) - (cycle DRAIN copied it). The
    design is safe only while this stays positive; nothing in the RTL
    enforces it.
    """
    p = dut.u_proto
    cycle = 0
    taken_at = None
    last_tag = None
    while True:
        await ReadOnly()
        v = int(p.pd_valid.value)
        # DRAIN copies the descriptor in the cycle pd_valid is high and
        # dcur_valid is low and no ciphertext block is arriving.
        took = (v and not int(p.dcur_valid.value)
                and not int(dut.u_aead.out_valid.value)
                and str(p.dr_state.value) == "000")   # D_IDLE
        tag = int(p.pd_tag.value)
        if took:
            taken_at = cycle
        if last_tag is not None and tag != last_tag and taken_at is not None:
            margin["min"] = min(margin.get("min", 1 << 30), cycle - taken_at)
            margin["n"] = margin.get("n", 0) + 1
            taken_at = None
        last_tag = tag
        await RisingEdge(dut.clk)
        cycle += 1


async def watch_response_regs(dut, g: Guard):
    """A response's header and shape must not move while TX ships it."""
    p = dut.u_proto
    prev = None
    prev_pending = 0
    cycle = 0
    while True:
        await ReadOnly()
        pending = int(p.rsp_pending.value)
        cur = (int(p.rsp_opcode.value), int(p.rsp_req_id.value),
               int(p.rsp_slot.value), int(p.rsp_status.value),
               int(p.rsp_body_len.value), int(p.rsp_extra.value),
               int(p.rsp_bank.value))
        if prev_pending and pending and prev is not None and cur != prev:
            g.fail(cycle, "published response registers moved mid-stream")
        prev, prev_pending = cur, pending
        await RisingEdge(dut.clk)
        cycle += 1


async def watch_engine_inputs(dut, obs: dict):
    """Does anything rewrite the engine's key/nonce/dec while it is busy?

    The overlap lets PROC parse packet N+1 and reach P_ARGS -- which
    writes eng_key, eng_nonce and eng_dec -- long before the engine has
    finished packet N. Safe only because chacha20_poly1305 latches
    key_r/nonce_r/dec_r on `start`. That is a cross-module invariant the
    single-packet design never leaned on and no test states.
    """
    p = dut.u_proto
    a = dut.u_aead
    cycle = 0
    prev = None
    while True:
        await ReadOnly()
        busy = int(a.busy.value)
        cur = (int(p.eng_key.value), int(p.eng_nonce.value),
               int(p.eng_dec.value))
        if busy and prev is not None and cur != prev:
            obs["rewrites"] = obs.get("rewrites", 0) + 1
            obs.setdefault("first", cycle)
        prev = cur
        await RisingEdge(dut.clk)
        cycle += 1


async def watch_tx_bank(dut, g: Guard):
    """The bank TX is reading must never be written by the next packet."""
    p = dut.u_proto
    cycle = 0
    while True:
        await ReadOnly()
        if int(p.rsp_pending.value) and int(p.tx_wr_en.value) \
                and int(p.tx_wr_bank.value) == int(p.rsp_bank.value):
            g.fail(cycle, "the transmit bank being streamed was written "
                          "by the packet behind it")
        if int(p.tx_wr_clear.value) and int(p.rsp_pending.value) \
                and int(p.tx_wr_bank.value) == int(p.rsp_bank.value):
            g.fail(cycle, "the transmit bank being streamed was cleared")
        await RisingEdge(dut.clk)
        cycle += 1


async def watch_rx_bank(dut, g: Guard):
    """The bank PROC is parsing must never be written or cleared by RX."""
    p = dut.u_proto
    cycle = 0
    while True:
        await ReadOnly()
        busy = str(p.pr_state.value) != "0000"     # not P_IDLE
        same = int(p.rx_wr_bank.value) == int(p.rx_rd_bank.value)
        if busy and same and (int(p.rx_wr_en.value)
                              or int(p.rx_wr_clear.value)):
            g.fail(cycle, "RX wrote or cleared the bank PROC was reading")
        await RisingEdge(dut.clk)
        cycle += 1


async def arm(dut, want=("desc", "resp", "txbank", "rxbank")):
    g = Guard()
    if "desc" in want:
        cocotb.start_soon(watch_descriptor(dut, g))
    if "resp" in want:
        cocotb.start_soon(watch_response_regs(dut, g))
    if "txbank" in want:
        cocotb.start_soon(watch_tx_bank(dut, g))
    if "rxbank" in want:
        cocotb.start_soon(watch_rx_bank(dut, g))
    return g


# ===========================================================================
# 1. The security property, attacked directly.
# ===========================================================================

@cocotb.test()
async def test_descriptor_integrity_under_stress(dut):
    """Random pipelined traffic, watching the descriptor register file.

    This is the mechanism behind "a failed tag returns no plaintext",
    not one of its symptoms: if pd_tag can move between PROC handing a
    descriptor over and DRAIN copying it, the tag a packet is judged
    against is the *successor's* bytes, and the payload assertions only
    catch it at the alignments they happen to hit.
    """
    g = await arm(dut)
    await setup(dut)
    rng = random.Random(0xA77AC)
    for slot, key in ((0, KEY), (1, KEY2), (2, KEY3)):
        await command(dut, build_load_key(slot, slot, key))

    for round_no in range(6):
        pkts = []
        for n in range(7):
            kind = rng.randrange(5)
            rid = (round_no << 8) | n
            slot = rng.randrange(3)
            key = (KEY, KEY2, KEY3)[slot]
            nonce = bytes(rng.getrandbits(8) for _ in range(12))
            mlen = rng.choice([0, 1, 24, 63, 64, 65, 128, 200, 320])
            alen = rng.choice([0, 8, 16, 64, 65])
            aad = bytes(rng.getrandbits(8) for _ in range(alen))
            msg = bytes(rng.getrandbits(8) for _ in range(mlen))
            ct, tag = aead_encrypt(key, nonce, aad, msg)
            if kind == 0:
                pkts.append(build_seal(rid, slot, nonce, aad, msg))
            elif kind == 1:
                pkts.append(build_open(rid, slot, nonce, aad, ct, tag))
            elif kind == 2:
                bad = bytes([tag[0] ^ 0x5A]) + tag[1:]
                pkts.append(build_open(rid, slot, nonce, aad, ct, bad))
            elif kind == 3:
                pkts.append(build_stats(rid))
            else:
                pkts.append(build_load_key(rid, slot, key))
        await pipeline(dut, pkts, gap=rng.randrange(0, 6),
                       stall_p=rng.choice([0.0, 0.3, 0.8]),
                       seed=0xD00D + round_no)
        g.check("descriptor integrity")
    g.check("descriptor integrity")


@cocotb.test()
async def test_pd_tag_slack_is_measured_not_guaranteed(dut):
    """Quantify the margin the pd_tag hazard survives on."""
    margin = {}
    cocotb.start_soon(watch_pd_tag_margin(dut, margin))
    await setup(dut)
    await command(dut, build_load_key(0, 0, KEY))

    msg = bytes(range(64)) * 3
    ct, tag = aead_encrypt(KEY, NONCE, b"", msg)
    bad = bytes([tag[0] ^ 1]) + tag[1:]
    succ = tag + bytes(64)
    for gap in range(0, 10):
        await pipeline(dut, [
            build_open(0x100 + gap, 0, NONCE, b"", ct, bad),
            build_seal(0x200 + gap, 0, NONCE2, b"", succ),
            build_stats(0x300 + gap),
        ], gap=gap, stall_p=0.0, seed=0x1234)
    dut._log.info(f"PDTAG_SLACK min={margin.get('min')} "
                  f"samples={margin.get('n')}")
    assert margin.get("min", 0) > 0, (
        f"pd_tag was rewritten before DRAIN copied it: {margin}")


@cocotb.test()
async def test_tag_swap_with_an_early_failing_successor(dut):
    """Slide the successor's parse across the predecessor's tag check.

    The published test uses a *seal* as the successor, which reaches
    P_ARGS through the same path every time. A successor that fails at
    P_DISPATCH (bad magic) never reaches P_ARGS and so leaves the drain
    free earlier, and one that fails at P_ARGS (bad length) reaches the
    pd_tag write and then stops -- different alignments of the same
    race. Both are tried behind a rejected open, with a third packet
    behind them carrying the right tag too.
    """
    g = await arm(dut)
    await setup(dut)
    await command(dut, build_load_key(0x01, 0, KEY))

    secret = b"NO-PLAINTEXT-OF-A-REJECTED-CIPHERTEXT-MAY-EVER-LEAVE"
    ct, tag = aead_encrypt(KEY, NONCE, b"", secret)
    bad = bytes([tag[0] ^ 0x01]) + tag[1:]

    # successors whose bytes 24..39 are exactly T
    carrier = tag + bytes(range(32))
    succ_seal = build_seal(0x0A, 0, NONCE2, b"", carrier)
    # bad magic, but the same bytes at 24..39
    succ_magic = bytearray(succ_seal)
    succ_magic[0] = 0x00
    # bad opcode
    succ_opcode = bytearray(succ_seal)
    succ_opcode[3] = 0x7F
    # a seal whose declared lengths do not match: fails at P_ARGS, after
    # pd_tag has already been written from its own bytes
    succ_len = bytearray(succ_seal)
    struct.pack_into("<H", succ_len, 22, len(carrier) + 8)
    # an open carrying T in its own tag field
    succ_open = build_open(0x0D, 0, NONCE2, b"", carrier, tag)

    variants = {
        "seal": bytes(succ_seal),
        "bad-magic": bytes(succ_magic),
        "bad-opcode": bytes(succ_opcode),
        "bad-length": bytes(succ_len),
        "open-with-T": succ_open,
    }
    for name, succ in variants.items():
        for gap in (0, 1, 2, 3, 4, 6, 9, 13, 18):
            for stall in (0.0, 0.5):
                pkts = [build_open(0x09, 0, NONCE, b"", ct, bad), succ,
                        build_stats(0x0F)]
                raws = await pipeline(dut, pkts, gap=gap, stall_p=stall,
                                      seed=0xBEEF + gap, raw=True)
                where = f"{name}, gap {gap}, stall {stall}"
                blob = b"".join(raws)
                assert secret not in blob, \
                    f"plaintext of a rejected ciphertext left the device ({where})"
                first = parse_response(raws[0])
                assert first["req_id"] == 0x09, f"out of order ({where})"
                assert first["body"] == b"", \
                    f"body {first['body']!r} for a failed tag ({where})"
                assert first["status"] == ST_AUTH_FAIL, \
                    f"status {first['status']} for a failed tag ({where})"
                g.check(where)


@cocotb.test()
async def test_response_cannot_start_before_its_tag_is_checked(dut):
    """No byte of a response may be offered before D_CHECK has run.

    Watches the output handshake against the drain state: for an open,
    the first beat of its response must come after the cycle its tag was
    compared. Store-and-forward is what the whole property rests on.
    """
    p = dut.u_proto
    g = Guard()
    seen = {"checks": 0, "publishes": 0}

    async def watch(dut):
        cycle = 0
        checked = True     # nothing in flight at reset
        while True:
            await ReadOnly()
            st = str(p.dr_state.value)
            if st == "011":                    # D_CHECK
                checked = True
                seen["checks"] += 1
            if st == "100":                    # D_PUBLISH
                if int(p.dcur_opcode.value) == 3 and int(p.dcur_crypto.value) \
                        and not checked:
                    g.fail(cycle, "an open was published without D_CHECK")
                seen["publishes"] += 1
                checked = False
            await RisingEdge(dut.clk)
            cycle += 1

    cocotb.start_soon(watch(dut))
    await setup(dut)
    await command(dut, build_load_key(0x02, 4, KEY))

    msg = bytes((i * 3) & 0xFF for i in range(300))
    ct, tag = aead_encrypt(KEY, NONCE, b"", msg)
    bad = bytes([tag[15] ^ 0xFF]) + tag[1:15] + tag[15:16]
    bad = tag[:15] + bytes([tag[15] ^ 0xFF])
    for stall in (0.0, 0.7):
        rsps = await pipeline(dut, [
            build_open(0x30, 4, NONCE, b"", ct, tag),
            build_open(0x31, 4, NONCE, b"", ct, bad),
            build_open(0x32, 4, NONCE, b"", ct, tag),
        ], stall_p=stall, seed=0x3131)
        assert [r["status"] for r in rsps] == [ST_OK, ST_AUTH_FAIL, ST_OK]
        assert rsps[1]["body"] == b""
    g.check("publish before check")
    assert seen["checks"] >= 6, f"D_CHECK never observed: {seen}"


# ===========================================================================
# 2. The key store, attacked.
# ===========================================================================

@cocotb.test()
async def test_rekey_cannot_reach_back_into_a_message_in_flight(dut):
    """A load-key behind a seal must not change the seal already started.

    The published suite proves the forward direction (a seal *behind* a
    load-key uses the new key). The dangerous direction is the other
    one: PROC latches eng_key in P_ARGS and then, under overlap, parses
    the next packet -- rewriting eng_key -- while the engine is still
    running the previous message. Nothing in oca_proto stops that; only
    chacha20_poly1305's key_r latch does.
    """
    obs = {}
    cocotb.start_soon(watch_engine_inputs(dut, obs))
    await setup(dut)
    await command(dut, build_load_key(0x10, 5, KEY))

    big = bytes((i * 13 + 7) & 0xFF for i in range(1200))
    small = b"after the rekey"
    want_ct, want_tag = aead_encrypt(KEY, NONCE, b"", big)
    want2_ct, want2_tag = aead_encrypt(KEY2, NONCE2, b"", small)
    wrong_ct, _ = aead_encrypt(KEY2, NONCE, b"", big)

    for gap in (0, 1, 3, 7, 15):
        rsps = await pipeline(dut, [
            build_seal(0x40, 5, NONCE, b"", big),
            build_load_key(0x41, 5, KEY2),
            build_seal(0x42, 5, NONCE2, b"", small),
        ], gap=gap, stall_p=0.0, seed=0x4040)
        where = f"gap {gap}"
        assert [r["req_id"] for r in rsps] == [0x40, 0x41, 0x42], where
        assert [r["status"] for r in rsps] == [ST_OK, ST_OK, ST_OK], \
            f"{[r['status'] for r in rsps]} ({where})"
        assert rsps[0]["body"] != want2_tag + wrong_ct, \
            f"the seal ahead of the re-key used the NEW key ({where})"
        assert rsps[0]["body"] == want_tag + want_ct, \
            f"the seal ahead of the re-key did not use the old key ({where})"
        assert rsps[2]["body"] == want2_tag + want2_ct, \
            f"the seal behind the re-key did not use the new key ({where})"
        # restore
        await command(dut, build_load_key(0x43, 5, KEY))
    dut._log.info(f"ENGINE_INPUT_REWRITES {obs}")


@cocotb.test()
async def test_two_messages_never_share_one_engine_start(dut):
    """One start per message, and never while the engine is busy.

    Two messages sharing a (key, nonce) derivation share (r, s), which
    is a total break of Poly1305 (Security.md section 4 item 3). The
    engine ignores `start` while busy, so the failure is silent: the
    second message's blocks are fed into the first message's state.
    """
    a = dut.u_aead
    g = Guard()

    async def watch(dut):
        cycle = 0
        open_msg = False
        prev_err = 0
        while True:
            await ReadOnly()
            start = int(a.start.value)
            if start:
                if int(a.busy.value):
                    g.fail(cycle, "start while busy")
                if open_msg:
                    g.fail(cycle, "start with a message still open")
                open_msg = True
            if int(a.done.value):
                if not open_msg:
                    g.fail(cycle, "done with no message open")
                open_msg = False
            err = int(a.err.value)
            if err and not prev_err:
                open_msg = False
            prev_err = err
            await RisingEdge(dut.clk)
            cycle += 1

    cocotb.start_soon(watch(dut))
    await setup(dut, monitor=False)
    for slot, key in ((0, KEY), (1, KEY2)):
        await command(dut, build_load_key(slot, slot, key))

    rng = random.Random(0x5747)
    for round_no in range(4):
        pkts = []
        for n in range(8):
            slot = n & 1
            key = KEY if slot == 0 else KEY2
            nonce = bytes(rng.getrandbits(8) for _ in range(12))
            msg = bytes(rng.getrandbits(8)
                        for _ in range(rng.choice([1, 64, 65, 192])))
            ct, tag = aead_encrypt(key, nonce, b"", msg)
            if n % 3 == 2:
                pkts.append(build_open(n, slot, nonce, b"", ct,
                                       bytes([tag[0] ^ 3]) + tag[1:]))
            else:
                pkts.append(build_seal(n, slot, nonce, b"", msg))
        await pipeline(dut, pkts, gap=round_no,
                       stall_p=0.4 if round_no & 1 else 0.0, seed=0x99 + round_no)
        g.check("engine ownership")
    g.check("engine ownership")


@cocotb.test()
async def test_load_key_for_the_slot_a_seal_is_using(dut):
    """Hammer the same slot from both sides at every alignment."""
    g = await arm(dut)
    await setup(dut)
    await command(dut, build_load_key(0x20, 7, KEY))

    m = bytes((i * 3 + 1) & 0xFF for i in range(256))
    probe = b"which key is in slot 7 now?"
    for gap in range(0, 8):
        ct_a, tag_a = aead_encrypt(KEY, NONCE, b"", m)
        ct_b, tag_b = aead_encrypt(KEY3, NONCE3, b"", probe)
        rsps = await pipeline(dut, [
            build_seal(0x50, 7, NONCE, b"", m),
            build_load_key(0x51, 7, KEY3),
            build_seal(0x52, 7, NONCE3, b"", probe),
            build_load_key(0x53, 7, KEY),
            build_seal(0x54, 7, NONCE, b"", m),
        ], gap=gap, stall_p=0.0, seed=0x5050 + gap)
        where = f"gap {gap}"
        assert [r["req_id"] for r in rsps] == [0x50, 0x51, 0x52, 0x53, 0x54], where
        assert rsps[0]["body"] == tag_a + ct_a, f"seal before rekey ({where})"
        assert rsps[2]["body"] == tag_b + ct_b, f"seal after rekey ({where})"
        assert rsps[4]["body"] == tag_a + ct_a, f"seal after restore ({where})"
        g.check(where)


# ===========================================================================
# 3. Ordering.
# ===========================================================================

@cocotb.test()
async def test_ordering_across_every_failure_class(dut):
    """Responses leave in arrival order whatever each packet costs.

    A header error costs three cycles and a 1400-byte seal costs
    hundreds; the only thing making the short one wait is that DRAIN is
    the single publication point. Every failure class is mixed in, at
    every gap, with the sink stalling.
    """
    g = await arm(dut)
    await setup(dut)
    await command(dut, build_load_key(0x30, 0, KEY))

    big = bytes((i * 9) & 0xFF for i in range(1400))
    big_ct, big_tag = aead_encrypt(KEY, NONCE, b"", big)
    small = b"x" * 16
    small_ct, small_tag = aead_encrypt(KEY, NONCE2, b"", small)

    def mangle(pkt, index, value):
        b = bytearray(pkt)
        b[index] = value
        return bytes(b)

    seal_big = build_seal(0x60, 0, NONCE, b"", big)
    bad_magic = mangle(build_seal(0x61, 0, NONCE2, b"", small), 1, 0xFF)
    seal_small = build_seal(0x62, 0, NONCE2, b"", small)
    bad_ver = mangle(build_seal(0x63, 0, NONCE2, b"", small), 2, 0x09)
    bad_op = mangle(build_seal(0x64, 0, NONCE2, b"", small), 3, 0x66)
    bad_slot = build_seal(0x65, 200, NONCE2, b"", small)
    bad_open = build_open(0x66, 0, NONCE, b"", big_ct,
                          bytes([big_tag[0] ^ 1]) + big_tag[1:])
    bad_len = bytearray(build_seal(0x67, 0, NONCE2, b"", small))
    struct.pack_into("<H", bad_len, 22, len(small) + 4)
    stats = build_stats(0x68)
    good_open = build_open(0x69, 0, NONCE2, b"", small_ct, small_tag)

    order = [seal_big, bad_magic, seal_small, bad_ver, bad_op, bad_slot,
             bad_open, bytes(bad_len), stats, good_open]
    want_ids = [0x60, 0x61, 0x62, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69]
    want_st = [ST_OK, ST_BAD_MAGIC, ST_OK, ST_BAD_VERSION, ST_BAD_OPCODE,
               ST_BAD_SLOT, ST_AUTH_FAIL, ST_BAD_LENGTH, ST_OK, ST_OK]

    for gap in (0, 1, 2, 5, 11):
        for stall in (0.0, 0.6, 0.95):
            rsps = await pipeline(dut, order, gap=gap, stall_p=stall,
                                  seed=0x6060 + gap)
            where = f"gap {gap}, stall {stall}"
            assert [r["req_id"] for r in rsps] == want_ids, \
                f"order {[hex(r['req_id']) for r in rsps]} ({where})"
            assert [r["status"] for r in rsps] == want_st, \
                f"statuses {[r['status'] for r in rsps]} ({where})"
            assert rsps[0]["body"] == big_tag + big_ct, f"big seal ({where})"
            assert rsps[6]["body"] == b"", f"failed open body ({where})"
            assert rsps[9]["body"] == small, f"good open body ({where})"
            g.check(where)


@cocotb.test()
async def test_short_packet_between_two_crypto_commands(dut):
    """The one packet that gets no answer must not shift the others."""
    g = await arm(dut)
    await setup(dut)
    await command(dut, build_load_key(0x40, 0, KEY))

    m = b"a message that must come back whole" * 3
    ct, tag = aead_encrypt(KEY, NONCE, b"", m)
    for runt_len in (0, 1, 7):
        for gap in (0, 2, 6):
            runt = bytes(range(runt_len))
            pkts = [build_seal(0x70, 0, NONCE, b"", m)]
            if runt:
                pkts.append(runt)
            pkts.append(build_open(0x71, 0, NONCE, b"", ct, tag))
            pkts.append(build_stats(0x72))
            nresp = 3
            rsps = await pipeline(dut, pkts, nresp=nresp, gap=gap,
                                  stall_p=0.3, seed=0x7070)
            where = f"runt {runt_len}, gap {gap}"
            assert [r["req_id"] for r in rsps] == [0x70, 0x71, 0x72], where
            assert [r["status"] for r in rsps] == [ST_OK, ST_OK, ST_OK], where
            assert rsps[1]["body"] == m, f"plaintext ({where})"
            g.check(where)


# ===========================================================================
# 4. Constant time.
# ===========================================================================

async def timed_pair(dut, first, second, stall_p=0.0):
    """Cycles from the last beat of `second` going in to its response's
    last beat coming out. `first` precedes it back to back."""
    cyc = {"n": 0}

    async def tick():
        while True:
            await RisingEdge(dut.clk)
            cyc["n"] += 1

    t = cocotb.start_soon(tick())
    out = []
    sink = cocotb.start_soon(recv_packets(dut, 2))
    marks = []

    async def src():
        for n, pkt in enumerate((first, second)):
            beats = words_of(pkt)
            for i, (data, keep) in enumerate(beats):
                await send_word(dut, data, keep, i == len(beats) - 1)
            marks.append(cyc["n"])
        dut.s_axis_tvalid.value = 0
        dut.s_axis_tlast.value = 0

    await src()
    got = await sink
    end = cyc["n"]
    t.kill()
    out = [parse_response(r) for r in got]
    return end - marks[1], out


@cocotb.test()
async def test_latency_does_not_depend_on_a_predecessors_data(dut):
    """Packet N+1's latency must not move with packet N's *content*.

    Same opcode, same lengths, same slot -- only the bytes differ. Any
    difference here is a data-dependent timing channel that spans
    packets, which is what the overlap newly makes possible.
    """
    await setup(dut, monitor=False)
    await command(dut, build_load_key(0x50, 0, KEY))

    probe = build_seal(0x81, 0, NONCE2, b"", b"probe" * 12)
    payloads = {
        "zeros": bytes(512),
        "ones": bytes([0xFF]) * 512,
        "random": bytes(random.Random(1).getrandbits(8) for _ in range(512)),
        "sparse": bytes(([0x00] * 511) + [0x01]),
    }
    lat = {}
    for name, pay in payloads.items():
        d, rsps = await timed_pair(
            dut, build_seal(0x80, 0, NONCE, b"", pay), probe)
        assert [r["status"] for r in rsps] == [ST_OK, ST_OK]
        lat[name] = d
    dut._log.info(f"LATENCY_BY_PREDECESSOR_CONTENT {lat}")
    assert len(set(lat.values())) == 1, \
        f"predecessor content changed the successor's latency: {lat}"

    # the same for an open whose ciphertext differs but whose tag is good
    m1, m2 = bytes(256), bytes([0xA5]) * 256
    c1, t1 = aead_encrypt(KEY, NONCE, b"", m1)
    c2, t2 = aead_encrypt(KEY, NONCE, b"", m2)
    d1, r1 = await timed_pair(dut, build_open(0x82, 0, NONCE, b"", c1, t1), probe)
    d2, r2 = await timed_pair(dut, build_open(0x83, 0, NONCE, b"", c2, t2), probe)
    assert r1[0]["status"] == ST_OK and r2[0]["status"] == ST_OK
    dut._log.info(f"LATENCY_OPEN_CONTENT {d1} vs {d2}")
    assert d1 == d2, f"open content changed the successor's latency: {d1} {d2}"


@cocotb.test()
async def test_tag_outcome_timing_residual_is_bounded(dut):
    """Measure the residual the module header admits to.

    A failed tag shortens the response, which shifts the packet behind
    it. The header argues the shift carries no information status 06 does
    not already publish. That argument only holds if the shift is
    explained entirely by the response being shorter: if the *check
    itself* took a different number of cycles, an observer would learn
    the outcome before the status byte arrives.
    """
    await setup(dut, monitor=False)
    await command(dut, build_load_key(0x60, 0, KEY))

    probe = build_seal(0x91, 0, NONCE2, b"", b"probe" * 12)
    msg = bytes((i * 5) & 0xFF for i in range(256))
    ct, tag = aead_encrypt(KEY, NONCE, b"", msg)
    bad = tag[:15] + bytes([tag[15] ^ 0x01])

    d_ok, r_ok = await timed_pair(dut, build_open(0x90, 0, NONCE, b"", ct, tag),
                                  probe)
    d_no, r_no = await timed_pair(dut, build_open(0x90, 0, NONCE, b"", ct, bad),
                                  probe)
    assert r_ok[0]["status"] == ST_OK and r_no[0]["status"] == ST_AUTH_FAIL
    beats_ok = (8 + len(msg) + 7) // 8
    beats_no = 1
    dut._log.info(f"TAG_RESIDUAL ok={d_ok} fail={d_no} delta={d_ok - d_no} "
                  f"beats_ok={beats_ok} beats_fail={beats_no} "
                  f"beat_delta={beats_ok - beats_no}")
    assert d_ok - d_no == beats_ok - beats_no, (
        "the timing difference between a passing and a failing tag is not "
        f"explained by the response length alone: {d_ok - d_no} cycles vs "
        f"{beats_ok - beats_no} beats")


# ===========================================================================
# 5. Things the report is silent about.
# ===========================================================================

@cocotb.test()
async def test_sink_that_never_accepts_does_not_wedge_the_device(dut):
    """Hold tready low long enough to back the whole pipeline up, then
    release: every response must still come out, in order, intact."""
    await setup(dut)
    await command(dut, build_load_key(0x70, 0, KEY))

    msgs = [bytes((i * (n + 2)) & 0xFF for i in range(128 * (n + 1)))
            for n in range(5)]
    want = [aead_encrypt(KEY, NONCE, b"", m) for m in msgs]
    pkts = [build_seal(0xA0 + n, 0, NONCE, b"", m) for n, m in enumerate(msgs)]

    dut.m_axis_tready.value = 0
    sink = cocotb.start_soon(recv_packets(dut, len(pkts), budget=200000))
    src = cocotb.start_soon(send_back_to_back(dut, pkts))
    for _ in range(3000):
        await RisingEdge(dut.clk)
    dut.m_axis_tready.value = 1
    await src
    raws = await sink
    rsps = [parse_response(r) for r in raws]
    assert [r["req_id"] for r in rsps] == [0xA0 + n for n in range(len(pkts))]
    for n, r in enumerate(rsps):
        ct, tag = want[n]
        assert r["status"] == ST_OK, f"#{n} status {r['status']}"
        assert r["body"] == tag + ct, f"#{n} body mismatch after a long stall"


@cocotb.test()
async def test_engine_error_does_not_strand_the_pipeline(dut):
    """A packet whose engine run aborts must still be answered, in order,
    and must not keep the token."""
    g = await arm(dut)
    await setup(dut)
    await command(dut, build_load_key(0x80, 0, KEY))
    # There is no legal way to make in_len exceed 64 from the wire, so
    # this only proves the healthy path is unaffected; the check is that
    # ST_ENGINE_ERR never appears spuriously under overlap.
    m = bytes((i * 17) & 0xFF for i in range(600))
    ct, tag = aead_encrypt(KEY, NONCE, b"", m)
    for gap in (0, 1, 4):
        rsps = await pipeline(dut, [
            build_seal(0xB0, 0, NONCE, b"", m),
            build_open(0xB1, 0, NONCE, b"", ct, tag),
            build_stats(0xB2),
            build_seal(0xB3, 0, NONCE2, b"", b""),
        ], gap=gap, stall_p=0.5, seed=0xB0B1)
        assert [r["status"] for r in rsps] == [ST_OK, ST_OK, ST_OK, ST_OK], \
            f"{[r['status'] for r in rsps]} at gap {gap}"
        assert rsps[1]["body"] == m
        g.check(f"gap {gap}")


@cocotb.test()
async def test_stats_counters_are_not_corrupted_by_the_descriptor_race(dut):
    """pd_tag doubles as the stats field. If a successor's parse can move
    it, a stats response reports another packet's tag bytes as counters."""
    await setup(dut)
    await command(dut, build_load_key(0x90, 0, KEY))
    base = counters(await command(dut, build_stats(0xC0)))

    m = bytes(128)
    ct, tag = aead_encrypt(KEY, NONCE, b"", m)
    for gap in range(0, 8):
        pkts = [build_seal(0xC1, 0, NONCE, b"", m),
                build_stats(0xC2),
                build_open(0xC3, 0, NONCE, b"", ct, tag),
                build_stats(0xC4)]
        rsps = await pipeline(dut, pkts, gap=gap, stall_p=0.0, seed=0xC0C0)
        a = counters(rsps[1])
        b = counters(rsps[3])
        assert a["rx"] < b["rx"], f"rx counter went backwards at gap {gap}: {a} {b}"
        assert b["rx"] - a["rx"] == 2, \
            f"rx moved by {b['rx'] - a['rx']} over two packets at gap {gap}"
        assert b["done"] >= a["done"], f"done went backwards at gap {gap}"
        assert a["drop"] == base["drop"], f"spurious drop at gap {gap}: {a}"
        assert b["auth"] == base["auth"], f"spurious auth fail at gap {gap}: {b}"


@cocotb.test()
async def test_zero_length_and_aad_only_under_overlap(dut):
    """Degenerate shapes, pipelined against full-size neighbours."""
    g = await arm(dut)
    await setup(dut)
    await command(dut, build_load_key(0xA0, 0, KEY))

    cases = [(b"", b""), (b"", b"x"), (b"a" * 64, b""), (b"a" * 65, b""),
             (b"a" * 63, b"m" * 1), (b"", b"m" * 64), (b"a" * 16, b"m" * 65)]
    pkts, want = [], []
    for n, (aad, msg) in enumerate(cases):
        nonce = bytes((i + n * 3) & 0xFF for i in range(12))
        ct, tag = aead_encrypt(KEY, nonce, aad, msg)
        pkts.append(build_seal(0xD0 + n, 0, nonce, aad, msg))
        want.append(tag + ct)
        pkts.append(build_open(0xE0 + n, 0, nonce, aad, ct, tag))
        want.append(msg)
    for gap in (0, 1, 3):
        rsps = await pipeline(dut, pkts, gap=gap, stall_p=0.4, seed=0xD0D0)
        for n, r in enumerate(rsps):
            assert r["status"] == ST_OK, \
                f"#{n} status {r['status']} at gap {gap}"
            assert r["body"] == want[n], f"#{n} body at gap {gap}"
        g.check(f"gap {gap}")
