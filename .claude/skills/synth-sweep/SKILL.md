---
name: synth-sweep
description: Use when a change might move area or clock, or before recording any synthesis number in a document or commit message. Synthesises an OCA target across several placer seeds, reports area and Fmax, and compares against the figures recorded in AGENTS.md.
---

# Synthesis sweep

Place and route one target on several seeds, then say what moved.

A single seed cannot distinguish a real change from placer noise. On
this script's own definition of spread, `(max-min)/min`, the pinned
toolchain gives:

- **`oca_dual` (a pair): 4.8%**, four seeds (50.37 / 48.12 / 48.05 /
  49.03 MHz) on the netlist committed today, secret zeroisation
  included, measured 2026-08-05 in `d4ee09f` (`AGENTS.md`, "Two cores
  measured", which carries what is known about the 2026-08-09
  re-check). This file cited 4.8% before too, and it was a different
  measurement that rounds the same way: five seeds of a single
  pre-zeroisation core, 47.48 to 49.76 MHz.
- **`oca_core` (a single core): 6.5%**, four seeds (47.93 / 50.91 /
  51.03 / 49.76 MHz, mean 49.91) on the netlist committed today,
  measured 2026-08-09 (`oca/hw/syn/README.md`, "Where the committed
  design stands").

One four-seed draw of each is not enough to say a single core spreads
wider than a pair as a property of the designs, so read those two
numbers as what each sweep measured, not as a rule.

Readings in both directions inside these bands have already been
mistaken for signal. So the unit of measurement here is a sweep, not a
run.

## Usage

```sh
.claude/skills/synth-sweep/sweep.sh <target> [seeds]
```

`target` is one of `run_synth.py`'s designs: `chacha20`, `poly1305`,
`chacha20_poly1305`, `oca_core`, `oca_dual`. `seeds` defaults to 4.

## What it does, and why each part matters

1. Runs `oca/hw/syn/run_synth.py` once per seed, from `oca/`.
2. Collects `TRELLIS_COMB`, `TRELLIS_FF`, `MULT18X18D`, `DP16KD` and
   Fmax from each run's `build/<target>.report.json`.
3. Prints the per-seed Fmax values, their mean, and the spread.
4. Prints the area once, not per seed: synthesis is deterministic, so
   area that differs between seeds means something is wrong.

**Output goes to a file the harness owns, not to the scratchpad.** A
long sweep once lost three of four seeds because its log lived in a
directory a concurrent agent had been told it could clean.

## Reading the result

**Area is deterministic; treat any difference as a defect in the
measurement.** Clock is not: report the mean over the sweep, and do not
claim a Fmax change smaller than the spread. If the sweep's own spread
is wider than 8%, say so: the design may have become harder to place,
which is itself the finding.

Compare against the figures in `AGENTS.md`'s status section. If they
disagree, stop and find out what changed rather than publishing the new
number. Note that "my RTL is unchanged" is not the same as "the netlist
is unchanged": builds that match on every per-type cell total have
placed differently here before. Naming the toolchain is the last step,
not the first.

**The flip-flop floors in `run_synth.py` are the point of running this
at all on a design with a key store.** If a `netlist check` line reports
FAILED, storage has vanished from the netlist and the design would build
and not work: that is the `cmp2lut` failure mode, and no simulation can
see it.

## After a sweep

If the numbers go into a document or a commit message, they carry the
seed count and the spread with them. A bare Fmax with no seed count is
not a measurement, it is an anecdote.
