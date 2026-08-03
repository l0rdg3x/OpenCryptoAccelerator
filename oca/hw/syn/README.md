# ECP5 synthesis

Open-toolchain synthesis and place & route of the OCA cores, targeting
the Lattice ECP5 on the MVP board (Colorlight i9 v7.2, LFE5U-45F-6BG381C
— see `BOM-MVP.md`).

```sh
.venv/bin/python hw/syn/run_synth.py chacha20_poly1305
.venv/bin/python hw/syn/run_synth.py --freq 50 poly1305
.venv/bin/python hw/syn/run_synth.py oca_core          # ~3 min
```

Outputs land in `hw/syn/build/` (gitignored): yosys and nextpnr logs,
the post-synthesis netlist and the nextpnr JSON report.

## Flow

- **yosys** `read_slang` + `synth_ecp5`. The Verilog-2005 frontend
  (`read_verilog -sv`) rejects the SystemVerilog these cores use
  (functions with `return`, concatenation assignments); the slang
  frontend built into yosys handles it.
- **nextpnr-ecp5** with `--out-of-context`: the cores carry 512-bit data
  buses, far more signals than the package has pins, so no IO buffers
  are inserted and the design is placed as a locked macro. The numbers
  characterise the core itself — a top level with a real host interface
  will add its own IO and routing pressure.
- `--timing-allow-fail` is deliberate: this is characterisation, a
  missed target must be reported, not turned into a build failure.
- The placer seed is fixed (`--seed 1`) so runs are comparable.

## Results

Measured 2026-08-03 on LFE5U-45F, CABGA381, speed grade 6, 100 MHz
constraint, seed 1. Tools: yosys 0.67+ (41a4b5a03), nextpnr-ecp5
8945407, prjtrellis 56bb170 — all built from source into `tools/`.

### Baselines

Same device, same seed, `--out-of-context`. These are the numbers any
datapath rework has to beat.

| design | TRELLIS_COMB | TRELLIS_FF | MULT18X18D | Fmax |
|--------|--------------|------------|------------|------|
| chacha20 | 3569 | 1417 | 0 | 28.66 MHz |
| poly1305 | 5001 | 885 | 65 | 22.94 MHz |
| chacha20_poly1305 | 11144 | 4777 | 65 | 26.77 MHz |

The AEAD engine measures *faster* than either core alone: place & route
noise, not a real effect. Treat single-digit percentage differences
between runs as noise; only order-of-magnitude changes mean anything.

Both cores are slow for the same reason — too much arithmetic between
two registers. Poly1305 does a 130x130 multiply and the reduction in one
cycle; ChaCha20 does two full rounds per cycle (34.89 ns critical path,
15.07 ns logic + 19.82 ns routing).

The baseline AEAD critical path was 37.36 ns — 20.77 ns logic, 16.59 ns
routing — entirely inside `u_poly`, from the `prod` register through a
long CCU2C carry chain and five cascaded MULT18X18D into the accumulator
`a`: the 130x130-bit multiply and the mod-2^130-5 reduction in a single
clock cycle.

### After the Poly1305 limb rework

Same device, package, speed grade, seed and 100 MHz constraint.
`poly1305.sv` is now the 26-bit limb datapath (five digits, reduction
folded into the accumulation, default `ROWS_PER_CYCLE = 1`);
`chacha20.sv` and `chacha20_poly1305.sv` are untouched.

| design | TRELLIS_COMB | TRELLIS_FF | MULT18X18D | Fmax |
|--------|--------------|------------|------------|------|
| poly1305 (baseline) | 5001 | 885 | 65 | 22.94 MHz |
| poly1305 (limb) | 3044 | 1830 | **20** | **52.68 MHz** |
| chacha20_poly1305 (baseline) | 11144 | 4777 | 65 | 26.77 MHz |
| chacha20_poly1305 (limb) | 9579 | 5723 | **20** | 26.10 MHz |

What the rework achieved, and what it did not:

- **Multipliers: target met.** 65 -> 20 MULT18X18D, 90.3% -> 27.8% of
  the device. Each of the five 28x29-bit products is wider than one
  18x18 block, so yosys decomposes it into four: 5 x 4 = 20. The engine
  is no longer DSP-bound — the multiplier budget of an LFE5U-45F would
  now hold three engines instead of one, though at 9579 LUTs each that
  is 65% of the fabric and has not been placed.
- **Poly1305 Fmax: target met.** 22.94 -> 52.68 MHz, +130%: far outside
  the noise band. Its critical path is 18.98 ns (8.24 ns logic, 10.74 ns
  routing), from the `r5_d` register through the `mul_b` row selection
  into one multiplier's partial-product carry chain, ending at the
  `prod` register. The path is the multiply itself; the reduction is no
  longer on it.
- **AEAD Fmax: unchanged.** 26.77 -> 26.10 MHz is -2.5%, inside the
  place & route noise band documented above — neither an improvement nor
  a regression, the same number. The AEAD engine did not get faster
  because its critical path left `u_poly` and landed in `u_chacha`:
  38.31 ns (15.08 ns logic, 23.22 ns routing) from the ChaCha20 state
  register `u_chacha.st`, through a long CCU2C carry chain, back into
  `u_chacha.st`. ChaCha20's two-rounds-per-cycle datapath is now the
  limit, as its 28.66 MHz standalone baseline already predicted.
- **LUTs down, flip-flops up.** Poly1305 trades 1957 LUTs for 945 FFs
  (the AEAD engine, 1565 for 946): the limb datapath registers r, 5*r,
  the digit sums, the products and the five 64-bit accumulators, but
  stops building a 261-bit reduction out of combinational logic.
- **Throughput went down.** A 16-byte block now costs 9 cycles in
  `poly1305.sv` instead of 3. Measured in simulation — the difference
  between a 4-block and an 8-block message, so start-up, key derivation,
  the length block and the tag all cancel — a steady-state 64-byte block
  costs **47 cycles against the baseline's 29**. (Neither is simply
  4 x the Poly1305 block cost: the AEAD FSM releases `in_ready` at the
  last MAC sub-block, so the next block's ChaCha20 encryption overlaps
  the tail of the previous block's MAC.) At the measured Fmax that is
  26.10 MHz x 64 B / 47 = **~0.28 Gbps**, against 26.77 MHz x 64 B / 29
  = ~0.47 Gbps for the baseline: **freeing the multipliers cost about
  40% of the throughput.** By the cost formula in the module header,
  `ROWS_PER_CYCLE` buys it back — 5 rows per cycle would return the
  Poly1305 block to 5 cycles at 100 MULT18X18D, more than the 45F has.
  Only the default is characterised here.

So of the two consequences recorded against the baseline, the first is
resolved and the second is not: the engine is no longer DSP-bound, and
it is still clock-bound at ~26 MHz, now by `chacha20.sv`. Against the
MVP target (saturate the GbE host link with margin; the >= 10 Gbps
aggregate figure moved to the Artix-7 phase — see `SPEC.md`), ~0.28 Gbps
is further from the goal than the baseline was. The ChaCha20 datapath is
the next thing to rework: it now owns the critical path, and until it
moves, spending the freed multipliers on `ROWS_PER_CYCLE` cannot pay
off.

Router runtime collapsed with the multiplier count: the full AEAD build
(yosys + nextpnr) now takes **1 min 44 s** on the dev machine where the
baseline needed ~38 minutes, and standalone `poly1305` takes 15 s.

### After the ChaCha20 round-per-cycle rework

Same device, package, speed grade, seed and 100 MHz constraint.
`chacha20.sv` now computes one round per cycle (`ROUNDS_PER_CYCLE = 1`,
22 cycles per block instead of 12): `double_round()` is split into
`column_round()` and `diagonal_round()`, and the FSM alternates them.
`poly1305.sv` keeps the limb datapath; `chacha20_poly1305.sv` is
untouched.

| design | TRELLIS_COMB | TRELLIS_FF | MULT18X18D | Fmax |
|--------|--------------|------------|------------|------|
| chacha20 (baseline, 2 rounds/cycle) | 3569 | 1417 | 0 | 28.66 MHz |
| chacha20 (1 round/cycle) | 4368 | 1418 | 0 | **53.11 MHz** |
| chacha20_poly1305 (baseline) | 11144 | 4777 | 65 | 26.77 MHz |
| chacha20_poly1305 (limb Poly1305) | 9579 | 5723 | 20 | 26.10 MHz |
| chacha20_poly1305 (limb + 1 round/cycle) | 10066 | 5724 | **20** | **37.87 MHz** |

- **ChaCha20 Fmax: target met.** 28.66 -> 53.11 MHz, +85%, far outside
  the noise band — and within 1% of the reworked Poly1305's 52.68 MHz.
  The two cores are now balanced, which is the point the plan aimed at.
  Its critical path is 18.83 ns (8.26 ns logic, 10.56 ns routing), from
  the state register `st` back into `st`, through 45 CCU2C carry stages
  belonging to the four adders of one quarter round (`chacha20.sv` lines
  49, 51, 53, 55) and a final 2:1 multiplexer. One round of logic
  between two registers, which is what the rework set out to build.
- **Area: +799 LUTs standalone (+22%), +487 in the AEAD engine (+5%),
  and exactly one flip-flop.** Two rounds per cycle chained one round
  into the next, so the two round functions shared no logic and needed
  no selection; one round per cycle keeps both as separate logic and
  adds a 512-bit multiplexer in front of the state register. The extra
  flip-flop is the fifth bit of `round_cnt`, which now counts to 20
  instead of 10. Multipliers unchanged at 20.
- **AEAD Fmax: 26.10 -> 37.87 MHz, +45%**, and +41% over the 26.77 MHz
  baseline. Both are far outside the place & route noise band.
- **The critical path moved to a third place, and it is in neither
  core.** It is now 26.41 ns (18.87 ns logic, 7.54 ns routing), from the
  wrapper's `c_data_in` register to its `src` register. Nothing on it
  belongs to `u_chacha` or `u_poly`: every one of its 224 carry stages
  is attributed to `chacha20_poly1305.sv:118`, the mask expression
  `m = (512'd1 << (len * 8)) - 512'd1` in `mask_bytes()`, whose 512-bit
  subtract becomes one full-width CCU2C ripple-carry chain. That path
  was always there — it only became the longest one once both cores got
  out of the way.
- **Throughput: up on the previous state, still below the baseline.**
  Measured the same way as before (the difference between an 8-block and
  a 4-block message, over 4, so start-up, key derivation, the length
  block and the tag cancel): 276 cycles for 4 blocks, 504 for 8, so a
  steady-state 64-byte block costs **57 cycles**, against 47 after the
  Poly1305 rework and 29 at the baseline. At the measured Fmax that is
  37.87 MHz x 64 B / 57 = **~0.34 Gbps**, against ~0.28 Gbps for the
  previous state (+20%) and ~0.47 Gbps for the original baseline
  (**-28%**). **The engine is faster than it was, and still slower than
  where it started.** Fmax has gained 41% over the baseline; the cycles
  per block have grown 97% (29 -> 57) across the two reworks, and that
  is the larger number.

Where that leaves the engine: it is no longer DSP-bound (20 of 72
multipliers), it is no longer clock-bound at ~26 MHz, and neither core
owns the critical path any more. What limits it now is the schedule. The
AEAD FSM runs the two phases of a block strictly in sequence — `S_ENC`
waits for `c_done`, only then does `S_MAC_W`/`S_MAC_P` walk the four
16-byte sub-blocks — so a block costs 22 + 4 x 9 = 58 cycles, and the
57 measured is one less because `S_MAC_P` raises `in_ready` on the last
sub-block. Poly1305 is idle for the whole ChaCha20 phase and ChaCha20 is
idle for the whole MAC phase, yet the MAC of block N only needs that
block's ciphertext. Overlapping the two phases is the next step and is
where the remaining factor towards the MVP target (saturate the GbE host
link with margin) has to come from. The wrapper's `mask_bytes()` path
and `ROWS_PER_CYCLE` are cheaper follow-ups, worth spending once the
schedule is what is being paid for.

Build time for this configuration, measured on the dev machine with
nothing else running: **1 min 52 s** for the full AEAD build (yosys +
nextpnr), 13 s for standalone `chacha20`. Both builds were run twice and
returned identical area and Fmax, as the fixed seed requires.

### After the per-byte length mask

Same device, package, speed grade, seed and 100 MHz constraint. Only
`mask_bytes()` in `chacha20_poly1305.sv` changed: the 512-bit
`(512'd1 << (len * 8)) - 512'd1` is gone, the mask is built one byte at
a time from 64 independent 7-bit comparisons. `chacha20.sv` and
`poly1305.sv` are untouched, and so is the FSM — the function is
combinational, the schedule cannot move.

| design | TRELLIS_COMB | TRELLIS_FF | MULT18X18D | Fmax |
|--------|--------------|------------|------------|------|
| chacha20_poly1305 (baseline) | 11144 | 4777 | 65 | 26.77 MHz |
| chacha20_poly1305 (limb Poly1305) | 9579 | 5723 | 20 | 26.10 MHz |
| chacha20_poly1305 (limb + 1 round/cycle) | 10066 | 5724 | 20 | 37.87 MHz |
| chacha20_poly1305 (+ per-byte mask) | 10580 | 5724 | **20** | **50.08 MHz** |
| *chacha20 standalone, for reference* | 4368 | 1418 | 0 | 53.11 MHz |

- **AEAD Fmax: 37.87 -> 50.08 MHz, +32%**, and +87% over the 26.77 MHz
  baseline. Far outside the place & route noise band.
- **The wrapper is off the critical path.** It is now 19.97 ns (8.18 ns
  logic, 11.78 ns routing), from the ChaCha20 state register
  `u_chacha.st` back into `u_chacha.st`, through 44 CCU2C carry stages
  belonging to the four adders of one quarter round (`chacha20.sv` lines
  49, 51, 53, 55) and a final multiplexer. Not one entry in nextpnr's
  report cites `chacha20_poly1305.sv` any more: this is the same path
  the standalone core reports, and the engine is within 6% of that
  core's 53.11 MHz. The wrapper no longer costs anything on top of what
  the two cores cost by themselves.
- **The logic delay is what collapsed.** 18.87 -> 8.18 ns logic against
  7.54 -> 11.78 ns routing. The 512-bit ripple was almost pure logic
  delay; removing it exposes a design that is now routing-dominated.
- **Area: +514 LUTs (+5.1%), same flip-flops, same multipliers.** The
  per-byte mask is not free in cells: against the same yosys and script,
  the netlist gains 71 CCU2C and 86 L6MUX21 and loses 72 LUT4 and 16
  PFUMX. More cells, but no long chain — and only the chain was on the
  critical path.
- **Throughput: 57 cycles per 64-byte block, unchanged.** The three
  cocotb tests take the same simulated time before and after the change
  (1690 / 3110 / 1700 ns), as a combinational-only edit requires. At the
  new Fmax that is 50.08 MHz x 64 B / 57 = **~0.45 Gbps**, against
  ~0.34 Gbps before (+32%) and ~0.47 Gbps for the original baseline
  (-5%). **The engine is back to roughly its baseline throughput while
  using 20 multipliers instead of 65.**

What limits it now is unchanged and is no longer a datapath: the
schedule. `S_ENC` still waits for `c_done` before `S_MAC_W`/`S_MAC_P`
walks the four 16-byte sub-blocks, so a block costs 22 + 4 x 9 = 58
cycles less one. Overlapping the two phases is the next step, and with
the three critical paths of the previous rounds all resolved it is the
only remaining source of the factor the MVP target needs. Of the two
follow-ups listed above, `mask_bytes()` is now done; `ROWS_PER_CYCLE`
remains, and is still worth spending only after the schedule is.

Build time: **1 min 18 s** for the full AEAD build (yosys 4.9 s +
nextpnr), on the same machine and with the same fixed seed.

### After overlapping the ChaCha20 and Poly1305 phases

Same device, package, speed grade, seed and 100 MHz constraint. Only
`chacha20_poly1305.sv` changed: the single FSM became two — an input FSM
that accepts blocks, runs ChaCha20 and emits ciphertext, and a MAC FSM
that drains a one-block buffer into Poly1305 — so block N is
authenticated while block N+1 is encrypted. `chacha20.sv` and
`poly1305.sv` are untouched.

| design | TRELLIS_COMB | TRELLIS_FF | MULT18X18D | Fmax |
|--------|--------------|------------|------------|------|
| chacha20_poly1305 (baseline) | 11144 | 4777 | 65 | 26.77 MHz |
| chacha20_poly1305 (limb Poly1305) | 9579 | 5723 | 20 | 26.10 MHz |
| chacha20_poly1305 (limb + 1 round/cycle) | 10066 | 5724 | 20 | 37.87 MHz |
| chacha20_poly1305 (+ per-byte mask) | 10580 | 5724 | 20 | 50.08 MHz |
| chacha20_poly1305 (+ overlapped phases) | **10040** | 5737 | **20** | **52.58 MHz** |
| *chacha20 standalone, for reference* | 4368 | 1418 | 0 | 53.11 MHz |

- **Throughput: 57 -> 40 cycles per 64-byte block, -30%.** Measured the
  same differential way as every earlier point (227 cycles for a 4-block
  message, 387 for 8, difference over 4, so start-up, key derivation,
  the length block and the tag all cancel). The three official-vector
  tests corroborate it in simulated time: 1530 / 2440 / 1540 ns against
  1690 / 3110 / 1700 ns before.
- **The engine is faster than where this series started.** At the
  measured Fmax, 52.58 MHz x 64 B / 40 = **~0.67 Gbps**:

  | state | Fmax | cycles / 64 B | throughput |
  |-------|------|---------------|------------|
  | baseline | 26.77 MHz | 29 | ~0.47 Gbps |
  | limb Poly1305 | 26.10 MHz | 47 | ~0.28 Gbps |
  | + 1 round/cycle | 37.87 MHz | 57 | ~0.34 Gbps |
  | + per-byte mask | 50.08 MHz | 57 | ~0.45 Gbps |
  | + overlapped phases | 52.58 MHz | 40 | **~0.67 Gbps** |

  That is **+50% on the previous state and +42% on the original
  baseline**, on 20 multipliers instead of 65. The three reworks before
  this one took the clock from 26.77 to 50.08 MHz (+87%) while paying
  cycles — two of them moved it, the limb datapath spent its gain on
  multipliers instead; this one gives cycles back, and it is the one
  that puts the engine ahead. The last 50.08 -> 52.58 MHz is noise, as
  the Fmax bullet below says, and is not part of that +87%. The
  cycle count is still above the baseline's 29 — the reworked Poly1305
  costs 9 cycles per 16-byte block instead of 3, and no amount of
  overlapping hides that — but the clock now more than covers it.
- **40 cycles, not the 38 the plan estimated, and the two extra are the
  handshake.** The block cost is set entirely by the MAC FSM: four
  16-byte sub-blocks at 9 Poly1305 cycles each, plus one cycle per
  sub-block because `p_blk` is registered — Poly1305 raises `blk_ready`,
  the MAC FSM samples it and asserts `p_blk` on the next edge, so the
  core spends two cycles in its `S_WAIT` instead of one. 4 x (9 + 1) =
  40. ChaCha20's 22 cycles are now entirely hidden underneath, which is
  what the overlap set out to do: 40 - 22 - the accept and emit cycles
  leaves the input FSM idling in `S_WAITBUF` for roughly 15 cycles of
  every block, waiting on the MAC side.
- **Confirmed by a falsifiable measurement, not by reading the FSM.** An
  AAD block never runs ChaCha20 at all, so if the schedule were still
  paying for both phases an AAD block would be cheaper. Measured the same
  differential way — 4 and 8 AAD blocks followed by a one-byte plaintext
  block, 214 and 374 cycles — an AAD block costs **exactly the same 40
  cycles**. The pace is the MAC FSM's alone.
- **Area went down, not up: -540 LUTs (-5.1%), +13 flip-flops.** The
  opposite of what the plan expected. The buffer is not new silicon: it
  replaces the old `src` register one for one, 512 bits for 512 bits, so
  the flip-flop cost is only the bookkeeping — `mac_len` (7), `cur_aad`,
  `mac_last`, `mac_valid` and the second FSM's state register, a dozen
  bits, which is the order of the measured delta. The LUTs come off the
  multiplexer fabric: against the same yosys and script the netlist loses
  540 LUT4 and gains 20 PFUMX, with **CCU2C (2088) and L6MUX21 (172)
  identical**
  — not one carry chain moved. The old `src` register was fed by three
  512-bit sources (masked `in_data`, `c_data_in`, masked `c_data_out`);
  `mac_buf` is fed by two, because AAD and decrypt now take the same
  path.
- **Fmax: 50.08 -> 52.58 MHz, +5%, which is noise.** Inside the place &
  route band documented above, and it has to be: the critical path is
  unchanged and the netlist's carry chains are bit-for-bit the same. It
  is 19.02 ns (8.12 ns logic, 10.90 ns routing), from the ChaCha20 state
  register `u_chacha.st` back into `u_chacha.st` through the CCU2C carry
  chains of the four adders of one quarter round (`chacha20.sv` lines
  49, 51, 53, 55) and a pair of L6MUX21/PFUMX stages. Not one entry in
  nextpnr's report cites `chacha20_poly1305.sv` or `poly1305.sv`. The
  engine is now within 1% of the 53.11 MHz standalone `chacha20` core,
  which is the ceiling until that core is reworked again.

What limits it now, in order: the clock is capped by `chacha20.sv` at
~53 MHz, and the cycles are capped by `poly1305.sv` at 9 per 16 bytes
plus the handshake. Both are datapath questions again — the schedule is
no longer the answer, since the slower core is now busy essentially all
the time. The cheap follow-ups are `ROWS_PER_CYCLE` in `poly1305.sv`
(5 rows per cycle would take a 16-byte block from 9 cycles to 5, at 100
MULT18X18D — more than the 45F's 72, so only 2 or 3 rows per cycle are
affordable) and removing the one-cycle `p_blk` bubble.

Only the second of those is free. `ROWS_PER_CYCLE` and the replication
below spend the same 72 multipliers: raising one engine to 2 rows per
cycle costs 40 of them, which leaves room for one engine, not three.
Per-engine speed and engine count come out of the same budget, and it is
the aggregate the MVP target is written against.

The next step, then, is replication rather than another datapath round:
at 20 multipliers and 10040 LUTs an LFE5U-45F holds **three engines** —
60 of 72 multipliers (83%) and 30120 of 43848 LUTs (69%) — for
**~2.0 Gbps aggregate**. A fourth is out of reach on multipliers alone
(80 > 72). But watch which of the two binds first in practice. The
multiplier figure is exact and final: 83% of a fixed budget, and nothing
else on the die wants a MULT18X18D. The 69% LUT figure is neither. It is
out-of-context — no IO buffers, no host interface — and the remaining
31% has to hold the GbE MAC, the packet buffering and whatever glue the
top level needs, at a clock the engines have to share. **LUTs, not
multipliers, are the likelier constraint on a real top level**, which
makes the LUT cost per engine the number worth watching from here.

Build time: **51 s** for the full AEAD build (yosys 4.5 s + nextpnr), on
the same machine and with the same fixed seed. The build was run twice
and returned identical area and Fmax.

### After the in_len guard (state of the branch as committed)

The review that closed this branch added an `err` output rejecting an
out-of-range `in_len` instead of wedging the MAC FSM. It is logic on the
input path, so it was characterised rather than assumed:

| design | TRELLIS_COMB | TRELLIS_FF | MULT18X18D | Fmax |
|--------|--------------|------------|------------|------|
| overlapped phases | 10040 | 5737 | 20 | 52.58 MHz |
| **+ in_len guard (committed)** | **10041** | **5738** | **20** | **50.17 MHz** |

The guard costs **one LUT and one flip-flop**. The Fmax difference is
not a cost: a three-seed sweep of the unguarded design alone returned
49.33 / 50.81 / 52.58 MHz, so 50.17 sits inside the spread of the thing
it is being compared with. Cycles per block are unchanged at 40 — the
guard is on a path that only fires on an illegal length.

What this does to the MVP arithmetic, stated at the margin rather than
rounded in our favour: at 50.17 MHz a 64-byte block costs
50.17e6 x 64 / 40 = 80.3 MB/s = **~0.64 Gbps per engine**, so three
engines give **~1.93 Gbps** — just *under* the >= 2 Gbps MVP target of
`SPEC.md`, where the 52.58 MHz figure put it just over. Both readings
are inside place & route noise of each other, which is the honest
summary: **this design lands on the target, not above it.** Any real top
level adds IO and glue at a shared clock, so the margin is more likely
to shrink than to grow. Replication is still the next step, but it will
need its own measurement rather than a multiplication of this one.

### After the area pass: one round datapath, and a narrow padding mask

Same device, package, speed grade, seed and 100 MHz constraint. Two
changes, in two files, both area-only by construction: `chacha20.sv`
carries **one** round datapath instead of two, and
`chacha20_poly1305.sv` masks the padding on the 16-byte sub-block
Poly1305 actually reads instead of on the 512-bit buses feeding it. No
FSM state and no transition moves in either; `poly1305.sv` is untouched.

This is the first entry in this series that buys area rather than speed.
Every earlier point that spent LUTs spent them to move the clock or to
close a hole — +487 for one round per cycle, +514 for the per-byte mask,
+1 for the `in_len` guard, 1002 in total. This one gives back 2683, and
changes nothing else.

**Why a round datapath could be deleted rather than optimised.** The
core alternates a column round and a diagonal round, and the
round-per-cycle rework built both: 32 32-bit adders, plus a 512-bit
multiplexer choosing which result went back into the state register. But
a diagonal round *is* a column round — applied to a state whose rows
have been rotated. This is the standard SIMD diagonalisation, and it is
an identity rather than an approximation: rotating row b left by one
column, row c by two and row d by three lands RFC 8439 2.3.1's four
diagonals (0,5,10,15) (1,6,11,12) (2,7,8,13) (3,4,9,14) on the four
columns, so `column_round(D(s)) == D(diagonal_round(s))`. Rotating by a
constant is wiring, not logic. The state register therefore alternates
between the plain frame and the diagonalised one, a single column-round
datapath serves both round types, and **16 of the 32 adders and the
multiplexer that chose between them are gone.** The price is a parity
obligation, not silicon: the register holds the plain frame only after
an *even* number of rounds, and the final addition against `st_init` is
defined only in that frame — which is why `NROUND` must be even for a
reason beyond the RFC counting rounds in pairs. The module header states
it, because nothing fails to elaborate over an odd value.

**Why the wide masks could be deleted.** The engine zeroed the bytes
past `len` of a partial block twice, both times over 512 bits: once on
the accepted input block, once on the ChaCha20 output on its way into
the MAC buffer. Every path those masks protected ends in the same place,
`mac_buf`, whose only consumer is the 16-byte slice handed to Poly1305 —
so **one mask on the 128-bit sub-block feed covers both**, at a quarter
of the width and once instead of twice. Only the last sub-block of a
block whose length is not a multiple of 16 is ever partial. Nothing else
read the padding: the length accounting adds `in_len`, never data, and
`out_data` past `out_len` was already unspecified.

That change was made verifiable before it was made. The project
testbench zero-pads, so it cannot see whether the engine masks its input
*at all* — with the mask removed entirely, its decrypt tests still pass.
`hw/sim/test_dirty_pad.py` drives random garbage into the bytes past
`in_len` and asserts that neither the ciphertext nor the tag moves; it is
the only test in the repository that can fail on this.

| design | TRELLIS_COMB | TRELLIS_FF | MULT18X18D | Fmax |
|--------|--------------|------------|------------|------|
| chacha20 (baseline, 2 rounds/cycle) | 3569 | 1417 | 0 | 28.66 MHz |
| chacha20 (1 round/cycle, two datapaths) | 4368 | 1418 | 0 | 53.11 MHz |
| chacha20 (1 round/cycle, **one** datapath) | **3125** | 1418 | 0 | 52.09 MHz |

| design | TRELLIS_COMB | TRELLIS_FF | MULT18X18D | Fmax |
|--------|--------------|------------|------------|------|
| chacha20_poly1305 (baseline) | 11144 | 4777 | 65 | 26.77 MHz |
| chacha20_poly1305 (limb Poly1305) | 9579 | 5723 | 20 | 26.10 MHz |
| chacha20_poly1305 (+ 1 round/cycle) | 10066 | 5724 | 20 | 37.87 MHz |
| chacha20_poly1305 (+ per-byte mask) | 10580 | 5724 | 20 | 50.08 MHz |
| chacha20_poly1305 (+ overlapped phases) | 10040 | 5737 | 20 | 52.58 MHz |
| chacha20_poly1305 (+ in_len guard) | 10041 | 5738 | 20 | 50.17 MHz |
| chacha20_poly1305 (+ one round datapath) | 8812 | 5738 | 20 | 54.21 MHz |
| **chacha20_poly1305 (+ sub-block mask, committed)** | **7358** | **5738** | **20** | 53.55 MHz |
| *chacha20 standalone, for reference* | 3125 | 1418 | 0 | 52.09 MHz |

- **Area: 10041 -> 7358 TRELLIS_COMB, -2683, -26.7%.** The two changes
  are separable and were measured separately: **-1229** for the round
  datapath (10041 -> 8812) and **-1454** for the mask (8812 -> 7358).
  Standalone, the ChaCha20 core goes 4368 -> 3125, **-1243, -28.5%**.
- **The deleted adders are visible cell for cell.** From yosys `stat` on
  the top level:

  | netlist | CCU2C | L6MUX21 | LUT4 | PFUMX |
  |---------|-------|---------|------|-------|
  | chacha20, two datapaths | 770 | 172 | 2630 | 517 |
  | chacha20, one datapath | **514** | 168 | 1963 | 373 |
  | engine, previous committed state | 2085 | 172 | 5037 | 553 |
  | engine, + one round datapath | 1832 | 168 | 4374 | 417 |
  | engine, + sub-block mask | **1526** | 168 | 3952 | 549 |

  The standalone core loses **exactly 256 CCU2C**, which is exactly the
  arithmetic that was deleted: 16 adders x 32 bits, two bits to a CCU2C.
  Inside the engine the same edit takes 253; the three-cell difference
  was not chased, and is mapping either side of the module boundary
  rather than a different amount of arithmetic. The mask edit then takes
  a further 306 CCU2C and 422 LUT4 — which is worth noting because the
  earlier rework had already stopped the *mask value* from being one long
  carry chain: on the reading these cells suggest, the per-byte
  comparators kept costing carry cells of their own. Not investigated
  further; the cell counts are the measurement, that sentence is a
  reading of them.
- **Flip-flops, multipliers and cycles are all unchanged.** 5738 FF and
  20 MULT18X18D, bit for bit. Cycles per 64-byte block are **40.0**,
  measured the same differential way as every earlier point in this
  series: 227 cycles for a 4-block message, 387 for 8, difference over 4.
  An AAD block, which never runs ChaCha20, costs the same 40.0 (214 and
  374 cycles) — the falsifiable check that the MAC FSM still sets the
  pace. Both figures are identical to the state before this pass, as an
  edit that moves no FSM state requires.
- **Fmax: measured over four seeds, because one seed could not settle
  it.** At seed 1 the engine reads 50.17 -> 53.55 MHz and the standalone
  core 53.11 -> 52.09 — a gain and a loss, from the same pair of edits,
  which is the signature of a number that is mostly placement. Seeds 1-4,
  same device and constraint, area identical on every seed as it must be:

  | design | seed 1 | seed 2 | seed 3 | seed 4 | mean | spread |
  |--------|--------|--------|--------|--------|------|--------|
  | chacha20, two datapaths (4368) | 53.11 | 51.89 | 51.32 | 49.69 | 51.50 | 6.9% |
  | chacha20, one datapath (3125) | 52.09 | 52.93 | 52.88 | 53.12 | **52.76** | 2.0% |
  | engine, previous state (10041) | 50.17 | 50.81 | 51.26 | 50.64 | 50.72 | 2.2% |
  | engine, committed (7358) | 53.55 | 52.46 | 53.97 | 51.32 | **52.83** | 5.2% |

  Standalone, the two distributions overlap heavily and the seed-1
  reading (-1.9%) is the *worst* of four: there is no effect to claim
  either way. In the engine there is a separation — mean +4.2%, and the
  four post-pass seeds happen to sit above all four pre-pass ones, though
  by 0.06 MHz at the closest point. **It is still not a datapath
  speedup**: the critical path is the same one it was, from `u_chacha.st`
  back into `u_chacha.st` through the CCU2C carry chains of the four
  adders of one quarter round (`chacha20.sv` lines 58, 60, 62, 64), and
  not one entry in nextpnr's report cites `chacha20_poly1305.sv` or
  `poly1305.sv`. At seed 1 it measures 18.67 ns (7.77 ns logic, 10.90 ns
  routing) against the previous state's 19.93 ns (8.08 ns logic, 11.85 ns
  routing) — three quarters of the difference is routing, which is what a
  design a quarter smaller would be expected to gain. **Recorded as a
  plausible congestion effect, not as a speed result, and deliberately
  not built into any throughput claim below**: four seeds and a 2 MHz
  separation are not enough to promote it, and the honest summary of this
  pass is still that it bought area.

**What the LUTs buy — and what they do not.** Three engines now take
3 x 7358 = **22074 of 43848 LUTs, 50.3%**, where the same three took
30123, **68.7%**, before this pass. That is 8049 LUTs and 18.4 points of
the device given back.

- **It does not buy a fourth engine.** Multipliers are unchanged at 20
  per engine, so three engines are 60 of 72 (83%) and a fourth needs
  80 — more than the device has. That was true before this pass and is
  true after it. What changed is which budget binds: on LUTs alone four
  engines would now fit (4 x 7358 = 29432, 67.1%), so for the first time
  in this series **the multiplier count, not the LUT count, is what caps
  engine replication**. It caps it at three.
- **It does not buy throughput.** Cycles per block and multipliers per
  engine are unchanged, so per-engine throughput moves only with a clock
  this pass is not claiming. 40 cycles per 64 bytes over the four seeds
  above is 0.66-0.69 Gbps per engine and **1.97-2.07 Gbps for three** —
  the same straddle of the >= 2 Gbps MVP target of `SPEC.md` the previous
  section reported from one seed, now with four behind it. The reading is
  unchanged: **this design lands on the target, not above it**, and it
  does so on 50.3% of the LUTs instead of 68.7%.
- **What it buys is headroom for the parts that do not exist yet.** The
  GbE MAC, packet buffering, the host interface and the glue of a real
  top level — none of which is in these numbers, all of which have to
  share the fabric and the clock. These builds are `--out-of-context`:
  no IO buffers, no top level, no pin constraints. **50.3% is therefore a
  floor for three engines, not a budget for the board.** Three engines
  plus a real top level went from tight to comfortable on paper; it did
  not become certain, and only a pinned-out top-level build will say.

**How this pass was found, and the bug that nearly shipped with it.**
Both changes came out of a workflow that put one analyst on each of the
three files independently. That is what found them — nobody reading the
wrapper alone would have proposed deleting a ChaCha20 adder. It is also
how the pass nearly shipped a corrupted tag. Two *other* proposals from
the same workflow, each correct against the file its author had read,
broke authentication when applied together: raising `blk_ready` a cycle
early in `poly1305.sv`, and making `p_blk` combinational in the wrapper.
Both target the same one-cycle handshake bubble, and each silently
assumed the *other* side of the handshake kept its signal registered.
Per-file review cannot see that class of defect by construction, and the
official-vector suite is not a safety net for it either — a broken
handshake is a data-dependent failure, not a lint error. The rule it
leaves behind, for whoever runs the next optimisation pass:
**proposals from separate analysts are not additive.** A change to one
side of a handshake invalidates every other proposal touching that
handshake, and a combination has to be measured as a combination, not
inferred from its parts.

Build time: **51 s** for the full AEAD build (yosys 3.8 s + nextpnr) and
**10 s** for standalone `chacha20`, on the same machine at seed 1. Every
figure in this section was measured while writing it rather than carried
over. The seed-1 engine build reproduced the committed report exactly;
the standalone `chacha20` report on disk had to be rebuilt, because it
predated the RTL change and still showed 4368; and the two intermediate
points (8812 and 10041 TRELLIS_COMB) were rebuilt from their own commits
in a throwaway worktree. The seed sweep above is 16 builds — four seeds x
two designs x two states — and cost under ten minutes in total, which is
cheap enough that a single-seed Fmax comparison is not worth publishing
again.

### The host protocol layer: oca_core

Same device, package, speed grade and 100 MHz constraint. This is the
first entry in this series that is not the engine: `oca_core` wraps
`chacha20_poly1305` in the host protocol of
`docs/design/2026-08-03-host-protocol.md` — two 2048-byte packet
buffers (`oca_pktbuf`), eight key slots (`oca_keystore`) and the
protocol FSM (`oca_proto`) — and exposes a pair of 8-bit AXI-Stream
ports. It is the module the Ethernet integration will instantiate. The
engine's own numbers are repeated alongside so the protocol layer's
cost is visible separately.

| design | TRELLIS_COMB | TRELLIS_FF | MULT18X18D | DP16KD | Fmax (seed 1) |
|--------|--------------|------------|------------|--------|------|
| chacha20_poly1305 (engine alone) | 7358 | 5738 | 20 | 0 | 53.55 MHz |
| **oca_core (engine + protocol)** | **11149** | **10842** | **20** | **2** | **50.95 MHz** |
| *protocol layer, by difference* | *+3791 (+51.5%)* | *+5104 (+89.0%)* | *0* | *+2* | *—* |

Device occupancy: 25.4% of the LUTs, 24.7% of the flip-flops, 27.8% of
the multipliers and **1.9% of the block RAM** (2 of 108 DP16KD).

- **The packet buffers infer block RAM. Both of them.** This was the
  open question the previous task could argue but not measure, and it is
  worth stating plainly because the alternative was expensive: 4096
  bytes of LUT RAM would have been a serious area regression. yosys maps
  them explicitly — `mapping memory oca_core.u_txbuf.mem via $__DP16KD_`
  and the same line for `u_rxbuf.mem` — and the final netlist contains
  **2 DP16KD and zero LUT RAM cells**: no `TRELLIS_DPR16X4`, no
  `DPR16X4C`, no `TRELLIS_RAM16X2`. One 16 Kbit block per 2048-byte
  buffer, which is the exact fit. What earns this is the coding style in
  `oca_pktbuf.sv`: the read port is registered, and the range check sits
  on the *address* rather than on the read data, so no multiplexer comes
  between the memory and its output register. Nothing is instantiated —
  the memory is inferred, as `SPEC.md`'s portability rule requires.
- **The multiplier count did not move.** 20 MULT18X18D, exactly the
  engine's. The protocol layer is buffers, comparators and a state
  machine; it wants no arithmetic. Since multipliers are what caps
  engine replication at three (60 of 72), **the protocol layer does not
  cost an engine.**
- **Flip-flops nearly doubled, and that is where the protocol lives.**
  +5104 FF against +3791 LUTs — the opposite ratio to every datapath
  entry above. Most of it is storage rather than logic: the key store is
  8 slots x 256 bits = 2048 FF plus a registered 256-bit read port, and
  `oca_proto` carries a 512-bit block being assembled, a 512-bit block
  draining back, 256 bits of parsed arguments, the header, the received
  tag and four 32-bit counters. None of it is on a critical path.
- **Fmax: 50.95 MHz at seed 1, and the protocol layer is not on the
  critical path.** The path is 19.63 ns (7.91 ns logic, 11.72 ns
  routing) from `u_aead.u_chacha.st` back into `u_chacha.st` through the
  CCU2C carry chains of the four adders of one quarter round — and every
  single entry in nextpnr's report cites `chacha20.sv` lines 58, 60, 62
  and 64. **Not one cites `oca_proto.sv`, `oca_pktbuf.sv`,
  `oca_keystore.sv`, `oca_core.sv`, `chacha20_poly1305.sv` or
  `poly1305.sv`.** It is structurally the same path the engine reports
  alone, at the same source lines.
- **Against the engine's Fmax the difference is inside the noise, and
  is routing rather than logic.** Four seeds, same netlist, area
  identical on every one as it must be:

  | design | seed 1 | seed 2 | seed 3 | seed 4 | mean | spread |
  |--------|--------|--------|--------|--------|------|--------|
  | chacha20_poly1305 (engine alone) | 53.55 | 52.46 | 53.97 | 51.32 | 52.83 | 5.0% |
  | oca_core (engine + protocol) | 50.95 | 50.02 | 49.70 | 51.67 | **50.59** | 3.9% |

  Mean -4.2%, and the distributions overlap: `oca_core`'s best seed
  (51.67) beats the engine's worst (51.32). The logic delay is what
  settles it — 7.77 ns in the engine against 7.91 ns here, +0.14 ns, on
  a path made of the same cells at the same source lines. The routing
  delay carries the rest (10.90 -> 11.72 ns), which is what a design
  half again as large would be expected to pay in congestion.
  **Recorded as congestion, not as a datapath cost of the protocol
  layer.**

**Throughput: measured, and 2.5x worse than the plan estimated.** The
implementation plan predicted 64 cycles to read a block out of the
buffer, 40 to process it and 64 to write it back — 168 cycles, about
0.16 Gbps. Measured the same differential way as every earlier point in
this series (a seal command at 4, 8, 12 and 16 blocks; the difference
over the block delta, so the header, the key schedule, the tag and every
fixed cost cancel), a 64-byte block costs **415 cycles end to end**. The
figure is exactly linear: the three consecutive pairs (4->8, 8->12,
12->16) and the 4->16 span all return 415.0.

| phase | cycles per 64 B | rate | share |
|-------|-----------------|------|-------|
| request in (`s_axis`) | 64 | 1.00 cyc/byte | 15% |
| buffer -> engine -> buffer | 159 | — | 38% |
| response out (`m_axis`) | **192** | **3.00 cyc/byte** | **46%** |
| **total** | **415** | | |

The plan's 168 was a model of the middle row alone — and that row is the
one it got roughly right, measuring 159. What it omitted is that this is
store and forward on an 8-bit port: the request must be received whole
before processing starts and the response transmitted whole afterwards,
and neither overlaps anything.

**The largest single term is the response path, and it is a handshake,
not a bandwidth limit.** `oca_proto`'s output loop spends three cycles
per byte: one in `S_RESP_FETCH` issuing the buffer address, one
asserting `m_tvalid`, and one completing the handshake and dropping it
again. The receive side does not pay this — `s_tready` is held for the
whole of `S_RX`, so the request streams at a byte per cycle, and the
internal reader that feeds the engine runs a two-deep valid pipeline
for the same reason. `oca_proto.sv` names the cost exactly, in the
comment on that reader: one byte per cycle "instead of the three a
per-byte handshake would cost". The response path is the one place the
same file did not take its own advice.

At the four-seed mean of 50.59 MHz, 64 B x 50.59e6 / 415 =
7.80 MB/s = **~62 Mbps (~0.062 Gbps)**, and 61-64 Mbps across the seed
spread. Set against the two figures that matter:

| | throughput | |
|---|---|---|
| the AEAD engine alone, 40 cyc / 64 B at 52.83 MHz | ~0.68 Gbps | |
| **oca_core end to end, 415 cyc / 64 B at 50.59 MHz** | **~0.062 Gbps** | **9% of the engine** |
| the GbE link the MVP board has to fill | 1 Gbps | **6% of it** |

**The protocol layer costs 91% of the engine's throughput** — the
engine is idle for about nine cycles in ten. That is the price of the
deliberate simplifications recorded at the top of the implementation
plan (a one-byte-wide buffer, nothing overlapping), and it is enough to
demonstrate the path end to end, which is what the MVP is for.

Two follow-ups, in cost order. **Fixing the response handshake is the
cheap one and needs no design change**: holding `m_tvalid` up and
pipelining the buffer read, exactly as the receive feed already does,
takes the response from 192 cycles to 64 and the block from 415 to 287
— **~90 Mbps, +45%** — for a rewrite of one state. Widening the buffers
to 32 bits and overlapping the phases is the structural fix and the
larger one; it needs a board to measure honestly.

**None of this has run on silicon.** Every cycle count here comes from
Verilator, every area and Fmax figure from yosys and nextpnr on an
`--out-of-context` build with no IO buffers, no pin constraints and no
Ethernet MAC — and the MAC, the RGMII wrapper and the PLL are not in
these numbers and have to share the fabric and the clock with them. This
is a simulation-derived estimate of a design that has never been
programmed into a device.

**One stale figure is knowingly left in the RTL.** The header comment of
`oca_pktbuf.sv` still carries the plan's pre-measurement estimate — "the
whole path runs at roughly 0.16 Gbps ... about 16% of the GbE link" —
which the 415-cycle measurement above supersedes. It was left because
this pass changed no RTL; it should be corrected to ~0.062 Gbps by
whoever next edits that file. Recorded here rather than fixed silently,
so the comment is not mistaken for a second, independent source.

Build time: **3 min 11 s** for the full `oca_core` build (yosys 8.1 s +
nextpnr), on the same machine at seed 1. The three extra seeds were run
against the same netlist, since yosys output does not depend on the
placer seed.
