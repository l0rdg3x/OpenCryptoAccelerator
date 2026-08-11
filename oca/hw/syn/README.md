# ECP5 synthesis

Open-toolchain synthesis and place & route of the OCA cores, targeting
the Lattice ECP5 on the MVP board (Colorlight i9 v7.2, LFE5U-45F-6BG381C
— see `BOM-MVP.md`).

```sh
.venv/bin/python hw/syn/run_synth.py chacha20_poly1305
.venv/bin/python hw/syn/run_synth.py --freq 50 poly1305
.venv/bin/python hw/syn/run_synth.py oca_core          # 4-10 min
```

The `oca_core` figure is routing and it varies with the netlist and the
machine: 4 min 14 s for the run behind the current numbers, 519 s of
`Router1 time` alone for the one before it. This line said "~3 min"
until 2026-08-04; that was the 8-bit core's time (3 min 11 s, measured
below) and it was never updated as the design grew by 3000 LUTs.

Outputs land in `hw/syn/build/` (gitignored): yosys and nextpnr logs,
the post-synthesis netlist and the nextpnr JSON report.

## Flow

- **yosys** `read_slang` + `synth_ecp5`. The Verilog-2005 frontend
  (`read_verilog -sv`) rejects the SystemVerilog these cores use
  (functions with `return`, concatenation assignments); the slang
  frontend built into yosys handles it.
- **nextpnr-ecp5**, and there are two kinds of build. A design with no
  `.lpf` gets `--out-of-context`: the cores carry 512-bit data buses,
  far more signals than the package has pins, so no IO buffers are
  inserted and the design is placed as a locked macro. Those numbers
  characterise the core itself. A design that carries an `.lpf`
  (`oca_top_stub`, `oca_top_mac`, `oca_top`) is built against the real
  pin map instead, with IO, the PLL and its own clock constraints.
- `--timing-allow-fail` stops nextpnr itself from failing, so that a
  missed target is reported rather than swallowed. It is not a licence
  to miss one: `check_timing()` re-reads the report afterwards and
  returns non-zero for any missed constraint nextpnr says it really
  applied, and `main()` then skips packing. That set is wider than the
  `.lpf` — `colorlight_i9.lpf` carries two `FREQUENCY` lines, `clk25`
  and `rgmii_rx_clk`, and the other two constraints are the ones nextpnr
  derives from the PLL, so all four are enforced. What is deliberately
  *not* enforced is `--freq`, which is nextpnr's fallback for an
  unconstrained net rather than a target this design set for itself.
- The placer seed comes from the design (`Design.seed`), and `--seed`
  overrides it. `oca_top` records 10, the best of the 32 in the sweep at
  the end of this file and not one that closes; everything else falls
  back to 1, so runs stay comparable.
- Two checks bracket synthesis, because nothing downstream would catch
  what they catch: `check_cmp2lut()` probes the toolchain before it is
  trusted, and `check_netlist()` asserts the netlist still contains the
  storage the design depends on (`NETLIST_FF_FLOOR`). Both are fatal.
- **ecppack** turns the Trellis text configuration into a compressed
  `.bit`, and runs only for a pinned design whose timing passed. A
  bitstream that misses its clock would be loaded, would appear to
  work, and would corrupt one frame in some number nobody is counting —
  packing it turns a build failure into a bench mystery.

  Measured both ways on `oca_top` **as it stood before `54a2df8`**, the
  last netlist of it that closed anything: at seed 6 the run exited 0
  and wrote `oca_top.bit`, 527142 bytes, header `Part:
  LFE5U-45F-6CABGA381`, which `ecpunpack` decoded back to a
  configuration naming the same part with USERCODE and DONE set. At seed
  1 it exited 1 and wrote no `.bit`, **two constraints having been
  missed** — `clk_tx` 124.69 against 125 and `rgmii_rx_clk` 115.77
  against 125, while `clk_sys` (52.16) and `clk25` (486.85) passed.
  `b9f68ea`'s commit message says three; it is two, and the report it
  was measured on is the one described here.

  **On the design in the tree today neither half reproduces**: it misses
  its constraints on every seed, so `run_synth.py oca_top` always takes
  the failing branch and there is no `oca_top.bit` at all. What still
  demonstrates the passing side is `oca_top_stub`, 163854 bytes.

  **Not packing was not enough, and that was found by reading rather
  than at the bench.** `pack()` is simply not reached when the check
  fails, so a `.bit` from an earlier run stayed where a programmer would
  find it — the build directory carried a seed-6 bitstream beside a
  seed-1 report for a day.

  **So the bitstream is removed the moment nextpnr has succeeded**, when
  the report it was built with has just been replaced. The property is
  not "a failed build deletes it" but the narrower and more useful one:
  **a `.bit` never outlives the report it was built with**.

  Both earlier placements of that line were wrong, and in the same
  direction. At the top of `main()` it destroyed the last good bitstream
  over a missing tool or a failed netlist check; immediately *before*
  nextpnr it destroyed it whenever nextpnr itself failed — and nextpnr
  writes neither the report nor the configuration unless place and route
  both succeed (`common/kernel/command.cc`), so on those failures the
  old report is still standing and the old bitstream still matches it.
  The difference is not academic on this design: no seed of the 32
  tried closes it, so retrying with `--seed` and `--pnr-arg` is the
  normal way to work here. `pack()` separately removes a `.bit` that `ecppack` failed
  partway through, since a truncated bitstream is one a programmer will
  load.

  Proved on all three:

  | mutation | design | verdict |
  |---|---|---|
  | `--pnr-only` before any netlist exists | `oca_top` | exit 1, `.bit` **kept** |
  | `--pnr-arg --this-flag-does-not-exist` | `oca_top_stub` | exit 2, `.bit` **kept**, report byte-identical |
  | `.lpf` `rgmii_rx_clk` set to 400 MHz | `oca_top_stub` | exit 1, 283.53 against 400 FAILED, `.bit` gone |
  | neither | `oca_top_stub` | exit 0, `.bit` present, 163854 bytes |

  `oca_top_stub` stands in wherever place & route is reached, because its
  takes seconds; the first row never reaches nextpnr, so it was run on
  `oca_top` itself. Restore the `.lpf` by name afterwards, never with
  `git checkout -- .`.

## The cmp2lut trap

**Stock yosys deletes this design's key store. The patch in
`patches/` is not optional.**

`synth_ecp5` runs `techmap -map +/cmp2lut.v -D LUT_WIDTH=4`
unconditionally (`techlibs/lattice/synth_lattice.cc:438`) to map narrow
comparisons against constants into a single LUT. In `gen_lut`, stock
`cmp2lut.v` sign-interprets the *variable* operand but not the
*constant* one:

```verilog
if (sign)
    i_var = n[width-1:0];   // signed
else
    i_var = n;
i_cst = operand;            // always unsigned  <-- the defect
```

So a signed comparison against a negative constant is enumerated
against that constant's unsigned value and the truth table comes out
wrong. `$signed(a) >= $signed(4'b1000)` is `a >= -8`, a tautology, and
maps to `16'h0000` — constant false. An exhaustive sweep of every
`$lt/$le/$gt/$ge` cell the pass accepts gets 480 of 1920 wrong at
`LUT_WIDTH=4` and 3024 of 12096 at `LUT_WIDTH=6`; every failure is a
signed comparison against a negative constant. Yosys's own regression
(`tests/lut/map_cmp.v`) only ever uses `+5` and `0`, so the blind spot
is exact.

How it reached the key store: `logic [255:0] keys [NUM_SLOTS]` is an
*ascending* unpacked array (`[0:7]`), and `read_slang` packs element
`i` at the reversed position. The index bounds check for
`keys[wr_slot[SLOT_W-1:0]]` therefore comes out as a signed comparison
of `{1'b1, ~slot}` against `4'b1000` — that is, against `-8` — which
is a tautology and mapped to constant false. The per-slot write mask
became constant 0, `opt_expr` collapsed the write mux to `D = Q`, and
`opt_dff` correctly deleted a register that can never change. Exactly
two cells in all of `oca_core` were hit, both in `oca_keystore.sv`
(the write decode and the read of `loaded`); everything downstream —
`rd_valid`, `eng_key`, `u_aead.key_r` folding to constants — followed
correctly from that one false premise.

**This was never a regression.** Synthesising `95c81f7`, the commit
before the packet-overlap rework, gives 2313 key-store flip-flops of
which 2056 have `.DI` tied to their own `.Q`: dead registers that were
simply not yet garbage-collected. The higher flip-flop count that made
the older build look healthy *was* the corpses. Every bitstream this
project could ever have produced had a non-functional key store.

Why no test caught it: Verilator elaborates the RTL and never runs
yosys, so all 72 simulation tests are consistent with a netlist in
which the key store does not exist. Hence `check_netlist()`, and hence
`hw/sim/run_keystore_gate.py`, which synthesises `oca_keystore` and
replays its four cocotb tests on the ECP5 netlist. That runner is the
non-vacuity proof for this whole section: against a netlist built with
the unpatched mapper, `test_write_then_read` and `test_reset_clears`
fail; against a patched one, all four pass.

The RTL is not at fault and has not been changed. Declaring the arrays
`[NUM_SLOTS-1:0]` instead also dodges the bug, but only by avoiding the
index inversion by coincidence — it fixes nothing for the next array,
and the mapper would still be wrong. The patch is upstreamable and is
the actual fix; see `patches/README.md`.

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
and overlapping the phases is the structural fix and the larger one.
(The width was settled on 2026-08-04 and is **64 bits**, not the 32 the
implementation plan floated: at 8 bits the buffer needs 66 cycles to
assemble a block the engine consumes in 40 — see
`docs/design/2026-08-03-host-protocol.md`.)

**None of this has run on silicon.** Every cycle count here comes from
Verilator, every area and Fmax figure from yosys and nextpnr on an
`--out-of-context` build with no IO buffers, no pin constraints and no
Ethernet MAC — and the MAC, the RGMII wrapper and the PLL are not in
these numbers and have to share the fabric and the clock with them. This
is a simulation-derived estimate of a design that has never been
programmed into a device.

**One stale figure was left in the RTL by this pass and has since been
corrected.** The header comment of `oca_pktbuf.sv` carried the plan's
pre-measurement estimate — "the whole path runs at roughly 0.16 Gbps
... about 16% of the GbE link" — which the 415-cycle measurement above
supersedes. It was left because this pass changed no RTL, and was
corrected to ~0.062 Gbps in the following commit. Recorded here rather
than fixed silently, so the comment is not mistaken for a second,
independent source.

Build time: **3 min 11 s** for the full `oca_core` build (yosys 8.1 s +
nextpnr), on the same machine at seed 1. The three extra seeds were run
against the same netlist, since yosys output does not depend on the
placer seed.

### The occupancy study: how many engines fit — and what the router says

Measured 2026-08-04, same device, package and speed grade as everything
above (LFE5U-45F, CABGA381, speed 6), `--out-of-context`, four placer
seeds per configuration that routes — seven were tried on the one that
does not. This is the measurement that decides the MVP's shape, and it
overturns the answer every section above assumed.

Every earlier section projected three engines by multiplying one
engine's area by three. This one instantiates them and runs place &
route. The multi-engine top levels are throwaway wrappers that
instantiate N units and are **not** in `run_synth.py`'s `DESIGNS`; the
single-core row is the committed `oca_core` build, whose report is
reproducible with the documented command.

| configuration | TRELLIS_COMB | of 43848 | MULT18X18D | of 72 | routed Fmax (mean of 4 seeds) |
|---|---|---|---|---|---|
| 1 `oca_core` | 11149 | 25.4% | 20 | 27.8% | 50.59 MHz |
| 2 `oca_core` | 22313 | 50.9% | 40 | 55.6% | 49.28 MHz |
| 3 engines + 1 protocol layer | 25983 | 59.3% | 60 | 83.3% | 42.80 MHz |
| **3 `oca_core`** | **33484** | **76.4%** | **60** | **83.3%** | **does not route** |

#### The finding: three engines do not fail on area. They fail to route.

The three-core configuration fits the device on every budget anyone had
been watching — 76.4% of the LUTs, 83.3% of the multipliers, both below
100% — and **nextpnr never produces a routed design from it**:

- **one seed fails placement outright**;
- **six further seeds were still routing after 55 minutes each** and
  were stopped;
- in every one of those attempts roughly **50000 arcs remain unrouted**;
- and that count does not move when the design is constrained at
  **100, 45, 40 or 35 MHz**. Four constraints spanning a factor of
  nearly three, the same roughly 50000 arcs left over each time.

That last line is the evidence, and it is what makes this a hard result
rather than a slow build. If the router were failing to *close timing*,
relaxing the constraint would let it finish and report a lower Fmax —
that is exactly what `--timing-allow-fail` exists to produce. Instead
the routing resource itself runs out, identically, regardless of what
clock is asked for. **It is congestion, not timing. A slower clock buys
nothing, and neither would a faster device speed grade.**

So the constraint on engine count is neither of the two this file has
argued about. It is not multipliers (60 of 72 fit), it is not LUTs
(76.4% fit), it is **routability** — a budget nothing in the earlier
sections was tracking, and the only one that cannot be predicted by
multiplying a single-core report.

#### What the intermediate configurations say

- **Two `oca_core` are comfortable.** 22313 LUTs against 2 x 11149 =
  22298 — replication is linear to +15 LUTs of glue — and 49.28 MHz
  against the single core's 50.59, **-2.6%**, well inside the 3.9%
  spread the single core shows across its own four seeds (50.95 / 50.02
  / 49.70 / 51.67 in the section above). Two engines cost essentially
  nothing in clock.
- **Three engines sharing one protocol layer do route**, at 25983 LUTs
  (59.3%) and 42.80 MHz. The gap to the failing configuration is the
  two extra protocol layers: 33484 - 25983 = 7501 LUTs, close to the
  2 x 3791 the protocol layer costs by difference, and 17.1 points of
  occupancy. So it is not the third engine's arithmetic that breaks the
  router — it is the last 17 points of a die that also has to carry
  three DSP-hungry datapaths.
- **That configuration is a probe, not a design.** No RTL feeds three
  engines from one protocol layer — `oca_core` is one engine and one
  protocol layer, and there is no arbiter — and at 8 bits one protocol
  layer cannot feed even *one* engine (66 cycles to assemble a block
  the engine consumes in 40). Its 42.80 MHz would be ~1.64 Gbps if such
  a design existed and could be fed; **neither is true today**, and the
  figure is recorded as occupancy and timing data, not as capacity.
- **Where the critical path went.** With three engines it leaves
  `chacha20.sv`, which has owned it since the per-byte mask rework, and
  lands in **`poly1305.sv` line 140** — the registered DSP products,
  `prod[sl][i] <= mul_a[sl][i] * mul_b[sl][i]`. It is routing-dominated:
  the third engine fills **83% of the DSP columns**, so the datapath has
  to cross the die to reach the multipliers it was given. The 42.80 MHz
  is therefore -15.4% on the single core (and -13.1% on two cores),
  which is far outside the seed spread — unlike the two-core reading,
  this one is a real effect, and it is placement pressure rather than
  logic.

#### What this device delivers

Two engines, at the two-core mean of 49.28 MHz and the 40 cycles per
64-byte block measured in simulation (1.6 bytes/cycle each):

| | figure |
|---|---|
| per engine | 49.28e6 x 1.6 = 78.8 MB/s = ~0.63 Gbps |
| **two engines** | **158 MB/s = ~1.26 Gbps of crypto capacity** |
| one GbE port | 125 MB/s = 1 Gbps |
| margin over one port | **26%** — retracted 2026-08-06, see below |

**~1.26 Gbps of aggregate crypto capacity is the ceiling of this
silicon.** The MVP board carries two GbE PHYs (`BOM-MVP.md`), so
2 Gbps of wire is present and >= 2 Gbps was the honest target to aim
at. `SPEC.md`'s performance target has been corrected to ~1.26 Gbps
accordingly.

**Retracted 2026-08-06.** This paragraph read "One GbE port saturated
with 26% margin is the ceiling of this silicon" and "**the second port
cannot be fed on an LFE5U-45F**". Both readings add the two engines
together against a single port. `oca_dual` wires each core to its own
AXI-Stream pair, so a port sees one core, and both PHYs can be fed in
cycle budget. Commit 23742dc retracted this for `AGENTS.md` and
`SPEC.md` and did not reach this file; see `AGENTS.md` for the measured
figures.

**Amended again 2026-08-09**, and this time it is the "both PHYs" half
that goes: one GbE port costs 8422 LUTs measured (19.2% of the device),
so two cores with two ports is 94.5% of it and two cores behind one port
75.3% — against the 76.4% at which this device stopped routing in the
occupancy study. Feeding both PHYs is a cycle budget, not a
configuration that fits. **The MVP that fits the current RTL is one core
on one port.**

Three qualifications, none of them in the project's favour:

- **This supersedes every "three engines" projection above**, including
  the 1.97-2.07 Gbps of the area-pass section and the "multipliers, not
  LUTs, cap engine count" reading of the two sections before it. Both
  were arithmetic on a single-core report; this is place & route.
- **1.26 Gbps is crypto capacity, not throughput.** What came out of
  `oca_core` at the time of this study was 415 cycles per 64-byte block,
  so two cores at 49.28 MHz delivered ~0.12 Gbps end to end — a tenth of
  the engines' capacity. The 8-bit host datapath is what stood between
  the two numbers, and moving it to 64 bits is the amendment recorded in
  `docs/design/2026-08-03-host-protocol.md`. **That was done the same
  day: the 415 became 64 and the end-to-end figure ~0.78 Gbps — see the
  next section, which supersedes the two sentences above.**
- **Still no silicon.** Out-of-context builds: no IO buffers, no pin
  constraints, no Ethernet MAC, no PLL. The MAC and the RGMII wrapper
  have yet to be placed alongside two engines *and* routed, and the
  three-core result is the reason not to assume that will be
  comfortable: this die runs out of routing before it runs out of
  cells.

Cost of the study, for whoever repeats it: the six three-core routing
attempts alone are over five hours of wall clock (6 x 55 min), most of
it spent in a router that was never going to converge; the seventh seed
never got that far, failing in placement. The 100 MHz run is the one
worth doing first — if roughly 50000 arcs are still unrouted after an
hour, relaxing the constraint is not the experiment to run next.

### After the 64-bit host datapath

Measured 2026-08-04, same device, package and speed grade as everything
above (LFE5U-45F, CABGA381, speed 6), 100 MHz constraint,
`--out-of-context`. Only the host protocol layer changed:
`oca_pktbuf.sv` is 256 x 64 with a byte count on writes, `oca_proto.sv`
reads header and arguments as whole words through a funnel shifter and
streams the response through a clock-enabled three-stage pipeline, and
`oca_core.sv` exposes a 64-bit AXI-Stream pair with `tkeep`.
`chacha20.sv`, `poly1305.sv` and `chacha20_poly1305.sv` are untouched —
the engine in these numbers is bit for bit the one characterised above.

The width conversion to the 8 bits `verilog-ethernet` hands over stays
*outside* `oca_core`, at the MAC boundary; the wire format is unchanged
and `hw/sim/proto_model.py` was not modified.

| design | TRELLIS_COMB | TRELLIS_FF | MULT18X18D | DP16KD | Fmax (seed 1) |
|--------|--------------|------------|------------|--------|------|
| oca_core (8-bit datapath) | 11149 | 10842 | 20 | 2 | 50.95 MHz |
| **oca_core (64-bit datapath)** | **11429** | **11228** | **20** | **4** | **51.71 MHz** |
| *cost of the widening* | *+280 (+2.5%)* | *+386 (+3.6%)* | *0* | *+2* | *—* |

Device occupancy: 26.1% of the LUTs, 25.6% of the flip-flops, 27.8% of
the multipliers and 3.7% of the block RAM (4 of 108 DP16KD).

- **Multipliers did not move: 20, exactly the engine's.** This is the
  check the implementation plan asked for, because the protocol layer
  wants no arithmetic and any change here would have meant something had
  gone wrong in the widening. Since multipliers are what capped engine
  replication in the occupancy study, **the wider datapath still does
  not cost an engine.**
- **The area cost is about half what the plan estimated on LUTs and a
  fifth over on flip-flops.** It predicted roughly +530 COMB and +325 FF;
  the measurement is +280 and +386. A 64-bit datapath costing 2.5% of the
  LUTs of a design this size is the point trap 1 of the plan was written
  to protect: every next-state multiplexer in `oca_proto` is a `case` on
  a registered selector rather than an `if / else if` chain over
  comparators. The plan measured that choice on a synthetic 64-bit 3:1
  multiplexer at 771 LUT4 against 129 — **a factor of six**, and 642 LUTs
  on that one mux alone, which is more than twice this design's entire
  measured increase. That is the plan's own figure for a synthetic case,
  not a measurement of `oca_proto`; what is measured here is only the
  +280 total.
- **The buffers went from 2 DP16KD to 4, and it is a width consequence,
  not a capacity one.** Each buffer still holds 2048 bytes. From the
  netlist, each is two DP16KD with `DATA_WIDTH_A = DATA_WIDTH_B = 36`
  (`u_rxbuf.mem.0.0` and `.0.1`, likewise `u_txbuf`): a DP16KD's widest
  port is 36 bits, so a 64-bit word spans two blocks side by side. Both
  buffers still map through `$__PDPW16KD_` — pseudo dual-port, which is
  what trap 2 of the plan required — and the netlist contains **zero LUT
  RAM cells**: no `TRELLIS_DPR16X4`, no `DPR16X4C`, no `TRELLIS_RAM16X2`.
  Since 36-bit mode is 512 x 36 and only 256 words are used, `BYTES`
  could go from 2048 to 4096 at no further block-RAM cost.
- **Fmax: unchanged, and the seed spread says so.** Seed 1 reads
  50.95 -> 51.71 MHz, which on its own means nothing; over four seeds,
  same netlist, area identical on every one as it must be:

  | design | seed 1 | seed 2 | seed 3 | seed 4 | mean | spread |
  |--------|--------|--------|--------|--------|------|--------|
  | oca_core (8-bit) | 50.95 | 50.02 | 49.70 | 51.67 | 50.59 | 3.9% |
  | oca_core (64-bit) | 51.71 | 48.75 | 51.50 | 50.79 | **50.69** | 5.8% |

  Mean +0.10 MHz, **+0.2%** — the distributions sit on top of each other,
  and no clock was bought or paid. **The widening is free in time and
  cheap in area.** The critical path is where it has been since the
  per-byte mask rework: 19.34 ns at seed 1 (7.64 ns logic, 11.70 ns
  routing) from `u_aead.u_chacha.st` back into `u_chacha.st`, through the
  CCU2C carry chains of the four adders of one quarter round, and every
  entry in nextpnr's report cites `chacha20.sv` lines 58, 60, 62 and 64.
  **Not one cites `oca_proto.sv`, `oca_pktbuf.sv`, `oca_keystore.sv` or
  `oca_core.sv`** — checked on all four seed reports, and true on every
  one of them; the protocol layer is no more on the critical path at
  64 bits than it was at 8. Seed 2, the slowest of the four, is the one
  that does not cite `chacha20.sv`, and it is not a protocol path either:
  it lands on `poly1305.sv:140`, the registered DSP products, at
  20.51 ns. Which of the two engine paths comes out longest is placement
  — but note that these are two different seeds, and each nextpnr report
  carries only its own worst path, so the *second* longest path within a
  seed is not in these reports and the two cannot be compared directly.

#### Throughput: 415 cycles per block to 64

Measured the same differential way as every point in this file — a seal
command at 4, 8, 12 and 16 blocks, the difference over the block delta,
so the header, the key schedule, the tag and every fixed cost cancel. A
64-byte block costs **64.0 cycles end to end**, and the figure is exactly
linear: the three consecutive pairs (4->8, 8->12, 12->16) and the 4->16
span all return 64.0.

| phase | 8-bit | 64-bit | rate now |
|-------|-------|--------|----------|
| request in (`s_axis`) | 64 | **8** | 8.00 B/cycle |
| buffer -> engine -> buffer | 159 | **48** | — |
| response out (`m_axis`) | 192 | **8** | 8.00 B/cycle |
| **total per 64 B** | **415** | **64** | **1.00 B/cycle** |

**6.5x, and it is less than 8x because 40 of the remaining 64 cycles
are not a datapath at all.** Eight times the width can only scale what
the width gates. The engine's own cost does not scale with the host
datapath, and it is now most of what is left: measured the same
differential way on this same RTL while writing this section, the AEAD
engine alone is **40.0 cycles per 64-byte block** (227 cycles for a
4-block message, 387 for 8). Against that floor the three phases did
this:

| phase | 8-bit | pure width would give | measured | why |
|---|---|---|---|---|
| request in | 64 | 8 | **8** | already 1 B/cycle at 8 bits, so exactly 8x |
| middle | 159 | — | **48** | 40 of it is the engine and never scaled |
| response out | 192 | 24 | **8** | **better than 8x**: the handshake went too |

Of the 351 cycles saved, the response path is the largest single term
(184), the middle row second (111) and the request path third (56). The
response is the only phase that beat the width, and it did so because the
three-cycle-per-beat handshake — a cycle fetching, a cycle asserting
`m_tvalid`, a cycle completing — was replaced by three stages under one
clock enable, which is a scheduling fix and not a width one.

So the protocol layer now costs **8 cycles of feed and drain** on top of
the engine's 40, plus **16 more receiving and transmitting the block**.
Those 16 are pure serialisation, not bandwidth: at 8 bytes per cycle the
stream ports are eight times faster than the wire they will be attached
to.

#### What two cores deliver, and whether that meets the MVP target

The MVP configuration is two `oca_core` instances. Synthesised as a
throwaway two-instance top level in the scratchpad — **not** in
`run_synth.py`'s `DESIGNS`, same wrapper shape as the 8-bit occupancy
study, each core's AXI-Stream pair wired straight to top-level ports so
the instances cannot be merged and nothing can be optimised away:

| configuration | TRELLIS_COMB | of 43848 | TRELLIS_FF | MULT18X18D | of 72 | DP16KD | routed Fmax |
|---|---|---|---|---|---|---|---|
| 1 `oca_core`, 8-bit | 11149 | 25.4% | 10842 | 20 | 27.8% | 2 | 50.59 (4 seeds) |
| 2 `oca_core`, 8-bit | 22313 | 50.9% | — | 40 | 55.6% | — | 49.28 (4 seeds) |
| 1 `oca_core`, 64-bit | 11429 | 26.1% | 11228 | 20 | 27.8% | 4 | 50.69 (4 seeds) |
| **2 `oca_core`, 64-bit** | **22891** | **52.2%** | **22456** | **40** | **55.6%** | **8** | **48.53 (2 of 4 seeds)** |

- **Replication is still linear.** 2 x 11429 = 22858 against 22891
  measured: **+33 LUTs of glue**, the same result the 8-bit study got
  (+15). Multipliers and block RAM are exactly double.
- **The clock is where two 8-bit cores were.** 47.85 MHz at seed 1 and
  49.21 at seed 4, mean **48.53 MHz** against the 8-bit pair's 49.28 —
  **-1.5%**, inside the 5.8% spread the single core shows across its own
  four seeds. Replication costs essentially nothing in clock at 64 bits
  either.
- **With two cores the critical path leaves `chacha20.sv`.** Both routed
  seeds put it on **`poly1305.sv:140`** — the registered DSP products,
  `prod[sl][i] <= mul_a[sl][i] * mul_b[sl][i]` — 20.90 ns at seed 1
  (8.29 logic, 12.60 routing) and 20.32 ns at seed 4. This is the same
  place the 8-bit study's *three*-engine configuration moved it to, and
  for the same reason: the DSP columns are 56% full, so the datapath
  crosses the die to reach the multipliers it was given. Routing, not
  logic. No entry cites a protocol module on either seed.
- **Two of the four seeds did not route, and that is new.** Seeds 1 and 4
  completed, in 2714 s and 1405 s, sharing the machine with the other
  two. Seeds 2 and 3 did not, and were **restarted alone** when the
  others finished; each then ran **3 h 22 min** without converging and
  was stopped there. (The restart matters for reading the wall times: the
  3 h 22 min is exclusive-machine time, not the same window as the
  2714 s.) Over their last 200 router reports the remaining arc count
  oscillates rather than descends — seed 2 between 53 and 2292 (median
  926.5), seed 3 between 77 and 2180 (median 866) — dipping close enough
  to finish repeatedly without ever closing. That is not the three-core failure mode of the occupancy study:
  there it was roughly 50000 arcs, flat, and unmoved by relaxing the
  constraint from 100 to 35 MHz. Here it is one to two orders of
  magnitude fewer arcs and the design does route on half the seeds it was
  given. **Recorded as a routability margin that has narrowed, not as a
  failure**: two 64-bit cores fit this device where two 8-bit cores were
  called "comfortable", and are no longer comfortable. It also means
  **the mean above is over two seeds, not four**, and is weaker evidence
  than every other Fmax in this file. Whether the other two would close
  with more time is not known — they were stopped, not shown to
  diverge.

**The arithmetic, and it does not reach the target.** Two cores, 64.0
cycles per 64-byte block, at the mean of the seeds that routed:

| | cycles / 64 B | bytes / cycle | throughput | vs one GbE port |
|---|---|---|---|---|
| one core | 64 | 1.00 | 48.5 MB/s = ~0.39 Gbps | 39% |
| **two cores, as built** | **64** | **2.00** | **97.1 MB/s = ~0.78 Gbps** | **78%** |
| one GbE port | | | 125 MB/s = 1 Gbps | 100% |

Across the two routed seeds the figure is 95.7 to 98.4 MB/s, ~0.77 to
~0.79 Gbps. **The MVP target is missed, and by how much depends on which
of its two clauses is measured against:**

| against | figure | shortfall |
|---|---|---|
| a bare GbE port, 125 MB/s | 97.1 of 125 MB/s = 78% | **22%** |
| the target as `SPEC.md` states it, ~1.26 Gbps *with margin* | 0.78 of 1.26 Gbps = 62% | **38%** |

The second row is the one the target asked for when this was measured —
"saturate one GbE host port **with margin**" — so **38% is the honest
headline** and 22% is only the distance to breaking even with the wire.
(`SPEC.md` no longer states it that way: as of 2026-08-06 the ~1.26 Gbps
is an aggregate cycle budget over both engines, and the per-port figure
is what the retraction above records.) To reach even
125 MB/s at 64 cycles per block, two cores would need **62.5 MHz**, 29%
above what this device gives them and above the 52-53 MHz the standalone
`chacha20` core has never beaten. **The clock is not where this comes
from.** `SPEC.md`'s MVP bullet has been corrected accordingly.

**Where it does come from: the 64 cycles are serialised, and they need
not be.** The three phases — 8 in, 48 through, 8 out — are strictly
sequential because `oca_core` is store and forward on a single pair of
buffers: the request must be received whole before processing starts and
the response transmitted whole afterwards, and neither overlaps anything.
Overlapping them *across successive packets* would make a core's cost
`max(8, 48, 8) = 48` cycles per block instead of their sum:

| | cycles / 64 B | two cores | vs one GbE port |
|---|---|---|---|
| as built | 64 | 97.1 MB/s = ~0.78 Gbps | 78% |
| **packet-level pipelining** | **48** | **129.4 MB/s = ~1.04 Gbps** | **104%** |
| *and feed/drain hidden behind the engine* | *40* | *155.3 MB/s = ~1.24 Gbps* | *124%* |

**Pipelining is necessary and it is not sufficient.** It clears the port
by 2 to 5% across the two routed seeds — which is not margin, it is the
noise band of the Fmax it is computed from. The margin only appears when
the 8 cycles of feed and drain come off the critical loop too, leaving
the engine's own 40, at which point two cores reach ~1.24 Gbps and 24% of
headroom. That figure is not a coincidence: it is the ~1.26 Gbps of
crypto capacity `SPEC.md` already records for two engines, recomputed at
this pair's 48.53 MHz instead of the 8-bit pair's 49.28 — same figure,
1.5% apart. **The aggregate target is reachable on this silicon, and
reaching it means the protocol layer must cost nothing on top of the
engine — not merely little.** Amended 2026-08-06: this read "24% of
headroom" and "24% of margin rather than 26%", both of which measure the
two engines together against one port. `oca_dual` gives each core its
own port, so the per-port figure is 0.569 Gbps at a 1500-byte MTU on
the committed pair's 48.89 MHz mean (0.561 at the 48.16 MHz this line
quoted until 2026-08-09). **Amended 2026-08-10: that is a cycle budget
and not a rate.** 48.89 MHz is an out-of-context Fmax, and `oca_clkrst`
gives `clk_sys` = 625/N with N = 13, so a pinned build of this topology
runs at 48.0769 MHz and 0.560 Gbps per port. Every *throughput* figure
in this file divides an Fmax into a cycle count, and no PLL divider
produces any of those clocks; the 1 and 2 Gbps that appear elsewhere are
wire rates and targets, which is a different kind of number.

The cost of pipelining is buffers, and it is affordable: a second receive
and a second transmit buffer per core takes block RAM from 4 DP16KD per
core to 8, so 16 of 108 (14.8%) for the pair. The security property
survives it — "a failed tag returns no plaintext at all" is a statement
about one packet, and each packet is still received whole before it is
processed and processed whole before it is transmitted. What changes is
only that three *different* packets may be in the three phases at once.
Whether the routability margin above survives two more buffers per core
is a separate question, and it has to be measured rather than assumed.

**Still no silicon.** Every cycle count here is Verilator; every area and
Fmax figure is yosys and nextpnr on an `--out-of-context` build with no
IO buffers, no pin constraints, no Ethernet MAC and no PLL — and the MAC,
the RGMII wrapper and the PLL still have to share this fabric and this
clock. Nothing in this section has been programmed into a device.

Suites behind these numbers, all re-run while writing this section:
`oca_core` 10/10, `oca_pktbuf` 5/5, `oca_keystore` 4/4, `chacha20` 5/5,
`poly1305` 4/4, `chacha20_poly1305` 7/7, `test_dirty_pad` 2/2.

Cost of this round, for whoever repeats it: the single-core build timed
7 min 1 s here (yosys 7.7 s + nextpnr) against the 3 min 11 s the 8-bit
core took, but that run shared the machine with four other nextpnr
processes and the comparison is not clean — the same netlist at the same
seed took 557 s in the sweep. The two-core seeds are the expensive part:
2714 s and 1405 s for the two that routed, 3 h 22 min each for the two
that did not. Unlike the three-core case, a stalled seed here is worth
waiting on for a while — seed 1 descended steadily through its last
thousands of iterations (773, 591, 534, 321, 217, 97, 98) before dropping
to zero — but a seed that has been bouncing between 50 and 2300 arcs for
three hours, as seeds 2 and 3 were, is giving a different signal from one
that is still coming down.

### After restoring the key store (the cmp2lut fix)

Measured 2026-08-04, same device, package and speed grade as everything
above (LFE5U-45F, CABGA381, speed 6), 100 MHz constraint,
`--out-of-context`. **The RTL is identical in both rows** — the only
difference is whether yosys's `cmp2lut.v` carries the patch in
`patches/`. See "The cmp2lut trap" above for what the defect is.

| build | TRELLIS_COMB | TRELLIS_FF | DP16KD | MULT18X18D | Fmax mean | seeds |
|---|---|---|---|---|---|---|
| stock yosys — key store deleted | 8620 | 8311 | 4 | 20 | 49.31 | 48.66, 49.96 |
| patched — key store present | 11590 | 12043 | 4 | 20 | 48.84 | 49.76, 47.48, 48.45, 48.84, 49.67 |

Seeds are 1 and 2 for the stock row; 1, 2, 4, 5 and 6 for the patched
row.

**Run again 2026-08-09, on the RTL of `ee54b06` and five seeds on both
rows**: patched 48.52, 51.26, 47.59, 50.05, 49.23 (mean **49.33**);
stock 51.93, 51.63, 46.84, 50.32, 54.08 (mean **50.96**). The area
columns come back unchanged — 11590 / 12043 patched and 8311 FF stock
exactly, 8616 against 8620 TRELLIS_COMB being nextpnr's packing, not
yosys.

**That run is not a re-measurement of this table, and the clocks were
never going to repeat across it.** This table is the RTL of `bf3930f`,
its stock row on two seeds; that one is `ee54b06`, five seeds on each
row. `5492e3a` had already measured what the RTL move alone does: the
two netlists match on every per-type cell total and still place
differently at seed 1 — `bf3930f` 49.76 MHz, the later RTL 48.52 — with
the worst path leaving `chacha20.sv` for `oca_proto`. Equal totals are
not an equal netlist, so the area columns repeating says nothing about
placement. The tools are not in it: `tools/` holds one nextpnr, built
2026-08-03 and never rebuilt, and the toolchain pin (`49691a4`,
2026-08-04 22:11) recorded the revisions of that already-built
toolchain six hours after this table was written. Read as its own
measurement on its own RTL, the 2026-08-09 run reaches the conclusion
below by itself: -3.2% on the means against a 46.84-54.08 stock spread
is "unchanged within the seed spread".

An earlier version of this note read the same numbers as proof that
"the nextpnr behind them was another binary" and dated the pin
2026-08-06. Both were wrong, and the second is what made the first look
supported.

**This is not a regression in area; it is the cost of having a key store
at all** — but read the two rows as what they are, which is one netlist
against the other and not against anything published earlier. The
+2970 LUTs and +3732 flip-flops between them are 2313 in
`oca_keystore.sv` (2048 key bits, 8 loaded bits, 256 `rd_key`, 1
`rd_valid`) plus the 256-bit `eng_key` and the status and response logic
that had folded behind them.

**`8620` / `8311` appears nowhere else in this file**, and no earlier
section compares against it: the last published single-core netlist is
the 64-bit one at **11429 LUTs / 11228 FF**, which did contain the key
store's 2313 flip-flops — 2056 of them wired `.DI` to their own `.Q`,
holding themselves, which is storage in name only but is still cells.
The stock row above is smaller than that one because at this RTL the
mapper managed to remove the store outright rather than freeze it.
**Published netlist to published netlist, the committed design is
therefore +161 LUTs and +815 flip-flops** over the 64-bit row — the
packet overlap and a live key store together — not the ~3000 the
difference between the two rows above suggests. What the patched netlist
does cost against the stock one is router effort, and that is real:
see below.

**Fmax is unchanged within the seed spread**: 49.31 MHz mean before,
48.84 MHz after — **-1.0%**, against a spread of 4.8% across the five
patched seeds alone (47.48 to 49.76). The 2026-08-09 run on `ee54b06`
reaches it independently: 50.96 -> 49.33, **-3.2%** against that run's
46.84-54.08 stock spread. That is the expected result — the
critical path lives in `poly1305.sv`'s DSP products and `chacha20.sv`,
not in a register file read through a mux — but it had to be measured
rather than assumed. The stock row is only two seeds, so treat its mean
as the weaker of the two.

**Router effort is the real cost, and it is not small.** Every seed
routed — none oscillated, none had to be abandoned — but the larger
netlist takes much longer to get there. `Router1 time`, against 196 s
for the stock netlist:

| seed | 1 | 2 | 4 | 5 | 6 |
|---|---|---|---|---|---|
| router time (s) | 519 | 2140 | 482 | 1041 | 1498 |

Seed 1 ran alone; 2, 4, 5 and 6 shared the machine with each other, so
their figures are inflated by contention and are an upper bound rather
than a clean measurement. Even so the cheapest patched seed is 2.5x the
stock netlist. This die was already described above as running out of
routing before it runs out of cells, and **router effort, not cell
count, is what this change spends against that margin**: the design is
+161 LUTs on the last published netlist, and it takes several times
longer to route. Budget for the time before the MAC, the RGMII wrapper
and the PLL arrive.

DP16KD stays at 4 and MULT18X18D at 20: the key store is flip-flops and
a decode, so neither the block RAMs nor the DSPs move.

### Where the committed design stands

Same flow, same device, 100 MHz constraint. This is what
`run_synth.py oca_core` reproduces on the RTL as merged, and what the
netlist checks now cover:

| | TRELLIS_COMB | TRELLIS_FF | DP16KD | MULT18X18D | Fmax mean | seeds |
|---|---|---|---|---|---|---|
| `oca_core`, as committed | 12308 | 12033 | 4 | 20 | 49.91 MHz | 47.93, 50.91, 51.03, 49.76 |

Four seeds, measured 2026-08-09; area is identical on all four, as
synthesis being deterministic requires. Spread **6.5%** on
`sweep.sh`'s definition, `(max-min)/min`. This table carried seed 1
alone (47.93 MHz) until that sweep was run, which made the single core
the one figure in this file quoted from a single draw.

**Two definitions of spread live in this file.** The seed tables above
divide by the mean; `sweep.sh` and every figure dated 2026-08-09
divide by the minimum. On these data they differ by a few tenths of a
point — the 64-bit row's 5.8% is 6.1% by the minimum — so never compare
a percentage from one against a percentage from the other without
recomputing.

Live flip-flops by source file, printed by `check_netlist` on every run:
`oca_proto.sv` 3645, `chacha20_poly1305.sv` 2479, `oca_keystore.sv`
2313, `poly1305.sv` 1789, `chacha20.sv` 1415, `oca_pktbuf.sv` 68, plus
324 yosys attributes to no file. Three floors guard that census. Two are
per file: 2313 for the key store (derived:
`NUM_SLOTS*256 + NUM_SLOTS + 256 + 1`) and 3600 for `oca_proto`
(measured, with head-room for the optimiser). The third is on the whole
netlist, 11900 against 12033 live, and it is what covers the AEAD
engine.

**Why the engine is floored by a total and not per file.** yosys's
per-file attribution moves. Across the secret-zeroisation merge the
census answered `poly1305.sv` 391 -> 1789 with the unattributed bucket
1753 -> 324, on a netlist that lost ten registers in total. `poly1305.sv`
gained reset branches over that delta and no new state, so those 1398
registers were relabelled rather than created; the merge's own new
state is elsewhere, the 20 registers of `oca_pktbuf`'s memory-clearing
walk. A per-file floor tight enough to catch the
accumulator vanishing would have failed that healthy build; one loose
enough to survive it would not catch the accumulator. A total is immune
to the relabelling and still tight, and the check is non-vacuous on all
three engine files: deleting every flip-flop attributed to `chacha20.sv`
(1415), `poly1305.sv` (1789) or `chacha20_poly1305.sv` (2479) from a
real netlist fails it, and in all three cases the two per-file floors
still pass. Every floor here has to be re-measured from the census
whenever the RTL changes; they only ever fail downwards, so adding
storage is free.

`hw/sim/run_proto_gate.py` covers what no census can: the tag comparison
is combinational, and it is replayed on a mapped `oca_proto` inside an
otherwise RTL `oca_core`.

**Two things this table is not.** It is not a two-core figure — the
22891 LUTs and 48.53 MHz above were measured on RTL from before the
packet overlap and before the key store was restored. The two-core build
of the committed RTL has since been placed and routed over four seeds;
`AGENTS.md` carries that result, and it is deliberately not repeated
here, because the same figure written down twice is how this file and
that one came to disagree about the single-core numbers.

And one seed is one sample. The 47.93 MHz this section used to carry
alone is the lowest of the four above, drawn from a **6.5%** spread —
wider than the pair's 4.8%, both on `(max-min)/min`, the definition
`sweep.sh` prints. Quote the mean, and quote it with its seed count.
The comparison
this paragraph used to make — against 49.76 MHz at seed 1 on a netlist
yosys reported cell for cell identical — no longer applies: the secret
zeroisation changed the netlist (11590 -> 12308 TRELLIS_COMB), so the
two are not the same design placed differently.

At seed 1 the worst path is back inside the engine: `u_aead.u_poly`'s
multiply into the carry chain (`poly1305.sv:159` through `mul2dsp`),
20.86 ns of which **8.01 ns is logic and 12.86 ns is routing** (the two
components are each rounded, so they sum to 20.87; 20.86 is nextpnr's
own cumulative total). It is
routing-dominated, which is the same thing the three-core study found by
a different route. The protocol layer, which appeared on this path once
on the pre-zeroisation netlist, does not appear on it here — one seed
either way is a placement result and not a property of the design, which
is why the sentences elsewhere about no protocol module appearing on any
seed stay scoped to the four seeds that measured it.

The cycle side is measured and does not depend on any of that: a
64-byte block costs **40 cycles** end to end through `oca_core`, exactly
linear (231, 391, 551, 711 cycles for 4, 8, 12, 16 blocks).

### The whole board, pinned: oca_top places, and does not close

The first design in this project that is a board rather than a core:
clocking, RGMII front end, MAC, Ethernet header parse and build,
ARP/IP/UDP, the seam and `oca_core`, against `colorlight_i9.lpf` with
real IO. LFE5U-45F CABGA381 speed 6, seed 10.

| resource | used | of device |
|---|---|---|
| TRELLIS_COMB | 18719 | 42.7% |
| TRELLIS_FF | 17249 | 39.3% |
| DP16KD | 13 | 12.0% |
| MULT18X18D | 20 | 27.8% |
| TRELLIS_IO | 17 | 6.9% |
| EHXPLLL | 1 | 25.0% |

**42.7%, against the 47.3% the area sum predicted.** One core measured
12308 LUTs alone and one port 8422, which adds to 20730; the optimiser
shares logic the two separate measurements each counted. Adding areas
measured apart still overestimates, though by 10% rather than the 14%
the first build showed.

**This table read 17802 / 40.6% until 2026-08-11**, on the netlist
before `54a2df8` connected the raw-IP ready pins. That fix is required
— without it one non-UDP frame stops reception permanently — and it
costs +917 TRELLIS_COMB and +400 TRELLIS_FF of vendor logic that yosys
had been folding away, which is also why the design no longer closes.
See the sweep below.

#### The receive clock, and what it took

The first build missed `rgmii_rx_clk` at **102.59 MHz** against 125. The
critical path was the MAC's receive CRC, and it split in two: 4.71 ns
from `crc_state` through the LFSR to `crc_next`, which has to close in
one cycle, and another 5.04 ns for the FCS comparison after it, which
does not — `axis_gmii_rx.v:220` compared the four received FCS bytes
against `~crc_next` combinationally in the same cycle.

Place and route could not reach it, measured four ways on one netlist:

| | rgmii_rx_clk |
|---|---|
| as committed, repeated | 102.59 MHz |
| `--placer-heap-critexp 4` | 102.59 MHz |
| `--placer-heap-timingweight 35` | 103.35 MHz |
| both, plus `--router router2` | did not converge |

The repeat matters as much as the rest: identical settings reproduce
102.59 exactly, so the differences are the settings and not noise. The
best was 0.7% against 21.8% needed. **`router2` diverges on this
design** — overused arcs fall to 6970 by iteration 22, then climb to
26003 by 58 — which answers a question the Ethernet design document had
left open since the occupancy study.

A patch to `axis_gmii_rx` moved the comparison off the path (see
`hw/vendor/patches/`), and the receive path went to **115.77 MHz** with
the critical path now in the receive FIFO's commit loop. Still short.

#### The seed sweep, and the honest reading of it

Thirteen seeds on the patched netlist, place and route only. **This
table is the netlist before `54a2df8` and is kept for the comparison
below; it is not the design that is in the tree.**

| seed | rgmii_rx_clk | clk_tx | clk_sys |
|---|---|---|---|
| 1 | 115.77 | 124.69 | 52.16 |
| 2 | **125.23** | 117.77 | 50.24 |
| 3 | 118.85 | 119.18 | 47.84 |
| 4 | 118.78 | 122.09 | 50.77 |
| 5 | 122.26 | 121.54 | 49.77 |
| **6** | **129.87** | **130.07** | **49.41** |
| 7 | 119.13 | **140.37** | 48.99 |
| 8 | 115.27 | **135.01** | 49.79 |
| 9 | 111.71 | **132.42** | 52.08 |
| 10 | 118.57 | 107.72 | 48.66 |
| 11 | 109.52 | **127.39** | 51.74 |
| 12 | 118.13 | 123.29 | 51.37 |
| 13 | 106.61 | **135.98** | 51.19 |

Targets are 125.00, 125.00 and 48.08 MHz. `rgmii_rx_clk` clears its own
on two seeds of thirteen, `clk_tx` on six, `clk_sys` on twelve — and
all three coincide on **one**. Both 125 MHz clocks swing widely across
the sweep and they do not swing together: `rgmii_rx_clk` by 21.8% from
worst to best and `clk_tx` by 30.3%.

**So the design closed, and it had no margin of its own.** Seed 6 was
recorded in `run_synth.py`'s DESIGNS entry, not passed on a command
line, so `run_synth.py oca_top` reproduced it. But a seed is not margin:
any RTL change reshuffles the placement and the seed has to be found
again, and there is no reason to expect the next one to exist.

**It did not exist, and the reason is worse than a lost seed. Amended
2026-08-11.** `54a2df8` connected three pins on `udp_complete_64`. Two
of them, the raw-IP ready pair, fix a wedge the board could not survive
— one non-UDP frame and reception stops for good — and cost **nothing**:
built with only those two, the netlist is 16849 flip-flops, exactly what
it was.

The third, `clear_arp_cache`, is the whole of the **+917 TRELLIS_COMB
and +400 TRELLIS_FF**, and what it bought is `arp_cache.v` going from
**0 live flip-flops to 130**. The earlier netlist — the one that closed
on seed 6 and was packed into a bitstream — had no ARP cache in it at
all, because an undriven input is not an input reading zero: yosys may
take it as don't-care and pick the value that simplifies most, here a
cache held permanently in reset.

So the sweep below is not a regression against 129.87 MHz. It is the
first sweep of a complete design.

Thirty-two seeds on that netlist, place and route only:

| seed | rgmii_rx_clk | clk_tx | clk_sys |
|---|---|---|---|
| 1 | 105.43 | 122.53 | 47.63 |
| 2 | 107.77 | **130.86** | **48.38** |
| 3 | 111.35 | 122.91 | **49.99** |
| 4 | 115.42 | 121.36 | 46.93 |
| 5 | 105.93 | 124.02 | **48.51** |
| 6 | 115.59 | 120.88 | **48.39** |
| 7 | 110.98 | 123.70 | **50.18** |
| 8 | 112.61 | **132.71** | **49.18** |
| 9 | 111.02 | **130.01** | 47.98 |
| **10** | 124.22 | 122.91 | 47.40 |
| 11 | 113.82 | **129.42** | 47.78 |
| 12 | 100.49 | **138.95** | **48.12** |
| 13 | 106.25 | **128.87** | **49.23** |
| 14 | 112.59 | 116.28 | 47.04 |
| 15 | 107.52 | **134.97** | 44.96 |
| 16 | 112.97 | **129.22** | **49.03** |
| 17 | 107.65 | 124.81 | **50.44** |
| 18 | 113.84 | **137.01** | **48.10** |
| 19 | 109.10 | 124.81 | 46.56 |
| 20 | 111.04 | **136.67** | 47.87 |
| 21 | 105.89 | **132.73** | **49.98** |
| 22 | 113.31 | **130.31** | 46.56 |
| 23 | 117.32 | **129.48** | **49.97** |
| 24 | 106.00 | **125.63** | **49.42** |
| 25 | 99.49 | 121.97 | **48.94** |
| 26 | 100.28 | **130.75** | 46.68 |
| 27 | 109.53 | **130.98** | **48.94** |
| 28 | 115.27 | 120.71 | 47.02 |
| 29 | 116.05 | **140.94** | **48.23** |
| 30 | 102.07 | 115.02 | **50.18** |
| 31 | 107.07 | **131.06** | **49.13** |
| 32 | 113.37 | 122.22 | **48.53** |

**`rgmii_rx_clk` clears 125 MHz on none of the thirty-two.** Best 124.22
at seed 10, short by 0.63%; second best 117.32; the bulk between 105 and
117. That shape matters: the best is a tail event, not a cluster sitting
just under the line, so more seeds are not a plan. `clk_tx` clears on 18
and `clk_sys` on 20, and no seed carries all three.

Seed 10 is recorded in the DESIGNS entry as the best measured, not as
one that works, and `run_synth.py oca_top` exits 1 and packs nothing.

What would give real margin is still less competition for the fabric
around the receive path -- the MAC alone closes at 132.98 MHz with 6.4%
to spare, so the shortfall was never the module. Untried, and the
obvious next thing: a third vendor patch that discards non-UDP frames
inside `udp_complete_64` rather than exposing the raw-IP port, which
would consume the frame without keeping its datapath alive.
