# SPDX-License-Identifier: MIT
"""The AEAD core over the real serial line, bit by bit.

This is the whole-path test the Ethernet route took with it. Nothing is
injected on an internal bus: every request is shifted into `uart_rx` as
start bit, eight data bits and stop bit at 217 clocks a bit, and every
response is recovered by sampling `uart_tx` the same way. The DUT is
`oca_uart_crypto` itself -- every module the bitstream carries except
the clocking around them, which is oca_crypto_pll's and cannot be
simulated (hw/sim/test_crypto_pll.py) -- so there is no generated
harness between the test and the thing that ships.

Every expected value comes from `aead_model` through `proto_model` and
`slip_model`. No ciphertext, tag or status byte is written by hand.

WHAT THE PREVIOUS WHOLE-PATH TESTBENCH GOT WRONG, and where each is
answered. They are recorded in the closed header of
docs/design/2026-08-05-ethernet-integration.md; defects 2, 3 and 4 were
each proved there by a mutation that was run, and they are shapes rather
than accidents:

1. WITHDRAWN THERE, AND THE ENTRY IS WORTH MORE THAN THE DEFECT WAS. It
   claimed the leak assertion "could not fail" -- `msg not in frame`,
   48 bytes of plaintext with no placement in a 60-byte frame. That
   frame has no fixed length: the checksum generator recomputes it from
   the payload, and under the mutation the docstring itself names the
   open returns the plaintext, the frame grows past 60 and the assertion
   fails first. Sixty is what the reply measures GIVEN an empty body,
   which is what the assertion beside it establishes -- the argument was
   circular, and it is the one entry of the four backed by arithmetic
   rather than by a mutation.

   What it teaches survives its own retraction, and it is why this file
   is built the way it is: a claim about a test, made without running
   it, is how a suite comes to be trusted for something it does not do.
   test_a_forged_tag_puts_no_plaintext_on_the_wire is falsifiable by
   construction instead of by argument. The needle is every FOUR-byte
   window of the plaintext against the EIGHT bytes a refused open
   produces, so a match is geometrically possible; a positive control
   runs first over the same needles, so an assertion that cannot fire
   shows up as the control failing; and forcing tag_match true fails
   this test and no other, in both builds, which is the only statement
   here that rests on a run rather than on reasoning.

2. THE TWO-PEER TEST DID NOT TEST CONCURRENCY: `1 <= watermark <= 2`
   with both bounds forced by construction. There is one peer on a
   serial line and no concurrency is claimed here. The shape avoided is
   the bound that cannot be violated: every frame, counter delta and
   reading of the `trouble` latch here is asserted against an exact
   value, with no inequality anywhere. cnt_done across a stats was `> ` until
   2026-08-12, which is a weaker claim than the design supports -- the
   delta is exactly one, for the same reason cnt_rx's is.

3. THE LINT GATE NEVER READ THE FILE IT NAMED, because the harness was
   generated from a hand-written pin list. There is no harness and no
   pin list here: cocotb elaborates oca_uart_crypto.sv itself, the file
   oca_crypto_pll instantiates and run_synth.py builds from. The pins
   are one level up now -- this module lost its LED to that top and
   gained a reset -- so which pads the four lines reach is that top's
   .lpf and not this file's business.

4. A TIE VALUE THAT DIFFERED FROM THE TOP WENT UNDETECTED, for the same
   reason. This file ties one input and only one: `rst_n` high, which is
   the standalone configuration oca_uart_crypto.sv's header describes --
   with nothing to gate on, the module's own power-on counter is the only
   reset root. THAT IS NOT THE TIE THE BOARD GETS. oca_crypto_pll wires
   rst_n to oca_clkrst's rst_n_sys, which is gated on PLL lock, and the
   test that reads it is hw/sim/test_crypto_pll.py -- which asserts the
   core is held while the PLL is unlocked and released once it locks.
   Nothing in this file could see that tie change. Every other signal
   driven here is the UART receive pin.
"""

import struct

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge

from aead_model import aead_encrypt
from proto_model import (HDR_LEN, MAGIC, OP_LOAD_KEY, OP_OPEN, OP_SEAL,
                         OP_STATS, ST_AUTH_FAIL, ST_BAD_SLOT, ST_OK,
                         build_load_key, build_open, build_seal, build_stats,
                         parse_response)
from slip_model import END, ESC, ESC_END, ESC_ESC, decode, encode

# CLK_HZ is a parameter now and this file elaborates the default, which
# is the 25 MHz board oscillator on P3. Both numbers below are that
# default's: at oca_crypto_pll's 48.0769 MHz the divisor is 417 and none
# of the bit timing here would land.
CLK_NS = 40                 # 25 MHz
DIV = 217                   # 25e6 / 115200
BYTE_CYCLES = 10 * DIV      # start + eight data + stop

BYTES = 2048                # oca_uart_crypto's localparam, both sides of it

# Out of reset the two packet buffers walk both their banks and the SLIP
# decoder walks its own, and each holds its input off while it does.
CLEAR_CYCLES = 4 * (BYTES // 8) + 256

KEY = bytes(range(32))
KEY2 = bytes(range(32, 64))
NONCE = bytes(range(12))
NONCE2 = bytes(range(100, 112))

# A plaintext that is not a run of one byte and not obviously structured,
# so that a four-byte window of it is not something a header could carry
# by accident.
SECRET = bytes((i * 61 + 137) & 0xFF for i in range(32))


class Wire:
    """Every byte the transmitter has put on the line since power-on."""

    def __init__(self):
        self.out = bytearray()


WIRE = Wire()
_cleared = False


async def _sink(dut):
    """8N1 receiver, in Python, on uart_tx.

    Driven by the falling edge of the line rather than by polling it
    every cycle: this coroutine lives for the whole simulation and a
    per-cycle callback over a million cycles is most of the run time.
    """
    while True:
        await FallingEdge(dut.uart_tx)
        await ClockCycles(dut.clk, DIV // 2)
        if dut.uart_tx.value != 0:
            continue                      # a glitch, not a start bit
        byte = 0
        for bit in range(8):
            await ClockCycles(dut.clk, DIV)
            byte |= int(dut.uart_tx.value) << bit
        await ClockCycles(dut.clk, DIV)
        assert dut.uart_tx.value == 1, f"stop bit low after 0x{byte:02x}"
        WIRE.out.append(byte)


async def setup(dut):
    """Restart the clock and the sink, and clear the buffers once.

    THE CLOCK IS RESTARTED PER TEST AND THAT IS NOT REDUNDANT. cocotb
    cancels the tasks a test started when that test ends, so a clock
    started in the first test stops with it and every test after it dies
    on the spot with "the simulation ended prematurely" -- measured, not
    guessed at: that is what this file did before this line existed, and
    it is why test_uart_console.py restarts its clock too.

    The DUT is NOT reset between tests: cocotb has no way to, and the key
    store, the protocol counters and the sticky `trouble` latch all carry
    over. The tests below are written in the order they need and each one
    says where it depends on that order.

    rst_n is tied high and never moves. This is the standalone build, the
    one with no PLL to gate on, where oca_uart_crypto's own power-on
    counter is the whole of the reset -- see item 4 of the header for
    what that does NOT say about the board.
    """
    global _cleared
    dut.uart_rx.value = 1
    dut.rst_n.value = 1
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    cocotb.start_soon(_sink(dut))
    if _cleared:
        await ClockCycles(dut.clk, 8)
        return
    _cleared = True
    await ClockCycles(dut.clk, CLEAR_CYCLES)


async def send_bytes(dut, data: bytes):
    """Shift bytes in at line rate, back to back with no idle between."""
    for byte in data:
        dut.uart_rx.value = 0
        await ClockCycles(dut.clk, DIV)
        for i in range(8):
            dut.uart_rx.value = (byte >> i) & 1
            await ClockCycles(dut.clk, DIV)
        dut.uart_rx.value = 1
        await ClockCycles(dut.clk, DIV)


async def wait_frame(dut, mark: int, budget_bytes: int = 4000):
    """The next complete SLIP frame on the line after offset `mark`.

    Returns (raw, decoded): the bytes as they left the transmitter,
    escapes included, and the one frame they carry.
    """
    for _ in range(budget_bytes):
        if END in WIRE.out[mark:]:
            break
        await ClockCycles(dut.clk, BYTE_CYCLES)
    else:
        raise AssertionError(
            f"no END within {budget_bytes} byte times; the line carried "
            f"{bytes(WIRE.out[mark:])!r}")
    idx = WIRE.out.index(END, mark)
    raw = bytes(WIRE.out[mark:idx + 1])
    frames = decode(raw)
    # `raw` is cut at the first END, so "one frame" is true by
    # construction and asserting it would prove nothing. What is not
    # true by construction is that the frame carries anything: a leading
    # END -- which RFC 1055 allows a sender to emit and oca_slip_tx
    # deliberately does not -- would land here as an empty frame and
    # every field assertion downstream would then be reading the frame
    # after it.
    assert frames[0], f"an empty frame arrived before the response: {raw!r}"
    return raw, frames[0]


async def exchange(dut, pkt: bytes, budget_bytes: int = 4000):
    """One request in, one response out. Returns (raw, decoded)."""
    mark = len(WIRE.out)
    await send_bytes(dut, encode(pkt))
    return await wait_frame(dut, mark, budget_bytes)


async def silence(dut, byte_times: int) -> bytes:
    """Wait, and return whatever the transmitter said while we did."""
    mark = len(WIRE.out)
    await ClockCycles(dut.clk, byte_times * BYTE_CYCLES)
    return bytes(WIRE.out[mark:])


def stats_of(frame: bytes) -> dict:
    rsp = parse_response(frame)
    assert rsp["status"] == ST_OK, f"stats status {rsp['status']}"
    rx, drop, done, auth = struct.unpack("<4I", rsp["body"])
    return {"rx": rx, "drop": drop, "done": done, "auth": auth}


@cocotb.test()
async def test_load_key_then_seal_over_the_wire(dut):
    """The headline: 115200 8N1 in, ciphertext and tag out.

    The plaintext deliberately carries both framing bytes, so the
    request exercises oca_slip_rx's unescape on the way in. What comes
    back is compared against aead_model and nothing else.
    """
    await setup(dut)

    _, frame = await exchange(dut, build_load_key(0x0001, 0, KEY))
    rsp = parse_response(frame)
    assert rsp["magic_ok"], f"response magic {frame[:2]!r}"
    assert (rsp["opcode"], rsp["req_id"], rsp["slot"], rsp["status"]) \
        == (OP_LOAD_KEY, 0x0001, 0, ST_OK), f"load_key answered {rsp}"
    assert rsp["body"] == b"", f"load_key returned a body: {rsp['body']!r}"
    assert len(frame) == HDR_LEN

    aad = b"aad"
    msg = bytes([END, ESC]) + b"crypto over a serial line"
    req = build_seal(0x0002, 0, NONCE, aad, msg)
    assert bytes([END]) in req and bytes([ESC]) in req, \
        "the request was meant to carry both framing bytes"
    assert len(encode(req)) == len(req) + 3, \
        "the encoded request should be two escapes and an END longer"

    _, frame = await exchange(dut, req)
    rsp = parse_response(frame)
    assert rsp["status"] == ST_OK, f"seal status {rsp['status']}"
    assert rsp["req_id"] == 0x0002 and rsp["opcode"] == OP_SEAL

    want_ct, want_tag = aead_encrypt(KEY, NONCE, aad, msg)
    assert rsp["body"][:16] == want_tag, \
        f"tag {rsp['body'][:16].hex()} != {want_tag.hex()}"
    assert rsp["body"][16:] == want_ct, \
        f"ciphertext {rsp['body'][16:].hex()} != {want_ct.hex()}"


@cocotb.test()
async def test_a_response_carrying_the_framing_bytes_is_escaped(dut):
    """The other direction: END and ESC inside a response.

    An open returns the plaintext, so the plaintext decides what the
    transmitter has to escape. A 0xC0 that left unescaped would end the
    frame at that byte and the host would read a short reply that decodes
    cleanly and parses as a header -- a truncation with nothing marking
    it as one.

    The escape sequences are asserted on the RAW bytes, so this test
    witnesses the encoding and not only the round trip: without that,
    a decoder and an encoder that agreed on the wrong convention would
    both pass.
    """
    await setup(dut)
    await exchange(dut, build_load_key(0x0010, 1, KEY2))

    msg = b"AB" + bytes([END]) + b"CD" + bytes([ESC]) + b"EF"
    _, sealed = await exchange(dut, build_seal(0x0011, 1, NONCE2, b"", msg))
    body = parse_response(sealed)["body"]
    tag, ct = body[:16], body[16:]

    raw, frame = await exchange(dut,
                                build_open(0x0012, 1, NONCE2, b"", ct, tag))
    rsp = parse_response(frame)
    assert rsp["status"] == ST_OK, f"open status {rsp['status']}"
    assert rsp["body"] == msg, f"{rsp['body']!r} != {msg!r}"

    assert bytes([ESC, ESC_END]) in raw, \
        f"no escaped END in the response on the wire: {raw.hex()}"
    assert bytes([ESC, ESC_ESC]) in raw, \
        f"no escaped ESC in the response on the wire: {raw.hex()}"

    # The whole frame, byte for byte, against slip_model's encoding of
    # the response proto_model says is due. This is the assertion that
    # can see an unescaped 0xC0: a bare one would end the frame at that
    # byte, wait_frame would return the truncated prefix, and this would
    # fail. Counting the ENDs in `raw` instead could not -- wait_frame
    # cuts at the first one, so the count is one whatever the DUT did.
    want = MAGIC + bytes([1, OP_OPEN]) + struct.pack("<H", 0x0012) \
        + bytes([1, ST_OK]) + msg
    assert raw == encode(want), \
        f"on the wire {raw.hex()} != {encode(want).hex()}"


@cocotb.test()
async def test_a_forged_tag_puts_no_plaintext_on_the_wire(dut):
    """The security property, with a control that proves it can fail.

    See item 1 of this file's header. The needle is every four-byte
    window of the plaintext; the frame a refused open produces is eight
    bytes, so a window fits in it and the negative assertion is
    falsifiable. The positive control runs first over the same needles.
    """
    await setup(dut)
    await exchange(dut, build_load_key(0x0020, 3, KEY))

    _, sealed = await exchange(dut, build_seal(0x0021, 3, NONCE, b"", SECRET))
    body = parse_response(sealed)["body"]
    tag, ct = body[:16], body[16:]
    want_ct, want_tag = aead_encrypt(KEY, NONCE, b"", SECRET)
    assert (tag, ct) == (want_tag, want_ct), "the seal is already wrong"

    windows = [SECRET[i:i + 4] for i in range(len(SECRET) - 3)]

    # The control searches the DECODED frame only. The raw bytes are
    # SLIP-escaped, so a window containing 0xC0 or 0xDB would be split
    # there and the control would fail for a reason that is not a leak.
    # SECRET is built to contain neither, and the negative assertion
    # below therefore searches both.
    assert not (bytes([END]) in SECRET or bytes([ESC]) in SECRET)
    _, frame_ok = await exchange(
        dut, build_open(0x0022, 3, NONCE, b"", ct, tag))
    rsp_ok = parse_response(frame_ok)
    assert rsp_ok["status"] == ST_OK, f"open status {rsp_ok['status']}"
    assert rsp_ok["body"] == SECRET
    missed = [w.hex() for w in windows if w not in frame_ok]
    assert missed == [], (
        f"the control failed: {len(missed)} four-byte windows of the "
        f"plaintext were not found in a frame that carries the whole "
        f"plaintext, so the search below could not have found them "
        f"either ({missed[:4]})")

    forged = bytearray(tag)
    forged[7] ^= 0x10
    raw_bad, frame_bad = await exchange(
        dut, build_open(0x0023, 3, NONCE, b"", ct, bytes(forged)))
    rsp_bad = parse_response(frame_bad)
    assert rsp_bad["status"] == ST_AUTH_FAIL, \
        f"a forged tag answered status {rsp_bad['status']}"
    assert len(frame_bad) == HDR_LEN, \
        f"a refused open returned {len(frame_bad)} bytes, header is {HDR_LEN}"
    assert frame_bad == MAGIC + bytes([1, OP_OPEN]) \
        + struct.pack("<H", 0x0023) + bytes([3, ST_AUTH_FAIL]), \
        f"a refused open returned {frame_bad.hex()}"

    leaked = [w.hex() for w in windows
              if w in frame_bad or w in raw_bad]
    assert leaked == [], (
        f"plaintext on the wire after a forged tag: windows {leaked} "
        f"found in frame {frame_bad.hex()} / raw {raw_bad.hex()}")


@cocotb.test()
async def test_stats_answers_over_the_wire(dut):
    """Opcode 04, the in-band diagnostic, end to end.

    It is also the measurement the next test uses, so it is established
    here that the counter moves by a constant per frame delivered.
    """
    await setup(dut)
    _, f1 = await exchange(dut, build_stats(0x0030))
    _, f2 = await exchange(dut, build_stats(0x0031))
    s1, s2 = stats_of(f1), stats_of(f2)
    assert parse_response(f2)["req_id"] == 0x0031
    assert s2["rx"] - s1["rx"] == 1, (
        f"two stats one after the other moved cnt_rx by "
        f"{s2['rx'] - s1['rx']}, expected 1")
    assert s2["done"] - s1["done"] == 1, (
        f"two stats one after the other moved cnt_done by "
        f"{s2['done'] - s1['done']}, expected 1")


@cocotb.test()
async def test_a_short_frame_is_refused_and_never_reaches_the_core(dut):
    """The blind spot, turned into an assertion.

    oca_proto sends a request under HDR_LEN to P_DROP and answers nothing
    at all, so on a byte stream nothing but oca_slip_rx's MIN_BYTES
    stands between a truncated frame and a host waiting for ever. A
    refused frame is invisible to every counter the protocol has --
    which is exactly what is measured here: cnt_rx must not move for it,
    and it must move for a frame that is merely wrong.

    The positive control is the middle step. A seal against an unloaded
    slot is refused by oca_proto and still counted, so if the
    measurement could not see an extra frame at all, that step fails
    instead of this one passing for the wrong reason.

    THE OTHER HALF OF THE BLIND SPOT IS `trouble`, and this is where it
    is measured now that the LED has left this module. Sticky by design,
    so the reading before the refused frame is as much a part of the
    claim as the one after: a latch already set says nothing about what
    set it, and the heartbeat tests that used to stand for this pair
    (hw/sim/test_crypto_pll.py) can only see the bit once it has crossed
    two clock domains. Here it is read at the port that carries it.
    """
    await setup(dut)
    _, f = await exchange(dut, build_stats(0x0040))
    base = stats_of(f)["rx"]

    _, refused = await exchange(dut, build_seal(0x0041, 7, NONCE, b"", b"x"))
    assert parse_response(refused)["status"] == ST_BAD_SLOT, \
        "the control frame was meant to be answered and refused"
    _, f = await exchange(dut, build_stats(0x0042))
    after_control = stats_of(f)["rx"]
    assert after_control - base == 2, (
        f"a frame oca_proto answers moved cnt_rx by "
        f"{after_control - base}, expected 2 (the frame and the stats)")

    assert int(dut.trouble.value) == 0, (
        "the trouble latch is already set before any frame was refused, "
        "so what it reports below is not the refusal: every exchange so "
        "far was well formed and none of its six sources should have "
        "fired")

    short = MAGIC + bytes([1, OP_STATS])          # four bytes, HDR_LEN is 8
    assert len(short) < HDR_LEN
    await send_bytes(dut, encode(short))
    quiet = await silence(dut, 20)
    assert quiet == b"", (
        f"a frame under HDR_LEN was answered with {quiet!r}; it must not "
        f"reach oca_core at all")

    assert int(dut.trouble.value) == 1, (
        "a frame oca_slip_rx refused left the trouble latch clear: no "
        "counter the protocol has can see that frame, so this bit is the "
        "only thing on the board that reports it")

    _, f = await exchange(dut, build_stats(0x0043))
    after_short = stats_of(f)["rx"]
    assert after_short - after_control == 1, (
        f"cnt_rx moved by {after_short - after_control} across the refused "
        f"frame and the stats that followed it, expected 1: the frame "
        f"reached oca_core")
