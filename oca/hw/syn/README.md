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
