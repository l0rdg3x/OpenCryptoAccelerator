# ECP5 synthesis

Open-toolchain synthesis and place & route of the OCA cores, targeting
the Lattice ECP5 on the MVP board (Colorlight i9 v7.2, LFE5U-45F-6BG381C
— see `BOM-MVP.md`).

```sh
.venv/bin/python hw/syn/run_synth.py chacha20_poly1305
.venv/bin/python hw/syn/run_synth.py --freq 50 poly1305
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
  baseline**, on 20 multipliers instead of 65. Three of the four reworks
  bought clock (26.77 -> 52.58 MHz, +96%) while paying cycles; this one
  gives cycles back, and it is the one that puts the engine ahead. The
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
