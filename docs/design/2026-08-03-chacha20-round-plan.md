# ChaCha20 round-per-cycle rework — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development to implement this plan
> task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Halve the ChaCha20 combinational path by computing one round
per cycle instead of two, so the AEAD engine stops being clock-bound at
~26 MHz and can use the headroom the Poly1305 rework created.

**Architecture:** `double_round()` already contains a column round
followed by a diagonal round, written as two clearly separated halves.
Split it into `column_round()` and `diagonal_round()`, and alternate them
per cycle under a `ROUNDS_PER_CYCLE` parameter (1 by default, 2 restoring
today's behaviour). 20 cycles per block instead of 10.

**Tech stack:** SystemVerilog, cocotb 2.x + Verilator, synthesis via
`oca/hw/syn/run_synth.py`.

**Why one round and not less:** Poly1305 now tops out at 52.68 MHz. A
half-round-per-cycle ChaCha20 would reach far beyond that and the limit
would simply move back to Poly1305 — wasted work. One round per cycle is
the point where the two cores balance.

> **Superseded in part, 2026-08-03, by the measurement of what it
> built.** The goal held: one round per cycle took the standalone core
> 28.66 -> 53.11 MHz and the AEAD engine to 37.87 MHz, and one round per
> cycle is still the right point. The **architecture did not survive the
> area numbers.** Building `column_round()` and `diagonal_round()` as two
> datapaths and alternating them cost +799 LUTs standalone, because two
> sets of adders share nothing and need a 512-bit multiplexer to choose
> between their results — a cost this plan accepted as inherent and which
> is not. A diagonal round is a column round applied to a row-rotated
> state (the standard SIMD diagonalisation), and rotating by a constant
> is wiring rather than logic.
>
> `chacha20.sv` therefore builds **one** column-round datapath and
> alternates the state register between the plain and the diagonalised
> frame. It measures **3125 TRELLIS_COMB against this plan's 4368**, at
> the same Fmax within place & route noise, the same flip-flop count and
> the same 22 cycles per block: 16 of the 32 adders deleted, exactly
> -256 CCU2C. `diagonal_round()` no longer exists, so the steps below
> that name it describe a shape the file no longer has — they are kept as
> the record of how it got here.
>
> Measurement and method: `oca/hw/syn/README.md`, section "After the area
> pass: one round datapath, and a narrow padding mask".

## Global Constraints

- Product in English: code, identifiers, comments, commit messages.
- `// SPDX-License-Identifier: CERN-OHL-P-2.0` on RTL, MIT on simulation
  files.
- Expected values are never hand-typed: vectors are parsed from
  `oca/tests/vectors/sources/rfc8439.txt`, and the Python model is
  validated against them before being trusted as an oracle.
- The module interface must not change: `chacha20_poly1305.sv` waits on
  `done` and is not modified by this plan.
- Work on branch `feat/ecp5-synthesis`. Nothing is pushed.
- All commands run from `oca/`. Python is `.venv/bin/python`.

## File Structure

- `oca/hw/sim/chacha20_model.py` (create) — ChaCha20 block function and
  keystream XOR in plain Python, mirroring `poly1305_model.py`.
- `oca/hw/sim/test_chacha20.py` (modify) — keeps the three official
  vector tests, gains model validation and randomised tests.
- `oca/hw/rtl/chacha20.sv` (modify) — the split rounds and the parameter.
- `oca/hw/syn/README.md`, `oca/README.md`, `AGENTS.md` (modify) — results
  and status.

---

### Task 1: ChaCha20 reference model and randomised tests

**Files:**
- Create: `oca/hw/sim/chacha20_model.py`
- Modify: `oca/hw/sim/test_chacha20.py`

**Interfaces:**
- Produces: `chacha20_block(key: bytes, counter: int, nonce: bytes) -> bytes`
  (32-byte key, 12-byte nonce, returns the 64-byte keystream block) and
  `chacha20_xor(key, counter, nonce, data: bytes) -> bytes`.

These tests must pass against the **current** RTL — that is what makes
them trustworthy when the datapath changes.

- [ ] **Step 1: Write the model**

```python
# SPDX-License-Identifier: MIT
"""ChaCha20 reference model (RFC 8439 section 2.3).

Plain integer arithmetic, used as the oracle for randomised RTL tests.
It is validated against the official vectors first — see test_chacha20.py.
"""

MASK = 0xFFFFFFFF


def _rotl(v: int, n: int) -> int:
    return ((v << n) | (v >> (32 - n))) & MASK


def _qr(s: list[int], a: int, b: int, c: int, d: int) -> None:
    s[a] = (s[a] + s[b]) & MASK; s[d] = _rotl(s[d] ^ s[a], 16)
    s[c] = (s[c] + s[d]) & MASK; s[b] = _rotl(s[b] ^ s[c], 12)
    s[a] = (s[a] + s[b]) & MASK; s[d] = _rotl(s[d] ^ s[a], 8)
    s[c] = (s[c] + s[d]) & MASK; s[b] = _rotl(s[b] ^ s[c], 7)


def chacha20_block(key: bytes, counter: int, nonce: bytes) -> bytes:
    """RFC 8439 2.3.1: returns the 64-byte keystream block."""
    assert len(key) == 32 and len(nonce) == 12
    const = [0x61707865, 0x3320646E, 0x79622D32, 0x6B206574]
    st = const + [int.from_bytes(key[i:i + 4], "little") for i in range(0, 32, 4)]
    st += [counter] + [int.from_bytes(nonce[i:i + 4], "little") for i in range(0, 12, 4)]
    work = list(st)
    for _ in range(10):
        _qr(work, 0, 4, 8, 12); _qr(work, 1, 5, 9, 13)
        _qr(work, 2, 6, 10, 14); _qr(work, 3, 7, 11, 15)
        _qr(work, 0, 5, 10, 15); _qr(work, 1, 6, 11, 12)
        _qr(work, 2, 7, 8, 13); _qr(work, 3, 4, 9, 14)
    out = b""
    for i in range(16):
        out += ((work[i] + st[i]) & MASK).to_bytes(4, "little")
    return out


def chacha20_xor(key: bytes, counter: int, nonce: bytes, data: bytes) -> bytes:
    """One block of keystream XORed with data (data must be <= 64 bytes)."""
    assert len(data) <= 64
    ks = chacha20_block(key, counter, nonce)
    return bytes(a ^ b for a, b in zip(data, ks))
```

- [ ] **Step 2: Validate the model against the official vectors**

Add to `test_chacha20.py`, reusing whatever vector parsing that file
already does — do not write a second parser:

```python
from chacha20_model import chacha20_block, chacha20_xor


@cocotb.test()
async def test_model_matches_official_vectors(dut):
    """The oracle must reproduce the official vectors before it judges
    the RTL."""
    key, nonce, counter, want = VEC_232      # RFC 8439 2.3.2
    got = chacha20_block(key, counter, nonce)
    assert got == want, f"2.3.2: model got {got.hex()} want {want.hex()}"
```

Adapt the names to the parsed values already present in the file. If the
existing test module exposes the 2.3.2 keystream under a different name,
use that name rather than re-parsing.

- [ ] **Step 3: Add the randomised test**

```python
import random


@cocotb.test()
async def test_randomised_blocks(dut):
    await setup(dut)
    rng = random.Random(0x5EED)      # fixed seed: failures replay
    for i in range(100):
        key = bytes(rng.getrandbits(8) for _ in range(32))
        nonce = bytes(rng.getrandbits(8) for _ in range(12))
        counter = rng.getrandbits(32)
        data = bytes(rng.getrandbits(8) for _ in range(64))
        want = chacha20_xor(key, counter, nonce, data)
        got = await run_block(dut, key, nonce, counter, data)
        assert got == want, (
            f"random #{i}: got {got.hex()} want {want.hex()}\n"
            f"  key={key.hex()} nonce={nonce.hex()} counter={counter}")
```

`run_block` is the existing helper that drives one block through the DUT.
If the current testbench drives the DUT inline instead of through a
helper, extract that code into `run_block(dut, key, nonce, counter, data)
-> bytes` first and make the existing tests call it — a refactor with no
behaviour change, verified by the official vectors still passing.

Note the counter is randomised over the full 32 bits: the RFC vectors
only ever use 0 and 1, so counter handling is otherwise untested.

- [ ] **Step 4: Run against the current RTL**

```sh
.venv/bin/python hw/sim/run_chacha20.py
```

Expected: all tests PASS (3 existing + 2 new). A failure here means the
test is wrong — the current core has been passing the official vectors
since it was written.

- [ ] **Step 5: Commit**

```sh
git add oca/hw/sim/chacha20_model.py oca/hw/sim/test_chacha20.py
git commit -m "sim: add a ChaCha20 reference model and randomised block tests"
```

---

### Task 2: One round per cycle

**Files:**
- Modify: `oca/hw/rtl/chacha20.sv`

- [ ] **Step 1: Split double_round into its two halves**

The existing `double_round()` is already two clearly separated blocks:
the column round writing `c` from `s`, then the diagonal round writing
`o` from `c`. Turn each into its own function with the body copied
verbatim — same quarter-round calls, same word indices:

```systemverilog
    // Column round: quarter rounds on the four columns (RFC 8439 2.3.1).
    function automatic logic [511:0] column_round(input logic [511:0] s);
        logic [511:0] c;
        logic [127:0] q;
        begin
            q = qr(s[ 0*32 +: 32], s[ 4*32 +: 32], s[ 8*32 +: 32], s[12*32 +: 32]);
            c[ 0*32 +: 32] = q[127:96]; c[ 4*32 +: 32] = q[95:64];
            c[ 8*32 +: 32] = q[ 63:32]; c[12*32 +: 32] = q[31: 0];
            q = qr(s[ 1*32 +: 32], s[ 5*32 +: 32], s[ 9*32 +: 32], s[13*32 +: 32]);
            c[ 1*32 +: 32] = q[127:96]; c[ 5*32 +: 32] = q[95:64];
            c[ 9*32 +: 32] = q[ 63:32]; c[13*32 +: 32] = q[31: 0];
            q = qr(s[ 2*32 +: 32], s[ 6*32 +: 32], s[10*32 +: 32], s[14*32 +: 32]);
            c[ 2*32 +: 32] = q[127:96]; c[ 6*32 +: 32] = q[95:64];
            c[10*32 +: 32] = q[ 63:32]; c[14*32 +: 32] = q[31: 0];
            q = qr(s[ 3*32 +: 32], s[ 7*32 +: 32], s[11*32 +: 32], s[15*32 +: 32]);
            c[ 3*32 +: 32] = q[127:96]; c[ 7*32 +: 32] = q[95:64];
            c[11*32 +: 32] = q[ 63:32]; c[15*32 +: 32] = q[31: 0];
            return c;
        end
    endfunction

    // Diagonal round: quarter rounds on the four diagonals.
    function automatic logic [511:0] diagonal_round(input logic [511:0] c);
        logic [511:0] o;
        logic [127:0] q;
        begin
            q = qr(c[ 0*32 +: 32], c[ 5*32 +: 32], c[10*32 +: 32], c[15*32 +: 32]);
            o[ 0*32 +: 32] = q[127:96]; o[ 5*32 +: 32] = q[95:64];
            o[10*32 +: 32] = q[ 63:32]; o[15*32 +: 32] = q[31: 0];
            q = qr(c[ 1*32 +: 32], c[ 6*32 +: 32], c[11*32 +: 32], c[12*32 +: 32]);
            o[ 1*32 +: 32] = q[127:96]; o[ 6*32 +: 32] = q[95:64];
            o[11*32 +: 32] = q[ 63:32]; o[12*32 +: 32] = q[31: 0];
            q = qr(c[ 2*32 +: 32], c[ 7*32 +: 32], c[ 8*32 +: 32], c[13*32 +: 32]);
            o[ 2*32 +: 32] = q[127:96]; o[ 7*32 +: 32] = q[95:64];
            o[ 8*32 +: 32] = q[ 63:32]; o[13*32 +: 32] = q[31: 0];
            q = qr(c[ 3*32 +: 32], c[ 4*32 +: 32], c[ 9*32 +: 32], c[14*32 +: 32]);
            o[ 3*32 +: 32] = q[127:96]; o[ 4*32 +: 32] = q[95:64];
            o[ 9*32 +: 32] = q[ 63:32]; o[14*32 +: 32] = q[31: 0];
            return o;
        end
    endfunction
```

Delete `double_round()`: with `ROUNDS_PER_CYCLE = 2` the FSM composes
the two functions instead, so nothing is lost and there is one copy of
each round rather than two.

- [ ] **Step 2: Add the parameter and rework the FSM**

```systemverilog
module chacha20 #(
    // Rounds computed per cycle. 1 halves the combinational path (20
    // cycles per block); 2 is the original behaviour (10 cycles) and
    // suits devices that can clock it.
    parameter int ROUNDS_PER_CYCLE = 1
) (
```

```systemverilog
    localparam int NROUND = 20;
    localparam int NCYCLE = NROUND / ROUNDS_PER_CYCLE;

    logic [4:0] round_cnt;
```

```systemverilog
                S_RUN: begin
                    if (ROUNDS_PER_CYCLE == 2)
                        st <= diagonal_round(column_round(st));
                    else
                        st <= round_cnt[0] ? diagonal_round(st)
                                           : column_round(st);
                    round_cnt <= round_cnt + 5'd1;
                    if (round_cnt == 5'(NCYCLE - 1))
                        state <= S_FINISH;
                end
```

Update the header comment: latency becomes 1 (load) + `NCYCLE` +
1 (serialize), so 22 cycles at the default instead of 12. State the
parameter and its trade-off, in the style of the `poly1305.sv` header.

- [ ] **Step 3: Lint**

```sh
../tools/verilator/bin/verilator --lint-only -Wall hw/rtl/chacha20.sv --top-module chacha20
```

Expected: clean. Fix width warnings rather than waiving them.

- [ ] **Step 4: Run the tests**

```sh
.venv/bin/python hw/sim/run_chacha20.py
```

Expected: all tests PASS. The testbench polls `done`, so the extra
cycles are invisible to it — unless a timeout is too tight, in which
case raise the timeout, never the expectation.

- [ ] **Step 5: Check both parameter values**

Build with `parameters={"ROUNDS_PER_CYCLE": 2}` added to
`runner.build()` in `hw/sim/run_chacha20.py`, re-run, expect all tests
PASS, then revert the runner change. With 2 the core must behave exactly
as the original did.

- [ ] **Step 6: Commit**

```sh
git add oca/hw/rtl/chacha20.sv
git commit -m "rtl: compute one ChaCha20 round per cycle"
```

---

### Task 3: Non-vacuity proof

- [ ] **Step 1: Break the round alternation**

In `S_RUN`, change `round_cnt[0] ? diagonal_round(st) : column_round(st)`
to `column_round(st)` — every round becomes a column round.

- [ ] **Step 2: Confirm the tests catch it**

```sh
.venv/bin/python hw/sim/run_chacha20.py
```

Expected: the official-vector tests and `test_randomised_blocks` FAIL;
`test_model_matches_official_vectors` still passes, since it does not
touch the RTL. That split is the signature of a working test suite.

- [ ] **Step 3: Restore and re-verify**

```sh
git checkout oca/hw/rtl/chacha20.sv
.venv/bin/python hw/sim/run_chacha20.py
```

Expected: all PASS.

---

### Task 4: Regression, synthesis, documentation

- [ ] **Step 1: AEAD regression and lint**

```sh
.venv/bin/python hw/sim/run_chacha20_poly1305.py
.venv/bin/python hw/sim/run_poly1305.py
../tools/verilator/bin/verilator --lint-only -Wall hw/rtl/*.sv --top-module chacha20_poly1305
```

Expected: 3/3, 4/4, lint clean.

- [ ] **Step 2: Measure cycles per 64-byte block**

The AEAD engine's cost per block is measured, not derived: run a
4-block and an 8-block message through it and take the difference over
4, so start-up, key derivation, the length block and the tag cancel.
Reuse the method already recorded in `hw/syn/README.md` (the reworked
Poly1305 measured 47 cycles against the baseline's 29). Expect roughly
57 with one round per cycle — report what you actually measure.

- [ ] **Step 3: Synthesise**

```sh
.venv/bin/python hw/syn/run_synth.py chacha20 --freq 100
.venv/bin/python hw/syn/run_synth.py chacha20_poly1305 --freq 100
```

Record LUTs, FFs, multipliers, Fmax and where the critical path now
sits (the nextpnr log prints it).

- [ ] **Step 4: Write up the results**

Extend `hw/syn/README.md` with a third result block, keeping the
before/after tables side by side. Compute the throughput as
`Fmax * 64 B / cycles` and compare it with both earlier points: the
original baseline (~0.47 Gbps) and the Poly1305-only state
(~0.28 Gbps). State plainly whether the engine is now faster than where
it started, and what still limits it. If the critical path has moved to
a third place, say where.

Update `oca/README.md` and `AGENTS.md` status sections, and set the next
step: overlapping the ChaCha20 and Poly1305 phases inside the AEAD
engine, which today run strictly in sequence and is what the remaining
factor towards the MVP target depends on.

- [ ] **Step 5: Commit**

```sh
git add oca/hw/syn/README.md oca/README.md AGENTS.md
git commit -m "docs: record the ChaCha20 round-per-cycle results"
```
