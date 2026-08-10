# SPDX-License-Identifier: MIT
"""The 1G MAC's receive path, and the FCS verdict a timing fix must not move.

Why this file exists. oca_top misses its 125 MHz receive constraint at
102.59 MHz, and the critical path is the FCS check inside the vendor's
axis_gmii_rx: crc_next through a 32-bit compare into a register, 5.04 ns.
Breaking that path means editing a vendor module the project had no test
of at all. So the tests come first, against the unpatched module, and
what they have to be sharp about is the one thing the patch can move --
which frames the MAC calls good.

Everything here is driven and observed at the wrapper's ports, in three
clock domains at once: GMII in at 125 MHz, AXI-Stream out at 48.0769 MHz,
and the transmit clock running alongside because the module has one.
Nothing reaches into the hierarchy; a patch is free to restructure the
compare, and these tests only see what came out.

What a passing run rests on, and how each rests on the other:

  * the frames are built by zlib.crc32, appended least significant byte
    first, which is the Ethernet FCS. If that generator were wrong every
    "good frame" test would really be another bad-FCS test, and they
    would all still pass -- but with the good/bad verdict inverted.
    test_the_fcs_is_checked_over_the_frame_least_significant_byte_first
    is what forbids that: it drives the same frame twice, once with the
    FCS the generator produces and once with those four bytes reversed,
    and requires the first accepted and the second rejected. A generator
    that agreed with the MAC by accident cannot satisfy both halves.

  * rx_error_bad_fcs and rx_error_bad_frame are counted as pulses in the
    logic domain, not sampled at a moment. They are toggle-synchroniser
    edge detectors (eth_mac_1g_fifo.v:170-171) one logic_clk wide, and a
    check that read them once would read them almost always low.

  * a frame the MAC marks bad never appears on rx_axis at all, because
    the wrapper sets RX_DROP_BAD_FRAME with RX_FRAME_FIFO: the FIFO
    commits its write pointer at tlast and rolls it back when tuser is
    set. That is observed here rather than assumed -- the assertion is on
    the frame count out of rx_axis, so a build that handed the corrupt
    bytes on instead of dropping them fails on the count, not on a
    missing counter.

  * rx_axis_tkeep is 8'h00 on every beat of every frame, which is wrong,
    which is not this patch's doing, and which is why no assertion here
    uses it to find the end of a frame. The whole of it is written up in
    test_rx_axis_tkeep_is_always_zero_which_is_an_upstream_defect, which
    exists to fail the day the behaviour changes.
"""

import zlib

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge, Timer

# 125 MHz on both GMII clocks and oca_clkrst's 48.0769 MHz in the middle.
# The logic clock is deliberately not a ratio of the other two -- 20.8 / 8 is
# 2.6 -- because everything the receive path reports crosses that boundary
# through a toggle synchroniser, and a harmonic ratio would let a crossing
# that only works at one phase relationship pass.
RX_CLK_NS = 8
TX_CLK_NS = 8
LOGIC_CLK_NS = 20.8

# The three clocks do not start together, for the same reason. On the board
# they come out of one PLL and their edges land where the PLL puts them; here
# nothing would give them a phase relationship at all, and t=0 alignment is
# the one relationship a real design never has.
RX_SKEW_NS = 1.3
TX_SKEW_NS = 3.7

PREAMBLE = b"\x55" * 7 + b"\xd5"

# 802.3 inter-frame gap: 96 bit times, which at GMII is 12 byte times of
# gmii_rx_dv low. This is the minimum, and the minimum is the interesting
# one: it is the shortest window the receive state machine has to get its
# five-deep delay line and its CRC back to a state that can judge the next
# frame on its own merits.
IFG_BYTES = 12

# Locally administered addresses and the local-experimental EtherType, so
# nothing here can be mistaken for real traffic. The MAC parses none of it --
# to the receive path a frame is bytes under a CRC -- but a frame that would
# be illegal on a wire is not a frame worth passing.
DST = bytes.fromhex("02005e000001")
SRC = bytes.fromhex("02005e000002")
ETHERTYPE = bytes.fromhex("88b5")

# The Ethernet minimum and maximum, both counting the FCS. 65 is next to the
# minimum because a 64-byte frame is 60 bytes into a 64-bit stream -- seven
# whole beats and one half -- and 65 moves the partial beat by one byte, which
# is where an off-by-one in the width adapter would live.
MIN_FRAME = 64
MAX_FRAME = 1518

# rx_axis beats a 1514-byte frame occupies, plus the crossing and the FIFO's
# read latency, rounded well up. What it bounds is the wait after the last
# frame goes onto the wire: long enough for everything expected to arrive,
# and long enough afterwards for an unexpected extra frame to be seen.
DRAIN_CYCLES = 320

STATUS = ("rx_error_bad_frame", "rx_error_bad_fcs", "rx_fifo_overflow",
          "rx_fifo_bad_frame", "rx_fifo_good_frame")


def frame(length: int, tag: int) -> bytes:
    """A frame `length` bytes long on the wire, FCS included but not appended.

    The payload is a function of `tag`, so two frames of the same length in
    one test are different frames: a receive path that delivered the first
    one twice would otherwise pass.
    """
    assert MIN_FRAME <= length <= MAX_FRAME, f"{length} is not a legal frame"
    payload = bytes((tag * 37 + 11 * i + 3) & 0xFF for i in range(length - 18))
    return DST + SRC + ETHERTYPE + payload


def fcs(f: bytes) -> bytes:
    """The four FCS bytes in the order they go onto the wire.

    CRC-32 over the frame from the destination address to the end of the
    payload, least significant byte first. zlib.crc32 already applies the
    initial 0xFFFFFFFF and the final inversion, which is what makes it the
    transmitted value rather than the raw register state.
    """
    return zlib.crc32(f).to_bytes(4, "little")


def wire(f: bytes, tail: bytes | None = None) -> bytes:
    """Preamble, start delimiter, frame, FCS -- the byte stream on GMII.

    `tail` replaces the computed FCS, which is the only way to build a frame
    whose FCS is wrong in a stated way rather than wrong by accident.
    """
    return PREAMBLE + f + (fcs(f) if tail is None else tail)


def with_one_payload_byte_altered(f: bytes) -> bytes:
    """One byte of payload flipped, past the 14-byte header.

    A payload byte and not a header byte: the MAC ignores addresses and
    EtherType entirely, so a change there and a change in the payload are the
    same event to it, and the payload is where a real corruption lands.
    """
    i = 20
    return f[:i] + bytes([f[i] ^ 0xFF]) + f[i + 1:]


class GmiiRxDriver:
    """The PHY side of GMII: one byte per rx_clk, and a real gap between frames."""

    def __init__(self, dut):
        self.dut = dut

    async def idle(self, byte_times: int) -> None:
        for _ in range(byte_times):
            await RisingEdge(self.dut.rx_clk)
            self.dut.gmii_rxd.value = 0
            self.dut.gmii_rx_dv.value = 0
            self.dut.gmii_rx_er.value = 0

    async def send(self, octets: bytes, er_at=(), gap: int = IFG_BYTES) -> None:
        """One frame, preamble to FCS, then the gap that must follow it.

        `er_at` is a set of octet indices into `octets` -- indices on the
        wire, preamble included -- where gmii_rx_er is raised for that one
        byte time. A PHY raises RX_ER for a symbol it could not decode; the
        data byte underneath is driven unchanged, which is what makes the
        error the only reason the frame is bad.
        """
        assert gap >= IFG_BYTES, (
            f"{gap} byte times is under the 802.3 inter-frame gap of "
            f"{IFG_BYTES}: no conformant PHY emits that, and a MAC that "
            "needed the slack would pass here and fail on a wire")
        for n, octet in enumerate(octets):
            await RisingEdge(self.dut.rx_clk)
            self.dut.gmii_rxd.value = octet
            self.dut.gmii_rx_dv.value = 1
            self.dut.gmii_rx_er.value = 1 if n in er_at else 0
        await self.idle(gap)


class Received:
    """One frame as it came off rx_axis.

    `octets` is every byte of every beat with tkeep ignored, so it is always a
    whole number of beats long and whatever follows the frame in it is what
    the width adapter left standing in the lanes of the last beat it did not
    write. Only the first len(frame) bytes of it mean anything.
    """

    def __init__(self, octets: bytearray, keeps: list, tuser: int):
        self.octets = bytes(octets)
        self.keeps = keeps
        self.tuser = tuser

    @property
    def beats(self) -> int:
        return len(self.keeps)


class AxisRxMonitor:
    """rx_axis reassembled by beat, because tkeep here carries nothing."""

    def __init__(self, dut):
        self.dut = dut
        self.frames = []       # Received, in arrival order
        self._octets = bytearray()
        self._keeps = []

    def check(self) -> None:
        assert not self._keeps, (
            f"{len(self._keeps)} beat(s) were delivered without a tlast: a "
            "frame is still open on rx_axis")

    async def run(self) -> None:
        d = self.dut
        while True:
            await RisingEdge(d.logic_clk)
            await ReadOnly()
            if not (int(d.rx_axis_tvalid.value) and int(d.rx_axis_tready.value)):
                continue
            self._octets += int(d.rx_axis_tdata.value).to_bytes(8, "little")
            self._keeps.append(int(d.rx_axis_tkeep.value))
            if int(d.rx_axis_tlast.value):
                self.frames.append(Received(self._octets, self._keeps,
                                            int(d.rx_axis_tuser.value)))
                self._octets, self._keeps = bytearray(), []


class StatusMonitor:
    """Every receive status wire, counted as pulses in the logic domain.

    All five are one logic_clk wide by construction -- every one of them is
    the exclusive-or of two stages of a toggle synchroniser, two in
    eth_mac_1g_fifo and three in the receive FIFO -- so counting edges is the
    only reading that means anything.
    """

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

        Naming only what should have happened would let a test pass while the
        MAC also reported an overflow or a second bad frame nobody asked
        about, which is exactly the leak between frames these tests exist to
        catch.
        """
        want = dict.fromkeys(STATUS, 0) | wanted
        assert self.counts == want, (
            f"status counts {self.counts}, want {want}")


async def _release(clk, rst) -> None:
    await RisingEdge(clk)
    rst.value = 0


async def setup(dut):
    """Three clocks, three resets released each to its own clock, two monitors.

    The reset polarity here is the vendor's, active HIGH, and asynchronously
    asserted -- posedge rst appears in the sensitivity list of both the status
    synchronisers and the FIFOs. Each one is deasserted just after a rising
    edge of the clock its logic runs on, which is the wrapper's documented
    requirement and the one that keeps two edge detectors from leaving reset
    on different edges.
    """
    dut.rx_rst.value = 1
    dut.tx_rst.value = 1
    dut.logic_rst.value = 1

    dut.gmii_rxd.value = 0
    dut.gmii_rx_dv.value = 0
    dut.gmii_rx_er.value = 0

    dut.tx_axis_tdata.value = 0
    dut.tx_axis_tkeep.value = 0
    dut.tx_axis_tvalid.value = 0
    dut.tx_axis_tlast.value = 0
    dut.tx_axis_tuser.value = 0

    dut.rx_axis_tready.value = 1

    dut.rx_clk_enable.value = 1
    dut.tx_clk_enable.value = 1
    dut.rx_mii_select.value = 0
    dut.tx_mii_select.value = 0

    dut.cfg_ifg.value = IFG_BYTES
    dut.cfg_tx_enable.value = 1
    dut.cfg_rx_enable.value = 1

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
    cocotb.start_soon(_release(dut.logic_clk, dut.logic_rst))

    # Both FIFOs build their own three-stage reset synchronisers in each
    # domain, and hold s_axis_tready low until they clear.
    await Timer(500, unit="ns")

    drv = GmiiRxDriver(dut)
    mon = AxisRxMonitor(dut)
    status = StatusMonitor(dut)
    cocotb.start_soon(mon.run())
    cocotb.start_soon(status.run())
    return drv, mon, status


async def drain(dut, cycles: int = DRAIN_CYCLES) -> None:
    for _ in range(cycles):
        await RisingEdge(dut.logic_clk)


def beats_for(length: int) -> int:
    """The rx_axis beats a frame of `length` bytes occupies at 64 bits."""
    return -(-length // 8)


def delivered(mon, expected: list) -> None:
    """Assert rx_axis carried exactly these frames, in this order, and no more.

    Length is asserted as a beat count and content as the leading bytes of
    those beats, because tkeep cannot say where the last beat ends here --
    test_rx_axis_tkeep_is_always_zero_which_is_an_upstream_defect is where
    that is pinned. A frame one byte longer or shorter than expected still
    changes its beat count for seven of every eight lengths, and a frame with
    a byte wrong fails on the content whatever its length.

    tuser is refused rather than reported: the receive FIFO is configured to
    drop frames marked bad, so one arriving here means the drop did not
    happen, and nothing downstream -- oca_udp_seam counts tuser and answers
    the request anyway -- would stop it.
    """
    mon.check()
    got_beats = [r.beats for r in mon.frames]
    want_beats = [beats_for(len(f)) for f in expected]
    assert got_beats == want_beats, (
        f"rx_axis delivered {len(mon.frames)} frame(s) of {got_beats} beat(s), "
        f"want {len(expected)} of {want_beats}")
    for n, (r, f) in enumerate(zip(mon.frames, expected)):
        assert r.tuser == 0, (
            f"frame {n} arrived on rx_axis with tuser set: the receive FIFO is "
            "configured to drop those, not to forward them")
        assert r.octets[:len(f)] == f, (
            f"frame {n} differs from the one that was sent, first at byte "
            f"{next((i for i, (a, b) in enumerate(zip(r.octets, f)) if a != b), '?')}")

        # And the FCS must not follow it. A beat count cannot see this: 60
        # and 61 bytes are both 8 beats, so a MAC that carried one FCS byte
        # through looks identical above. It is not a harmless extra byte
        # either -- tkeep is 8'h00 on every beat here (the upstream defect
        # test 2 pins), so nothing downstream can tell where the frame ends,
        # and eth_axis_rx would parse that byte as payload.
        #
        # This exists because it was measured missing: a patch that moves
        # the FCS check off the critical path by extending the state machine
        # naturally emits one extra byte, and the suite passed it 8/8 on
        # every length until this assertion was added.
        tail = r.octets[len(f):]
        want_fcs = fcs(f)
        leaked = 0
        while (leaked < len(tail) and leaked < len(want_fcs)
               and tail[leaked] == want_fcs[leaked]):
            leaked += 1
        assert leaked == 0, (
            f"frame {n} was followed by {leaked} byte(s) of its own FCS "
            f"({want_fcs[:leaked].hex()}): the MAC is meant to strip all four, "
            f"and with tkeep tied to zero nothing downstream can tell")


@cocotb.test(timeout_time=400, timeout_unit="us")
async def test_a_good_frame_arrives_whole_and_is_counted(dut):
    """(a) A well-formed minimum-length frame, end to end.

    64 bytes on the wire is 60 into rx_axis once the MAC has stripped the
    FCS, and the assertion is on all 60 of them: a MAC that delivered the
    right length with one byte wrong, or that carried the FCS through into
    the stream, fails on the bytes rather than on the count.
    """
    drv, mon, status = await setup(dut)
    f = frame(MIN_FRAME, tag=1)

    await drv.send(wire(f))
    await drain(dut)

    delivered(mon, [f])
    status.expect(rx_fifo_good_frame=1)


@cocotb.test(timeout_time=400, timeout_unit="us")
async def test_rx_axis_tkeep_is_always_zero_which_is_an_upstream_defect(dut):
    """What rx_axis_tkeep does, pinned here because it is wrong.

    Every beat of every received frame arrives with tkeep == 8'h00, the last
    one included, so nothing reading rx_axis can tell how long a frame is.
    This is not the FCS path and no patch to it will move this: it is
    axis_adapter's upsize branch writing s_axis_tkeep into the wide keep
    register whatever S_KEEP_ENABLE says (axis_adapter.v:178 and :181), while
    eth_mac_1g_fifo ties that port to a literal 0 because it set
    S_KEEP_ENABLE(0) (eth_mac_1g_fifo.v:304). The parameter's documented
    meaning -- "if disabled, tkeep assumed to be 1'b1" -- is honoured in the
    bypass branch (:126) and not in this one.

    It does not stop here. oca_top wires mac_rx_tkeep straight into
    oca_eth_axis_64 (oca_top.sv:241), and eth_axis_rx reads tkeep to find
    where a frame ends.

    The test exists so that the timing fix cannot be charged with this, and
    so that the day it changes -- a vendor bump, a KEEP_ENABLE corrected, a
    patch that touches the receive width adapter -- this file goes red and
    somebody has to decide about it, instead of every length assertion here
    quietly resting on a different shape.
    """
    drv, mon, status = await setup(dut)
    f = frame(MIN_FRAME, tag=9)

    await drv.send(wire(f))
    await drain(dut)

    delivered(mon, [f])
    keeps = mon.frames[0].keeps
    assert keeps == [0] * beats_for(len(f)), (
        "rx_axis_tkeep came out as "
        f"{[format(k, '#04x') for k in keeps]}: the upstream defect this test "
        "pins has moved, and every length assertion in this file is written "
        "around it")
    status.expect(rx_fifo_good_frame=1)


@cocotb.test(timeout_time=400, timeout_unit="us")
async def test_the_fcs_is_checked_over_the_frame_least_significant_byte_first(dut):
    """The generator, proved against the MAC in both directions.

    Every other test in this file is built on zlib.crc32 over the frame with
    the result appended least significant byte first. A generator that were
    wrong would make every "good frame" test a bad-FCS test with its verdict
    inverted, and all of them would still pass.

    So: the same frame twice, once with those four bytes and once with them
    reversed. The MAC must accept one and reject the other. Reversing is the
    sharp mutation of the two candidate byte orders -- it is the mistake
    anyone writing this generator actually makes -- and no generator can pass
    both halves by coincidence.
    """
    drv, mon, status = await setup(dut)
    f = frame(MIN_FRAME, tag=2)

    await drv.send(wire(f))
    await drv.send(wire(f, tail=fcs(f)[::-1]))
    await drain(dut)

    delivered(mon, [f])
    status.expect(rx_fifo_good_frame=1, rx_error_bad_frame=1,
                  rx_error_bad_fcs=1, rx_fifo_bad_frame=1)


@cocotb.test(timeout_time=400, timeout_unit="us")
async def test_a_bad_fcs_is_rejected_counted_and_never_reaches_rx_axis(dut):
    """(b) One payload byte altered under an otherwise correct FCS.

    Three separate claims, and the frame count is the one that matters: the
    wrapper sets RX_DROP_BAD_FRAME with RX_FRAME_FIFO, so the FIFO rolls its
    write pointer back at the bad tlast and the frame does not reach rx_axis
    at all -- not as good data, and not as data with tuser set either.
    """
    drv, mon, status = await setup(dut)
    f = frame(MIN_FRAME, tag=3)
    corrupt = with_one_payload_byte_altered(f)

    await drv.send(wire(corrupt, tail=fcs(f)))
    await drain(dut)

    delivered(mon, [])
    status.expect(rx_error_bad_frame=1, rx_error_bad_fcs=1, rx_fifo_bad_frame=1)


@cocotb.test(timeout_time=400, timeout_unit="us")
async def test_a_receive_error_mid_frame_is_a_bad_frame_and_not_a_bad_fcs(dut):
    """(c) gmii_rx_er for one byte time in the middle of the payload.

    The data underneath is untouched and the FCS is the right one, so the
    receive error is the only thing wrong with this frame. That is what makes
    the test bite: a MAC that ignored gmii_rx_er would find a perfectly good
    frame and deliver it.

    rx_error_bad_fcs must stay at zero. The error branch leaves the payload
    state before the end of the frame, so no FCS comparison is ever made, and
    a design that reported both would be reporting an FCS failure it did not
    measure.
    """
    drv, mon, status = await setup(dut)
    f = frame(MIN_FRAME, tag=4)
    stream = wire(f)
    er_index = len(PREAMBLE) + 30

    await drv.send(stream, er_at={er_index})
    await drain(dut)

    delivered(mon, [])
    status.expect(rx_error_bad_frame=1, rx_fifo_bad_frame=1)


@cocotb.test(timeout_time=400, timeout_unit="us")
async def test_two_good_frames_at_the_minimum_gap_both_arrive(dut):
    """(d) Two good frames, 12 byte times apart, different contents.

    The gap is the 802.3 minimum, which is the shortest the receive path ever
    gets to reset its CRC and drain a five-deep delay line before it must
    judge the next frame. Different payloads in the two frames, so a path
    that delivered the first one twice fails on the bytes.
    """
    drv, mon, status = await setup(dut)
    first = frame(MIN_FRAME, tag=5)
    second = frame(MIN_FRAME, tag=6)
    assert first != second

    await drv.send(wire(first))
    await drv.send(wire(second))
    await drain(dut)

    delivered(mon, [first, second])
    status.expect(rx_fifo_good_frame=2)


@cocotb.test(timeout_time=400, timeout_unit="us")
async def test_a_good_frame_right_after_a_bad_fcs_one_is_still_good(dut):
    """(e) The case a restructured compare is most likely to break.

    A bad frame's verdict is formed from crc_state and from four bytes of
    delay line. Both have to be gone before the next frame's verdict is
    formed, and the only thing that separates them is the minimum gap. A
    compare moved into a later cycle, or a register added between crc_next
    and the comparison, is exactly the change that would leave one of them
    standing.
    """
    drv, mon, status = await setup(dut)
    bad = frame(MIN_FRAME, tag=7)
    good = frame(MIN_FRAME, tag=8)

    await drv.send(wire(with_one_payload_byte_altered(bad), tail=fcs(bad)))
    await drv.send(wire(good))
    await drain(dut)

    delivered(mon, [good])
    status.expect(rx_error_bad_frame=1, rx_error_bad_fcs=1, rx_fifo_bad_frame=1,
                  rx_fifo_good_frame=1)


@cocotb.test(timeout_time=400, timeout_unit="us")
async def test_frames_of_several_lengths_arrive_whole(dut):
    """(f) 64, 65, 256 and 1518 bytes, back to back at the minimum gap.

    64 is the Ethernet minimum and lands 60 bytes on rx_axis: seven whole
    64-bit beats and a partial one. 65 moves that partial beat by a byte,
    which is where an off-by-one in the 8-to-64 width adapter would live.
    1518 is the maximum, 1514 bytes and 190 beats after the FCS is stripped,
    long enough that the FIFO is being read while it is still being written.
    """
    drv, mon, status = await setup(dut)
    # 68 and 260 land exactly on a beat boundary once the FCS is stripped
    # (64 and 256 bytes, 8 and 32 whole beats, nothing partial). That is the
    # case axis_adapter.v:192 takes only when tlast and the last segment
    # coincide, and no other length here reaches it: 64, 65, 256 and 1518
    # leave 4, 5, 4 and 2 bytes in their final beat.
    sent = [frame(length, tag=10 + n)
            for n, length in enumerate((MIN_FRAME, 65, 68, 256, 260, MAX_FRAME))]

    for f in sent:
        await drv.send(wire(f))
    await drain(dut)

    delivered(mon, sent)
    status.expect(rx_fifo_good_frame=len(sent))
