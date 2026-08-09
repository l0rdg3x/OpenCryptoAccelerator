# SPDX-License-Identifier: MIT
"""Tests for oca_udp_seam, driven where udp_complete_64 would stand.

The seam's whole job is that reply k carries requester k's address, so the
drivers here are worth more than the assertions: a permissive driver that
hands the seam a stream udp_complete_64 could never produce proves nothing
about the design that has to survive udp_complete_64.

What the receive driver refuses, and where each refusal comes from:

  * it never presents a header while the previous one is still unaccepted.
    udp_ip_rx_64.v:287 makes s_ip_hdr_ready depend on !m_udp_hdr_valid, so
    the stack physically cannot load a second header over a pending one, and
    a driver that did would hide the seam accepting headers out of order.

  * it holds every header field stable from the cycle hdr_valid rises to the
    cycle it is taken (:273 holds the same registers), and it holds tdata,
    tkeep, tlast and tuser stable across a low tready. A self-check watches
    both, so the driver cannot quietly stop being a legal master.

  * it never gates the payload on hdr_ready. This is the one the design
    exists for: m_udp_hdr_ready appears nowhere in udp_ip_rx_64's payload
    states (:325-409), so a whole packet including tlast crosses whether or
    not its header has been taken.

  * it raises the next packet's header while the current packet's final beat
    is still on the bus, which is the real overlap: udp_ip_rx_64 returns to
    STATE_IDLE on the payload tlast it sees at its own input (:351), while
    its two output registers (:476-486) may still be holding the last beat
    or two on the way to the seam.

  * it leads the payload by at least one cycle, never zero. m_udp_hdr_valid
    is registered at the edge that ends STATE_READ_HEADER (:309) and the
    first payload beat can only reach the output register one edge later, so
    a header arriving with its own first beat is not a stream the stack
    produces.

What the transmit monitor refuses:

  * s_udp_payload_axis_tready is `s_udp_payload_fifo_tready &&
    shift_payload_in` (udp_checksum_gen_64.v:250) and shift_payload_in is set
    only in STATE_SUM_PAYLOAD, two states after the header handshake leaves
    IDLE. So no payload beat can be taken in the handshake cycle or the one
    after it, and the monitor holds tready low over exactly that window
    rather than accepting whatever it is offered.

  * it holds s_udp_hdr_ready low while a response is in flight, because the
    generator is not in IDLE then, and it can hold it low for thousands of
    cycles on demand: ip_64 blocks in STATE_ARP_QUERY until an ARP reply and
    that timeout is about 30 s.

  * it fails the test if a payload beat is offered before the header
    handshake, if a second header is offered inside one response, or if a
    header handshake is not followed by a tlast within a budget -- the wedge
    udp_checksum_gen_64 has no timeout and no error output for.
"""

import os

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import Event, ReadOnly, RisingEdge

from aead_model import aead_encrypt
from proto_model import (OP_STATS, ST_OK, build_load_key, build_seal,
                         build_stats, parse_response)
from run_udp_seam import HDR_Q_DEPTH, LOCAL_IP, LOCAL_PORT, REPLY_TTL

KEY = bytes(range(32))
NONCE = bytes(range(12))

# oca_pktbuf zeroes both banks one word per cycle out of reset and oca_core
# holds s_axis_tready low until it is done.
BYTES = 2048
CLEAR_CYCLES = 2 * (BYTES // 8)
MAX_REQUEST = BYTES - 8

# The largest UDP payload an MTU-1500 frame carries: 1500 - 20 - 8. The seam
# imposes no limit of its own, which is why the maximum the core accepts is
# also exercised even though no unfragmented frame could deliver it.
MTU_PAYLOAD = 1472

DEPTH = int(os.environ.get("OCA_SEAM_DEPTH", str(HDR_Q_DEPTH)))

PEER_A = (0x0A000001, 40001)
PEER_B = (0x0A000002, 40002)
PEER_C = (0xC0A80101, 55555)


def beats_of(pkt: bytes):
    """Split a payload into beats, only the last one partial.

    udp_ip_rx_64 masks tkeep solely where word_count_reg <= 8 (:342), so a
    partial beat anywhere but the end is not a stream the stack emits.
    """
    out = []
    for off in range(0, len(pkt), 8):
        chunk = pkt[off:off + 8]
        out.append((int.from_bytes(chunk.ljust(8, b"\x00"), "little"),
                    (1 << len(chunk)) - 1))
    return out


class Req:
    """One datagram as the stack would hand it over.

    `raw` replaces the derived beats with explicit (tdata, tkeep) pairs, which
    is the only way to present the shapes a well-formed packet cannot have:
    a datagram whose UDP length field is 8 arrives as a single tlast beat with
    tkeep == 8'h00 (udp_ip_rx_64.v:342 masking against a zero word_count).
    """

    def __init__(self, pkt=b"", peer=PEER_A, dst_port=LOCAL_PORT, raw=None,
                 tuser_last=False):
        self.pkt = pkt
        self.src_ip, self.src_port = peer
        self.dst_port = dst_port
        self.raw = raw
        self.tuser_last = tuser_last

    def beats(self):
        raw = self.raw if self.raw is not None else beats_of(self.pkt)
        assert raw, "a datagram with no payload beat is not something the stack emits"
        return [(d, k, n == len(raw) - 1,
                 self.tuser_last and n == len(raw) - 1)
                for n, (d, k) in enumerate(raw)]


class RxDriver:
    """The stack's m_udp_* side, with the stack's own refusals."""

    def __init__(self, dut, hdr_lead: int = 2, gap: int = 0):
        assert hdr_lead >= 1, (
            "m_udp_hdr_valid is registered one edge before the first payload "
            "beat can reach the output register (udp_ip_rx_64.v:309): a lead "
            "of 0 is not a stream the stack produces")
        self.dut = dut
        self.hdr_lead = hdr_lead
        self.gap = gap
        self.hdrs_taken = 0
        self.failures = []

    def fail(self, what):
        self.failures.append(what)

    def check(self):
        assert not self.failures, "rx driver: " + "; ".join(self.failures)

    def idle(self):
        d = self.dut
        d.rx_hdr_valid.value = 0
        d.rx_ip_source_ip.value = 0
        d.rx_source_port.value = 0
        d.rx_dest_port.value = 0
        d.rx_payload_tdata.value = 0
        d.rx_payload_tkeep.value = 0
        d.rx_payload_tvalid.value = 0
        d.rx_payload_tlast.value = 0
        d.rx_payload_tuser.value = 0

    async def run(self, reqs):
        self._hdr_up = [Event() for _ in reqs]
        self._hdr_ok = [Event() for _ in reqs]
        if reqs:
            self._hdr_ok[0].set()
        h = cocotb.start_soon(self._hdr_loop(reqs))
        p = cocotb.start_soon(self._pay_loop(reqs))
        await h
        await p

    async def _hdr_loop(self, reqs):
        d = self.dut
        for i, r in enumerate(reqs):
            await self._hdr_ok[i].wait()
            d.rx_ip_source_ip.value = r.src_ip
            d.rx_source_port.value = r.src_port
            d.rx_dest_port.value = r.dst_port
            d.rx_hdr_valid.value = 1
            self._hdr_up[i].set()
            held = (r.src_ip, r.src_port, r.dst_port)
            while True:
                await ReadOnly()
                now = (int(d.rx_ip_source_ip.value), int(d.rx_source_port.value),
                       int(d.rx_dest_port.value))
                if now != held:
                    self.fail("header fields moved while unaccepted "
                              "(udp_ip_rx_64.v:273 holds them)")
                taken = (d.rx_hdr_valid.value == 1 and d.rx_hdr_ready.value == 1)
                await RisingEdge(d.clk)
                if taken:
                    break
            d.rx_hdr_valid.value = 0
            self.hdrs_taken += 1
            # The stack needs at least the cycles to read the next frame's IP
            # header before it can raise this again; one is the floor.
            await RisingEdge(d.clk)

    async def _pay_loop(self, reqs):
        d = self.dut
        for i, r in enumerate(reqs):
            await self._hdr_up[i].wait()
            for _ in range(self.hdr_lead):
                await RisingEdge(d.clk)
            for data, keep, last, user in r.beats():
                if last and i + 1 < len(reqs):
                    self._hdr_ok[i + 1].set()
                await self._beat(data, keep, last, user)
            d.rx_payload_tvalid.value = 0
            d.rx_payload_tlast.value = 0
            d.rx_payload_tuser.value = 0
            for _ in range(self.gap):
                await RisingEdge(d.clk)

    async def _beat(self, data, keep, last, user):
        d = self.dut
        d.rx_payload_tdata.value = data
        d.rx_payload_tkeep.value = keep
        d.rx_payload_tlast.value = 1 if last else 0
        d.rx_payload_tuser.value = 1 if user else 0
        d.rx_payload_tvalid.value = 1
        while True:
            await ReadOnly()
            if (int(d.rx_payload_tdata.value) != data
                    or int(d.rx_payload_tkeep.value) != keep
                    or (d.rx_payload_tlast.value == 1) != last):
                self.fail("payload beat moved while tready was low")
            ready = d.rx_payload_tready.value == 1
            await RisingEdge(d.clk)
            if ready:
                return


class TxMonitor:
    """The stack's s_udp_* side, with the stack's own refusals."""

    PAY_BUDGET = 400_000

    def __init__(self, dut):
        self.dut = dut
        self.replies = []
        self.failures = []
        self.hdr_stall = 0
        self.hdr_handshakes = 0

    def fail(self, what):
        self.failures.append(what)

    def check(self):
        assert not self.failures, "tx monitor: " + "; ".join(self.failures)

    async def run(self):
        d = self.dut
        d.tx_hdr_ready.value = 0
        d.tx_payload_tready.value = 0
        state = "WAIT"
        age = 0
        cur = None
        buf = bytearray()
        while True:
            if state == "WAIT":
                d.tx_hdr_ready.value = 0 if self.hdr_stall > 0 else 1
                d.tx_payload_tready.value = 0
            else:
                d.tx_hdr_ready.value = 0
                d.tx_payload_tready.value = 1 if age >= 2 else 0

            await ReadOnly()
            hv = d.tx_hdr_valid.value == 1
            hr = d.tx_hdr_ready.value == 1
            pv = d.tx_payload_tvalid.value == 1
            pr = d.tx_payload_tready.value == 1
            took_hdr = False
            done = False

            if state == "WAIT":
                if pv:
                    self.fail("a payload beat was offered before the header "
                              "handshake: udp_checksum_gen_64 is still in IDLE "
                              "and has nothing to attach it to")
                if hv and hr:
                    cur = {
                        "dest_ip": int(d.tx_ip_dest_ip.value),
                        "dest_port": int(d.tx_dest_port.value),
                        "source_ip": int(d.tx_ip_source_ip.value),
                        "source_port": int(d.tx_source_port.value),
                        "ttl": int(d.tx_ip_ttl.value),
                        "dscp": int(d.tx_ip_dscp.value),
                        "ecn": int(d.tx_ip_ecn.value),
                    }
                    took_hdr = True
                    self.hdr_handshakes += 1
            else:
                if hv:
                    self.fail("a second header was offered inside one response; "
                              "the generator takes exactly one per tlast")
                if pv and pr:
                    if d.tx_payload_tuser.value == 1:
                        self.fail(
                            "a reply beat carried tuser: on hardware that is "
                            "the bad-frame marker, so axis_gmii_tx.v:312 "
                            "corrupts the FCS deliberately and the peer's NIC "
                            "discards the reply with nothing counting it")
                    keep = int(d.tx_payload_tkeep.value)
                    raw = int(d.tx_payload_tdata.value).to_bytes(8, "little")
                    n = keep.bit_count()
                    if raw[n:] != bytes(8 - n):
                        self.fail(f"reply beat leaks {8 - n} bytes past tkeep")
                    buf += raw[:n]
                    done = d.tx_payload_tlast.value == 1

            await RisingEdge(d.clk)
            if self.hdr_stall > 0:
                self.hdr_stall -= 1

            if state == "WAIT":
                if took_hdr:
                    state = "PAY"
                    age = 0
                    buf = bytearray()
            else:
                age += 1
                if done:
                    cur["payload"] = bytes(buf)
                    self.replies.append(cur)
                    state = "WAIT"
                elif age > self.PAY_BUDGET:
                    self.fail("a header was handshaken and no tlast followed: "
                              "udp_checksum_gen_64 waits for one forever")
                    state = "WAIT"


async def setup(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    drv = RxDriver(dut)
    drv.idle()
    dut.tx_hdr_ready.value = 0
    dut.tx_payload_tready.value = 0
    dut.rst_n.value = 0
    for _ in range(5):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    mon = TxMonitor(dut)
    cocotb.start_soon(mon.run())
    for _ in range(CLEAR_CYCLES + 8):
        await RisingEdge(dut.clk)
    return drv, mon


async def wait_replies(dut, mon, n, budget=400_000):
    for _ in range(budget):
        if len(mon.replies) >= n:
            return mon.replies
        await RisingEdge(dut.clk)
    raise AssertionError(f"only {len(mon.replies)} of {n} replies in {budget} cycles")


async def settle(dut, cycles=400):
    for _ in range(cycles):
        await RisingEdge(dut.clk)


def counters(dut) -> dict:
    return {
        "accepted": int(dut.cnt_accepted.value),
        "short": int(dut.cnt_drop_short.value),
        "port": int(dut.cnt_drop_port.value),
        "full": int(dut.cnt_drop_full.value),
        "nohdr": int(dut.cnt_drop_nohdr.value),
        "tuser": int(dut.cnt_tuser.value),
        "orphan": int(dut.cnt_resp_orphan.value),
        "watermark": int(dut.hdr_q_watermark.value),
    }


async def wait_drained(dut, mon, budget=400_000, quiet=5_000):
    """Wait until every admitted request has been answered and stays that way.

    Which requests were admitted is not known in advance once the queue guard
    starts refusing, so the suite waits for the seam's own accounting to hold
    still rather than for a count it assumed.
    """
    stable = 0
    for _ in range(budget):
        if len(mon.replies) == int(dut.cnt_accepted.value):
            stable += 1
            if stable >= quiet:
                return mon.replies
        else:
            stable = 0
        await RisingEdge(dut.clk)
    raise AssertionError(
        f"{len(mon.replies)} replies against {int(dut.cnt_accepted.value)} "
        f"accepted after {budget} cycles")


def check_replies(reps, sent):
    """Every reply carries its own requester, and they leave in order.

    Correlating on req_id rather than on position is what keeps this honest
    when the queue guard refuses some of the requests: the admitted set is
    then not a prefix of what was sent, and a positional check would either
    fail on a correct design or pass on a misaddressed one.
    """
    ids = [rid for rid, _ in sent]
    peers = dict(sent)
    pos = -1
    for n, rep in enumerate(reps):
        rsp = parse_response(rep["payload"])
        rid = rsp["req_id"]
        assert rid in peers, f"reply {n} carries an unknown req_id {rid:#x}"
        idx = ids.index(rid)
        assert idx > pos, (
            f"reply {n} (req_id {rid:#x}) left the seam out of admission order")
        pos = idx
        assert (rep["dest_ip"], rep["dest_port"]) == peers[rid], (
            f"reply for {rid:#x} went to {rep['dest_ip']:#x}:"
            f"{rep['dest_port']} instead of {peers[rid][0]:#x}:{peers[rid][1]}")
        check_reply_is_ours(rep)


def check_reply_is_ours(rep):
    assert rep["source_ip"] == LOCAL_IP, (
        f"reply source IP {rep['source_ip']:#x} != LOCAL_IP {LOCAL_IP:#x}; "
        "ip_64's local_ip input is dead (ip_64.v:143) and udp_64.v:347 copies "
        "this straight into the header and the checksum")
    assert rep["source_port"] == LOCAL_PORT, "reply source port is not ours"
    assert rep["ttl"] == REPLY_TTL, "reply TTL is not the configured one"


async def load_key(dut, drv, mon, peer=PEER_A):
    await drv.run([Req(build_load_key(0x0001, 0, KEY), peer=peer)])
    reps = await wait_replies(dut, mon, 1)
    rsp = parse_response(reps[0]["payload"])
    assert rsp["status"] == ST_OK, f"load_key status {rsp['status']}"
    return reps


@cocotb.test(timeout_time=400, timeout_unit="us")
async def test_the_board_has_one_address_and_not_two(dut):
    """stack_local_ip must be the address the replies actually carry.

    The stack's local_ip is not a spare input: arp.v:197 sends it as the
    sender protocol address and arp.v:305 tests it to decide which ARP
    requests the board answers at all. If a top level wired that from a
    constant of its own while the seam replied from LOCAL_IP, the board
    would answer ARP for one address and reply from another. Requests would
    arrive and be processed, every reply would be discarded by the peer's
    UDP layer for matching no socket, and no counter here would move,
    because from the seam's point of view nothing went wrong.

    So the seam publishes the address and the top wires the stack from it.
    This test is what keeps the two the same value.
    """
    drv, mon = await setup(dut)
    published = int(dut.stack_local_ip.value)
    assert published == LOCAL_IP, (
        f"stack_local_ip is {published:#x}, LOCAL_IP is {LOCAL_IP:#x}: the "
        "stack would answer ARP for an address our replies never use")

    await drv.run([Req(build_stats(0x00A1), peer=PEER_A)])
    reps = await wait_replies(dut, mon, 1)
    await settle(dut)
    drv.check()
    mon.check()

    assert len(reps) == 1, f"{len(reps)} replies for one request"
    assert reps[0]["source_ip"] == published, (
        f"reply came from {reps[0]['source_ip']:#x} while the stack is told "
        f"to be {published:#x}; a peer would discard it")


@cocotb.test(timeout_time=400, timeout_unit="us")
async def test_a_short_datagram_does_not_steal_the_next_reply(dut):
    """A 4-byte datagram, then two requests from different peers.

    This is the failure the seam is built around. oca_proto answers nothing at
    all when rx_rd_count < HDR_LEN (oca_proto.sv:779-785), so a 4-byte
    datagram whose header had been enqueued would leave that header at the
    head of the queue for good and every later reply would go to the peer
    before it. Both replies below must carry their own peer.
    """
    drv, mon = await setup(dut)
    reqs = [
        Req(raw=[(0xDEADBEEF, 0x0F)], peer=PEER_C),
        Req(build_stats(0x0011), peer=PEER_A),
        Req(build_stats(0x0022), peer=PEER_B),
    ]
    await drv.run(reqs)
    reps = await wait_replies(dut, mon, 2)
    await settle(dut)
    drv.check()
    mon.check()

    assert len(reps) == 2, f"{len(reps)} replies for two answerable requests"
    for rep, peer, req_id in ((reps[0], PEER_A, 0x0011), (reps[1], PEER_B, 0x0022)):
        rsp = parse_response(rep["payload"])
        assert rsp["req_id"] == req_id, (
            f"reply order broken: req_id {rsp['req_id']:#x} != {req_id:#x}")
        assert rsp["opcode"] == OP_STATS and rsp["status"] == ST_OK
        assert (rep["dest_ip"], rep["dest_port"]) == peer, (
            f"reply for {req_id:#x} went to "
            f"{rep['dest_ip']:#x}:{rep['dest_port']} instead of "
            f"{peer[0]:#x}:{peer[1]}")
        check_reply_is_ours(rep)

    c = counters(dut)
    assert c["short"] == 1, f"short drop not counted: {c}"
    assert c["accepted"] == 2, f"accepted {c['accepted']}, want 2: {c}"
    assert c["orphan"] == 0, f"a response arrived with no header behind it: {c}"


@cocotb.test(timeout_time=400, timeout_unit="us")
async def test_a_zero_keep_beat_is_dropped_and_counted(dut):
    """udp_length == 8 inside a longer IP datagram.

    word_count_reg is udp_length - 8, so it is zero, and udp_ip_rx_64.v:342
    masks the beat with count2keep(0): a legal transfer carrying no bytes at
    all, with tlast. oca_proto would drop it in silence.
    """
    drv, mon = await setup(dut)
    await drv.run([Req(raw=[(0x0123456789ABCDEF, 0x00)], peer=PEER_C),
                   Req(build_stats(0x0033), peer=PEER_B)])
    reps = await wait_replies(dut, mon, 1)
    await settle(dut)
    drv.check()
    mon.check()

    assert len(reps) == 1, f"{len(reps)} replies; the empty beat was answered"
    assert parse_response(reps[0]["payload"])["req_id"] == 0x0033
    assert (reps[0]["dest_ip"], reps[0]["dest_port"]) == PEER_B, \
        "the reply carries the empty datagram's peer"
    c = counters(dut)
    assert c["short"] == 1, f"the empty beat was not counted: {c}"
    assert c["accepted"] == 1 and c["orphan"] == 0, c


@cocotb.test(timeout_time=400, timeout_unit="us")
async def test_a_wrong_destination_port_is_dropped_and_counted(dut):
    drv, mon = await setup(dut)
    await drv.run([Req(build_stats(0x0044), peer=PEER_C, dst_port=LOCAL_PORT + 1),
                   Req(build_stats(0x0055), peer=PEER_A)])
    reps = await wait_replies(dut, mon, 1)
    await settle(dut)
    drv.check()
    mon.check()

    assert len(reps) == 1, "the datagram for another port was answered"
    assert parse_response(reps[0]["payload"])["req_id"] == 0x0055
    assert (reps[0]["dest_ip"], reps[0]["dest_port"]) == PEER_A
    c = counters(dut)
    assert c["port"] == 1, f"wrong-port drop not counted: {c}"
    assert c["short"] == 0 and c["accepted"] == 1 and c["orphan"] == 0, c


@cocotb.test(timeout_time=400, timeout_unit="us")
async def test_headers_survive_a_ten_thousand_cycle_stall(dut):
    """Eight requests while the transmit header handshake is refused.

    ip_64 blocks in STATE_ARP_QUERY until an ARP reply and that timeout is
    about 30 s, so s_udp_hdr_ready low for a very long time is the ordinary
    case, not the pathological one. Nothing may be lost, nothing misaddressed,
    and whatever the queue guard refuses must be on a counter.
    """
    drv, mon = await setup(dut)
    peers = [(0x0A00_0010 + n, 41000 + n) for n in range(8)]
    reqs = [Req(build_stats(0x100 + n), peer=peers[n]) for n in range(8)]

    mon.hdr_stall = 10_000
    await drv.run(reqs)
    for _ in range(10_500):
        await RisingEdge(dut.clk)
    assert mon.hdr_stall == 0, "the stall did not run its course"

    dut._log.info("stalled queue: %s", counters(dut))
    reps = await wait_drained(dut, mon)
    drv.check()
    mon.check()
    c = counters(dut)

    assert c["accepted"] + c["full"] + c["short"] + c["port"] + c["nohdr"] == 8, \
        f"eight datagrams in, {c} out: one left no trace"
    assert c["short"] == 0 and c["port"] == 0 and c["nohdr"] == 0, c
    assert c["orphan"] == 0, f"a response outran its header: {c}"
    assert len(reps) == c["accepted"], \
        f"{len(reps)} replies for {c['accepted']} accepted requests"
    check_replies(reps, [(0x100 + n, peers[n]) for n in range(8)])

    if DEPTH >= 8:
        assert c["full"] == 0, (
            f"depth {DEPTH} overflowed on eight stalled requests: {c}")
        assert c["watermark"] >= 4, (
            f"the queue never went deeper than {c['watermark']}; this test is "
            "no longer reaching the condition the depth was chosen for")
    else:
        assert c["full"] > 0, (
            f"depth {DEPTH} never filled, so the guard was never exercised: {c}")
        assert c["watermark"] == DEPTH, \
            f"watermark {c['watermark']} at depth {DEPTH}"


@cocotb.test(timeout_time=400, timeout_unit="us")
async def test_a_full_size_seal_and_its_reply(dut):
    """The largest request a 1500-byte MTU can carry, and the core's own
    maximum on top of it, so the seam is provably not the narrower of the two.
    """
    drv, mon = await setup(dut)
    await load_key(dut, drv, mon)

    for total, peer in ((MTU_PAYLOAD, PEER_B), (MAX_REQUEST, PEER_C)):
        msg = bytes((i * 3) & 0xFF for i in range(total - 24))
        before = len(mon.replies)
        await drv.run([Req(build_seal(0x0200, 0, NONCE, b"", msg), peer=peer)])
        reps = await wait_replies(dut, mon, before + 1)
        rep = reps[-1]
        rsp = parse_response(rep["payload"])
        assert rsp["status"] == ST_OK, \
            f"seal of {total} bytes: status {rsp['status']}"
        want_ct, want_tag = aead_encrypt(KEY, NONCE, b"", msg)
        assert rsp["body"] == want_tag + want_ct, \
            f"seal of {total} bytes: body wrong through the seam"
        assert (rep["dest_ip"], rep["dest_port"]) == peer
        check_reply_is_ours(rep)

    await settle(dut)
    drv.check()
    mon.check()
    c = counters(dut)
    assert c["accepted"] == 3 and c["orphan"] == 0, c


@cocotb.test(timeout_time=400, timeout_unit="us")
async def test_tuser_is_forwarded_and_counted(dut):
    """A frame the stack marks bad still gets its answer.

    tuser only ever rides the beat that carries tlast, by which point
    oca_core holds the whole packet and will answer it. Dropping the packet
    here would leave its header in the queue with no response to spend it on,
    which is the same desynchronisation as the short datagram.
    """
    drv, mon = await setup(dut)
    await drv.run([Req(build_stats(0x0066), peer=PEER_A, tuser_last=True),
                   Req(build_stats(0x0077), peer=PEER_B)])
    reps = await wait_replies(dut, mon, 2)
    await settle(dut)
    drv.check()
    mon.check()

    assert parse_response(reps[0]["payload"])["req_id"] == 0x0066
    assert (reps[0]["dest_ip"], reps[0]["dest_port"]) == PEER_A
    assert parse_response(reps[1]["payload"])["req_id"] == 0x0077
    assert (reps[1]["dest_ip"], reps[1]["dest_port"]) == PEER_B
    c = counters(dut)
    assert c["tuser"] == 1, f"tuser not counted: {c}"
    assert c["accepted"] == 2 and c["orphan"] == 0, c


@cocotb.test(timeout_time=400, timeout_unit="us")
async def test_back_to_back_requests_keep_their_own_peers(dut):
    """Six datagrams with no idle cycle between them, at the tightest header
    lead the stack can produce."""
    drv, mon = await setup(dut)
    drv.hdr_lead = 1
    peers = [(0x0A00_0020 + n, 42000 + n) for n in range(6)]
    await drv.run([Req(build_stats(0x300 + n), peer=peers[n]) for n in range(6)])
    reps = await wait_drained(dut, mon)
    drv.check()
    mon.check()

    c = counters(dut)
    check_replies(reps, [(0x300 + n, peers[n]) for n in range(6)])
    assert len(reps) == c["accepted"], \
        f"{len(reps)} replies for {c['accepted']} accepted requests"
    assert c["accepted"] + c["full"] == 6, f"a datagram left no trace: {c}"
    assert c["orphan"] == 0 and c["short"] == 0 and c["port"] == 0, c
    if DEPTH >= 8:
        assert c["accepted"] == 6 and c["full"] == 0, \
            f"the shipping depth refused a back-to-back burst of six: {c}"


@cocotb.test(timeout_time=400, timeout_unit="us")
async def test_a_payload_with_no_header_is_refused(dut):
    """Fault injection: a beat with nothing standing behind it.

    udp_ip_rx_64 raises m_udp_hdr_valid at the edge that ends
    STATE_READ_HEADER (:309) and holds it until it is taken (:273), so every
    payload beat it emits has a header behind it and RxDriver will not produce
    this. The seam still has to decide what happens if that ever stops being
    true -- binding the beat to the *next* packet's header is the failure --
    and a guard nothing can reach is a guard nothing can trust. So the beat is
    driven here by hand, deliberately outside the driver.
    """
    drv, mon = await setup(dut)
    dut.rx_payload_tdata.value = 0x1122334455667788
    dut.rx_payload_tkeep.value = 0xFF
    dut.rx_payload_tlast.value = 1
    dut.rx_payload_tuser.value = 0
    dut.rx_payload_tvalid.value = 1
    while True:
        await ReadOnly()
        ready = dut.rx_payload_tready.value == 1
        await RisingEdge(dut.clk)
        if ready:
            break
    drv.idle()
    await settle(dut, 20)

    await drv.run([Req(build_stats(0x0088), peer=PEER_B)])
    reps = await wait_drained(dut, mon)
    drv.check()
    mon.check()

    assert len(reps) == 1, "the headerless beat was answered"
    check_replies(reps, [(0x0088, PEER_B)])
    c = counters(dut)
    assert c["nohdr"] == 1, f"the headerless beat left no trace: {c}"
    assert c["accepted"] == 1 and c["short"] == 0 and c["orphan"] == 0, c


@cocotb.test(timeout_time=400, timeout_unit="us")
async def test_short_and_wrong_port_between_good_requests(dut):
    """Every refusal shape interleaved, so a rejected packet is proved not to
    consume the queue entry of the packet after it."""
    drv, mon = await setup(dut)
    good = [PEER_A, PEER_B, PEER_C]
    reqs = [
        Req(build_stats(0x0401), peer=good[0]),
        Req(raw=[(0x1122334455667788, 0x7F)], peer=(0x0B00_0001, 1)),
        Req(build_stats(0x0402), peer=good[1]),
        Req(build_stats(0x9999), peer=(0x0B00_0002, 2), dst_port=1),
        Req(raw=[(0, 0x00)], peer=(0x0B00_0003, 3)),
        Req(build_stats(0x0403), peer=good[2]),
    ]
    await drv.run(reqs)
    reps = await wait_replies(dut, mon, 3)
    await settle(dut)
    drv.check()
    mon.check()

    for n, rep in enumerate(reps):
        rsp = parse_response(rep["payload"])
        assert rsp["req_id"] == 0x0401 + n, f"reply {n}: {rsp['req_id']:#x}"
        assert (rep["dest_ip"], rep["dest_port"]) == good[n], \
            f"reply {n} inherited a rejected datagram's peer"
    c = counters(dut)
    assert c["accepted"] == 3, c
    assert c["short"] == 2, f"the 7-byte and the empty beat: {c}"
    assert c["port"] == 1, c
    assert c["orphan"] == 0 and c["nohdr"] == 0, c
    assert len(mon.replies) == 3, "a rejected datagram produced a reply"
