# SPDX-License-Identifier: MIT
"""The whole path, from a frame on the wire back out to one.

The device under test is the chain oca_top.sv builds between the GMII pins
and oca_core, generated into a single toplevel by run_oca_path.py. Nothing
here reaches into the hierarchy: a frame is driven onto gmii_rxd one byte
per rx_clk and whatever comes back is read off gmii_txd one byte per
tx_clk, which is the only view a peer on the segment has.

This is the item docs/design/2026-08-05-ethernet-integration.md:20-23 asks
for: a synthetic Ethernet frame carrying a UDP packet with an OCA command,
through the stack, through oca_core, and back out as a frame. proto_model
builds every request and parses every response and is not modified -- it is
the definition of the wire format, and a bench that edited it would be
agreeing with itself. Every cryptographic expectation comes from
aead_model, never from a value written here by hand.

ARP COMES FIRST, ALWAYS. Every outgoing IP datagram goes through an ARP
lookup: ip_64.v:283-292 raises arp_request_valid on s_ip_hdr_valid and sits
in STATE_ARP_QUERY until it is answered. On a cache miss the retry schedule
in arp.v:341-378, scaled by oca_udp_complete_64.v:233-234, is 2 s + 2 s +
2 s + 30 s of simulated time before ip_tx_error_arp_failed fires and the
datagram is dropped. arp.v:300-302 writes (spa, sha) into the cache for any
well-formed ARP frame whatever its oper, so one request from the peer both
fills the cache and draws the reply the ARP tests check.

WHAT MAKES A FRAME DISAPPEAR, all of it observed in the RTL rather than
guessed at:

  * The FCS is checked and a bad one is silently dropped -- the receive
    FIFO is RX_DROP_BAD_FRAME (oca_eth_mac_1g_fifo_64.v:175-176) -- so the
    frames here are built by test_eth_mac.fcs, which is the generator that
    file proves against the MAC in both directions.

  * The IPv4 header checksum is checked and a bad one is discarded into
    STATE_WAIT_LAST with nothing downstream ever seeing it
    (ip_eth_rx_64.v:438-442), and the version and IHL must be 4 and 5
    exactly (:360-361), so no IP options. The UDP checksum is NOT checked
    anywhere (udp_ip_rx_64.v:470-471 only captures the field); it is
    computed correctly here because a peer would, and no test asserts a
    wrong one is refused.

  * Both length fields are load-bearing, not decorative. ip_eth_rx_64.v:337
    takes the payload word count from the IP total length and
    udp_ip_rx_64.v:302 takes its own from the UDP length, and :340-342 masks
    tkeep from it. They are what trims the Ethernet padding off a short
    request, so a wrong one truncates or over-runs the payload in silence.

  * The receive path has no back pressure toward the wire.
    eth_mac_1g_fifo.v:305 leaves the receive FIFO's s_axis_tready
    unconnected and the wrapper sets RX_DROP_WHEN_FULL (:192), so a stalled
    logic side does not stall the wire: the frame is dropped and only
    rx_fifo_overflow says so. That is why every status wire the harness
    brings out is counted here and every count not named must be zero. At
    this boundary "no reply" and "silently dropped" are the same silence.

  * Three things are still clearing after the resets go away, and the
    longest sets the wait before the first frame: the FIFOs' reset
    synchronisers, arp_cache's 64-cycle walk (arp_cache.v:197-201, :211)
    and oca_pktbuf's 512-cycle zeroing (oca_core.sv:55-57). 512 clk_sys
    cycles, not 64 and not 500 ns.

  * arp.v:296 ties incoming_frame_ready to outgoing_frame_ready, so an
    incoming ARP frame is back-pressured while the ARP transmitter is
    busy. Nothing here sends two ARP frames without a gap.

Every test carries a timeout. Without one a request the board cannot
resolve leaves ip_64 in STATE_ARP_QUERY for 35.9 s of simulated time and
the job dies on the wall clock with no diagnostic at all.
"""

import struct

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge, Timer
from cocotb.utils import get_sim_time

from aead_model import aead_encrypt
from proto_model import (HDR_LEN, OP_LOAD_KEY, OP_OPEN, OP_SEAL, OP_STATS,
                         ST_AUTH_FAIL, ST_OK, VERSION, build_load_key,
                         build_open, build_seal, build_stats, parse_response)
from run_oca_path import (ARP_CACHE_CLEAR_CYCLES, HDR_Q_DEPTH, LOCAL_IP,
                          LOCAL_MAC, LOCAL_PORT, PKTBUF_CLEAR_CYCLES,
                          REPLY_TTL)
from test_eth_mac import (LOGIC_CLK_NS, MIN_FRAME, PREAMBLE, RX_CLK_NS,
                          RX_SKEW_NS, TX_CLK_NS, TX_SKEW_NS, GmiiRxDriver,
                          fcs, wire)

LOCAL_MAC_BYTES = LOCAL_MAC.to_bytes(6, "big")
BROADCAST_MAC = b"\xff" * 6

# The same fixtures test_oca_core.py:25-26 and test_udp_seam.py:69-70 use.
# One key across all three suites is what makes a seal computed here
# comparable with a seal computed there.
KEY = bytes(range(32))
NONCE = bytes(range(12))


class Peer:
    """One host on the segment: what a reply has to be addressed to."""

    def __init__(self, mac: str, ip: int, port: int):
        self.mac = bytes.fromhex(mac)
        self.ip = ip
        self.port = port

    def __repr__(self) -> str:
        return f"Peer({self.mac.hex(':')}, {self.ip:#010x}, {self.port})"


# Two peers, both locally administered so nothing here can be mistaken for
# real traffic, and both inside 192.168.1.0/24 -- arp.v:392 compares an
# outbound destination against gateway_ip rather than local_ip, so a peer
# outside the subnet would be a different exchange from the one under test,
# and arp.v:387-400 hands back the broadcast MAC with no ARP at all for
# 255.255.255.255 and for 192.168.1.255. Different source ports as well as
# different addresses, because the seam has to carry both back.
PEER_A = Peer("02005e0000aa", 0xC0A8010A, 40001)   # 192.168.1.10
PEER_B = Peer("02005e0000bb", 0xC0A8010B, 40002)   # 192.168.1.11

ETHERTYPE_IPV4 = 0x0800
ETHERTYPE_ARP = 0x0806
IP_PROTO_UDP = 17
IP_PROTO_ICMP = 1
ARP_HTYPE_ETHERNET = 0x0001
ARP_PTYPE_IPV4 = 0x0800
ARP_OPER_REQUEST = 1
ARP_OPER_REPLY = 2

# The flags-and-fragment-offset word every reply carries. ip_64.v:224-225
# drives s_ip_flags(3'b010) and s_ip_fragment_offset(13'd0) into
# ip_eth_tx_64, which is Don't Fragment with nothing fragmented -- correct
# for a datagram this stack never splits, and a constant rather than
# anything derived, so the tests can compare against it exactly.
IP_FLAGS_DF = 0x4000

# What a peer puts in its own datagrams. Nothing in the receive path reads
# it -- ip_eth_rx_64 neither checks nor decrements a TTL -- and it is
# deliberately not REPLY_TTL, so a reply that echoed the request's header
# instead of building its own would be visible.
REQUEST_TTL = 55

# The frame the MAC puts on the wire, FCS excluded: ENABLE_PADDING with
# MIN_FRAME_LENGTH 64 (oca_eth_mac_1g_fifo_64.v:130-131) makes
# axis_gmii_tx.v:331-354 zero-pad to this before the FCS, so a reply shorter
# than 60 bytes arrives padded and the FCS covers the padding.
PADDED_FRAME = MIN_FRAME - 4

# The wait before the first frame. oca_pktbuf's 512 cycles is the longest of
# the three clear windows and so the one that sets it; the margin covers the
# FIFOs' reset synchronisers, which test_eth_mac.py:319-321 measures at
# 500 ns.
READY_CYCLES = PKTBUF_CLEAR_CYCLES + 64

# Long enough for a seal of a few hundred bytes to cross the whole path,
# be encrypted, and cross back through the store-and-forward checksum FIFO,
# and short enough that a request that will never be answered fails with the
# status counts in the message instead of running out the timeout.
REPLY_BUDGET = 8000

# Long enough after the last expected reply for an unexpected extra one to
# be seen.
DRAIN_CYCLES = 400

# Every wire the MAC and the stack report an event on, all of them one
# clk_sys wide, so they are counted as pulses and never sampled at a moment.
MAC_STATUS = ("tx_error_underflow", "tx_fifo_overflow", "tx_fifo_bad_frame",
              "tx_fifo_good_frame", "rx_error_bad_frame", "rx_error_bad_fcs",
              "rx_fifo_overflow", "rx_fifo_bad_frame", "rx_fifo_good_frame")

STACK_STATUS = ("eth_rx_early_term",
                "ip_rx_err_hdr_early", "ip_rx_err_payload_early",
                "ip_rx_err_invalid_hdr", "ip_rx_err_invalid_csum",
                "ip_tx_err_payload_early", "ip_tx_err_arp_failed",
                "udp_rx_err_hdr_early", "udp_rx_err_payload_early",
                "udp_tx_err_payload_early")

STATUS = MAC_STATUS + STACK_STATUS

# Levels rather than events. They are asserted low only once the exchange
# has drained: a busy that never falls is a stage holding a frame it cannot
# finish, which is the failure mode the raw-IP receive port left unconnected
# at oca_top.sv:324 produces and which no error wire reports.
BUSY = ("eth_rx_busy", "eth_tx_busy", "ip_rx_busy", "ip_tx_busy",
        "udp_rx_busy", "udp_tx_busy")

COUNTERS = ("cnt_accepted", "cnt_drop_short", "cnt_drop_port", "cnt_drop_full",
            "cnt_drop_nohdr", "cnt_tuser", "cnt_resp_orphan")


# ----------------------------------------------------------------------
# Checksums
# ----------------------------------------------------------------------

def ones_complement_sum(data: bytes) -> int:
    """RFC 1071's 16-bit ones' complement sum, odd lengths padded with zero."""
    if len(data) % 2:
        data += b"\x00"
    total = sum(int.from_bytes(data[i:i + 2], "big")
                for i in range(0, len(data), 2))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return total


def checksum16(data: bytes) -> int:
    return ones_complement_sum(data) ^ 0xFFFF


def _pseudo_header(src_ip: int, dst_ip: int, udp_length: int) -> bytes:
    """RFC 768's pseudo header: the addresses the UDP checksum covers."""
    return (src_ip.to_bytes(4, "big") + dst_ip.to_bytes(4, "big")
            + bytes([0, IP_PROTO_UDP]) + udp_length.to_bytes(2, "big"))


# ----------------------------------------------------------------------
# Building what a peer sends
# ----------------------------------------------------------------------

def arp_frame(oper: int, sender_mac: bytes, sender_ip: int,
              target_mac: bytes, target_ip: int, *,
              dest_mac: bytes, src_mac: bytes) -> bytes:
    """One ARP frame over Ethernet, padded to the wire minimum.

    Padding rather than a bare 42 bytes because the MAC pads its own
    replies, and a request the peer had not padded would not be the frame a
    conformant PHY emits. arp_eth_rx.v:260-266 tolerates the extra bytes: it
    only refuses a frame that ends inside the 28-byte ARP header.
    """
    arp = (struct.pack(">HHBBH", ARP_HTYPE_ETHERNET, ARP_PTYPE_IPV4, 6, 4, oper)
           + sender_mac + sender_ip.to_bytes(4, "big")
           + target_mac + target_ip.to_bytes(4, "big"))
    frame = dest_mac + src_mac + ETHERTYPE_ARP.to_bytes(2, "big") + arp
    return frame.ljust(PADDED_FRAME, b"\x00")


def ipv4_frame(peer: Peer, protocol: int, payload: bytes, *,
               ident: int) -> bytes:
    """One IPv4 datagram from `peer` to the board, as a padded frame.

    Everything the receive path checks is computed rather than asserted:
    ip_eth_rx_64.v:438-442 discards a frame whose header checksum fails
    with nothing downstream ever seeing it, and :360-361 requires version 4
    with IHL exactly 5, so there are no options here. The destination MAC
    is LOCAL_MAC for realism only -- there is no address filter anywhere in
    this design (ip_complete_64.v:208-263 demuxes on EtherType alone), so
    no test here claims a foreign one would be dropped.
    """
    total_length = 20 + len(payload)
    ip = (bytes([0x45, 0x00]) + total_length.to_bytes(2, "big")
          + ident.to_bytes(2, "big") + b"\x00\x00"
          + bytes([REQUEST_TTL, protocol]) + b"\x00\x00"
          + peer.ip.to_bytes(4, "big") + LOCAL_IP.to_bytes(4, "big"))
    ip = ip[:10] + checksum16(ip).to_bytes(2, "big") + ip[12:]
    frame = (LOCAL_MAC_BYTES + peer.mac + ETHERTYPE_IPV4.to_bytes(2, "big")
             + ip + payload)
    return frame.ljust(PADDED_FRAME, b"\x00")


def request_frame(peer: Peer, payload: bytes, *, ident: int = 0x4F43) -> bytes:
    """An OCA request as the Ethernet frame a peer would put on the wire."""
    udp_length = 8 + len(payload)
    udp = (peer.port.to_bytes(2, "big") + LOCAL_PORT.to_bytes(2, "big")
           + udp_length.to_bytes(2, "big") + b"\x00\x00" + payload)
    # RFC 768: a computed checksum of zero goes on the wire as all ones,
    # because zero is the value that means "no checksum".
    csum = checksum16(_pseudo_header(peer.ip, LOCAL_IP, udp_length) + udp)
    udp = udp[:6] + (csum or 0xFFFF).to_bytes(2, "big") + udp[8:]
    return ipv4_frame(peer, IP_PROTO_UDP, udp, ident=ident)


def icmp_echo_frame(peer: Peer, *, ident: int = 0x5EC0, seq: int = 1,
                    data: bytes = b"areyouthere") -> bytes:
    """An ICMP echo request: a frame this design has no receiver for.

    Well formed in every respect the receive path inspects, so nothing
    upstream of the demux has a reason to drop it. It is IP protocol 0x01
    rather than 0x11, which is the only thing that decides its fate:
    udp_complete_64.v:289 routes anything that is not 0x11 to the raw IP
    receive port, and :361-365 makes that port's ready signals the ones
    that consume it.
    """
    icmp = (bytes([8, 0, 0, 0]) + ident.to_bytes(2, "big")
            + seq.to_bytes(2, "big") + data)
    icmp = icmp[:2] + checksum16(icmp).to_bytes(2, "big") + icmp[4:]
    return ipv4_frame(peer, IP_PROTO_ICMP, icmp, ident=0x1CE0)


# ----------------------------------------------------------------------
# Reading what the board sends
# ----------------------------------------------------------------------

def arp_fields(frame: bytes) -> dict:
    """What an ARP frame says, for a failure message a diff cannot give."""
    if len(frame) < 42:
        return {"too_short": len(frame)}
    htype, ptype, hlen, plen, oper = struct.unpack(">HHBBH", frame[14:22])
    return {"dest_mac": frame[:6].hex(":"), "src_mac": frame[6:12].hex(":"),
            "eth_type": f"{int.from_bytes(frame[12:14], 'big'):#06x}",
            "htype": htype, "ptype": f"{ptype:#06x}", "hlen": hlen,
            "plen": plen, "oper": oper,
            "sha": frame[22:28].hex(":"),
            "spa": f"{int.from_bytes(frame[28:32], 'big'):#010x}",
            "tha": frame[32:38].hex(":"),
            "tpa": f"{int.from_bytes(frame[38:42], 'big'):#010x}"}


def decode_reply(frame: bytes, dut, peer: Peer) -> dict:
    """Peel a reply frame back to the OCA response, checking every layer.

    The addressing assertions are the point of the seam and are made here
    rather than in each test: reply k must carry requester k's MAC, IP and
    source port. A board that answered the right bytes to the wrong peer
    would pass every payload assertion in this file and be useless on a
    segment with two hosts on it.

    The Ethernet padding is trimmed with the IP total length rather than
    with the length of what the test expected, so a stack that padded into
    the payload, or that declared a length its payload does not match, is
    caught by the fields and not hidden by the trim.
    """
    assert len(frame) >= PADDED_FRAME, (
        f"a {len(frame)}-byte frame left the board; the MAC pads to "
        f"{PADDED_FRAME}")
    assert frame[:6] == peer.mac, (
        f"the reply went to {frame[:6].hex(':')}, not to the requester "
        f"{peer.mac.hex(':')}")
    assert frame[6:12] == LOCAL_MAC_BYTES, (
        f"the reply came from {frame[6:12].hex(':')}, not from LOCAL_MAC "
        f"{LOCAL_MAC_BYTES.hex(':')}")
    eth_type = int.from_bytes(frame[12:14], "big")
    assert eth_type == ETHERTYPE_IPV4, f"EtherType {eth_type:#06x}, want IPv4"

    ip = frame[14:34]
    assert ip[0] == 0x45, (
        f"IP version/IHL {ip[0]:#04x}, want 0x45: ip_eth_rx_64.v:360-361 "
        "accepts nothing else, so the board must not send anything else")
    assert ones_complement_sum(ip) == 0xFFFF, (
        f"the IPv4 header checksum does not verify: {ip.hex()}")
    total_length = int.from_bytes(ip[2:4], "big")
    assert int.from_bytes(ip[6:8], "big") == IP_FLAGS_DF, (
        f"the reply's flags and fragment offset are {ip[6:8].hex()}, want "
        f"{IP_FLAGS_DF:#06x}: ip_64.v:224-225 hardcodes Don't Fragment with "
        "offset zero, so anything else is a byte that moved")
    assert ip[8] == REPLY_TTL, f"reply TTL {ip[8]}, want {REPLY_TTL}"
    assert ip[9] == IP_PROTO_UDP, f"IP protocol {ip[9]}, want {IP_PROTO_UDP}"

    published = int(dut.stack_local_ip.value)
    assert published == LOCAL_IP, (
        f"the seam publishes {published:#010x} and the harness elaborates "
        f"with LOCAL_IP {LOCAL_IP:#010x}")
    source_ip = int.from_bytes(ip[12:16], "big")
    assert source_ip == published, (
        f"the reply's source IP is {source_ip:#010x} and the address the "
        f"stack answers ARP for is {published:#010x}: a peer's UDP layer "
        "would discard this for matching no socket")
    dest_ip = int.from_bytes(ip[16:20], "big")
    assert dest_ip == peer.ip, (
        f"the reply went to {dest_ip:#010x}, not to the requester "
        f"{peer.ip:#010x}")

    assert 28 <= total_length <= len(frame) - 14, (
        f"IP total length {total_length} does not fit a {len(frame)}-byte "
        "frame")
    datagram = frame[34:14 + total_length]
    udp_length = int.from_bytes(datagram[4:6], "big")
    assert udp_length == len(datagram), (
        f"UDP length {udp_length} against {len(datagram)} bytes of datagram")
    source_port = int.from_bytes(datagram[0:2], "big")
    assert source_port == LOCAL_PORT, (
        f"the reply came from port {source_port}, want {LOCAL_PORT}")
    dest_port = int.from_bytes(datagram[2:4], "big")
    assert dest_port == peer.port, (
        f"the reply went to port {dest_port}, not to the requester's "
        f"{peer.port}")
    assert ones_complement_sum(
        _pseudo_header(source_ip, dest_ip, udp_length) + datagram) == 0xFFFF, (
        f"the UDP checksum does not verify: header {datagram[:8].hex()}. "
        "udp_checksum_gen_64 computed it, this is not our arithmetic")

    rsp = parse_response(datagram[8:])
    rsp["frame"] = frame
    rsp["peer"] = peer
    return rsp


class GmiiTxMonitor:
    """The wire out of the board: one byte per tx_clk while gmii_tx_en is high.

    It refuses rather than reports. A monitor that accepted whatever arrived
    and left the checking to the tests would pass a reply with a preamble of
    the wrong length or an FCS computed over the wrong bytes, and both are
    changes a transmit path can make without touching a single byte of the
    frame a test compares.

    What each refusal rests on:

      * The preamble is seven 0x55 and one 0xd5, emitted by
        axis_gmii_tx.v:261-292 before any frame byte. Matching it exactly is
        also what makes the rest of the stream a frame rather than an offset
        guess.

      * The four bytes after the frame are its FCS, least significant byte
        first, over everything from the destination address to the end of
        the padding. The generator is test_eth_mac.fcs, the same one
        test_the_fcs_is_checked_over_the_frame_least_significant_byte_first
        proves against the receive path in both directions.

      * gmii_tx_er is raised on transmit underflow. A frame carrying it is
        a frame the peer's PHY discards, so it is a failure here and not a
        note.

    Frames are claimed with take() rather than read by index, so that a
    frame nobody claimed is visible as `pending` at the end of a test. An
    extra reply and a missing one are different failures.
    """

    def __init__(self, dut):
        self.dut = dut
        self.frames = []       # frame bodies, padded, FCS checked and stripped
        self.failures = []
        self._octets = bytearray()
        self._taken = 0

    @property
    def pending(self) -> int:
        return len(self.frames) - self._taken

    def take(self) -> bytes:
        frame = self.frames[self._taken]
        self._taken += 1
        return frame

    def check(self) -> None:
        assert not self.failures, "; ".join(self.failures)
        assert not self._octets, (
            f"{len(self._octets)} byte(s) are still on the wire: gmii_tx_en "
            "never went low")
        assert self.pending == 0, (
            f"{self.pending} frame(s) followed the ones this test expected: "
            f"{[f.hex() for f in self.frames[self._taken:]]}")

    def _finish(self, stream: bytes) -> None:
        if not stream.startswith(PREAMBLE):
            self.failures.append(
                f"frame {len(self.frames)} opened with "
                f"{stream[:len(PREAMBLE)].hex()}, want {PREAMBLE.hex()}")
            return
        body = stream[len(PREAMBLE):]
        if len(body) < PADDED_FRAME + 4:
            self.failures.append(
                f"frame {len(self.frames)} is {len(body)} byte(s) including "
                f"its FCS: the MAC pads to {PADDED_FRAME} before adding one")
            return
        frame, tail = body[:-4], body[-4:]
        want = fcs(frame)
        if tail != want:
            self.failures.append(
                f"frame {len(self.frames)} carries FCS {tail.hex()}, want "
                f"{want.hex()} over the {len(frame)} bytes ahead of it")
            return
        self.frames.append(frame)

    async def run(self) -> None:
        d = self.dut
        while True:
            await RisingEdge(d.tx_clk)
            await ReadOnly()
            if int(d.gmii_tx_en.value):
                if int(d.gmii_tx_er.value):
                    self.failures.append(
                        f"gmii_tx_er at byte {len(self._octets)} of frame "
                        f"{len(self.frames)}: the MAC under-ran and the peer "
                        "would discard this frame")
                self._octets.append(int(d.gmii_txd.value))
            elif self._octets:
                self._finish(bytes(self._octets))
                self._octets = bytearray()


class StatusMonitor:
    """Every status wire, counted as pulses in the logic domain."""

    def __init__(self, dut):
        self.dut = dut
        self.counts = dict.fromkeys(STATUS, 0)

    async def run(self) -> None:
        while True:
            await RisingEdge(self.dut.logic_clk)
            await ReadOnly()
            for name in STATUS:
                if int(getattr(self.dut, name).value):
                    self.counts[name] += 1

    def expect(self, **wanted: int) -> None:
        """Assert the named counts, and that every count not named is zero.

        Naming only what should have happened would let a test pass while a
        frame was also dropped on overflow or an IPv4 header was also
        rejected, and at this boundary neither of those is visible in any
        other way.
        """
        want = dict.fromkeys(STATUS, 0) | wanted
        assert self.counts == want, f"status counts {self.counts}, want {want}"


def counters(dut) -> dict:
    return {name: int(getattr(dut, name).value) for name in COUNTERS}


def busy(dut) -> dict:
    return {name: int(getattr(dut, name).value) for name in BUSY}


def stats_body(rsp: dict) -> dict:
    """A stats response body: four little-endian 32-bit counters.

    Named the way test_oca_core.py:43-46 names them, because they are the
    same four registers seen through one more layer of wrapping.
    """
    assert len(rsp["body"]) == 16, (
        f"a stats body is four 32-bit counters; this one is "
        f"{len(rsp['body'])} byte(s): {rsp['body'].hex()}")
    rx, drop, done, auth = struct.unpack("<4I", rsp["body"])
    return {"rx": rx, "drop": drop, "done": done, "auth": auth}


async def _release(clk, rst) -> None:
    await RisingEdge(clk)
    rst.value = 0


async def setup(dut, *, settle_cycles: int = READY_CYCLES):
    """Three clocks, three resets released each to its own clock, two monitors.

    The reset polarity is the vendor's, active HIGH and asynchronously
    asserted, and each one is deasserted just after a rising edge of the
    clock its logic runs on (oca_eth_mac_1g_fifo_64.v:41-45). The seam and
    the core take the active-low form, which the harness makes the inverse
    of logic_rst and nothing else -- on the board oca_clkrst derives both
    polarities from one flop for the same reason.

    Both monitors start before the settling wait, so a frame lost or a
    status wire pulsed inside the clear windows is counted rather than
    missed.

    Returns the driver, the monitors, and the simulated time in nanoseconds
    at which logic_rst went away: the clear windows are measured from there.
    """
    dut.rx_rst.value = 1
    dut.tx_rst.value = 1
    dut.logic_rst.value = 1

    dut.gmii_rxd.value = 0
    dut.gmii_rx_dv.value = 0
    dut.gmii_rx_er.value = 0

    async def start_clock(signal, period_ns, skew_ns):
        if skew_ns:
            await Timer(skew_ns, unit="ns")
        await Clock(signal, period_ns, unit="ns").start()

    cocotb.start_soon(start_clock(dut.logic_clk, LOGIC_CLK_NS, 0))
    cocotb.start_soon(start_clock(dut.rx_clk, RX_CLK_NS, RX_SKEW_NS))
    cocotb.start_soon(start_clock(dut.tx_clk, TX_CLK_NS, TX_SKEW_NS))

    await Timer(200, unit="ns")
    cocotb.start_soon(_release(dut.rx_clk, dut.rx_rst))
    cocotb.start_soon(_release(dut.tx_clk, dut.tx_rst))
    await _release(dut.logic_clk, dut.logic_rst)
    released_ns = get_sim_time("ns")

    tx = GmiiTxMonitor(dut)
    status = StatusMonitor(dut)
    cocotb.start_soon(tx.run())
    cocotb.start_soon(status.run())

    for _ in range(settle_cycles):
        await RisingEdge(dut.logic_clk)

    return GmiiRxDriver(dut), tx, status, released_ns


async def drain(dut, cycles: int = DRAIN_CYCLES) -> None:
    for _ in range(cycles):
        await RisingEdge(dut.logic_clk)


async def next_frame(dut, tx, status) -> bytes:
    """The next frame off the wire, or what the board did instead of one."""
    for _ in range(REPLY_BUDGET):
        if tx.pending:
            return tx.take()
        await RisingEdge(dut.logic_clk)
    raise AssertionError(
        f"no frame left the board in {REPLY_BUDGET} clk_sys cycles. status "
        f"{status.counts}, busy {busy(dut)}, seam {counters(dut)}")


def check_arp_reply(got: bytes, dut, peer: Peer) -> None:
    """The reply is an ARP reply for this peer, from the published address.

    Built from stack_local_ip rather than from LOCAL_IP, and the two are
    compared separately. That is the property: arp.v:305 tests local_ip to
    decide which requests to answer and arp.v:197 sends it as the sender
    protocol address, so a board with a second constant somewhere would
    answer for one address and reply from another -- the peer's UDP layer
    would then discard every reply for matching no socket, and nothing on
    the board would notice.
    """
    published = int(dut.stack_local_ip.value)
    assert published == LOCAL_IP, (
        f"the seam publishes {published:#010x} and the harness elaborates "
        f"with LOCAL_IP {LOCAL_IP:#010x}")
    want = arp_frame(ARP_OPER_REPLY, LOCAL_MAC_BYTES, published,
                     peer.mac, peer.ip,
                     dest_mac=peer.mac, src_mac=LOCAL_MAC_BYTES)
    assert got == want, (
        f"the reply is {arp_fields(got)}, want {arp_fields(want)}")


async def arp_resolve(dut, drv, tx, status, peer: Peer) -> None:
    """Put the peer in the board's ARP cache, and take the reply it draws.

    One request from the peer does both jobs: arp.v:300-302 writes
    (spa, sha) into the cache for any well-formed ARP frame regardless of
    oper, and arp.v:303-311 answers it because the target protocol address
    is ours. Skipping this and sending the datagram cold also works, but
    only if the transmit monitor decodes the board's own broadcast ARP
    request and answers it -- and getting that wrong costs two seconds of
    simulated time before anything says so.
    """
    request = arp_frame(ARP_OPER_REQUEST, peer.mac, peer.ip, bytes(6), LOCAL_IP,
                        dest_mac=BROADCAST_MAC, src_mac=peer.mac)
    await drv.send(wire(request))
    check_arp_reply(await next_frame(dut, tx, status), dut, peer)


async def send_request(drv, peer: Peer, request: bytes) -> None:
    await drv.send(wire(request_frame(peer, request)))


async def command(dut, drv, tx, status, peer: Peer, request: bytes) -> dict:
    """One OCA request onto the wire, one reply frame decoded back off it."""
    await send_request(drv, peer, request)
    return decode_reply(await next_frame(dut, tx, status), dut, peer)


def check_header(rsp: dict, opcode: int, req_id: int, *, slot: int = 0) -> None:
    """The four header fields a response echoes, all of them.

    req_id in particular: it is how a peer matches an answer to its
    question, and a stack that answered the right peer with the previous
    request's body would be caught here and nowhere else in this file.
    """
    assert rsp["magic_ok"], f"response magic is wrong: {rsp['frame'].hex()}"
    assert rsp["version"] == VERSION, f"response version {rsp['version']}"
    assert rsp["opcode"] == opcode, (
        f"response opcode {rsp['opcode']:#04x}, want {opcode:#04x}")
    assert rsp["req_id"] == req_id, (
        f"response req_id {rsp['req_id']:#06x}, want {req_id:#06x}")
    assert rsp["slot"] == slot, f"response slot {rsp['slot']}, want {slot}"


def quiet(dut, *, watermark: int = 0, **wanted: int) -> None:
    """Nothing is still holding a frame, and the seam's books balance.

    Every counter not named must be zero. A drop the seam recorded and the
    test did not ask for looks exactly like a reply that never came, and at
    this boundary these seven registers are the only thing that tells them
    apart.
    """
    still = {name: value for name, value in busy(dut).items() if value}
    assert not still, (
        f"{sorted(still)} still busy after the exchange drained: a stage is "
        "holding a frame it cannot finish, which no error wire reports")
    want = dict.fromkeys(COUNTERS, 0) | wanted
    got = counters(dut)
    assert got == want, f"seam counters {got}, want {want}"
    assert int(dut.hdr_q_watermark.value) == watermark, (
        f"the seam's header queue reached {int(dut.hdr_q_watermark.value)}, "
        f"want {watermark}")


@cocotb.test(timeout_time=400, timeout_unit="us")
async def test_arp_request_is_answered_from_the_published_address(dut):
    """A peer asks who has 192.168.1.100, and the board answers.

    Everything in the path runs, in both directions, before any of it is
    asked to carry a request: the frame is checked by the receive FCS,
    widened to 64 bits, crossed into clk_sys, parsed by eth_axis_rx,
    demuxed to arp by EtherType (ip_complete_64.v:208-209), answered,
    rebuilt by eth_axis_tx, narrowed back to 8 bits, padded, given an FCS
    and clocked out on tx_clk.

    The reply is compared as whole bytes rather than field by field, so a
    transmit path that got the frame right and the padding wrong, or that
    left a byte of a previous frame standing in the last beat, fails here
    too.
    """
    drv, tx, status, _ = await setup(dut)

    await arp_resolve(dut, drv, tx, status, PEER_A)
    await drain(dut)

    tx.check()
    status.expect(rx_fifo_good_frame=1, tx_fifo_good_frame=1)
    quiet(dut)


@cocotb.test(timeout_time=400, timeout_unit="us")
async def test_an_arp_frame_sent_during_the_cache_clear_window_is_still_answered(dut):
    """A request that arrives while arp_cache is still clearing is answered.

    arp_cache.v:211 raises clear_cache_reg on reset and :197-201 then walks
    wr_ptr one entry per clk_sys cycle until it wraps, 64 of them at
    ARP_CACHE_ADDR_WIDTH 6 (oca_udp_complete_64.v:218). Inside that window
    write_request_ready is held low (:181) -- and arp.v:245 leaves that pin
    unconnected, so the cache write is fire and forget and an entry offered
    inside the window is discarded with no trace at any port of the design.

    The reply, on the other hand, does not go through the cache at all:
    arp.v:303-311 raises outgoing_frame_valid straight from the incoming
    frame's fields. So this is the honest form of the property -- the answer
    survives the window, the cache entry may not -- and the test asserts
    exactly that much.

    It asserts it without becoming a claim about timing it cannot make.
    What is measured is that the last byte of the request is on the wire
    before the window closes, which is arithmetic on the simulated time and
    not an assumption. Whether arp.v had parsed the frame by then is a
    handful of cycles either way and is not asserted.

    The second request, from a different peer and long after every window
    has closed, is what makes the first one mean something: two peers, two
    replies, each carrying its own target. A board that answered only the
    late request, or that answered the late one twice, fails on the frames
    rather than on a count.

    WHAT THIS TEST CANNOT SEE, and why no assertion pretends to: whether
    peer A's entry survived into the cache. Reading it back means making the
    board originate a datagram, which is what the UDP tests below do -- and
    they resolve their peers well outside this window on purpose, so that a
    lost entry there would be a different failure from this one.
    """
    drv, tx, status, released_ns = await setup(dut, settle_cycles=0)

    early = arp_frame(ARP_OPER_REQUEST, PEER_A.mac, PEER_A.ip,
                      bytes(6), LOCAL_IP,
                      dest_mac=BROADCAST_MAC, src_mac=PEER_A.mac)
    await drv.send(wire(early))
    on_the_wire = (get_sim_time("ns") - released_ns) / LOGIC_CLK_NS
    assert on_the_wire < ARP_CACHE_CLEAR_CYCLES, (
        f"the request was not fully on the wire until clk_sys cycle "
        f"{on_the_wire:.1f} after reset, and arp_cache stops clearing at "
        f"{ARP_CACHE_CLEAR_CYCLES}: this test is no longer inside the window "
        "it was written for")

    for _ in range(READY_CYCLES):
        await RisingEdge(dut.logic_clk)

    late = arp_frame(ARP_OPER_REQUEST, PEER_B.mac, PEER_B.ip,
                     bytes(6), LOCAL_IP,
                     dest_mac=BROADCAST_MAC, src_mac=PEER_B.mac)
    await drv.send(wire(late))
    await drain(dut)

    check_arp_reply(await next_frame(dut, tx, status), dut, PEER_A)
    check_arp_reply(await next_frame(dut, tx, status), dut, PEER_B)
    tx.check()
    status.expect(rx_fifo_good_frame=2, tx_fifo_good_frame=2)
    quiet(dut)


@cocotb.test(timeout_time=400, timeout_unit="us")
async def test_a_stats_request_crosses_the_whole_path_as_a_frame(dut):
    """The cheapest OCA command there is, in and out as Ethernet frames.

    Eight bytes of payload, which is both a bare header and exactly
    MIN_REQ_BYTES, so the request sits on the seam's short-packet boundary:
    one byte less and oca_udp_seam.sv:284 would refuse it onto
    cnt_drop_short and no answer would come back at all. cnt_drop_short is
    asserted zero by quiet(), which is what makes this the boundary case
    and not merely a small one.

    It is also the shortest datagram this design can receive, so the
    request needs 18 bytes of Ethernet padding to reach the wire minimum
    and the receive path has to trim them off using the two length fields
    rather than the frame length. A stack that took the payload length
    from the frame would hand oca_proto 26 bytes instead of 8, and
    oca_proto answers a malformed length with a drop and no response --
    which at this boundary is silence.

    The four counters are asserted exactly, not as a shape. rx counts the
    stats request itself and done does not, because the snapshot is taken
    before the command completes -- the property
    test_oca_core.py:1043-1053 pins from the far side. So the first stats
    command after reset must report exactly rx 1, drop 0, done 0, auth 0,
    and any other reading means the core saw traffic this test did not
    send or lost traffic it did.
    """
    drv, tx, status, _ = await setup(dut)
    await arp_resolve(dut, drv, tx, status, PEER_A)

    request = build_stats(0x0101)
    assert len(request) == HDR_LEN, "a stats request is a bare header"
    rsp = await command(dut, drv, tx, status, PEER_A, request)
    await drain(dut)

    check_header(rsp, OP_STATS, 0x0101)
    assert rsp["status"] == ST_OK, f"stats status {rsp['status']}"
    assert stats_body(rsp) == {"rx": 1, "drop": 0, "done": 0, "auth": 0}, (
        f"the core's counters say {stats_body(rsp)} after one stats request")

    tx.check()
    status.expect(rx_fifo_good_frame=2, tx_fifo_good_frame=2)
    quiet(dut, cnt_accepted=1, watermark=1)


@cocotb.test(timeout_time=400, timeout_unit="us")
async def test_a_seal_and_its_open_round_trip_over_ethernet(dut):
    """Real cryptography, over the wire, against the software model.

    Three commands and three frames each way. The seal's answer is
    compared byte for byte with aead_model's, which is the only thing in
    this file that says the ciphertext is right rather than merely
    self-consistent: an engine that encrypted with the wrong counter, or
    that authenticated the plaintext instead of the ciphertext, produces a
    body of exactly the right length that this comparison refuses.

    The message and the AAD are sized to reach the paths a round number
    would miss. 100 bytes of message is one whole ChaCha20 block and a
    partial one, and 13 bytes of AAD is not a multiple of 16, so the
    Poly1305 input needs padding after the AAD and again after the
    ciphertext (aead_model._mac_data, RFC 8439 2.8.1). The request is 137
    bytes of payload, so no Ethernet padding is involved either way and
    the length fields carry the whole story.

    The open then takes the tag and the ciphertext the BOARD produced,
    not the model's, and requires the plaintext back. That direction is
    what makes it a round trip: an engine self-consistent in both
    directions but wrong in both would pass the open and fail the seal,
    and one wrong only on decrypt would pass the seal and fail the open.
    """
    drv, tx, status, _ = await setup(dut)
    await arp_resolve(dut, drv, tx, status, PEER_A)

    rsp = await command(dut, drv, tx, status, PEER_A,
                        build_load_key(0x0201, 3, KEY))
    check_header(rsp, OP_LOAD_KEY, 0x0201, slot=3)
    assert rsp["status"] == ST_OK, f"load_key status {rsp['status']}"
    assert rsp["body"] == b"", f"load_key answered a body: {rsp['body'].hex()}"

    aad = bytes(range(13))
    msg = bytes((i * 37 + 11) & 0xFF for i in range(100))

    rsp = await command(dut, drv, tx, status, PEER_A,
                        build_seal(0x0202, 3, NONCE, aad, msg))
    check_header(rsp, OP_SEAL, 0x0202, slot=3)
    assert rsp["status"] == ST_OK, f"seal status {rsp['status']}"
    want_ct, want_tag = aead_encrypt(KEY, NONCE, aad, msg)
    tag, ct = rsp["body"][:16], rsp["body"][16:]
    assert tag == want_tag, f"tag {tag.hex()}, want {want_tag.hex()}"
    assert ct == want_ct, f"ciphertext {ct.hex()}, want {want_ct.hex()}"

    rsp = await command(dut, drv, tx, status, PEER_A,
                        build_open(0x0203, 3, NONCE, aad, ct, tag))
    await drain(dut)
    check_header(rsp, OP_OPEN, 0x0203, slot=3)
    assert rsp["status"] == ST_OK, f"open status {rsp['status']}"
    assert rsp["body"] == msg, (
        f"the open returned {rsp['body'].hex()}, want {msg.hex()}")

    tx.check()
    status.expect(rx_fifo_good_frame=4, tx_fifo_good_frame=4)
    quiet(dut, cnt_accepted=3, watermark=1)


@cocotb.test(timeout_time=400, timeout_unit="us")
async def test_a_corrupted_tag_puts_no_plaintext_on_the_wire(dut):
    """The security property of the whole design, asserted at the wire.

    test_oca_core.py:415-433 already pins it at the core's AXI-Stream
    boundary. What that test cannot see is everything after: the seam
    builds a reply header before the core has answered
    (oca_udp_seam.sv:118-120), udp_checksum_gen_64 holds the payload in a
    2048-byte store-and-forward FIFO, eth_axis_tx prepends a header and
    axis_gmii_tx pads to 60 bytes. Any of those can carry a stale buffer's
    contents out onto the wire in the bytes of a reply that is, at the
    core's port, correctly empty.

    So the assertion is on the frame, not on the response: the plaintext
    must not appear anywhere in the 60 bytes that leave the board,
    padding included. The status is checked last and on purpose. Break the
    tag comparison in oca_proto.sv and this test has to fail on plaintext
    reaching the wire, because a failure on the status alone would say only
    that a status changed, which is not the property.

    A second seal follows the failed open, from the same slot and key, and
    must still be correct. An engine left in a bad state by a rejected tag
    is a real failure mode and it is invisible to any test that stops at
    the rejection.
    """
    drv, tx, status, _ = await setup(dut)
    await arp_resolve(dut, drv, tx, status, PEER_A)

    await command(dut, drv, tx, status, PEER_A, build_load_key(0x0301, 1, KEY))

    msg = b"secret payload that must never reach the segment"
    rsp = await command(dut, drv, tx, status, PEER_A,
                        build_seal(0x0302, 1, NONCE, b"", msg))
    assert rsp["status"] == ST_OK, f"seal status {rsp['status']}"
    tag, ct = bytearray(rsp["body"][:16]), rsp["body"][16:]
    assert msg not in rsp["frame"], (
        "the seal put the plaintext on the wire alongside its ciphertext")

    tag[0] ^= 0x01
    rsp = await command(dut, drv, tx, status, PEER_A,
                        build_open(0x0303, 1, NONCE, b"", ct, bytes(tag)))
    assert msg not in rsp["frame"], (
        f"plaintext on the wire after a forged tag: {rsp['frame'].hex()}")
    assert rsp["body"] == b"", (
        f"the failed open answered a body: {rsp['body'].hex()}")
    check_header(rsp, OP_OPEN, 0x0303, slot=1)
    assert rsp["status"] == ST_AUTH_FAIL, (
        f"a forged tag was answered {rsp['status']:#04x}, want "
        f"{ST_AUTH_FAIL:#04x}")

    rsp = await command(dut, drv, tx, status, PEER_A,
                        build_seal(0x0304, 1, NONCE, b"", msg))
    await drain(dut)
    assert rsp["status"] == ST_OK, (
        f"the seal after a rejected tag answered {rsp['status']}")
    want_ct, want_tag = aead_encrypt(KEY, NONCE, b"", msg)
    assert rsp["body"] == want_tag + want_ct, (
        "the rejected open left the engine returning a different seal for "
        "the same key, nonce and message")

    tx.check()
    status.expect(rx_fifo_good_frame=5, tx_fifo_good_frame=5)
    quiet(dut, cnt_accepted=4, watermark=1)


@cocotb.test(timeout_time=400, timeout_unit="us")
async def test_two_peers_in_flight_each_get_their_own_reply(dut):
    """Reply k carries requester k's address, with both requests in flight.

    The two datagrams go onto the wire at the 802.3 minimum gap, so the
    second is inside the stack before the first has been answered. That is
    the condition oca_udp_seam's header queue exists for: udp_ip_rx_64
    presents the requester's address once, at the head of the datagram, and
    it is gone by the time oca_core answers. A seam that read the address
    at reply time instead of queueing it would send both answers to
    whichever peer spoke last, and every payload assertion in this file
    would still pass.

    The two peers differ in MAC, in IP and in source port, and the two
    requests differ in req_id, so nothing correlates by accident. The
    replies are matched on req_id rather than on arrival order -- the
    stack is free to answer in either order, and a positional check would
    either fail a correct design or pass a misaddressed one.

    The watermark is asserted as a range rather than a figure. Two
    datagrams can be in the queue at once and one is enough to answer
    both; what is not acceptable is zero, which would mean the seam never
    queued a header at all.
    """
    drv, tx, status, _ = await setup(dut)
    await arp_resolve(dut, drv, tx, status, PEER_A)
    await arp_resolve(dut, drv, tx, status, PEER_B)

    await send_request(drv, PEER_A, build_stats(0x0401))
    await send_request(drv, PEER_B, build_stats(0x0402))

    frames = [await next_frame(dut, tx, status) for _ in range(2)]
    await drain(dut)

    # Which frame belongs to which peer is decided by the frame's own
    # destination MAC and nothing else, and decode_reply then refuses it if
    # any other field disagrees. Assigning by arrival order and checking the
    # address afterwards would be the same assertion written so that it
    # cannot fail on a swap.
    by_peer = {}
    for frame in frames:
        peers = [p for p in (PEER_A, PEER_B) if p.mac == frame[:6]]
        assert len(peers) == 1, (
            f"a reply went to {frame[:6].hex(':')}, which is neither peer")
        assert peers[0] not in by_peer.values(), (
            f"both replies went to {peers[0]}")
        by_peer[decode_reply(frame, dut, peers[0])["req_id"]] = peers[0]

    assert by_peer == {0x0401: PEER_A, 0x0402: PEER_B}, (
        "each reply must reach the peer whose request carried its req_id; "
        f"got {[(f'{k:#06x}', v) for k, v in by_peer.items()]}")

    tx.check()
    status.expect(rx_fifo_good_frame=4, tx_fifo_good_frame=4)
    watermark = int(dut.hdr_q_watermark.value)
    assert 1 <= watermark <= 2, (
        f"the seam's header queue reached {watermark} for two datagrams in "
        "flight; zero would mean it never queued a header at all")
    quiet(dut, cnt_accepted=2, watermark=watermark)


@cocotb.test(timeout_time=300, timeout_unit="us")
async def test_a_non_udp_frame_does_not_wedge_the_receive_path(dut):
    """One ICMP echo request, and the board must still answer the next UDP one.

    This is the composition property no per-module suite can see. Every
    module in the chain is individually correct: ip_eth_rx_64 parses the
    frame, udp_complete_64.v:289 routes it to the raw IP receive port
    because its protocol is 0x01 and not 0x11, and that port is a real
    output pair with a real handshake. What decides the outcome is one
    line of integration, udp_complete_64.v:361-365:

        ip_rx_ip_hdr_ready = (s_select_udp && udp_rx_ip_hdr_ready) ||
                             (s_select_ip  && m_ip_hdr_ready);

    With m_ip_hdr_ready reading 0 the frame is never consumed, ip_eth_rx_64
    holds it forever, back pressure reaches the MAC receive FIFO -- which
    has none toward the wire (eth_mac_1g_fifo.v:305, RX_DROP_WHEN_FULL at
    oca_eth_mac_1g_fifo_64.v:192) -- and every frame after it is dropped.
    oca_udp_complete_64.v:29-44 named this failure before the top level was
    written; the toolchain printed the warning; nothing gated on it.

    No ICMP reply is expected and none is asserted: this design has no ICMP
    responder and the raw IP port goes nowhere. The property is only that
    the frame is CONSUMED and the path keeps working, so the assertion is
    on the stats reply that follows and on rx_fifo_overflow staying at
    zero.

    The counters make it sharper than a liveness check. The stats body must
    read rx 1, which says the ICMP frame reached the demux and stopped
    there rather than being handed to oca_core as a malformed request, and
    cnt_accepted must be 1 for the same reason one layer up.
    """
    drv, tx, status, _ = await setup(dut)
    await arp_resolve(dut, drv, tx, status, PEER_A)

    await drv.send(wire(icmp_echo_frame(PEER_A)))
    rsp = await command(dut, drv, tx, status, PEER_A, build_stats(0x0501))
    await drain(dut)

    check_header(rsp, OP_STATS, 0x0501)
    assert rsp["status"] == ST_OK, f"stats status {rsp['status']}"
    assert stats_body(rsp) == {"rx": 1, "drop": 0, "done": 0, "auth": 0}, (
        f"the core's counters say {stats_body(rsp)}: the ICMP frame must be "
        "discarded at the demux, never offered to oca_core")

    tx.check()
    status.expect(rx_fifo_good_frame=3, tx_fifo_good_frame=2)
    quiet(dut, cnt_accepted=1, watermark=1)
