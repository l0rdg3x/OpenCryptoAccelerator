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
