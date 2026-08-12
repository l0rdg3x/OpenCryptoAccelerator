# SPDX-License-Identifier: MIT
"""The SLIP encoder, driven at the two handshakes it sits between.

What it must get right is small and each part of it is a way to corrupt
a response silently:

  little-endian            byte 0 of the response is tdata[7:0], the
                           same order oca_proto builds its header in.
  tkeep and nothing else   oca_proto.sv:556-564 masks the final beat's
                           bytes past the response length, so the bytes
                           are already zero -- but they are zero, not
                           absent, and a walk that trusted the data
                           instead of the count would emit them.
  escape both values       an unescaped 0xC0 inside a response ends the
                           frame early, and the host reads a truncated
                           reply with no indication that it is one.
  push only when ready     tx_push raised against a full FIFO is a byte
                           deleted from the middle of a reply.

Every expected byte stream comes from slip_model.encode over the
payload, so nothing here is a hand-written escape sequence.
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from slip_model import END, ESC, ESC_END, ESC_ESC, decode, encode

CLK_NS = 40


def payload(n: int, seed: int = 0) -> bytes:
    out = bytearray()
    b = seed
    while len(out) < n:
        b = (b * 7 + 13) & 0xFF
        out.append(b)
    return bytes(out)


def beats_of(pkt: bytes):
    """Little-endian beats, only the last one partial."""
    beats = []
    for off in range(0, len(pkt), 8):
        chunk = pkt[off:off + 8]
        beats.append((int.from_bytes(chunk.ljust(8, b"\x00"), "little"),
                      (1 << len(chunk)) - 1))
    return beats


class ByteSink:
    """The transmit FIFO's write port, and a record of what it refused.

    `illegal` is the whole point of collecting separately. Filtering on
    `tx_push and tx_ready` is what a correct FIFO does, and a test that
    did the same would tidy away every push made while the FIFO was
    full and pass on a module that dropped bytes into a full queue --
    the mutation that survived five of five runs in test_console.py.
    """

    def __init__(self):
        self.out = bytearray()
        self.illegal = []


async def collect(dut, sink, ready=None):
    cycle = 0
    while True:
        dut.tx_ready.value = 1 if ready is None else int(ready(cycle))
        await ReadOnly()
        if int(dut.tx_push.value) == 1:
            if int(dut.tx_ready.value) == 1:
                sink.out.append(int(dut.tx_data.value))
            else:
                sink.illegal.append(int(dut.tx_data.value))
        await RisingEdge(dut.clk)
        cycle += 1


async def setup(dut, ready=None):
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    dut.rst_n.value = 0
    dut.s_axis_tdata.value = 0
    dut.s_axis_tkeep.value = 0
    dut.s_axis_tvalid.value = 0
    dut.s_axis_tlast.value = 0
    dut.tx_ready.value = 1
    await ClockCycles(dut.clk, 4)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)
    sink = ByteSink()
    cocotb.start_soon(collect(dut, sink, ready))
    return sink


async def send_beat(dut, data: int, keep: int, last: bool):
    """Offer one beat and hold it until tready is seen high.

    The handshake is sampled before the transfer edge, never after it:
    reading tready after the edge is reading what the slave offered
    before the transfer, and RTL shaped around such a source grows a
    tready that outlives the state consuming the beat.
    """
    dut.s_axis_tdata.value = data
    dut.s_axis_tkeep.value = keep
    dut.s_axis_tlast.value = 1 if last else 0
    dut.s_axis_tvalid.value = 1
    while True:
        await ReadOnly()
        ready = int(dut.s_axis_tready.value) == 1
        await RisingEdge(dut.clk)
        if ready:
            return


async def send_frame(dut, pkt: bytes, drop_valid: bool = True):
    beats = beats_of(pkt)
    for i, (data, keep) in enumerate(beats):
        await send_beat(dut, data, keep, i == len(beats) - 1)
    if drop_valid:
        dut.s_axis_tvalid.value = 0
        dut.s_axis_tlast.value = 0


def check(sink):
    assert sink.illegal == [], (
        f"tx_push was raised {len(sink.illegal)} times with tx_ready low; a "
        f"FIFO would have refused those bytes and the reply would be short")


@cocotb.test()
async def test_a_response_that_needs_escaping(dut):
    """0xC0 and 0xDB in the body, and both escaped forms beside them.

    An unescaped 0xC0 here ends the frame at that byte and the host
    reads a short reply that decodes cleanly -- the failure that looks
    like success, which is why this is the first test.
    """
    sink = await setup(dut)
    body = bytes([0x4F, 0x43, 0x01, 0x02, 0x00, 0x00, 0x00, 0x00,
                  END, ESC, ESC_END, ESC_ESC, END, END, ESC, ESC, 0x5A])
    await send_frame(dut, body)
    await ClockCycles(dut.clk, 200)

    check(sink)
    assert bytes(sink.out) == encode(body), (
        f"the frame went out as {bytes(sink.out).hex()}, "
        f"want {encode(body).hex()}")
    assert decode(bytes(sink.out)) == [body], "the frame does not decode back"


@cocotb.test()
async def test_every_length_modulo_eight(dut):
    """A final partial beat of every size, one to eight.

    A walk that emitted a fixed eight bytes per beat passes on the
    aligned lengths alone, and a response is only aligned when its body
    happens to be: the header is eight bytes and the tag sixteen, so a
    seal of a 5-byte message is not.
    """
    for n in range(1, 18):
        sink = await setup(dut)
        body = payload(n, seed=n)
        await send_frame(dut, body)
        await ClockCycles(dut.clk, 200)
        check(sink)
        assert bytes(sink.out) == encode(body), (
            f"length {n} went out as {bytes(sink.out).hex()}, "
            f"want {encode(body).hex()}")


@cocotb.test()
async def test_tkeep_is_the_only_length(dut):
    """The bytes past tkeep are not the response, however they read.

    oca_proto masks them to zero, so a walk that stopped at the first
    zero byte would agree with it today and disagree the moment a
    response legitimately contains one. This beat carries non-zero
    bytes past tkeep, which only tkeep can tell apart.
    """
    sink = await setup(dut)
    await send_beat(dut, 0xDEADBEEF_44332211, 0x0F, True)
    dut.s_axis_tvalid.value = 0
    dut.s_axis_tlast.value = 0
    await ClockCycles(dut.clk, 100)

    check(sink)
    assert bytes(sink.out) == encode(bytes([0x11, 0x22, 0x33, 0x44])), (
        f"tkeep 0x0F emitted {bytes(sink.out).hex()}; the four bytes past it "
        f"are not part of the response")


@cocotb.test()
async def test_worst_case_escape_expansion(dut):
    """Eight bytes that all need escaping: seventeen bytes out of one beat."""
    sink = await setup(dut)
    body = bytes([END] * 4 + [ESC] * 4)
    await send_frame(dut, body)
    await ClockCycles(dut.clk, 200)

    check(sink)
    want = bytes([ESC, ESC_END] * 4 + [ESC, ESC_ESC] * 4 + [END])
    assert bytes(sink.out) == want, \
        f"escape expansion gave {bytes(sink.out).hex()}, want {want.hex()}"
    assert bytes(sink.out) == encode(body), "the model disagrees with itself"


@cocotb.test()
async def test_back_to_back_frames(dut):
    """tvalid never falls between one frame's tlast and the next's first beat."""
    sink = await setup(dut)
    bodies = [payload(8, 1), payload(13, 2), payload(24, 3)]
    for body in bodies:
        await send_frame(dut, body, drop_valid=False)
    dut.s_axis_tvalid.value = 0
    dut.s_axis_tlast.value = 0
    await ClockCycles(dut.clk, 400)

    check(sink)
    want = b"".join(encode(b) for b in bodies)
    assert bytes(sink.out) == want, (
        f"back to back gave {bytes(sink.out).hex()}, want {want.hex()}")
    assert decode(bytes(sink.out)) == bodies, "the frames do not decode back"


@cocotb.test()
async def test_a_full_byte_sink_stalls_without_losing_bytes(dut):
    """tx_ready low must pause the frame, not delete part of it."""
    stall = 300
    sink = await setup(dut, ready=lambda c: c > stall)
    body = bytes([END, ESC]) + payload(20, 4)
    await send_frame(dut, body)
    await ClockCycles(dut.clk, stall + 400)

    check(sink)
    assert bytes(sink.out) == encode(body), (
        f"after a {stall}-cycle stall the frame went out as "
        f"{bytes(sink.out).hex()}, want {encode(body).hex()}")


@cocotb.test()
async def test_a_sink_ready_every_third_cycle(dut):
    """The stall taken and released repeatedly, including mid-escape."""
    sink = await setup(dut, ready=lambda c: (c % 3) == 0)
    body = bytes([ESC, END] * 6) + payload(11, 5)
    await send_frame(dut, body)
    await ClockCycles(dut.clk, 600)

    check(sink)
    assert bytes(sink.out) == encode(body), (
        f"under backpressure the frame went out as {bytes(sink.out).hex()}, "
        f"want {encode(body).hex()}")
