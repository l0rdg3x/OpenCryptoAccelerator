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

### chacha20_poly1305 (AEAD engine, ChaCha20 + Poly1305)

| resource | used | available | % |
|----------|------|-----------|---|
| TRELLIS_COMB | 11144 | 43848 | 25.4% |
| TRELLIS_FF | 4777 | 43848 | 10.9% |
| MULT18X18D | 65 | 72 | 90.3% |

Fmax: **26.77 MHz** (constraint 100 MHz, not met).

Critical path: 37.36 ns — 20.77 ns logic, 16.59 ns routing — entirely
inside `u_poly`, from the `prod` register through a long CCU2C carry
chain and five cascaded MULT18X18D into the accumulator `a`. That is
`poly1305.sv` doing the 130×130-bit multiply and the mod-2^130-5
reduction in a single clock cycle.

Two consequences for the MVP target (>= 10 Gbps aggregate):

1. **DSP-bound**: one AEAD engine takes 90% of the ECP5-45F multipliers,
   so the core cannot be replicated on this device as written. The
   Poly1305 multiplier has to be decomposed (limb-based, pipelined)
   before any parallel-core plan.
2. **Clock-bound**: at 26.77 MHz the engine is far below what the target
   needs. From the FSM, a full 64-byte block costs roughly 27 cycles
   (12 for the ChaCha20 block plus 4 Poly1305 sub-blocks at 3 cycles) —
   an estimate read off the RTL, not yet measured in simulation, worth
   ~0.5 Gbps. Splitting the single-cycle multiply and reduction across
   pipeline stages is the same fix as (1).

Router runtime for this design was ~38 minutes on the dev machine
(nextpnr Router1); plan for it.
