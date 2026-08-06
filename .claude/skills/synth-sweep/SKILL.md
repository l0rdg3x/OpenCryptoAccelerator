---
name: synth-sweep
description: Synthesise an OCA target across several placer seeds, report area and Fmax, and compare against the figures recorded in AGENTS.md. Use whenever a change might move area or clock, and before recording any synthesis number in a document or commit message.
---

# Synthesis sweep

Place and route one target on several seeds, then say what moved.

A single seed cannot distinguish a real change from placer noise: the
documented spread on this design is **4.8%** across seeds, and readings
in both directions inside that band have already been mistaken for
signal. So the unit of measurement here is a sweep, not a run.

## Usage

```sh
.claude/skills/synth-sweep/sweep.sh <target> [seeds]
```

`target` is one of `run_synth.py`'s designs — `chacha20`, `poly1305`,
`chacha20_poly1305`, `oca_core`, `oca_dual`. `seeds` defaults to 4.

## What it does, and why each part matters

1. Runs `oca/hw/syn/run_synth.py` once per seed, from `oca/`.
2. Collects `TRELLIS_COMB`, `TRELLIS_FF`, `MULT18X18D`, `DP16KD` and
   Fmax from each run's `build/<target>.report.json`.
3. Prints per-seed rows plus mean, min and max Fmax, and the spread.
4. Prints the area once, not per seed — synthesis is deterministic, so
   area that differs between seeds means something is wrong.

**Output goes to a file the harness owns, not to the scratchpad.** A
long sweep once lost three of four seeds because its log lived in a
directory a concurrent agent had been told it could clean.

## Reading the result

**Area is deterministic; treat any difference as a defect in the
measurement.** Clock is not: report the mean over the sweep, and do not
claim a Fmax change smaller than the spread. If the sweep's own spread
is wider than 4.8%, say so — the design may have become harder to place,
which is itself the finding.

Compare against the figures in `AGENTS.md`'s status section. If they
disagree and your RTL is unchanged, the toolchain moved: stop and find
out which, rather than publishing the new number.

**The flip-flop floors in `run_synth.py` are the point of running this
at all on a design with a key store.** If a `netlist check` line reports
FAILED, storage has vanished from the netlist and the design would build
and not work — that is the `cmp2lut` failure mode, and no simulation can
see it.

## After a sweep

If the numbers go into a document or a commit message, they carry the
seed count and the spread with them. A bare Fmax with no seed count is
not a measurement, it is an anecdote.
