#!/usr/bin/env bash
# Sweep one synthesis target across placer seeds and report the spread.
# See SKILL.md for why a single seed is not a measurement here.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
TARGET="${1:?usage: sweep.sh <target> [seeds]}"
SEEDS="${2:-4}"
REPORT="$ROOT/oca/hw/syn/build/${TARGET}.report.json"
# The transient per-seed log lives in the syn build dir, not /tmp: a
# shared scratch directory is exactly where a sweep once lost three of
# four seeds to a concurrent cleanup.
LOG="$ROOT/oca/hw/syn/build/.sweep-$$.log"
# run_synth.py only creates this directory once it starts, inside
# main(), after bash has already tried to open the redirect below.
mkdir -p "$(dirname "$LOG")"

cd "$ROOT/oca"

declare -a fmax=()
area=""
clk=""
for ((s = 1; s <= SEEDS; s++)); do
    printf 'seed %d ... ' "$s" >&2
    if ! .venv/bin/python hw/syn/run_synth.py "$TARGET" --seed "$s" \
            >"$LOG" 2>&1; then
        echo "FAILED" >&2
        tail -20 "$LOG" >&2
        rm -f "$LOG"
        exit 1
    fi
    # The floors are the only thing standing between a mapper bug and a
    # netlist that builds and does not work. Never let a sweep pass one.
    if grep -q "FAILED" "$LOG"; then
        echo "NETLIST CHECK FAILED" >&2
        grep "netlist check" "$LOG" >&2
        rm -f "$LOG"
        exit 1
    fi
    # Rounded in python: this locale uses a decimal comma and bash
    # printf refuses a dotted number outright.
    #
    # The clock reported is the one with the least margin over its own
    # constraint, not the fastest. On a design with a PLL the report
    # carries a key per clock, and the fastest of them is the pad into
    # the PLL: oca_top_mac reads 542.59 MHz on clk25 beside 135.19 on
    # clk_sys, so max() would have called a 48 MHz design a 542 MHz one.
    # The name is printed with the number because on more than one clock
    # a bare figure says nothing about which.
    read -r f c a < <(python3 - "$REPORT" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
u = r.get("utilization", {})
fm = r.get("fmax", {})
best, name = 0.0, "-"
ratio = None
for k, d in fm.items():
    ach, con = d.get("achieved", 0.0), d.get("constraint", 0.0) or 0.0
    rt = ach / con if con else float("inf")
    if ratio is None or rt < ratio:
        ratio, best, name = rt, ach, k
a = ",".join(f"{k}={u[k]['used']}" for k in sorted(u) if u[k].get("used"))
print(f"{best:.2f}", name, a)
PY
)
    fmax+=("$f")
    if [[ -z $area ]]; then area=$a
    elif [[ $area != "$a" ]]; then
        echo >&2
        echo "AREA CHANGED BETWEEN SEEDS — synthesis should be deterministic." >&2
        echo "  seed 1: $area" >&2
        echo "  seed $s: $a" >&2
        exit 1
    fi
    echo "$f MHz on $c" >&2
    if [[ -z $clk ]]; then clk=$c
    elif [[ $clk != "$c" ]]; then
        echo >&2
        echo "BINDING CLOCK CHANGED BETWEEN SEEDS — seed 1 reported $clk," >&2
        echo "seed $s reports $c. The sweep is no longer comparing one" >&2
        echo "clock across seeds, so its mean and spread mean nothing." >&2
        exit 1
    fi
done
rm -f "$LOG"

python3 - "$TARGET" "$clk" "$area" "${fmax[@]}" <<'PY'
import sys
target, clk, area, *vals = sys.argv[1:]
v = [float(x) for x in vals]
mean = sum(v) / len(v)
spread = (max(v) - min(v)) / min(v) * 100 if min(v) else 0.0
print(f"\n=== {target}: {len(v)} seeds ===")
for k in area.split(","):
    print(f"  {k}")
print(f"  clock {clk}")
print(f"  Fmax  {' / '.join(f'{x:.2f}' for x in v)}")
print(f"  mean  {mean:.2f} MHz   spread {spread:.1f}%")
if spread > 8.0:
    print("  NOTE: spread exceeds every seed spread recorded on the"
          "\n        committed design (widest 7.3%, 2026-08-15) - it"
          "\n        may have become harder to place. That is a finding,"
          "\n        not noise.")
PY
