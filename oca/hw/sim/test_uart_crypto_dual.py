# SPDX-License-Identifier: MIT
"""One smoke test: the dual fabric behind the real serial line.

Everything the fabric promises is proved at the 64-bit stream in
test_dual_fabric.py, where mutation runs are cheap; this file exists
because oca_uart_crypto_dual is a NEW top wiring the same leaves by
hand, and no AXI-level harness can see a wiring slip in it. One
request path is therefore walked bit by bit through the file the
bitstream will carry: 115200 8N1 in, SLIP off, dispatch, both cores,
collect, SLIP on, 8N1 out.

The structure of the top is what gives the single test its meaning:
the load_key is answered once although two cores answered it, and the
two seals that follow are routed to different engines — asserted from
the dispatcher's own record, peeked through the top — yet both return
the model's ciphertext on the one line. That is the dual visible from
the wire, and it is all this file claims: timing, escapes, refusals
and the trouble latch's other six sources are test_uart_crypto.py's,
unchanged leaves, not restated here.
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, ReadOnly, RisingEdge

from aead_model import aead_encrypt
from proto_model import (HDR_LEN, OP_LOAD_KEY, ST_OK, build_load_key,
                         build_seal, parse_response)
from slip_model import END, decode, encode

CLK_NS = 40                 # 25 MHz: CLK_HZ's default, the standalone build
DIV = 217                   # 25e6 / 115200
BYTE_CYCLES = 10 * DIV

BYTES = 2048
# Both cores' packet buffers and the SLIP decoder's bank clear in
# parallel out of reset, so the pair is no slower than the single-core
# top; the figure is test_uart_crypto.py's.
CLEAR_CYCLES = 4 * (BYTES // 8) + 256

KEY = bytes(range(32))
NONCE = bytes(range(12))
NONCE2 = bytes(range(100, 112))


class Wire:
    def __init__(self):
        self.out = bytearray()


WIRE = Wire()
RECORDS = []


async def _sink(dut):
    """8N1 receiver on uart_tx, edge-driven (test_uart_crypto.py)."""
    while True:
        await FallingEdge(dut.uart_tx)
        await ClockCycles(dut.clk, DIV // 2)
        if dut.uart_tx.value != 0:
            continue
        byte = 0
        for bit in range(8):
            await ClockCycles(dut.clk, DIV)
            byte |= int(dut.uart_tx.value) << bit
        await ClockCycles(dut.clk, DIV)
        assert dut.uart_tx.value == 1, f"stop bit low after 0x{byte:02x}"
        WIRE.out.append(byte)


async def _watch_dispatch(dut):
    """The routing record, read at the dispatcher inside the real top."""
    while True:
        await ReadOnly()
        p0 = int(dut.u_dispatch.push0.value)
        p1 = int(dut.u_dispatch.push1.value)
        if p0 and p1:
            RECORDS.append("b")
        elif p0:
            RECORDS.append("0")
        elif p1:
            RECORDS.append("1")
        await RisingEdge(dut.clk)


async def send_bytes(dut, data: bytes):
    for byte in data:
        dut.uart_rx.value = 0
        await ClockCycles(dut.clk, DIV)
        for i in range(8):
            dut.uart_rx.value = (byte >> i) & 1
            await ClockCycles(dut.clk, DIV)
        dut.uart_rx.value = 1
        await ClockCycles(dut.clk, DIV)


async def exchange(dut, pkt: bytes, budget_bytes: int = 4000) -> bytes:
    mark = len(WIRE.out)
    await send_bytes(dut, encode(pkt))
    for _ in range(budget_bytes):
        if END in WIRE.out[mark:]:
            break
        await ClockCycles(dut.clk, BYTE_CYCLES)
    else:
        raise AssertionError(f"no END within {budget_bytes} byte times; "
                             f"the line carried {bytes(WIRE.out[mark:])!r}")
    idx = WIRE.out.index(END, mark)
    frames = decode(bytes(WIRE.out[mark:idx + 1]))
    assert frames[0], "an empty frame arrived before the response"
    return frames[0]


@cocotb.test()
async def test_two_cores_answer_on_one_line(dut):
    dut.uart_rx.value = 1
    dut.rst_n.value = 1
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    cocotb.start_soon(_sink(dut))
    cocotb.start_soon(_watch_dispatch(dut))
    await ClockCycles(dut.clk, CLEAR_CYCLES)

    frame = await exchange(dut, build_load_key(0x0001, 0, KEY))
    rsp = parse_response(frame)
    assert (rsp["opcode"], rsp["req_id"], rsp["status"]) \
        == (OP_LOAD_KEY, 0x0001, ST_OK), f"load_key answered {rsp}"
    assert len(frame) == HDR_LEN, "load_key returned a body"

    # A broadcast is answered once: the line stays silent afterwards
    # for longer than a second header frame would take to leave.
    mark = len(WIRE.out)
    await ClockCycles(dut.clk, 20 * BYTE_CYCLES)
    assert bytes(WIRE.out[mark:]) == b"", (
        f"the line spoke again after the load_key response: "
        f"{bytes(WIRE.out[mark:])!r} — a broadcast must be answered once")

    for req_id, nonce, msg in (
            (0x0002, NONCE, b"first seal, one engine"),
            (0x0003, NONCE2, b"second seal, the other engine")):
        frame = await exchange(dut, build_seal(req_id, 0, nonce, b"", msg))
        rsp = parse_response(frame)
        assert rsp["req_id"] == req_id and rsp["status"] == ST_OK, \
            f"seal answered {rsp}"
        want_ct, want_tag = aead_encrypt(KEY, nonce, b"", msg)
        assert rsp["body"] == want_tag + want_ct, (
            f"seal {req_id:#06x} disagrees with the model: the engine "
            f"it landed on does not hold the broadcast key")

    assert RECORDS == ["b", "0", "1"], (
        f"dispatch went {RECORDS}, want ['b', '0', '1']: the two seals "
        f"were meant to run on different engines")
    assert int(dut.trouble.value) == 0, "trouble raised by clean traffic"
