# Host protocol implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development to implement this plan
> task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn a UDP payload into an AEAD operation and back, so the
MVP board can be driven from a ten-line Python script — with no
Ethernet in the simulation.

**Architecture:** Four modules behind an 8-bit AXI-Stream boundary.
`oca_pktbuf` stores the packet whole, `oca_proto` parses the header and
runs the command, `oca_keystore` holds the keys, `oca_core` wires them
to `chacha20_poly1305`. Store and forward throughout.

**Tech stack:** SystemVerilog, cocotb 2.x + Verilator, synthesis via
`oca/hw/syn/run_synth.py`.

**Design doc:** `docs/design/2026-08-03-host-protocol.md` — read it
first; this plan implements it and does not restate its reasoning.

## Global Constraints

- **Keep it simple. This is an MVP.** Nothing beyond what the design
  document specifies. No feature is added because it "might be useful".
- Product in English: code, identifiers, comments, commit messages.
- `// SPDX-License-Identifier: CERN-OHL-P-2.0` on RTL, MIT (`#` form in
  Python) on simulation files.
- No expected values are hand-typed. The cryptographic oracle is the
  existing `oca/hw/sim/aead_model.py`.
- No vendor primitives instantiated; memories are inferred, not
  instantiated (`SPEC.md` portability rule).
- Latency must not depend on secret data (`Security.md` section 3).
  Depending on *lengths* is fine — they are public.
- Work on branch `feat/host-protocol`. Nothing is pushed.
- All commands run from `oca/`. Python is `.venv/bin/python`.
- Every commit carries the two harness trailers used across this repo.

## Deliberate simplifications, and what they cost

Written down because a future reader will otherwise assume they were
oversights:

- **The packet buffer is one byte wide, and nothing overlaps.** A
  64-byte block costs 64 cycles to read out, 40 to process, 64 to write
  back: about 168 cycles per block against the engine's own 40. Expect
  roughly **0.16 Gbps end to end**, against 0.68 Gbps for the engine
  alone. That is 16% of the GbE link. It is enough to demonstrate the
  path end to end, which is what the MVP is for; widening the memory to
  32 bits and overlapping the phases is the obvious follow-up, and needs
  a board to measure honestly.
- **No fragmentation.** One command, one packet, as the design states.
- **No back-to-back command pipelining.** One command is processed to
  completion before the next is accepted.

## File Structure

- `oca/hw/rtl/oca_keystore.sv` — key slots. One responsibility: hold key
  material and say whether a slot is usable.
- `oca/hw/rtl/oca_pktbuf.sv` — one byte-wide memory with a write port
  (from the stream), a random-access read port (for header parsing) and
  a sequential read port (for the engine).
- `oca/hw/rtl/oca_proto.sv` — the protocol FSM. Parses, validates,
  sequences the engine, builds the response.
- `oca/hw/rtl/oca_core.sv` — instantiates the three above plus
  `chacha20_poly1305`, and exposes the two AXI-Stream ports.
- `oca/hw/sim/proto_model.py` — builds and parses packets in Python.
- `oca/hw/sim/test_keystore.py`, `test_pktbuf.py`, `test_oca_core.py`
  and their `run_*.py` runners, matching the existing style.

---

### Task 1: The protocol model in Python

**Files:**
- Create: `oca/hw/sim/proto_model.py`
- Create: `oca/hw/sim/test_proto_model.py` (plain pytest-style checks
  run through cocotb is overkill here — see step 3)

**Interfaces:**
- Consumes: `aead_encrypt`, `aead_decrypt` from `aead_model`.
- Produces:
  - `build_load_key(req_id: int, slot: int, key: bytes) -> bytes`
  - `build_seal(req_id, slot, nonce: bytes, aad: bytes, msg: bytes) -> bytes`
  - `build_open(req_id, slot, nonce, aad, ct: bytes, tag: bytes) -> bytes`
  - `build_stats(req_id: int) -> bytes`
  - `parse_response(pkt: bytes) -> dict` with keys `magic_ok`, `version`,
    `opcode`, `req_id`, `slot`, `status`, `body`
  - constants `MAGIC`, `VERSION`, `OP_LOAD_KEY`, `OP_SEAL`, `OP_OPEN`,
    `OP_STATS`, and `ST_OK` … `ST_ENGINE_ERR`

- [ ] **Step 1: Write the model**

```python
# SPDX-License-Identifier: MIT
"""Builder and parser for the OCA host protocol.

The wire format is defined in docs/design/2026-08-03-host-protocol.md.
This module is the reference for it: the RTL is judged against what
these functions produce, and the cryptography comes from aead_model so
no expected value is ever written by hand.
"""

import struct

MAGIC = b"\x4f\x43"
VERSION = 1

OP_LOAD_KEY = 0x01
OP_SEAL = 0x02
OP_OPEN = 0x03
OP_STATS = 0x04

ST_OK = 0x00
ST_BAD_MAGIC = 0x01
ST_BAD_VERSION = 0x02
ST_BAD_OPCODE = 0x03
ST_BAD_SLOT = 0x04
ST_BAD_LENGTH = 0x05
ST_AUTH_FAIL = 0x06
ST_ENGINE_ERR = 0x07

HDR_LEN = 8


def _header(opcode: int, req_id: int, slot: int, status: int = 0) -> bytes:
    return MAGIC + bytes([VERSION, opcode]) + struct.pack("<H", req_id) \
        + bytes([slot, status])


def build_load_key(req_id: int, slot: int, key: bytes) -> bytes:
    assert len(key) == 32
    return _header(OP_LOAD_KEY, req_id, slot) + key


def build_seal(req_id: int, slot: int, nonce: bytes, aad: bytes,
               msg: bytes) -> bytes:
    assert len(nonce) == 12
    return (_header(OP_SEAL, req_id, slot) + nonce
            + struct.pack("<HH", len(aad), len(msg)) + aad + msg)


def build_open(req_id: int, slot: int, nonce: bytes, aad: bytes,
               ct: bytes, tag: bytes) -> bytes:
    assert len(nonce) == 12 and len(tag) == 16
    return (_header(OP_OPEN, req_id, slot) + nonce
            + struct.pack("<HH", len(aad), len(ct)) + tag + aad + ct)


def build_stats(req_id: int) -> bytes:
    return _header(OP_STATS, req_id, 0)


def parse_response(pkt: bytes) -> dict:
    assert len(pkt) >= HDR_LEN, f"response shorter than a header: {len(pkt)}"
    return {
        "magic_ok": pkt[0:2] == MAGIC,
        "version": pkt[2],
        "opcode": pkt[3],
        "req_id": struct.unpack("<H", pkt[4:6])[0],
        "slot": pkt[6],
        "status": pkt[7],
        "body": pkt[HDR_LEN:],
    }
```

- [ ] **Step 2: Write the round-trip check**

Create `oca/hw/sim/test_proto_model.py`:

```python
# SPDX-License-Identifier: MIT
"""Self-consistency checks for proto_model, run without any RTL."""

from proto_model import (HDR_LEN, MAGIC, OP_SEAL, ST_OK, build_seal,
                         parse_response, _header)


def test_header_round_trips():
    pkt = _header(OP_SEAL, 0x1234, 5, ST_OK)
    got = parse_response(pkt)
    assert got["magic_ok"] and got["version"] == 1
    assert got["opcode"] == OP_SEAL and got["req_id"] == 0x1234
    assert got["slot"] == 5 and got["status"] == ST_OK


def test_seal_layout():
    pkt = build_seal(1, 0, b"n" * 12, b"aad", b"msg")
    # literal, not the imported MAGIC: comparing the packet against the
    # same constant that built it passes for any value and proves nothing
    assert pkt[:2] == b"\x4f\x43"
    assert len(pkt) == HDR_LEN + 12 + 4 + 3 + 3
    assert pkt[HDR_LEN + 12:HDR_LEN + 16] == b"\x03\x00\x03\x00"
    assert pkt[HDR_LEN + 16:] == b"aadmsg"


if __name__ == "__main__":
    test_header_round_trips()
    test_seal_layout()
    print("proto_model: OK")
```

- [ ] **Step 3: Run it**

```sh
cd hw/sim && ../../.venv/bin/python test_proto_model.py
```

Expected: `proto_model: OK`. Plain `python` rather than the cocotb
runner — there is no DUT here, and pulling a simulator into a pure
Python check would be noise.

- [ ] **Step 4: Prove it can fail**

Change `MAGIC` to `b"XX"` and re-run: `test_seal_layout` must fail on
the first assertion. Restore, re-run, confirm OK.

- [ ] **Step 5: Commit**

```sh
git add oca/hw/sim/proto_model.py oca/hw/sim/test_proto_model.py
git commit -F <message file>
```

---

### Task 2: oca_keystore

**Files:**
- Create: `oca/hw/rtl/oca_keystore.sv`
- Create: `oca/hw/sim/test_keystore.py`, `oca/hw/sim/run_keystore.py`

**Interfaces:**
- Produces:

```systemverilog
module oca_keystore #(
    parameter int NUM_SLOTS = 8
) (
    input  logic         clk,
    input  logic         rst_n,
    input  logic         wr_en,
    input  logic [  7:0] wr_slot,
    input  logic [255:0] wr_key,
    input  logic [  7:0] rd_slot,
    output logic [255:0] rd_key,
    output logic         rd_valid
);
```

- [ ] **Step 1: Write the failing test**

Create `oca/hw/sim/test_keystore.py`:

```python
# SPDX-License-Identifier: MIT
"""Key slots: a slot reads back what was written, an unwritten or
out-of-range slot reports invalid, and reset clears everything."""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

NUM_SLOTS = 8


async def setup(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst_n.value = 0
    dut.wr_en.value = 0
    dut.wr_slot.value = 0
    dut.wr_key.value = 0
    dut.rd_slot.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)


async def write_slot(dut, slot: int, key: bytes):
    dut.wr_slot.value = slot
    dut.wr_key.value = int.from_bytes(key, "little")
    dut.wr_en.value = 1
    await RisingEdge(dut.clk)
    dut.wr_en.value = 0
    await RisingEdge(dut.clk)


async def read_slot(dut, slot: int):
    dut.rd_slot.value = slot
    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)
    return int(dut.rd_key.value).to_bytes(32, "little"), int(dut.rd_valid.value)


@cocotb.test()
async def test_unwritten_slots_are_invalid(dut):
    await setup(dut)
    for slot in range(NUM_SLOTS):
        _, valid = await read_slot(dut, slot)
        assert valid == 0, f"slot {slot} valid before any write"


@cocotb.test()
async def test_write_then_read(dut):
    await setup(dut)
    rng = random.Random(0x5107)
    keys = {}
    for slot in range(NUM_SLOTS):
        keys[slot] = bytes(rng.getrandbits(8) for _ in range(32))
        await write_slot(dut, slot, keys[slot])
    for slot in range(NUM_SLOTS):
        got, valid = await read_slot(dut, slot)
        assert valid == 1, f"slot {slot} invalid after write"
        assert got == keys[slot], f"slot {slot}: {got.hex()} != {keys[slot].hex()}"


@cocotb.test()
async def test_out_of_range_slot_is_invalid(dut):
    await setup(dut)
    await write_slot(dut, 0, b"\xaa" * 32)
    for slot in (NUM_SLOTS, NUM_SLOTS + 1, 255):
        _, valid = await read_slot(dut, slot)
        assert valid == 0, f"out-of-range slot {slot} reported valid"


@cocotb.test()
async def test_reset_clears(dut):
    await setup(dut)
    await write_slot(dut, 3, b"\x5a" * 32)
    _, valid = await read_slot(dut, 3)
    assert valid == 1
    dut.rst_n.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)
    key, valid = await read_slot(dut, 3)
    assert valid == 0, "slot still valid after reset"
    assert key == bytes(32), "key material survived reset"
```

Create `oca/hw/sim/run_keystore.py` modelled exactly on the existing
`run_poly1305.py`: same Verilator path handling, `hdl_toplevel` and
`test_module` set to the keystore, `build_dir` `sim_build_keystore`,
`always=True`.

- [ ] **Step 2: Run it and watch it fail**

```sh
.venv/bin/python hw/sim/run_keystore.py
```

Expected: the build fails because `oca/hw/rtl/oca_keystore.sv` does not
exist. That is the right failure for this step.

- [ ] **Step 3: Write the module**

```systemverilog
// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * Key slots for the OCA host protocol.
 *
 * NUM_SLOTS one-time keys, written by the load-key command and read by
 * index. This is the only place key material lives.
 *
 * A slot carries a loaded bit: reading a slot that was never written
 * reports rd_valid = 0 rather than handing back a key of zeros, so a
 * host mistake becomes a protocol error instead of a message encrypted
 * under a key an attacker can guess.
 *
 * Reset clears both the keys and the loaded bits (Security.md).
 */
module oca_keystore #(
    parameter int NUM_SLOTS = 8
) (
    input  logic         clk,
    input  logic         rst_n,
    input  logic         wr_en,
    input  logic [  7:0] wr_slot,
    input  logic [255:0] wr_key,
    input  logic [  7:0] rd_slot,
    output logic [255:0] rd_key,
    output logic         rd_valid
);

    logic [255:0] keys   [NUM_SLOTS];
    logic         loaded [NUM_SLOTS];

    logic in_range;
    always_comb in_range = (rd_slot < 8'(NUM_SLOTS));

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            for (int i = 0; i < NUM_SLOTS; i++) begin
                keys[i]   <= '0;
                loaded[i] <= 1'b0;
            end
            rd_key   <= '0;
            rd_valid <= 1'b0;
        end else begin
            if (wr_en && wr_slot < 8'(NUM_SLOTS)) begin
                keys[wr_slot]   <= wr_key;
                loaded[wr_slot] <= 1'b1;
            end
            rd_key   <= in_range ? keys[rd_slot]   : '0;
            rd_valid <= in_range ? loaded[rd_slot] : 1'b0;
        end
    end

endmodule
```

- [ ] **Step 4: Run the tests**

```sh
.venv/bin/python hw/sim/run_keystore.py
```

Expected: 4/4 PASS.

- [ ] **Step 5: Lint**

```sh
../tools/verilator/bin/verilator --lint-only -Wall hw/rtl/oca_keystore.sv --top-module oca_keystore
```

Expected: clean.

- [ ] **Step 6: Prove the tests can fail**

Delete `&& wr_slot < 8'(NUM_SLOTS)` from the write guard and re-run:
nothing should change (writes above the range are already unreachable in
these tests) — so instead remove the `loaded` check from `rd_valid`,
making it `in_range`. Expected: `test_unwritten_slots_are_invalid` and
`test_reset_clears` FAIL. Restore and confirm 4/4.

- [ ] **Step 7: Commit**

---

### Task 3: oca_pktbuf

**Files:**
- Create: `oca/hw/rtl/oca_pktbuf.sv`
- Create: `oca/hw/sim/test_pktbuf.py`, `oca/hw/sim/run_pktbuf.py`

**Interfaces:**
- Produces:

```systemverilog
module oca_pktbuf #(
    parameter int BYTES = 2048
) (
    input  logic        clk,
    input  logic        rst_n,
    // sequential write from the packet stream
    input  logic        wr_en,
    input  logic [ 7:0] wr_data,
    input  logic        wr_clear,        // restart at offset 0
    output logic [11:0] wr_count,        // bytes written since clear
    output logic        wr_full,
    // random-access read
    input  logic [11:0] rd_addr,
    output logic [ 7:0] rd_data          // valid one cycle after rd_addr
);
```

One byte wide and single-purpose: the design's simplification. The
engine's 64-byte blocks are assembled by `oca_proto`, not here — this
module only stores bytes and hands them back.

- [ ] **Step 1: Write the failing test**

```python
# SPDX-License-Identifier: MIT
"""Packet buffer: bytes come back at the offset they went in, the
counter tracks the write position, and the full flag fires at BYTES."""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

BYTES = 2048


async def setup(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst_n.value = 0
    dut.wr_en.value = 0
    dut.wr_data.value = 0
    dut.wr_clear.value = 0
    dut.rd_addr.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)


async def write_bytes(dut, data: bytes):
    for b in data:
        dut.wr_data.value = b
        dut.wr_en.value = 1
        await RisingEdge(dut.clk)
    dut.wr_en.value = 0
    await RisingEdge(dut.clk)


async def read_byte(dut, addr: int) -> int:
    dut.rd_addr.value = addr
    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)
    return int(dut.rd_data.value)


@cocotb.test()
async def test_write_then_read_back(dut):
    await setup(dut)
    rng = random.Random(0xB0FF)
    payload = bytes(rng.getrandbits(8) for _ in range(300))
    await write_bytes(dut, payload)
    assert int(dut.wr_count.value) == len(payload)
    for addr in (0, 1, 63, 64, 65, 299):
        got = await read_byte(dut, addr)
        assert got == payload[addr], f"offset {addr}: {got} != {payload[addr]}"


@cocotb.test()
async def test_clear_restarts_at_zero(dut):
    await setup(dut)
    await write_bytes(dut, b"first")
    dut.wr_clear.value = 1
    await RisingEdge(dut.clk)
    dut.wr_clear.value = 0
    await RisingEdge(dut.clk)
    assert int(dut.wr_count.value) == 0
    await write_bytes(dut, b"second")
    assert int(dut.wr_count.value) == 6
    assert await read_byte(dut, 0) == ord("s")


@cocotb.test()
async def test_full_flag_and_no_overrun(dut):
    await setup(dut)
    await write_bytes(dut, bytes(BYTES))
    assert int(dut.wr_full.value) == 1, "full not asserted at capacity"
    before = int(dut.wr_count.value)
    await write_bytes(dut, b"\xff" * 4)
    assert int(dut.wr_count.value) == before, "counter moved past capacity"
    assert await read_byte(dut, 0) == 0, "overrun wrapped and corrupted offset 0"
```

Create `run_pktbuf.py` in the established style, `build_dir`
`sim_build_pktbuf`. Note the third test writes 2048 bytes one per
cycle — it is the slowest test here and still takes under a second.

- [ ] **Step 2: Run it and watch it fail** — the module does not exist yet.

- [ ] **Step 3: Write the module**

```systemverilog
// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * Packet buffer for the OCA host protocol.
 *
 * One byte wide, written sequentially from the packet stream and read
 * at random offsets. Deliberately the simplest thing that works: at one
 * byte per cycle a 64-byte block takes 64 cycles to read out against the
 * AEAD engine's 40 to process it, so the buffer, not the engine, sets
 * the pace. Widening this memory is the first optimisation to make once
 * there is hardware to measure it on
 * (docs/design/2026-08-03-host-protocol.md).
 *
 * Writes past BYTES are dropped and wr_full is raised: a truncated
 * packet becomes a length error at the protocol layer rather than a
 * silent wrap that corrupts what is already stored.
 */
module oca_pktbuf #(
    parameter int BYTES = 2048
) (
    input  logic        clk,
    input  logic        rst_n,
    input  logic        wr_en,
    input  logic [ 7:0] wr_data,
    input  logic        wr_clear,
    output logic [11:0] wr_count,
    output logic        wr_full,
    input  logic [11:0] rd_addr,
    output logic [ 7:0] rd_data
);

    logic [7:0] mem [BYTES];

    always_comb wr_full = (wr_count >= 12'(BYTES));

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            wr_count <= '0;
            rd_data  <= '0;
        end else begin
            if (wr_clear) begin
                wr_count <= '0;
            end else if (wr_en && !wr_full) begin
                mem[wr_count] <= wr_data;
                wr_count      <= wr_count + 12'd1;
            end
            rd_data <= mem[rd_addr];
        end
    end

endmodule
```

- [ ] **Step 4: Run the tests** — expected 3/3 PASS.

- [ ] **Step 5: Lint** — clean, top module `oca_pktbuf`.

- [ ] **Step 6: Prove the tests can fail**

Remove `&& !wr_full` from the write guard so writes wrap. Expected:
`test_full_flag_and_no_overrun` FAILS on the offset-0 assertion.
Restore, confirm 3/3.

- [ ] **Step 7: Commit**

---

### Task 4: oca_proto and oca_core

**Files:**
- Create: `oca/hw/rtl/oca_proto.sv`, `oca/hw/rtl/oca_core.sv`
- Create: `oca/hw/sim/test_oca_core.py`, `oca/hw/sim/run_oca_core.py`

**Interfaces:**
- Consumes: `oca_keystore` and `oca_pktbuf` as declared above, and
  `chacha20_poly1305` as it exists in `oca/hw/rtl/`.
- Produces `oca_core`, the module the Ethernet integration will
  instantiate:

```systemverilog
module oca_core #(
    parameter int NUM_SLOTS = 8,
    parameter int BYTES     = 2048
) (
    input  logic       clk,
    input  logic       rst_n,
    // request payload in
    input  logic [7:0] s_axis_tdata,
    input  logic       s_axis_tvalid,
    output logic       s_axis_tready,
    input  logic       s_axis_tlast,
    // response payload out
    output logic [7:0] m_axis_tdata,
    output logic       m_axis_tvalid,
    input  logic       m_axis_tready,
    output logic       m_axis_tlast
);
```

This is the largest task in the plan. Build it in the order below and
run the tests after each stage rather than writing it all and debugging
at the end.

- [ ] **Step 1: Write the end-to-end test first**

```python
# SPDX-License-Identifier: MIT
"""End-to-end tests for oca_core: packets in, packets out.

Requests are injected on the 8-bit stream that verilog-ethernet will
later drive, so this runs with no Ethernet in the simulation. Every
expected value comes from aead_model through proto_model.
"""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

from aead_model import aead_encrypt
from proto_model import (HDR_LEN, OP_LOAD_KEY, OP_OPEN, OP_SEAL, ST_BAD_SLOT,
                         ST_OK, build_load_key, build_open, build_seal,
                         parse_response)

KEY = bytes(range(32))
NONCE = bytes(range(12))


async def setup(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst_n.value = 0
    dut.s_axis_tdata.value = 0
    dut.s_axis_tvalid.value = 0
    dut.s_axis_tlast.value = 0
    dut.m_axis_tready.value = 1
    for _ in range(3):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)


async def send_packet(dut, pkt: bytes):
    for i, b in enumerate(pkt):
        dut.s_axis_tdata.value = b
        dut.s_axis_tlast.value = 1 if i == len(pkt) - 1 else 0
        dut.s_axis_tvalid.value = 1
        await RisingEdge(dut.clk)
        while dut.s_axis_tready.value == 0:
            await RisingEdge(dut.clk)
    dut.s_axis_tvalid.value = 0
    dut.s_axis_tlast.value = 0


async def recv_packet(dut, budget: int = 20000) -> bytes:
    out = bytearray()
    for _ in range(budget):
        await RisingEdge(dut.clk)
        if dut.m_axis_tvalid.value == 1:
            out.append(int(dut.m_axis_tdata.value))
            if dut.m_axis_tlast.value == 1:
                return bytes(out)
    raise AssertionError(f"no response within {budget} cycles "
                         f"({len(out)} bytes seen)")


async def command(dut, pkt: bytes) -> dict:
    await send_packet(dut, pkt)
    return parse_response(await recv_packet(dut))


@cocotb.test()
async def test_load_key_then_seal(dut):
    await setup(dut)
    rsp = await command(dut, build_load_key(0x0001, 0, KEY))
    assert rsp["status"] == ST_OK and rsp["req_id"] == 0x0001
    assert rsp["opcode"] == OP_LOAD_KEY

    aad, msg = b"header", b"the quick brown fox"
    rsp = await command(dut, build_seal(0x0002, 0, NONCE, aad, msg))
    assert rsp["status"] == ST_OK, f"seal status {rsp['status']}"
    want_ct, want_tag = aead_encrypt(KEY, NONCE, aad, msg)
    assert rsp["body"][:16] == want_tag, "tag mismatch"
    assert rsp["body"][16:] == want_ct, "ciphertext mismatch"


@cocotb.test()
async def test_seal_then_open_round_trip(dut):
    await setup(dut)
    await command(dut, build_load_key(1, 2, KEY))
    aad, msg = b"", b"round trip"
    sealed = await command(dut, build_seal(2, 2, NONCE, aad, msg))
    tag, ct = sealed["body"][:16], sealed["body"][16:]
    opened = await command(dut, build_open(3, 2, NONCE, aad, ct, tag))
    assert opened["status"] == ST_OK, f"open status {opened['status']}"
    assert opened["body"] == msg, f"{opened['body']!r} != {msg!r}"


@cocotb.test()
async def test_unloaded_slot_is_refused(dut):
    await setup(dut)
    rsp = await command(dut, build_seal(4, 7, NONCE, b"", b"x"))
    assert rsp["status"] == ST_BAD_SLOT, f"status {rsp['status']}"
    assert rsp["body"] == b"", "body returned for a refused command"
```

Create `run_oca_core.py` in the established style, sources listing
`chacha20.sv`, `poly1305.sv`, `chacha20_poly1305.sv`, `oca_keystore.sv`,
`oca_pktbuf.sv`, `oca_proto.sv`, `oca_core.sv`, `hdl_toplevel`
`oca_core`, `build_dir` `sim_build_core`.

- [ ] **Step 2: Run it and watch it fail** — the modules do not exist.

- [ ] **Step 3: Write oca_proto, receive path only**

Start with: accept the stream into `oca_pktbuf`, and on `tlast` validate
the header. Emit a response header with the right status for
`ST_BAD_MAGIC`, `ST_BAD_VERSION`, `ST_BAD_OPCODE` and `ST_BAD_SLOT`, and
nothing else. `test_unloaded_slot_is_refused` should pass at this stage;
the other two will not.

The skeleton, which the following steps fill in. Header fields sit at
fixed offsets, so parsing is a counter and a case statement, not a
general parser:

```systemverilog
// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * Protocol engine for the OCA host interface.
 *
 * Parses the fixed 8-byte header out of the receive buffer, runs the
 * command, and builds the response. Knows nothing about cryptography
 * beyond driving chacha20_poly1305 and comparing its tag.
 *
 * Store and forward: the whole request is buffered before the engine
 * sees any of it, and the whole response is built before a byte leaves.
 * That is what lets a failed tag return no plaintext at all.
 *
 * The wire format is docs/design/2026-08-03-host-protocol.md.
 */
module oca_proto #(
    parameter int NUM_SLOTS = 8,
    parameter int BYTES     = 2048
) (
    input  logic        clk,
    input  logic        rst_n,
    // stream in
    input  logic [ 7:0] s_tdata,
    input  logic        s_tvalid,
    output logic        s_tready,
    input  logic        s_tlast,
    // stream out
    output logic [ 7:0] m_tdata,
    output logic        m_tvalid,
    input  logic        m_tready,
    output logic        m_tlast,
    // receive buffer
    output logic        rx_wr_en,
    output logic [ 7:0] rx_wr_data,
    output logic        rx_wr_clear,
    input  logic [11:0] rx_wr_count,
    input  logic        rx_wr_full,
    output logic [11:0] rx_rd_addr,
    input  logic [ 7:0] rx_rd_data,
    // transmit buffer
    output logic        tx_wr_en,
    output logic [ 7:0] tx_wr_data,
    output logic        tx_wr_clear,
    input  logic [11:0] tx_wr_count,
    output logic [11:0] tx_rd_addr,
    input  logic [ 7:0] tx_rd_data,
    // key store
    output logic        ks_wr_en,
    output logic [ 7:0] ks_wr_slot,
    output logic [255:0] ks_wr_key,
    output logic [ 7:0] ks_rd_slot,
    input  logic [255:0] ks_rd_key,
    input  logic        ks_rd_valid,
    // AEAD engine
    output logic        eng_start,
    output logic        eng_dec,
    output logic [255:0] eng_key,
    output logic [ 95:0] eng_nonce,
    output logic        eng_in_valid,
    output logic        eng_in_aad,
    output logic        eng_in_last,
    output logic [  6:0] eng_in_len,
    output logic [511:0] eng_in_data,
    input  logic        eng_in_ready,
    input  logic        eng_out_valid,
    input  logic [511:0] eng_out_data,
    input  logic [  6:0] eng_out_len,
    input  logic        eng_done,
    input  logic [127:0] eng_tag,
    input  logic        eng_err
);

    localparam logic [15:0] MAGIC   = 16'h434F;   // "OC", byte 0 = 0x4F
    localparam logic [ 7:0] VERSION = 8'h01;
    localparam int HDR_LEN = 8;

    typedef enum logic [3:0] {
        S_RX,        // accept bytes into the receive buffer
        S_PARSE,     // read header bytes 0..7
        S_DISPATCH,  // decide, or fail with a status
        S_LOADKEY,   // read 32 key bytes, write the slot
        S_ARGS,      // read nonce and lengths, validate them
        S_FEED,      // stream 64-byte blocks into the engine
        S_DRAIN,     // collect out_data into the transmit buffer
        S_CHECK,     // compare the tag (open only)
        S_BUILD,     // write the response header and body
        S_RESPOND    // stream the response out
    } fsm_t;
    fsm_t state;

    logic [ 7:0] opcode, slot, status;
    logic [15:0] req_id;
    logic [15:0] aad_len, msg_len;
    logic [11:0] rx_len;          // bytes of request received
    logic [31:0] cnt_rx, cnt_drop, cnt_done, cnt_auth_fail;

    // ... states filled in by the steps below
endmodule
```

Lengths are read from offsets 20-21 (AAD) and 22-23 (message or
ciphertext), little-endian, low byte first.

- [ ] **Step 4: Add the load-key command**

On `OP_LOAD_KEY`, read 32 bytes from buffer offsets 8..39 into a
register, present them to `oca_keystore` with `wr_en`, and respond with
the header and `ST_OK`. `test_load_key_then_seal`'s first half now
passes.

- [ ] **Step 5: Add seal**

Validate that `HDR_LEN + 12 + 4 + aad_len + msg_len` equals the received
byte count, and that both lengths fit the buffer — otherwise
`ST_BAD_LENGTH`. Then drive the engine: assert `start` with the key from
the slot and the nonce from offsets 8..19, feed AAD blocks with
`in_aad = 1` and message blocks with `in_aad = 0`, 64 bytes at a time
with the final partial block carrying its true `in_len` and `in_last`.
Write `out_data` back into a second `oca_pktbuf` instance as it arrives,
then the tag ahead of it in the response.

Watch the engine's `err` output: if it asserts, respond `ST_ENGINE_ERR`.

- [ ] **Step 6: Add open**

Same as seal with `dec = 1`, and the ciphertext fed as the message. When
`done` arrives, compare the engine's `tag` with the 16 bytes at offsets
24..39 **in one combinational comparison** — `engine_tag == rx_tag`, a
single 128-bit equality, which is constant-time by construction. On
mismatch respond `ST_AUTH_FAIL` with no body; on match stream the
plaintext.

- [ ] **Step 7: Add the stats counters**

Four 32-bit counters — packets received, packets dropped for an invalid
header, commands completed, authentication failures — returned in that
order, little-endian, by `OP_STATS`.

- [ ] **Step 8: Write oca_core**

Instantiate the two `oca_pktbuf` instances, `oca_keystore`,
`oca_proto` and `chacha20_poly1305`, and wire the AXI-Stream ports.
`oca_core` is wiring only: no logic of its own.

- [ ] **Step 9: Run the tests** — expected 3/3 PASS.

- [ ] **Step 10: Lint**

```sh
../tools/verilator/bin/verilator --lint-only -Wall hw/rtl/*.sv --top-module oca_core
```

- [ ] **Step 11: Commit**

---

### Task 5: The failure cases, and the security property

**Files:**
- Modify: `oca/hw/sim/test_oca_core.py`

- [ ] **Step 1: Write the error-path tests**

```python
from proto_model import (ST_AUTH_FAIL, ST_BAD_LENGTH, ST_BAD_MAGIC,
                         ST_BAD_OPCODE, ST_BAD_VERSION, MAGIC, _header)


@cocotb.test()
async def test_corrupt_tag_yields_no_plaintext(dut):
    """The security property of the whole design: a failed tag must
    return an error and not one byte of plaintext."""
    await setup(dut)
    await command(dut, build_load_key(1, 0, KEY))
    msg = b"secret payload that must not leak"
    sealed = await command(dut, build_seal(2, 0, NONCE, b"", msg))
    tag, ct = bytearray(sealed["body"][:16]), sealed["body"][16:]
    tag[0] ^= 0x01
    rsp = await command(dut, build_open(3, 0, NONCE, b"", ct, bytes(tag)))
    assert rsp["status"] == ST_AUTH_FAIL, f"status {rsp['status']}"
    assert rsp["body"] == b"", f"plaintext leaked: {rsp['body']!r}"
    assert msg not in rsp["body"], "plaintext present in the response"


@cocotb.test()
async def test_bad_header_fields(dut):
    await setup(dut)
    bad_magic = b"XX" + build_seal(1, 0, NONCE, b"", b"x")[2:]
    assert (await command(dut, bad_magic))["status"] == ST_BAD_MAGIC

    pkt = bytearray(build_seal(2, 0, NONCE, b"", b"x"))
    pkt[2] = 0x99
    assert (await command(dut, bytes(pkt)))["status"] == ST_BAD_VERSION

    pkt = bytearray(build_seal(3, 0, NONCE, b"", b"x"))
    pkt[3] = 0x7F
    assert (await command(dut, bytes(pkt)))["status"] == ST_BAD_OPCODE


@cocotb.test()
async def test_inconsistent_lengths(dut):
    await setup(dut)
    await command(dut, build_load_key(1, 0, KEY))
    pkt = bytearray(build_seal(2, 0, NONCE, b"aad", b"message"))
    pkt[22] = 0xFF          # msg_len low byte, now far past the packet
    pkt[23] = 0x00
    rsp = await command(dut, bytes(pkt))
    assert rsp["status"] == ST_BAD_LENGTH, f"status {rsp['status']}"
```

- [ ] **Step 2: Run and fix until green** — 6/6.

- [ ] **Step 3: Add the randomised round trip**

```python
@cocotb.test()
async def test_randomised_round_trips(dut):
    await setup(dut)
    rng = random.Random(0x0CA0)
    for i in range(20):
        key = bytes(rng.getrandbits(8) for _ in range(32))
        nonce = bytes(rng.getrandbits(8) for _ in range(12))
        slot = rng.randrange(8)
        alen = rng.choice([0, 1, 16, 63, 64, 65])
        mlen = rng.choice([1, 16, 63, 64, 65, 128, 200])
        aad = bytes(rng.getrandbits(8) for _ in range(alen))
        msg = bytes(rng.getrandbits(8) for _ in range(mlen))
        await command(dut, build_load_key(i, slot, key))
        sealed = await command(dut, build_seal(i, slot, nonce, aad, msg))
        want_ct, want_tag = aead_encrypt(key, nonce, aad, msg)
        assert sealed["status"] == ST_OK, f"#{i} seal status"
        assert sealed["body"][:16] == want_tag, f"#{i} tag"
        assert sealed["body"][16:] == want_ct, f"#{i} ct"
        opened = await command(dut, build_open(
            i, slot, nonce, aad, want_ct, want_tag))
        assert opened["status"] == ST_OK, f"#{i} open status"
        assert opened["body"] == msg, f"#{i} plaintext"
```

- [ ] **Step 4: Prove the security test is not vacuous**

Break the tag comparison in `oca_proto.sv` so it always reports a match,
re-run, and confirm `test_corrupt_tag_yields_no_plaintext` FAILS on the
leak assertion — not on the status. Restore and confirm green.

This is the single most important check in the plan: it is the
difference between the design's security property being implemented and
merely being written down.

- [ ] **Step 5: Commit**

**One case from the design document is deliberately not tested here.**
It lists "a load-key command arriving while the engine is busy". This
implementation processes one command to completion before accepting the
next — the simplification recorded at the top of this plan — so the
engine is never busy when a command arrives, and a test for it would
assert on an unreachable state. If command pipelining is ever added,
that test becomes both possible and mandatory. Note it in the commit
message rather than silently skipping it.

---

### Task 6: Synthesis and documentation

**Files:**
- Modify: `oca/hw/syn/run_synth.py`, `oca/hw/syn/README.md`,
  `oca/README.md`, `AGENTS.md`, `Security.md`

- [ ] **Step 1: Add oca_core to the synthesis flow**

In `run_synth.py`, add to `DESIGNS`:

```python
    "oca_core": ["chacha20.sv", "poly1305.sv", "chacha20_poly1305.sv",
                 "oca_keystore.sv", "oca_pktbuf.sv", "oca_proto.sv",
                 "oca_core.sv"],
```

- [ ] **Step 2: Synthesise**

```sh
.venv/bin/python hw/syn/run_synth.py oca_core --freq 100
```

Record LUTs, flip-flops, multipliers, block RAM and Fmax. The packet
buffers should infer DP16KD block RAM rather than LUT RAM — if they do
not, say so, because 2048 bytes in LUTs would be a serious area
regression and the fix is a coding-style change, not a design change.

- [ ] **Step 3: Write up**

New result block in `oca/hw/syn/README.md` in the established style,
with the engine's own numbers alongside so the protocol layer's cost is
visible separately. State the expected end-to-end throughput and where
it comes from (the one-byte buffer), and that it is not measured on
hardware.

- [ ] **Step 4: Update Security.md**

Add the protocol's three exposures from the design document (key in the
clear on load, no authentication of the requester, host-supplied nonce),
and amend section 4 item 1: the tag comparison obligation now applies
only to callers instantiating `chacha20_poly1305.sv` directly, because
traffic through `oca_core` is compared on the FPGA in constant time.

- [ ] **Step 5: Update status**

`oca/README.md` and `AGENTS.md`: the new modules, the new test suites and
their counts, and the next step — the Ethernet integration, which needs
the board.

- [ ] **Step 6: Commit**
