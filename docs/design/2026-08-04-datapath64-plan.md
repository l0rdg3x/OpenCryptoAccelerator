# 64-bit host datapath — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development to implement this plan
> task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Widen the datapath inside `oca_core` from 8 to 64 bits, so the
packet buffer stops starving the AEAD engine, and fix the response
handshake that costs three cycles per beat.

**Architecture:** The wire format does not change and `proto_model.py` is
not touched. `oca_pktbuf` becomes 256 x 64 with a byte count on writes;
`oca_proto` reads header and arguments as whole words, gains a funnel
shifter for the one misaligned boundary (AAD to message), and replaces
its fetch-then-present response states with a clock-enabled pipeline.

**Tech stack:** SystemVerilog, cocotb 2.x + Verilator, synthesis via
`oca/hw/syn/run_synth.py`.

**Source analysis:** measured on 2026-08-04; the numbers below are from
that study unless marked as estimates.

## Global Constraints

- **Maximum throughput the hardware allows.** This plan exists because
  the 8-bit path could not feed one engine. Where a choice affects
  throughput, take the faster one and measure it.
- Correctness is not tradeable: the 9 `oca_core` tests, the 12 official
  Poly1305 vectors and the constant-time property all hold at the end.
- Product in English; SPDX CERN-OHL-P-2.0 on RTL, MIT on Python.
- `oca/hw/sim/proto_model.py` is **not modified**. It is the definition
  of the wire format, and the format is unchanged.
- No vendor primitives; memories inferred, not instantiated.
- Work on branch `feat/host-protocol`. Nothing is pushed.
- Every commit carries the two harness trailers.
- All commands run from `oca/`. Python is `.venv/bin/python`.

## Three traps, measured, that this plan exists to avoid

1. **A 3:1 multiplexer at 64 bits costs 771 LUT4 written as an
   `if / else if` chain over comparators, and 129 written as a `case`
   over a registered 2-bit select.** Six times. Every next-state
   multiplexer in `oca_proto` must be a `case` on a registered selector.
2. **A second read port on `oca_pktbuf` destroys PDP mode**, taking it
   from 2 DP16KD per buffer to 4. yosys maps the current single-write,
   single-read shape via `$__PDPW16KD_` at 36 bits wide. Keep exactly one
   of each port.
3. **`tkeep` semantics are not documented upstream.** `verilog-axis`'s
   `axis_adapter` implicitly assumes right-justified contiguous keep.
   Decode it with a priority encoder and **fail closed** — status `05` —
   if a non-last beat arrives with a partial keep, because the word write
   pointer would otherwise desynchronise silently.

## Field offsets, worked out

Every fixed field is word-aligned. Only one boundary is not.

| bytes | field | word | slice |
|-------|-------|------|-------|
| 0-7 | header | W0 | whole word |
| 8-19 | nonce | W1, W2 | `{W2[31:0], W1[63:0]}` |
| 20-21 | aad_len | W2 | `[47:32]` |
| 22-23 | msg_len / ct_len | W2 | `[63:48]` |
| 8-39 | key (load) | W1..W4 | `{W4,W3,W2,W1}` |
| 24-39 | received tag (open) | W3, W4 | `{W4,W3}` |
| 24 / 40 | payload start | W3 / W5 | byte 0, shift 0 |
| +aad_len | **message start** | varies | **shift `aad_len[2:0]`** |

The existing slices in `oca_proto` (`args[95:0]`, `args[111:96]`,
`args[127:112]`, `args[255:128]`) stay exactly as written; only the fill
changes, to `args <= {rx_rd_data, args[255:64]}`, four shifts instead of
thirty-two. The `hdr` register disappears — it is one word.

---

### Task 1: oca_pktbuf at 64 bits

**Files:**
- Modify: `oca/hw/rtl/oca_pktbuf.sv`
- Rewrite: `oca/hw/sim/test_pktbuf.py`

**Interface produced:**

```systemverilog
module oca_pktbuf #(
    parameter int BYTES = 2048
) (
    input  logic        clk,
    input  logic        rst_n,
    input  logic        wr_en,
    input  logic [63:0] wr_data,
    input  logic [ 3:0] wr_bytes,   // 1..8 valid bytes in wr_data
    input  logic        wr_clear,
    output logic [11:0] wr_count,   // still counts BYTES, not words
    output logic        wr_full,
    input  logic [ 8:0] rd_addr,    // word address
    output logic [63:0] rd_data
);
```

`wr_count` staying a byte count is deliberate: `rx_len`, `want_len` and
`resp_len` all compare against it, and that comparison is what keeps a
field from being read out of bytes the packet never wrote.

- [ ] **Step 1: Rewrite the testbench first**

Rewrite `test_pktbuf.py` to drive words. Keep the three existing
behaviours — write-then-read-back, clear-restarts-at-zero, full-flag —
and add the cases the width introduces:

```python
@cocotb.test()
async def test_partial_final_word(dut):
    """A final word with 3 valid bytes advances wr_count by 3, not 8."""
    await setup(dut)
    await write_word(dut, int.from_bytes(b"abcdefgh", "little"), 8)
    await write_word(dut, int.from_bytes(b"xyz00000", "little"), 3)
    assert int(dut.wr_count.value) == 11, f"count {int(dut.wr_count.value)}"
    w0 = await read_word(dut, 0)
    assert w0.to_bytes(8, "little") == b"abcdefgh"
    w1 = await read_word(dut, 1)
    assert w1.to_bytes(8, "little")[:3] == b"xyz"
```

- [ ] **Step 2: Run it and watch it fail** — the port widths do not match
      yet, so the build fails. That is the right failure.

- [ ] **Step 3: Widen the module**

`WORDS = BYTES/8`, `ADDR_W = $clog2(WORDS)`; the write index is
`wr_count[ADDR_W+2:3]`; `wr_count <= wr_count + 12'(wr_bytes)`. Keep the
read port registered and single. Update the header comment: the
415-cycle paragraph is now wrong, and the honest replacement is that a
block costs 8 reads plus the pipeline.

- [ ] **Step 4: Run the tests** — all pass.

- [ ] **Step 5: Confirm block RAM inference**

```sh
../tools/yosys/bin/yosys -p "read_slang --top oca_pktbuf hw/rtl/oca_pktbuf.sv; synth_ecp5 -top oca_pktbuf; stat" | grep -E "DP16KD|PDPW16KD"
```

Expected: 2 DP16KD, mapped via `$__PDPW16KD_`. If you see 4, or any
`TRELLIS_DPR16X4`, stop and report — something forced it out of PDP mode
and that is worth fixing before going further.

- [ ] **Step 6: Prove the tests can fail** — make `wr_count` advance by 8
      regardless of `wr_bytes`; `test_partial_final_word` must fail on the
      count. Restore.

- [ ] **Step 7: Lint and commit**

---

### Task 2: The testbench at 64 bits

**Files:**
- Modify: `oca/hw/sim/test_oca_core.py`

This lands **before** the RTL, and the suite will fail until Task 3. That
is intended: it is what makes Task 3's completion meaningful.

**The nine test bodies do not change.** Only four helpers and `setup`:

- [ ] **Step 1: Widen the helpers**

`send_packet` splits the packet into little-endian 8-byte words, the last
carrying `keep = (1 << n) - 1` and zero padding. `recv_packet` reassembles
with `int(tdata).to_bytes(8, "little")[:popcount(keep)]`. `setup` drives
`s_axis_tkeep` and reads `m_axis_tkeep`.

Keep the handshake discipline exactly as it is — sample `tready` at
`ReadOnly`, then `RisingEdge` — it is a hard rule in `AGENTS.md` and the
reason the back-to-back test works.

- [ ] **Step 2: Run and confirm the expected failure** — the DUT still has
      8-bit ports, so the build fails on width mismatch. Record it.

- [ ] **Step 3: Commit** the testbench alone, with a message saying the
      suite is red until the RTL follows. A red commit is acceptable here
      *only* because the next task makes it green; say so.

---

### Task 3: oca_proto and oca_core at 64 bits

**Files:**
- Modify: `oca/hw/rtl/oca_proto.sv`, `oca/hw/rtl/oca_core.sv`

The largest task. Build it in the order below, running the suite after
each stage.

- [ ] **Step 1: Ports and the shared reader**

Widen `tdata`, add `s_tkeep`/`m_tkeep`, `wr_bytes` on both buffers,
`rd_addr` to 9 bits. The shared sequential reader becomes word-based:
`rd_ptr` 9 bits, `rd_left` 4 bits.

`S_RX` decodes `tkeep` with a priority encoder and **fails closed** on a
partial keep in a non-last beat — status `05`, per trap 3.

- [ ] **Step 2: Header and arguments**

`S_PARSE` is now one read. `S_ARGS` is four. The `hdr` register goes
away; the `args` slices are unchanged, filled by
`args <= {rx_rd_data, args[255:64]}`.

Run: the header-validation tests and `test_unloaded_slot_is_refused`
should pass at this point.

- [ ] **Step 3: The funnel shifter**

One 128-to-64 funnel with a 3-bit amount and a `prev` register:

```systemverilog
    logic [63:0] prev, cur;
    logic [ 2:0] shift;
    logic [63:0] aligned;
    always_comb aligned = {cur, prev} >> (8 * shift);
```

Keep `feed_addr` a byte address exactly as today and derive the word
address `feed_addr[11:3]` and `shift = feed_addr[2:0]`. All the existing
section logic stays untouched, and `shift` is automatically 0 through the
AAD and `aad_len[2:0]` through the message.

`S_FEED` becomes 9 reads plus 2 pipeline cycles instead of 66. Nine, not
eight: the funnel needs priming with the previous word, and keeping it
uniform costs one cycle per block and simplifies the control.

Header and argument reads bypass the funnel — they are always aligned.

- [ ] **Step 4: The drain path**

`S_DRAIN` writes words with `tx_wr_bytes`. Only the last block of the
message section can be partial, so the write pointer stays word-aligned
everywhere else.

Run: the seal tests pass.

- [ ] **Step 5: Zero the tail of the last response beat**

New at 64 bits, and it is a security matter: the final beat carries up to
7 bytes beyond `resp_len` — engine output past `out_len` — which are
hidden only by the downstream MAC honouring `tkeep`. Mask them in
`oca_proto`. At 8 bits this could not happen, so it is a regression the
width introduces, not an existing gap.

- [ ] **Step 6: oca_core wiring** — widths, two `tkeep`, two `wr_bytes`.
      No logic of its own.

- [ ] **Step 7: Run the whole suite** — 9/9, and `test_randomised_round_trips`
      is the one that exercises the funnel: its `alen` values 0, 1, 16,
      63, 64, 65 give shifts 0, 1, 0, 7, 0, 1.

- [ ] **Step 8: Prove the funnel is under test** — force `shift` to 0 and
      confirm the misaligned cases fail while the aligned ones pass.
      Restore.

- [ ] **Step 9: Lint and commit**

---

### Task 4: The response pipeline

**Files:**
- Modify: `oca/hw/rtl/oca_proto.sv`

Measured today: the response spends three cycles per beat because
`S_RESP_FETCH` and `S_RESPOND` alternate through the registered read
port. At 64 bits that is 24 cycles per block instead of 192 — better, but
still 3x what it should be, and by then the largest single term in the
loop.

- [ ] **Step 1: Replace the two states with a clock-enabled pipeline**

Three stages — word index, registered buffer read, output register —
frozen by a single enable:

```systemverilog
    always_comb go = !m_tvalid || m_tready;
```

Freezing everything means the buffer address holds, the BRAM re-reads the
same address, and no data is lost: no skid buffer, and a low `tready`
costs exactly one cycle. The enable lands on the flip-flops' CE inputs,
so it is free in LUTs.

`tx_rd_addr = idx - body_start_w` is a 9-bit subtraction, about 5 LUTs —
count it.

- [ ] **Step 2: Measure the improvement**

Use the differential harness at
`/tmp/claude-1000/-home-l0rdg3x-coding-OpenCryptoAccelerator/47eea256-247e-4aff-ba64-60e7d4bdacab/scratchpad/run_measure.py`
(4-block and 8-block messages, difference over 4). Expect around 63
cycles per 64-byte block against today's 415. Report what you measure; if
it is materially different, find out why before writing it up.

- [ ] **Step 3: Confirm backpressure still works** — the existing
      `test_backpressure_is_transparent` covers a stalling sink, which is
      exactly what the clock enable handles. It must still pass, and it is
      not vacuous here: force `go` to `1'b1` and confirm it fails.

- [ ] **Step 4: Lint and commit**

---

### Task 5: Synthesis and documentation

**Files:**
- Modify: `oca/hw/syn/README.md`, `oca/README.md`, `AGENTS.md`,
  `docs/design/2026-08-03-host-protocol.md`

- [ ] **Step 1: Synthesise**

```sh
.venv/bin/python hw/syn/run_synth.py oca_core --freq 100
```

Against the 8-bit baseline of 11149 COMB, 10842 FF, 20 MULT, 2 DP16KD,
50.95 MHz. Expect roughly +530 COMB and +325 FF (estimates), 4 DP16KD,
and multipliers unchanged — if multipliers moved, something is wrong.

- [ ] **Step 2: Measure two engines**

The MVP configuration is two `oca_core` instances. Synthesise a
throwaway two-instance top level in the scratchpad and report LUT, FF,
multiplier and DP16KD totals plus Fmax over four seeds, against the
49.28 MHz measured for two 8-bit cores. This is the number that says
whether the MVP target still holds.

- [ ] **Step 3: Compute the end-to-end figure**

Cycles per block from Task 4, Fmax from step 2, two engines:
`2 x 64 bytes x Fmax / cycles`. Compare against one GbE port's 125 MB/s
and state the margin. Label it a simulation-derived figure — nothing has
run on hardware.

- [ ] **Step 4: Write it up** in `oca/hw/syn/README.md`, and update the
      status sections of `oca/README.md` and `AGENTS.md`.

- [ ] **Step 5: Amend the design document**

`docs/design/2026-08-03-host-protocol.md` already carries a dated
amendment about the 64-bit move. Extend it with what was actually built
and measured, in the same style — appended, not rewritten.

- [ ] **Step 6: Commit**
