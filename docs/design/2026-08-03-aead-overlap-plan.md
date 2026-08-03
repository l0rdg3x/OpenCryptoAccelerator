# AEAD ChaCha20/Poly1305 overlap — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development to implement this plan
> task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the AEAD engine from running its two cores in sequence.
Today ChaCha20 encrypts a block (22 cycles) and only then does Poly1305
authenticate it (4 sub-blocks x 9 cycles), so each core idles through the
other's phase: 57 measured cycles per 64-byte block. Overlapping them
puts the cost at the slower of the two, around 38 cycles.

**Architecture:** split the single FSM into two that communicate through
a one-block buffer. The *input* FSM accepts blocks, runs ChaCha20 and
emits ciphertext; the *MAC* FSM drains the buffer into Poly1305. A depth
of one is enough: while the MAC FSM spends ~36 cycles on block N, the
input FSM encrypts block N+1 in 22.

**Tech stack:** SystemVerilog, cocotb 2.x + Verilator, synthesis via
`oca/hw/syn/run_synth.py`.

**Risk, stated up front:** this is the first change in this series that
rewrites control logic rather than a datapath, and the AEAD engine has
only three tests. Task 1 fixes that before any RTL moves.

## Global Constraints

- Product in English: code, identifiers, comments, commit messages.
- `// SPDX-License-Identifier: CERN-OHL-P-2.0` on RTL, MIT on simulation
  files.
- Expected values are never hand-typed: vectors are parsed from
  `oca/tests/vectors/sources/rfc8439.txt`, and any Python model is
  validated against them before being trusted.
- The module's external interface must not change: same ports, same
  `in_ready`/`in_valid`/`in_last` and `out_valid` handshakes, same
  `done`/`tag`. Ciphertext blocks must still come out in order.
- `chacha20.sv` and `poly1305.sv` are not modified by this plan.
- Constant-time behaviour is preserved: no stage may iterate on data.
- Work on branch `feat/ecp5-synthesis`. Nothing is pushed.
- All commands run from `oca/`. Python is `.venv/bin/python`.
- Every commit carries the two harness trailers used across this branch.

## File Structure

- `oca/hw/sim/aead_model.py` (create) — AEAD_CHACHA20_POLY1305 reference
  model, composed from the existing `chacha20_model.py` and
  `poly1305_model.py`.
- `oca/hw/sim/test_chacha20_poly1305.py` (modify) — keeps the three
  official-vector tests, gains model validation and randomised tests.
- `oca/hw/rtl/chacha20_poly1305.sv` (modify) — the two FSMs.
- `oca/hw/syn/README.md`, `oca/README.md`, `AGENTS.md` (modify).

---

### Task 1: AEAD reference model and randomised tests

**Files:**
- Create: `oca/hw/sim/aead_model.py`
- Modify: `oca/hw/sim/test_chacha20_poly1305.py`

**Interfaces:**
- Consumes: `chacha20_block`, `chacha20_xor` from `chacha20_model`;
  `poly1305_tag` from `poly1305_model`.
- Produces: `aead_encrypt(key, nonce, aad, pt) -> (ct, tag)` and
  `aead_decrypt(key, nonce, aad, ct) -> (pt, tag)`.

These tests must pass against the **current** engine. They are what
makes the FSM rewrite safe.

- [ ] **Step 1: Write the model**

```python
# SPDX-License-Identifier: MIT
"""AEAD_CHACHA20_POLY1305 reference model (RFC 8439 section 2.8).

Composed from the two core models. Validated against the official 2.8.2
and A.5 vectors before it is used as an oracle for randomised tests.
"""

from chacha20_model import chacha20_block, chacha20_xor
from poly1305_model import poly1305_tag


def _pad16(data: bytes) -> bytes:
    return b"" if len(data) % 16 == 0 else bytes(16 - (len(data) % 16))


def _mac_data(aad: bytes, ct: bytes) -> bytes:
    return (aad + _pad16(aad) + ct + _pad16(ct)
            + len(aad).to_bytes(8, "little") + len(ct).to_bytes(8, "little"))


def aead_encrypt(key: bytes, nonce: bytes, aad: bytes,
                 pt: bytes) -> tuple[bytes, bytes]:
    """RFC 8439 2.8.1. Returns (ciphertext, tag)."""
    otk = chacha20_block(key, 0, nonce)[:32]
    ct = b"".join(chacha20_xor(key, i + 1, nonce, pt[o:o + 64])
                  for i, o in enumerate(range(0, len(pt), 64)))
    return ct, poly1305_tag(otk, _mac_data(aad, ct))


def aead_decrypt(key: bytes, nonce: bytes, aad: bytes,
                 ct: bytes) -> tuple[bytes, bytes]:
    """Returns (plaintext, expected tag). The caller compares tags."""
    otk = chacha20_block(key, 0, nonce)[:32]
    pt = b"".join(chacha20_xor(key, i + 1, nonce, ct[o:o + 64])
                  for i, o in enumerate(range(0, len(ct), 64)))
    return pt, poly1305_tag(otk, _mac_data(aad, ct))
```

- [ ] **Step 2: Validate the model on the official vectors**

Add to `test_chacha20_poly1305.py`, reusing the values the file's
existing parser already produces — do not write a second parser:

```python
from aead_model import aead_decrypt, aead_encrypt


@cocotb.test()
async def test_model_matches_official_vectors(dut):
    """The oracle must reproduce both official vectors before it judges
    the RTL."""
    key, nonce, aad, pt, ct, tag = VECS["enc"]
    got_ct, got_tag = aead_encrypt(key, nonce, aad, pt)
    assert got_ct == ct, f"2.8.2 ct: {got_ct.hex()} != {ct.hex()}"
    assert got_tag == tag, f"2.8.2 tag: {got_tag.hex()} != {tag.hex()}"

    key, nonce, aad, pt, ct, tag = VECS["dec"]
    got_pt, got_tag = aead_decrypt(key, nonce, aad, ct)
    assert got_pt == pt, f"A.5 pt: {got_pt.hex()} != {pt.hex()}"
    assert got_tag == tag, f"A.5 tag: {got_tag.hex()} != {tag.hex()}"
```

Adapt `VECS` to whatever the module already calls the parsed dictionary.

- [ ] **Step 3: Randomised tests, encrypt and decrypt**

```python
import random


@cocotb.test()
async def test_randomised_encrypt(dut):
    await setup(dut)
    rng = random.Random(0xA11CE)      # fixed seed: failures replay
    for i in range(40):
        key = bytes(rng.getrandbits(8) for _ in range(32))
        nonce = bytes(rng.getrandbits(8) for _ in range(12))
        # lengths chosen around the 64-byte block and 16-byte MAC
        # boundaries, where padding and partial blocks interact
        alen = rng.choice([0, 1, 12, 15, 16, 17, 64, 65])
        plen = rng.choice([1, 15, 16, 63, 64, 65, 127, 128, 130])
        aad = bytes(rng.getrandbits(8) for _ in range(alen))
        pt = bytes(rng.getrandbits(8) for _ in range(plen))
        want_ct, want_tag = aead_encrypt(key, nonce, aad, pt)
        got_ct, got_tag = await run_aead(dut, key, nonce, aad, pt)
        assert got_ct == want_ct, (
            f"random enc #{i} (aad={alen} pt={plen}): ct mismatch\n"
            f"  got  {got_ct.hex()}\n  want {want_ct.hex()}")
        assert got_tag == want_tag, (
            f"random enc #{i} (aad={alen} pt={plen}): tag "
            f"{got_tag.hex()} != {want_tag.hex()}")


@cocotb.test()
async def test_randomised_decrypt(dut):
    await setup(dut)
    rng = random.Random(0xDEC0DE)
    for i in range(40):
        key = bytes(rng.getrandbits(8) for _ in range(32))
        nonce = bytes(rng.getrandbits(8) for _ in range(12))
        alen = rng.choice([0, 12, 16, 17, 64])
        clen = rng.choice([1, 16, 63, 64, 65, 128, 130])
        aad = bytes(rng.getrandbits(8) for _ in range(alen))
        ct = bytes(rng.getrandbits(8) for _ in range(clen))
        want_pt, want_tag = aead_decrypt(key, nonce, aad, ct)
        got_pt, got_tag = await run_aead(dut, key, nonce, aad, ct, dec=True)
        assert got_pt == want_pt, f"random dec #{i}: plaintext mismatch"
        assert got_tag == want_tag, (
            f"random dec #{i} (aad={alen} ct={clen}): tag "
            f"{got_tag.hex()} != {want_tag.hex()}")
```

`run_aead` is the existing helper; it already takes `dec`.

An AAD length of 0 is included on purpose: the engine has a dedicated
path for an empty section and nothing currently tests it.

- [ ] **Step 4: Run against the current engine**

```sh
.venv/bin/python hw/sim/run_chacha20_poly1305.py
```

Expected: 6 tests PASS. A failure here is a failure of the test or a
genuine pre-existing bug in the engine — investigate and report which,
do not adjust the expectation. If the engine turns out to mishandle a
length the official vectors never exercise, that is a real find: stop
and report it.

- [ ] **Step 5: Commit**

```sh
git add oca/hw/sim/aead_model.py oca/hw/sim/test_chacha20_poly1305.py
git commit -m "sim: add an AEAD reference model and randomised engine tests"
```

---

### Task 2: Split the FSM and overlap the phases

**Files:**
- Modify: `oca/hw/rtl/chacha20_poly1305.sv`

The current FSM is one sequence: `S_RUN` accepts a block, `S_ENC` waits
for ChaCha20, then `S_MAC_W`/`S_MAC_P` feed Poly1305 four sub-blocks
before `in_ready` rises again. The rewrite keeps every one of those
steps but puts them in two FSMs joined by a one-block buffer.

**The buffer** holds what still needs authenticating:

```systemverilog
    logic [511:0] mac_buf;      // bytes to MAC (AAD, or ciphertext)
    logic [  6:0] mac_len;      // valid bytes, 0..64
    logic         mac_last;     // final block of the whole message
    logic         mac_valid;    // buffer holds a block to authenticate
    logic         mac_take;     // MAC FSM has consumed it
```

`mac_valid` is set by the input FSM and cleared by the MAC FSM; a block
is accepted into the buffer only when it is free. That single handshake
is what makes the overlap safe — never widen it to a deeper queue
without revisiting the ordering argument below.

**Ordering argument, which must stay true:** Poly1305 must see AAD
blocks, then ciphertext blocks, in order, then the length block. The
input FSM writes the buffer in exactly the order it accepts blocks, and
the MAC FSM drains it in order, so ordering is preserved by
construction. Ciphertext still leaves through `out_valid` in the order
produced.

- [ ] **Step 1: Rewrite the input FSM**

States: `S_IDLE` (wait `start`, kick off the counter-0 ChaCha20 block
for the Poly1305 key), `S_KEY` (wait `c_done`, hand the key to
Poly1305), `S_ACCEPT` (raise `in_ready`, take a block), `S_ENC` (wait
`c_done`, emit ciphertext, fill the buffer), `S_WAITBUF` (buffer busy).

Key differences from today:
- `in_ready` rises as soon as the *ChaCha20* side is free and the buffer
  can take another block — not after the MAC finishes.
- On an AAD block, no ChaCha20 run is needed: the block goes straight
  into the buffer once it is free.
- On a plaintext block, ChaCha20 starts immediately; when `c_done`
  arrives, the ciphertext goes out on `out_valid` and into the buffer.
- On decrypt (`dec_r`), the buffer takes the *input* block (the
  ciphertext), exactly as today.

- [ ] **Step 2: Write the MAC FSM**

States: `S_M_IDLE` (wait `mac_valid`), `S_M_FEED` (wait `p_blk_ready`,
push sub-block `sub_idx`), `S_M_NEXT` (advance or release the buffer),
`S_M_LEN` (push the length block after the final message block),
`S_M_TAG` (wait `p_done`, pulse `done`).

The `sub_cnt` computation, the 16-byte slicing of `mac_buf` and the
length-block contents (`{ct_len, aad_len}`) all move over unchanged from
the current `S_MAC_W`/`S_MAC_P`/`S_LEN` states. `aad_len` and `ct_len`
are still accumulated by the input FSM as blocks are accepted.

- [ ] **Step 3: Lint**

```sh
../tools/verilator/bin/verilator --lint-only -Wall hw/rtl/*.sv --top-module chacha20_poly1305
```

Expected: clean. Watch for a latch or a multiply-driven signal — two
always_ff blocks must never drive the same register. If lint complains
about that, the split is wrong, not the linter.

- [ ] **Step 4: Test**

```sh
.venv/bin/python hw/sim/run_chacha20_poly1305.py
```

Expected: all 6 tests PASS. The randomised tests from Task 1 are the
ones that will catch an ordering or handshake mistake; the three
official vectors alone would not.

- [ ] **Step 5: Commit**

```sh
git add oca/hw/rtl/chacha20_poly1305.sv
git commit -m "rtl: overlap ChaCha20 and Poly1305 phases in the AEAD engine"
```

---

### Task 3: Non-vacuity proof

- [ ] **Step 1: Break the buffer handshake**

Make the input FSM overwrite the buffer without waiting for
`mac_valid` to clear — remove that condition from the acceptance
guard. This is the exact bug the overlap risks introducing: a block
authenticated twice, or skipped.

- [ ] **Step 2: Confirm the tests catch it**

```sh
.venv/bin/python hw/sim/run_chacha20_poly1305.py
```

Expected: the randomised tests FAIL. If only the official vectors fail,
note it; if *nothing* fails, the randomised tests are not covering
multi-block messages and must be strengthened before this rewrite can
be trusted.

- [ ] **Step 3: Restore and re-verify**

```sh
git checkout oca/hw/rtl/chacha20_poly1305.sv
.venv/bin/python hw/sim/run_chacha20_poly1305.py
```

Expected: 6/6 PASS.

---

### Task 4: Measure, synthesise, document

- [ ] **Step 1: Full regression**

```sh
.venv/bin/python hw/sim/run_chacha20.py           # 5/5
.venv/bin/python hw/sim/run_poly1305.py           # 4/4
.venv/bin/python hw/sim/run_chacha20_poly1305.py  # 6/6
../tools/verilator/bin/verilator --lint-only -Wall hw/rtl/*.sv --top-module chacha20_poly1305
```

- [ ] **Step 2: Measure cycles per 64-byte block**

Same differential method as the previous reworks: a 4-block and an
8-block message, difference over 4. Expect roughly 38 against the
current 57 — report what you measure, and if it is not close to 38,
say so and look at where the extra cycles go before writing it up.

- [ ] **Step 3: Synthesise**

```sh
.venv/bin/python hw/syn/run_synth.py chacha20_poly1305 --freq 100
```

Record LUTs, FFs, multipliers, Fmax and the critical path location.
Expect the LUT and FF count to rise — a second FSM and a 512-bit buffer
are not free — and say by how much.

- [ ] **Step 4: Write up and update status**

Extend `oca/hw/syn/README.md` with the new result block, computing
throughput as `Fmax * 64 B / cycles` and comparing against all previous
points, including the original baseline of ~0.47 Gbps. State plainly
whether the engine is now faster than where this series of reworks
started.

Update `oca/README.md` and `AGENTS.md`. The next step to record is
replicating engines: with 20 multipliers each the device holds three,
and that is what turns per-engine throughput into the aggregate the MVP
target is written against.

- [ ] **Step 5: Commit**

```sh
git add oca/hw/syn/README.md oca/README.md AGENTS.md
git commit -m "docs: record the AEAD overlap results"
```
