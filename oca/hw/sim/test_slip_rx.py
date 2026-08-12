# SPDX-License-Identifier: MIT
"""The SLIP decoder, held to what oca_proto cannot survive without.

Four properties of the AXI-Stream side are not negotiable, and each of
them is checked on every beat of every test rather than in a test of its
own, because the way they fail is by being violated once in a corner:

  byte 0 in tdata[7:0]      oca_proto reads MAGIC out of hdr[15:0] and
                            oca_proto.sv:215 pins byte 0 at 0x4F, so a
                            big-endian beat is a bad-magic status.
  tkeep 0xFF before tlast   oca_proto.sv:644-645 raises keep_bad on
                            anything else and answers status 05.
  tkeep contiguous and
  right-justified on tlast  oca_proto.sv:293-298 is a priority encoder
                            that takes the highest bit set plus one, so
                            a gap in the middle over-counts in silence.
  nothing past tkeep        the bytes a partial beat carries past the
                            frame are not the frame's; asserting they
                            are zero is what makes every test here a
                            witness that the last word is masked.

The refusals get a test each. Their shared property is the one a status
code cannot express: the frame must not reach the stream at all, so each
of those tests asserts that tvalid was never once seen high.
"""

import os

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from slip_model import END, ESC, ESC_END, ESC_ESC, encode

CLK_NS = 40

# The build decides BYTES and the tests must agree with it, so the runner
# passes the same number here that it elaborated the module with.
BYTES = int(os.environ.get("OCA_SLIP_BYTES", "2048"))
WORDS = BYTES // 8
MIN_BYTES = 8


def payload(n: int, seed: int = 0) -> bytes:
    """n bytes that are not all alike and are not END or ESC.

    Kept off the two framing values so that a test about lengths is
    about lengths: the escape path has its own tests and doubling a
    byte there would move every beat boundary with it.
    """
    out = bytearray()
    b = seed
    while len(out) < n:
        b = (b * 7 + 13) & 0xFF
        if b not in (END, ESC):
            out.append(b)
    return bytes(out)


class ByteSource:
    """oca_fifo's read port, driven from a queue.

    rx_data is combinational off the FIFO memory and rx_valid is
    !empty, so the byte on the wire is the head of the queue and it
    leaves on the edge where rx_pop is high.

    A pop raised while rx_valid is low is recorded, not tolerated.
    oca_fifo guards its own pointer with `pop && !empty` and would
    absorb it, so a bridge that popped blindly would pass every test
    written against that FIFO and lose a byte against any source
    without the guard -- a test double must refuse what the real thing
    refuses.
    """

    def __init__(self):
        self.queue = []
        self.illegal_pops = 0

    def send(self, data: bytes):
        self.queue.extend(data)


async def drive_bytes(dut, src):
    while True:
        if src.queue:
            dut.rx_data.value = src.queue[0]
            dut.rx_valid.value = 1
        else:
            dut.rx_data.value = 0
            dut.rx_valid.value = 0
        await ReadOnly()
        popped = int(dut.rx_pop.value) == 1
        if popped and not src.queue:
            src.illegal_pops += 1
        await RisingEdge(dut.clk)
        if popped and src.queue:
            src.queue.pop(0)


class AxisSink:
    """Collector and monitor in one: what came out, and how it came out."""

    def __init__(self):
        self.frames = []
        self.keeps = []
        self.errors = []
        self.saw_valid = False
        self.stalled_beats = 0
        self._cur = bytearray()
        self._cur_keeps = []

    def fail(self, msg: str):
        self.errors.append(msg)

    def take(self, data: int, keep: int, last: bool, cycle: int):
        n = keep.bit_count()
        if n == 0:
            self.fail(f"cycle {cycle}: a beat carrying no bytes was offered")
        if keep != (1 << n) - 1:
            self.fail(f"cycle {cycle}: tkeep {keep:#04x} is not contiguous "
                      f"and right-justified; oca_proto.sv:293-298 reads the "
                      f"highest bit set plus one and would over-count it")
        if not last and keep != 0xFF:
            self.fail(f"cycle {cycle}: a non-final beat carries tkeep "
                      f"{keep:#04x}; oca_proto.sv:644-645 answers status 05")
        raw = data.to_bytes(8, "little")
        if raw[n:] != bytes(8 - n):
            self.fail(f"cycle {cycle}: beat leaks {8 - n} bytes past tkeep "
                      f"(tdata {data:#018x}, tkeep {keep:#04x})")
        self._cur += raw[:n]
        self._cur_keeps.append(keep)
        if last:
            self.frames.append(bytes(self._cur))
            self.keeps.append(list(self._cur_keeps))
            self._cur = bytearray()
            self._cur_keeps = []


async def drain_axis(dut, sink, ready=None):
    """Take beats, and hold the master to the handshake.

    A beat offered and not taken must still be there, unchanged, on the
    next cycle: tvalid may not fall before tready and the payload may
    not move under it. A master that recomputes its output from a
    counter breaks exactly this, and no payload assertion can see it,
    because the beat it corrupts is one the sink never took.

    `stalled_beats` is here because that check alone is blind to the
    worse violation. A master whose tvalid is ANDed with tready never
    shows a beat offered and not taken, so `held` is never armed and
    nothing fires -- measured: the mutation left all twelve tests green.
    Counting the cycles where a beat stood against a low tready turns
    the property positive, and the stall tests assert it moved.
    """
    held = None
    cycle = 0
    while True:
        dut.m_axis_tready.value = 1 if ready is None else int(ready(cycle))
        await ReadOnly()
        valid = int(dut.m_axis_tvalid.value) == 1
        rdy = int(dut.m_axis_tready.value) == 1
        beat = (int(dut.m_axis_tdata.value),
                int(dut.m_axis_tkeep.value),
                int(dut.m_axis_tlast.value) == 1)
        if valid:
            sink.saw_valid = True
            if not rdy:
                sink.stalled_beats += 1
        if held is not None:
            if not valid:
                sink.fail(f"cycle {cycle}: tvalid dropped before tready")
            elif beat != held:
                sink.fail(f"cycle {cycle}: the beat moved while tvalid was "
                          f"held: {held} then {beat}")
        held = beat if (valid and not rdy) else None
        if valid and rdy:
            sink.take(beat[0], beat[1], beat[2], cycle)
        await RisingEdge(dut.clk)
        cycle += 1


async def setup(dut, ready=None):
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    dut.rst_n.value = 0
    dut.rx_data.value = 0
    dut.rx_valid.value = 0
    dut.m_axis_tready.value = 1
    await ClockCycles(dut.clk, 4)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)
    src = ByteSource()
    sink = AxisSink()
    cocotb.start_soon(drive_bytes(dut, src))
    cocotb.start_soon(drain_axis(dut, sink, ready))
    # The buffer is walked out of reset, one word per cycle, and no byte
    # is accepted until it is done.
    await ClockCycles(dut.clk, WORDS + 4)
    return src, sink


async def flush(dut, src, extra: int = 64):
    """Wait for every queued byte to be taken, then for the frame to leave."""
    budget = 8 * len(src.queue) + 8 * WORDS + 512
    for _ in range(budget):
        if not src.queue:
            break
        await RisingEdge(dut.clk)
    else:
        raise AssertionError(f"{len(src.queue)} bytes were never consumed "
                             f"in {budget} cycles")
    await ClockCycles(dut.clk, extra + 2 * WORDS)


def check(src, sink):
    assert src.illegal_pops == 0, \
        f"rx_pop was raised {src.illegal_pops} times with no byte offered"
    assert sink.errors == [], "\n".join(sink.errors)


def counters(dut) -> tuple:
    return (int(dut.cnt_short.value),
            int(dut.cnt_long.value),
            int(dut.cnt_esc.value))


@cocotb.test()
async def test_round_trip_of_a_payload_that_needs_escaping(dut):
    """The escape path is the point: 0xC0 and 0xDB must survive it.

    A frame carrying the two framing bytes is the one a length prefix
    would have carried unchanged and SLIP cannot. Both escaped forms
    appear unescaped in the payload as well (0xDC, 0xDD), so a decoder
    that unescapes a byte it never escaped is caught here too.
    """
    src, sink = await setup(dut)
    body = bytes([0x4F, 0x43, 0x01, 0x02, END, ESC, ESC_END, ESC_ESC,
                  END, END, ESC, ESC, 0x00, 0xFF, ESC, END])
    src.send(encode(body))
    await flush(dut, src)

    check(src, sink)
    assert sink.frames == [body], \
        f"the frame came back as {sink.frames}, want {body!r}"
    assert counters(dut) == (0, 0, 0), \
        f"a good frame moved a refusal counter: {counters(dut)}"


@cocotb.test()
async def test_final_beat_keep_for_every_length(dut):
    """The last beat's tkeep, asserted for every length modulo 8.

    Including the aligned case, which is the one a `(1 << len%8) - 1`
    written without its zero branch turns into tkeep 0x00: oca_proto
    would read a beat carrying no bytes, and the packet would be eight
    bytes short with every length check still agreeing.
    """
    src, sink = await setup(dut)
    lengths = list(range(MIN_BYTES, MIN_BYTES + 17))
    for n in lengths:
        src.send(encode(payload(n, seed=n)))
    await flush(dut, src)

    check(src, sink)
    assert len(sink.frames) == len(lengths), \
        f"{len(sink.frames)} frames arrived, want {len(lengths)}"
    for n, frame, keeps in zip(lengths, sink.frames, sink.keeps):
        assert frame == payload(n, seed=n), f"length {n} came back wrong"
        rest = ((n - 1) % 8) + 1
        want = [0xFF] * (len(keeps) - 1) + [(1 << rest) - 1]
        assert keeps == want, (
            f"length {n} produced tkeep {[hex(k) for k in keeps]}, "
            f"want {[hex(k) for k in want]}")


@cocotb.test()
async def test_back_to_back_frames(dut):
    """No idle byte between the END and the next frame's first byte."""
    src, sink = await setup(dut)
    bodies = [payload(8, 1), payload(23, 2), payload(9, 3), payload(16, 4)]
    stream = b"".join(encode(b) for b in bodies)
    src.send(stream)
    await flush(dut, src)

    check(src, sink)
    assert sink.frames == bodies, \
        f"back to back gave {len(sink.frames)} frames: {sink.frames}"


@cocotb.test()
async def test_a_frame_shorter_than_a_header_is_refused(dut):
    """oca_proto.sv:779-785 answers such a request with nothing at all.

    On UDP that path was unreachable, because oca_udp_seam sank a short
    datagram before its header was ever enqueued (oca_udp_seam.sv:52-62).
    On a byte stream nothing else stands between the host and a request
    that is never answered, so the guard has to be here, and the host
    has to be able to see it happened.
    """
    src, sink = await setup(dut)
    src.send(encode(payload(MIN_BYTES - 1)))
    await flush(dut, src)

    check(src, sink)
    assert not sink.saw_valid, "a short frame reached the AXI-Stream output"
    assert counters(dut) == (1, 0, 0), \
        f"a short frame left the counters at {counters(dut)}"

    # And it must not have wedged the decoder.
    body = payload(24, 9)
    src.send(encode(body))
    await flush(dut, src)
    check(src, sink)
    assert sink.frames == [body], \
        f"after a short frame the next one came back as {sink.frames}"
    assert counters(dut) == (1, 0, 0), \
        f"a good frame after a short one moved a counter: {counters(dut)}"


@cocotb.test()
async def test_a_frame_longer_than_the_buffer_is_refused(dut):
    """One byte past BYTES, and none of it downstream.

    oca_pktbuf drops writes past its own BYTES and turns them into a
    length error, so forwarding this would be answered -- but answered
    about a request the host did not send, out of a prefix of the one
    it did.
    """
    src, sink = await setup(dut)
    src.send(encode(payload(BYTES + 1, 5)))
    await flush(dut, src)

    check(src, sink)
    assert not sink.saw_valid, "an oversize frame reached the output"
    assert counters(dut) == (0, 1, 0), \
        f"an oversize frame left the counters at {counters(dut)}"

    # A frame of exactly BYTES is the largest legal one and must pass.
    body = payload(BYTES, 6)
    src.send(encode(body))
    await flush(dut, src)
    check(src, sink)
    assert sink.frames == [body], \
        f"a frame of exactly BYTES bytes came back as {len(sink.frames)} frames"
    assert counters(dut) == (0, 1, 0), \
        f"the largest legal frame moved a counter: {counters(dut)}"


@cocotb.test()
async def test_a_bad_escape_is_refused(dut):
    """ESC followed by neither ESC_END nor ESC_ESC.

    RFC 1055 leaves the reaction open. Passing the frame on would put a
    byte the host never sent into a request it will be answered about,
    and on an `open` that reads back as an authentication failure --
    a transport error wearing a cryptographic status code.
    """
    src, sink = await setup(dut)
    good = payload(24, 7)
    src.send(bytes(good[:10]) + bytes([ESC, 0x41]) + bytes(good[10:]) +
             bytes([END]))
    await flush(dut, src)

    check(src, sink)
    assert not sink.saw_valid, "a frame with a bad escape reached the output"
    assert counters(dut) == (0, 0, 1), \
        f"a bad escape left the counters at {counters(dut)}"

    src.send(encode(good))
    await flush(dut, src)
    check(src, sink)
    assert sink.frames == [good], \
        f"after a bad escape the next frame came back as {sink.frames}"


@cocotb.test()
async def test_an_escape_dangling_at_the_end_is_refused(dut):
    """ESC immediately before END: END still terminates, and it counts.

    Letting the escape swallow the END would be the one corruption SLIP
    is chosen to be immune to -- a lost frame boundary, and every frame
    after it shifted by one.
    """
    src, sink = await setup(dut)
    src.send(bytes(payload(24, 8)) + bytes([ESC, END]))
    await flush(dut, src)

    check(src, sink)
    assert not sink.saw_valid, "a frame ending on a dangling ESC reached the output"
    assert counters(dut) == (0, 0, 1), \
        f"a dangling escape left the counters at {counters(dut)}"

    body = payload(16, 11)
    src.send(encode(body))
    await flush(dut, src)
    check(src, sink)
    assert sink.frames == [body], \
        f"the END after a dangling ESC did not terminate the frame: {sink.frames}"


@cocotb.test()
async def test_empty_frames_are_discarded_in_silence(dut):
    """Two ENDs in a row are ordinary SLIP, not an error.

    RFC 1055 has senders emit a leading END to flush line noise, so a
    peer that does it must not raise a counter on this side once per
    frame -- a counter that moves for normal traffic is a counter the
    operator stops reading.
    """
    src, sink = await setup(dut)
    body = payload(16, 12)
    src.send(bytes([END, END, END]) + encode(body) + bytes([END, END]))
    await flush(dut, src)

    check(src, sink)
    assert sink.frames == [body], \
        f"empty frames produced {len(sink.frames)} frames: {sink.frames}"
    assert counters(dut) == (0, 0, 0), \
        f"an empty frame was counted as an error: {counters(dut)}"


@cocotb.test()
async def test_resynchronisation_after_garbage(dut):
    """Garbage, then END, then a frame that must arrive intact.

    This is the property SLIP was chosen for and a length prefix does
    not have: whatever state the decoder was left in, the next END puts
    it back at a frame boundary. The garbage carries a bad escape on
    purpose, so the recovery is from an error state and not merely from
    the middle of a frame.
    """
    src, sink = await setup(dut)
    body = payload(40, 13)
    src.send(bytes([0x11, ESC, 0x41, 0x22, ESC, 0x33, END]))
    src.send(encode(body))
    await flush(dut, src)

    check(src, sink)
    assert sink.frames == [body], \
        f"after garbage the good frame came back as {sink.frames}"
    assert counters(dut) == (0, 0, 1), \
        f"the garbage left the counters at {counters(dut)}"


@cocotb.test()
async def test_a_sink_that_never_becomes_ready(dut):
    """tready held low for far longer than a frame takes to arrive.

    The frame arriving afterwards is the weaker half: a master that
    only raised tvalid when tready was already high would deliver it
    too, and the stability check cannot see that one at all because a
    beat offered and not taken never exists. So the beat must be
    observed standing against a low tready before anything else is
    asserted.
    """
    stall = 500
    src, sink = await setup(dut, ready=lambda c: c > stall)
    body = payload(33, 14)
    src.send(encode(body))
    await flush(dut, src, extra=stall + 64)

    check(src, sink)
    assert sink.stalled_beats > 0, (
        "tvalid was never once high while tready was low: the master is "
        "waiting for tready before offering a beat, which AXI-Stream forbids "
        "and which deadlocks against a slave that waits for tvalid")
    assert sink.frames == [body], \
        f"after a {stall}-cycle stall the frame came back as {sink.frames}"


@cocotb.test()
async def test_a_sink_that_is_ready_every_other_cycle(dut):
    """Backpressure taken and released repeatedly across one frame."""
    src, sink = await setup(dut, ready=lambda c: (c % 3) == 0)
    bodies = [payload(31, 15), payload(8, 16)]
    src.send(b"".join(encode(b) for b in bodies))
    await flush(dut, src, extra=8 * WORDS + 128)

    check(src, sink)
    assert sink.stalled_beats > 0, \
        "no beat ever stood against a low tready across two whole frames"
    assert sink.frames == bodies, \
        f"under backpressure the frames came back as {sink.frames}"


@cocotb.test()
async def test_reset_zeroises_the_frame_buffer(dut):
    """The bytes of the last request must not outlive a reset.

    This buffer holds whatever the host last sent -- and for a load-key
    command that is the 32 raw key bytes, on their way to the slot
    (Security.md, "The packet buffers are walked, not reset").
    oca_pktbuf is walked out of reset for exactly this reason and this
    buffer holds the same bytes one stage earlier.

    Every word is read back before the reset, so the check after it
    cannot pass on a buffer that was never written.
    """
    src, sink = await setup(dut)
    body = payload(BYTES, 17)
    src.send(encode(body))
    await flush(dut, src)
    check(src, sink)
    assert sink.frames == [body], "the frame under test did not arrive"

    before = [int(dut.mem[a].value) for a in range(WORDS)]
    assert all(w != 0 for w in before), \
        "the buffer was not full of the frame before the reset"

    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 3)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, WORDS + 4)

    after = [int(dut.mem[a].value) for a in range(WORDS)]
    leaked = [a for a, w in enumerate(after) if w != 0]
    assert not leaked, \
        f"{len(leaked)} words still hold the last request after reset: " \
        f"word {leaked[0]} reads {after[leaked[0]]:#018x}"
