# Poly1305 datapath rework — implementation plan

> **Status: executed on 2026-08-03, in `1205c68`** ("rtl: rework
> Poly1305 into a 26-bit limb datapath"). The result is in
> `oca/hw/syn/README.md` — 65 -> 20 MULT18X18D, and 22.94 -> 52.68 MHz
> on the netlist of that day, 55.41 MHz on today's — and
> `hw/sim/run_poly1305.py` covers it, 4/4 plus 4 more at
> `ROWS_PER_CYCLE = 5`. **The `- [ ]` boxes below are the plan as it was
> written, not work outstanding.** None is re-ticked here: a box ticked
> without walking its step one by one would claim more than this line
> does, and the commit and the suite are the evidence.

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps
> use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single-cycle 131x128 multiply and 261-bit reduction
in `oca/hw/rtl/poly1305.sv` with a five-digit, 26-bit limb datapath that
folds the mod 2^130-5 reduction into the accumulation, cutting multiplier
use from 65 and removing the critical path — without changing the module
interface.

**Architecture:** `a` and `r` are held as five 26-bit digits. One row of
`r` is consumed per cycle (parameter `ROWS_PER_CYCLE`), producing five
products per row that accumulate into five 64-bit accumulators; products
landing above bit 130 re-enter the low digits multiplied by 5. Carry
normalisation is split across two cycles. Multiplier inputs and outputs
are registered so ECP5/7-series/UltraScale+ DSP blocks absorb them.

**Tech stack:** SystemVerilog (yosys `read_slang` + Verilator), cocotb
2.x testbenches under `oca/.venv`, synthesis via
`oca/hw/syn/run_synth.py`.

**Design doc:** `docs/design/2026-08-03-poly1305-datapath.md`

## Global Constraints

- Product in English: code, identifiers, comments, commit messages.
- RTL files carry `// SPDX-License-Identifier: CERN-OHL-P-2.0`;
  simulation and tooling files carry `// SPDX-License-Identifier: MIT`
  (`#` comment form in Python).
- Expected values are never hand-typed. Official vectors are parsed from
  `oca/tests/vectors/sources/rfc8439.txt`; the Python model is validated
  against them before being used as an oracle.
- The module interface (ports, `blk_ready`/`blk`/`last` handshake,
  `data_len` 1..16 support) must not change: `chacha20_poly1305.sv` is
  not modified by this plan.
- Latency must stay independent of the data (constant-time requirement,
  SPEC.md). No stage may iterate on the value of the accumulator.
- No vendor primitives instantiated; multiplications stay written as `*`.
- Work happens on branch `feat/ecp5-synthesis`. Nothing is pushed.
- All commands run from `oca/`. Python is `.venv/bin/python`.

## File Structure

- `oca/hw/sim/poly1305_model.py` (create) — Poly1305 reference model in
  plain Python integers, plus the RFC vector parser reused by the
  testbench. One responsibility: say what the correct tag is.
- `oca/hw/sim/test_poly1305.py` (modify) — keeps the official-vector
  test, gains model-validation, edge-case and randomised tests.
- `oca/hw/rtl/poly1305.sv` (rewrite) — the new datapath.
- `oca/hw/syn/README.md` (modify) — before/after synthesis numbers.
- `oca/README.md`, `AGENTS.md` (modify) — status.

---

### Task 1: Reference model, validated on the official vectors

**Files:**
- Create: `oca/hw/sim/poly1305_model.py`
- Test: `oca/hw/sim/test_poly1305.py` (new test added)

**Interfaces:**
- Produces: `poly1305_tag(key: bytes, msg: bytes) -> bytes` (32-byte key,
  arbitrary-length message, returns the 16-byte tag) and
  `parse_rfc8439() -> list[tuple[str, bytes, bytes, bytes]]` returning
  `(name, key32, msg, tag16)`, moved here from the testbench.

- [ ] **Step 1: Move the vector parser and write the model**

Create `oca/hw/sim/poly1305_model.py`. The parser functions
(`_section`, `_colonhex_after`, `_hexdumps`, `parse_rfc8439`) move
verbatim from `test_poly1305.py` — same code, new home, so both the
model test and the testbench use one parser.

```python
# SPDX-License-Identifier: MIT
"""Poly1305 reference model and RFC 8439 vector parser.

The model is plain integer arithmetic straight from RFC 8439 2.5.1. It
is the oracle for the randomised RTL tests, so it is itself checked
against the official vectors first (see test_poly1305.py).
"""

P = (1 << 130) - 5
CLAMP = 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF


def poly1305_tag(key: bytes, msg: bytes) -> bytes:
    """RFC 8439 2.5.1. key = r || s, 32 bytes."""
    assert len(key) == 32
    r = int.from_bytes(key[:16], "little") & CLAMP
    s = int.from_bytes(key[16:], "little")
    acc = 0
    for off in range(0, len(msg), 16):
        chunk = msg[off:off + 16]
        n = int.from_bytes(chunk, "little") | (1 << (8 * len(chunk)))
        acc = ((acc + n) * r) % P
    return ((acc + s) % (1 << 128)).to_bytes(16, "little")
```

Then append the parser functions copied from `test_poly1305.py` lines
19-76 unchanged, and fix `SRC` to keep pointing at
`tests/vectors/sources/rfc8439.txt`:

```python
SRC = Path(__file__).resolve().parents[2] / "tests" / "vectors" / "sources" / "rfc8439.txt"
```

- [ ] **Step 2: Write the test that validates the model**

In `oca/hw/sim/test_poly1305.py`, replace the local parser with an
import and add a model test that runs without any RTL:

```python
from poly1305_model import parse_rfc8439, poly1305_tag

VECS = parse_rfc8439()


@cocotb.test()
async def test_model_matches_official_vectors(dut):
    """The oracle must reproduce every official vector before it is
    trusted to judge the RTL."""
    for name, key, msg, tag in VECS:
        got = poly1305_tag(key, msg)
        assert got == tag, f"{name}: model got {got.hex()} want {tag.hex()}"
    assert len(VECS) == 5, f"expected 5 official vectors, parsed {len(VECS)}"
```

The `len(VECS) == 5` assertion is deliberate: if the parser silently
stops finding vectors, a model test over an empty list would pass.

- [ ] **Step 3: Run it and watch it pass on the current core**

```sh
.venv/bin/python hw/sim/run_poly1305.py
```

Expected: `test_model_matches_official_vectors` PASS, plus the existing
`test_all_vectors` PASS (RTL untouched so far).

- [ ] **Step 4: Prove the model test can fail**

Temporarily break the model — change `CLAMP` to `(1 << 128) - 1` — and
re-run. Expected: `test_model_matches_official_vectors` FAILS with a tag
mismatch. Restore `CLAMP`, re-run, confirm PASS.

- [ ] **Step 5: Commit**

```sh
git add oca/hw/sim/poly1305_model.py oca/hw/sim/test_poly1305.py
git commit -m "sim: add a Poly1305 reference model validated on RFC 8439 vectors"
```

---

### Task 2: Randomised and edge-case testbench, against the current core

**Files:**
- Modify: `oca/hw/sim/test_poly1305.py`

**Interfaces:**
- Consumes: `poly1305_tag`, `parse_rfc8439` from Task 1.
- Produces: `run_mac(dut, key, msg) -> bytes` (existing helper, timeout
  raised) used by every RTL test.

This task must pass against the **current** RTL. That is the point: a
testbench validated on a known-good core is trustworthy when the core is
replaced.

- [ ] **Step 1: Raise the handshake timeouts**

In `run_mac`, the two polling loops use `range(20)`. The new datapath
needs ~9 cycles per block plus final reduction; raise both to
`range(64)` so the test fails on wrong results, not on a tight timeout.

- [ ] **Step 2: Write the edge-case test**

```python
def edge_case_messages() -> list[tuple[str, bytes, bytes]]:
    """(name, key, msg) triples aimed at 26-bit digit carries."""
    k_ff = b"\xff" * 32
    k_mix = bytes(range(32))
    cases = [
        ("all-ff-1blk", k_ff, b"\xff" * 16),
        ("all-ff-4blk", k_ff, b"\xff" * 64),
        ("all-ff-partial", k_ff, b"\xff" * 17),
        ("one-byte", k_mix, b"\x01"),
        ("fifteen-bytes", k_mix, b"\xff" * 15),
        ("digit-boundary", k_mix, (1 << 26).to_bytes(16, "little")),
        ("high-bit", k_mix, (1 << 127).to_bytes(16, "little")),
        ("zeros", k_mix, bytes(16)),
        ("r-zero", bytes(16) + b"\xaa" * 16, b"\xff" * 32),
        ("s-zero", b"\xaa" * 16 + bytes(16), b"\xff" * 32),
    ]
    return cases


@cocotb.test()
async def test_edge_cases(dut):
    await setup(dut)
    for name, key, msg in edge_case_messages():
        want = poly1305_tag(key, msg)
        got = await run_mac(dut, key, msg)
        assert got == want, f"{name}: got {got.hex()} want {want.hex()}"
        dut._log.info(f"{name}: OK ({len(msg)} bytes)")
```

`digit-boundary` and `high-bit` sit exactly where the 26-bit digits
split; `all-ff-*` maximise carries between digits.

- [ ] **Step 3: Write the randomised test**

```python
import random


@cocotb.test()
async def test_randomised(dut):
    await setup(dut)
    rng = random.Random(0xC0FFEE)   # fixed seed: failures are reproducible
    for i in range(200):
        key = bytes(rng.getrandbits(8) for _ in range(32))
        n = rng.choice([1, 15, 16, 17, 31, 32, 33, 64, 129])
        msg = bytes(rng.getrandbits(8) for _ in range(n))
        want = poly1305_tag(key, msg)
        got = await run_mac(dut, key, msg)
        assert got == want, (
            f"random #{i} ({n} bytes): got {got.hex()} want {want.hex()}\n"
            f"  key={key.hex()}\n  msg={msg.hex()}")
```

200 messages keeps simulation time sane; the seed is fixed so a failure
is reproducible. The failure message prints key and message because a
random failure you cannot replay is nearly useless.

- [ ] **Step 4: Run the whole suite against the current RTL**

```sh
.venv/bin/python hw/sim/run_poly1305.py
```

Expected: 4 tests PASS (`test_model_matches_official_vectors`,
`test_all_vectors`, `test_edge_cases`, `test_randomised`). If anything
fails here, the testbench is wrong — the current core is known good.
Fix the testbench, not the RTL.

- [ ] **Step 5: Commit**

```sh
git add oca/hw/sim/test_poly1305.py
git commit -m "sim: add edge-case and randomised Poly1305 tests against the model"
```

---

### Task 3: The new datapath

**Files:**
- Rewrite: `oca/hw/rtl/poly1305.sv`

**Interfaces:**
- Consumes: nothing from earlier tasks (the testbench judges it).
- Produces: same module interface as today, plus
  `parameter int ROWS_PER_CYCLE = 1`.

Arithmetic, fixed before writing code:

- Digits are 26 bits, five of them (5 x 26 = 130).
- `r` clamped as today; `r5_d[j] = 5 * r_d[j] < 2^29`, precomputed once
  per key with a shift and an add, no multiplier.
- `sum_d[i] = a_d[i] + n_d[i]` is **not** carry-propagated: digits stay
  under 2^28, which the multipliers accept. This is the standard lazy
  representation.
- Products are 28x29 bits; five of them accumulate into 64-bit
  accumulators (5 * 2^57 < 2^60).
- Row `j` contributes `sum_d[i] * (i + j >= 5 ? r5_d[j] : r_d[j])` to
  accumulator `(i + j) mod 5`. Instead of dynamically indexing the
  accumulator write port, the accumulator vector **rotates by
  ROWS_PER_CYCLE each cycle** and products are written at fixed local
  indices; a constant one-position rotation at the end compensates the
  offset. Rotation by a constant is wiring, not logic.

- [ ] **Step 1: Write the module header and declarations**

```systemverilog
// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * Poly1305 one-time authenticator (RFC 8439 section 2.5).
 *
 * Usage: pulse `start` with the 256-bit one-time key (r || s,
 * key[127:0] = r, key[255:128] = s, little-endian words). Then feed the
 * message in 16-byte blocks: while `blk_ready` is high, pulse `blk`
 * with data_in/data_len (1..16 valid bytes, byte 0 = bits [7:0]) and
 * `last` on the final block. `done` pulses with `tag` valid.
 *
 * WARNING: r and s must be single-use (RFC 8439 2.5). The caller is
 * responsible for key generation (e.g. chacha20 block with counter 0).
 *
 * Datapath: the accumulator and r are five 26-bit digits (5*26 = 130,
 * the width of the modulus). Products landing above bit 130 re-enter the
 * low digits multiplied by 5, because 2^130 = 5 (mod 2^130-5): the
 * reduction is part of the accumulation, not a stage of its own.
 * Digits are kept lazily reduced (under 2^28) between blocks; only the
 * final tag is fully reduced.
 *
 * Cost per block: 1 (add) + ceil(5/ROWS_PER_CYCLE) (multiply rows)
 * + 1 (pipeline drain) + 2 (carry normalisation) cycles, so 9 cycles at
 * the default ROWS_PER_CYCLE = 1, with 5 multipliers.
 *
 * Latency does not depend on the data: no stage iterates on the value of
 * the accumulator, and the final conditional subtract is a fixed-duration
 * combinational choice. This constant-time property is required by
 * SPEC.md and must survive any change.
 *
 * Portability: no vendor primitives are instantiated and multiplier
 * inputs and outputs are registered, so ECP5 MULT18X18D, 7-series
 * DSP48E1 and UltraScale+ DSP48E2 blocks can absorb them.
 */
module poly1305 #(
    // Rows of r consumed per cycle. 1 costs 5 multipliers and 5 cycles;
    // raise it on devices with DSP to spare (see SPEC.md, OCA-10/50).
    parameter int ROWS_PER_CYCLE = 1
) (
    input  logic         clk,
    input  logic         rst_n,
    input  logic         start,
    input  logic         blk,
    input  logic         last,
    output logic         busy,
    output logic         blk_ready,
    output logic         done,
    input  logic [255:0] key,
    input  logic [127:0] data_in,
    input  logic [  4:0] data_len,
    output logic [127:0] tag
);

    localparam int NL   = 5;                 // digits
    localparam int LW   = 26;                // digit width
    localparam int MCYC = (NL + ROWS_PER_CYCLE - 1) / ROWS_PER_CYCLE;
    localparam logic [127:0] CLAMP = 128'h0fff_fffc_0fff_fffc_0fff_fffc_0fff_ffff;

    // p = 2^130 - 5
    localparam logic [130:0] P = 131'h3_FFFF_FFFF_FFFF_FFFF_FFFF_FFFF_FFFF_FFFB;

    logic [LW-1:0] r_d  [NL];    // clamped r, by digit
    logic [LW+2:0] r5_d [NL];    // 5*r_d, < 2^29
    logic [127:0]  s;
    logic [LW+1:0] a_d  [NL];    // accumulator, lazily reduced (< 2^28)
    logic [LW+1:0] sum_d[NL];    // a + n, no carry propagation
    logic [63:0]   t    [NL];    // row accumulators
    logic [63:0]   c1   [NL];    // half-normalised digits, S_C1 -> S_C2
    logic [LW-1:0] f    [NL];    // fully propagated digits, S_FIN -> S_FIN2
    logic [63:0]   fold;         // bit-130 overflow, times 5
    logic [130:0]  a_flat;       // canonical accumulator, < p
    logic [2:0]    row;          // current multiply cycle, 0..MCYC-1
    logic          last_r;
```

- [ ] **Step 2: Write the block-to-digits conversion and the multiply array**

`n` is the 16-byte block with the RFC's trailing 1 byte appended, so up
to 129 bits — five digits.

```systemverilog
    logic [7:0]   nbits;
    logic [127:0] masked;
    logic [128:0] n_full;
    logic [LW+1:0] n_d [NL];

    always_comb begin
        nbits  = {3'b000, data_len} << 3;
        masked = data_in & ({128{1'b1}} >> (8'd128 - nbits));
        n_full = {1'b0, masked} | (129'd1 << nbits);
        for (int i = 0; i < NL; i++)
            n_d[i] = (LW+2)'((n_full >> (i * LW)) & ((129'd1 << LW) - 129'd1));
    end

    // One row of products per slot, registered on both sides so the DSP
    // blocks can absorb them.
    logic [LW+1:0] mul_a [ROWS_PER_CYCLE][NL];
    logic [LW+2:0] mul_b [ROWS_PER_CYCLE][NL];
    logic [56:0]   prod  [ROWS_PER_CYCLE][NL];

    always_comb begin
        for (int sl = 0; sl < ROWS_PER_CYCLE; sl++) begin
            automatic int unsigned j = row * ROWS_PER_CYCLE + sl;
            for (int i = 0; i < NL; i++) begin
                mul_a[sl][i] = sum_d[i];
                if (j >= NL)
                    mul_b[sl][i] = '0;                 // padding row
                else
                    mul_b[sl][i] = (i + j >= NL) ? r5_d[j] : {1'b0, r_d[j]};
            end
        end
    end
```

The accumulator rotates by `ROWS_PER_CYCLE` positions per cycle so that
products are written at fixed indices instead of dynamically computed
ones. Rotation by a constant is wiring; a dynamic write index would be a
five-way multiplexer on every 64-bit accumulator:

```systemverilog
    logic [63:0] t_next [NL];

    always_comb begin
        for (int k = 0; k < NL; k++)
            t_next[k] = t[(k + ROWS_PER_CYCLE) % NL];
        for (int sl = 0; sl < ROWS_PER_CYCLE; sl++)
            for (int i = 0; i < NL; i++)
                t_next[(i + sl) % NL] = t_next[(i + sl) % NL] + 64'(prod[sl][i]);
    end
```

After the five rotations the digits sit one position off their absolute
weight; `S_C1` compensates by reading `t[(k + 1) % NL]`, which is again
wiring.

- [ ] **Step 3: Write the FSM**

```systemverilog
    typedef enum logic [3:0] {
        S_IDLE, S_WAIT, S_MUL, S_DRAIN, S_C1, S_C2, S_FIN, S_FIN2, S_TAG
    } fsm_t;
    fsm_t state;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state     <= S_IDLE;
            busy      <= 1'b0;
            blk_ready <= 1'b0;
            done      <= 1'b0;
        end else begin
            done <= 1'b0;
            // registered products, every cycle: the DSP blocks absorb
            // these registers
            for (int sl = 0; sl < ROWS_PER_CYCLE; sl++)
                for (int i = 0; i < NL; i++)
                    prod[sl][i] <= mul_a[sl][i] * mul_b[sl][i];

            case (state)
                S_IDLE: if (start) begin
                    for (int i = 0; i < NL; i++) begin
                        automatic logic [LW-1:0] rd =
                            LW'(((key[127:0] & CLAMP) >> (i * LW)));
                        r_d[i]  <= rd;
                        r5_d[i] <= (LW+3)'(rd) + ((LW+3)'(rd) << 2);  // *5
                        a_d[i]  <= '0;
                    end
                    s         <= key[255:128];
                    busy      <= 1'b1;
                    blk_ready <= 1'b1;
                    state     <= S_WAIT;
                end
                S_WAIT: if (blk) begin
                    for (int i = 0; i < NL; i++) begin
                        sum_d[i] <= a_d[i] + n_d[i];
                        t[i]     <= '0;
                    end
                    last_r    <= last;
                    blk_ready <= 1'b0;
                    row       <= '0;
                    state     <= S_MUL;
                end
                S_MUL: begin
                    // the first cycle has no products in flight yet
                    if (row != '0)
                        for (int k = 0; k < NL; k++) t[k] <= t_next[k];
                    if (row == 3'(MCYC - 1))
                        state <= S_DRAIN;
                    row <= row + 3'd1;
                end
                S_DRAIN: begin
                    for (int k = 0; k < NL; k++) t[k] <= t_next[k];
                    state <= S_C1;
                end
```

- [ ] **Step 4: Write the two carry-normalisation cycles and the final reduction**

`S_C1` propagates carries through the low digits, `S_C2` finishes the
chain and folds the bit-130 overflow back multiplied by 5. The
one-position rotation left over from the accumulation is undone by
reading `t[(k + 1) % NL]`.

```systemverilog
                S_C1: begin
                    automatic logic [63:0] carry = '0;
                    for (int k = 0; k < 3; k++) begin
                        automatic logic [63:0] v = t[(k + 1) % NL] + carry;
                        c1[k] <= {38'd0, v[LW-1:0]};
                        carry  = v >> LW;
                    end
                    c1[3] <= t[4] + carry;
                    c1[4] <= t[0];
                    state <= S_C2;
                end
                S_C2: begin
                    automatic logic [63:0] v3 = c1[3];
                    automatic logic [63:0] v4 = c1[4] + (v3 >> LW);
                    // carry out of digit 4 is the bit-130 overflow: it
                    // re-enters digit 0 multiplied by 5
                    automatic logic [63:0] v0 =
                        c1[0] + (((v4 >> LW) << 2) + (v4 >> LW));
                    a_d[0] <= (LW+2)'(v0[LW-1:0]);
                    a_d[1] <= (LW+2)'(c1[1] + (v0 >> LW));
                    a_d[2] <= (LW+2)'(c1[2]);
                    a_d[3] <= (LW+2)'(v3[LW-1:0]);
                    a_d[4] <= (LW+2)'(v4[LW-1:0]);
                    if (last_r) begin
                        state <= S_FIN;
                    end else begin
                        blk_ready <= 1'b1;
                        state     <= S_WAIT;
                    end
                end
                S_FIN: begin
                    // no laziness left: propagate every digit
                    automatic logic [63:0] carry = '0;
                    for (int k = 0; k < NL; k++) begin
                        automatic logic [63:0] v = {36'd0, a_d[k]} + carry;
                        f[k]  <= v[LW-1:0];
                        carry  = v >> LW;
                    end
                    fold  <= (carry << 2) + carry;   // *5
                    state <= S_FIN2;
                end
                S_FIN2: begin
                    automatic logic [130:0] flat = 131'(fold);
                    for (int k = 0; k < NL; k++)
                        flat = flat + (131'(f[k]) << (k * LW));
                    // flat < 2p here, so one conditional subtract is enough
                    a_flat <= (flat >= P) ? (flat - P) : flat;
                    state  <= S_TAG;
                end
                S_TAG: begin
                    tag   <= a_flat[127:0] + s;
                    busy  <= 1'b0;
                    done  <= 1'b1;
                    state <= S_IDLE;
                end
                default: state <= S_IDLE;
            endcase
        end
    end

endmodule
```

Width budget, worth checking against the code as written — this is where
a limb datapath goes wrong silently:

| value | bound | width |
|-------|-------|-------|
| `a_d[i]` after `S_C2` | < 2^27 | 28 |
| `n_d[i]` | < 2^26 | 28 |
| `sum_d[i]` | < 2^28 | 28 |
| `r5_d[j]` | < 2^29 | 29 |
| `prod` | < 2^57 | 57 |
| `t[k]` (5 products) | < 2^60 | 64 |
| carry out of a digit | < 2^38 | 64 |
| `flat` | < 2^131 | 131 |

- [ ] **Step 5: Lint**

```sh
../tools/verilator/bin/verilator --lint-only -Wall hw/rtl/poly1305.sv --top-module poly1305
```

Expected: clean. Fix width and unused-signal warnings rather than
waiving them.

- [ ] **Step 6: Run the full testbench**

```sh
.venv/bin/python hw/sim/run_poly1305.py
```

Expected: all four tests PASS. Failures in `test_randomised` or
`test_edge_cases` with the official vectors passing point at carry
handling — the digit boundaries are where this datapath breaks.

If instead *every* vector fails while the structure looks right, suspect
the rotation offset before suspecting the arithmetic: the compensating
read in `S_C1` is `t[(k + 1) % NL]` on the reasoning that five rotations
leave the digits one position off. Try `t[k]` and `t[(k + NL - 1) % NL]`
and see which one makes RFC 8439 2.5.2 pass — the offset is a wiring
constant, and getting it wrong shifts every digit by one weight.

- [ ] **Step 7: Check the parameter actually works**

Temporarily build with `ROWS_PER_CYCLE = 2` by adding
`parameters={"ROWS_PER_CYCLE": 2}` to `runner.build()` in
`hw/sim/run_poly1305.py`, re-run the suite, expect all tests PASS, then
revert the runner change. A parameter nobody ever exercises is a
parameter that does not work.

- [ ] **Step 8: Commit**

```sh
git add oca/hw/rtl/poly1305.sv
git commit -m "rtl: rework Poly1305 into a 26-bit limb datapath"
```

---

### Task 4: Non-vacuity proof

**Files:** none changed permanently.

- [ ] **Step 1: Break the carry propagation**

In `poly1305.sv`, in the `S_C1` loop, drop the carry into digit 1 —
change `automatic logic [63:0] v = t[(k + 1) % NL] + carry;` to
`... = t[(k + 1) % NL];`.

- [ ] **Step 2: Run and confirm the right tests fail**

```sh
.venv/bin/python hw/sim/run_poly1305.py
```

Expected: `test_edge_cases` and/or `test_randomised` FAIL with a tag
mismatch. If everything still passes, the tests are decoration and must
be strengthened before going further.

- [ ] **Step 3: Restore and re-verify**

```sh
git checkout oca/hw/rtl/poly1305.sv
.venv/bin/python hw/sim/run_poly1305.py
```

Expected: all tests PASS again.

---

### Task 5: Regression, synthesis, documentation

**Files:**
- Modify: `oca/hw/syn/README.md`, `oca/README.md`, `AGENTS.md`

- [ ] **Step 1: AEAD regression**

```sh
.venv/bin/python hw/sim/run_chacha20_poly1305.py
../tools/verilator/bin/verilator --lint-only -Wall hw/rtl/*.sv --top-module chacha20_poly1305
```

Expected: 3/3 PASS, lint clean. The AEAD engine drives `data_len` at a
constant 16 and waits on `blk_ready`, so it should be unaffected — this
step proves it rather than assuming it.

- [ ] **Step 2: Synthesise the reworked core**

```sh
.venv/bin/python hw/syn/run_synth.py poly1305 --freq 100
.venv/bin/python hw/syn/run_synth.py chacha20_poly1305 --freq 100
```

The AEAD run takes ~40 minutes. Record multipliers, LUTs and Fmax.

- [ ] **Step 3: Record the before/after numbers**

Update the results table in `oca/hw/syn/README.md` with a second row set
for the reworked core, next to the baselines. If the multiplier count
did not drop into the ~20 range, or Fmax did not improve, say so in the
document — a rework that misses its target is information, not a
failure to hide.

- [ ] **Step 4: Update status**

`oca/README.md`: describe `poly1305.sv` as the limb datapath with its
cycle cost. `AGENTS.md`: update the Phase 2 status lines and the next
step (ChaCha20 rework).

- [ ] **Step 5: Commit**

```sh
git add oca/hw/syn/README.md oca/README.md AGENTS.md
git commit -m "docs: record the Poly1305 rework results"
```
