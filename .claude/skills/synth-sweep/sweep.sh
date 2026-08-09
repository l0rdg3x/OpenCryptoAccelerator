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

cd "$ROOT/oca"

declare -a fmax=()
area=""
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
    read -r f a < <(python3 - "$REPORT" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
u = r.get("utilization", {})
f = max((d.get("achieved", 0.0) for d in r.get("fmax", {}).values()), default=0.0)
a = ",".join(f"{k}={u[k]['used']}" for k in sorted(u) if u[k].get("used"))
print(f"{f:.2f}", a)
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
    echo "$f MHz" >&2
done
rm -f "$LOG"

python3 - "$TARGET" "$area" "${fmax[@]}" <<'PY'
import sys
target, area, *vals = sys.argv[1:]
v = [float(x) for x in vals]
mean = sum(v) / len(v)
spread = (max(v) - min(v)) / min(v) * 100 if min(v) else 0.0
print(f"\n=== {target}: {len(v)} seeds ===")
for k in area.split(","):
    print(f"  {k}")
print(f"  Fmax  {' / '.join(f'{x:.2f}' for x in v)}")
print(f"  mean  {mean:.2f} MHz   spread {spread:.1f}%")
if spread > 8.0:
    print("  NOTE: spread exceeds the 7.4% the single core measures across"
          "\n        seeds on the pinned toolchain — the design may have"
          "\n        become harder to place. That is a finding, not noise.")
PY
