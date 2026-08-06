#!/usr/bin/env bash
# Sweep one synthesis target across placer seeds and report the spread.
# See SKILL.md for why a single seed is not a measurement here.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
TARGET="${1:?usage: sweep.sh <target> [seeds]}"
SEEDS="${2:-4}"
REPORT="$ROOT/oca/hw/syn/build/${TARGET}.report.json"

cd "$ROOT/oca"

declare -a fmax=()
area=""
for ((s = 1; s <= SEEDS; s++)); do
    printf 'seed %d ... ' "$s" >&2
    if ! .venv/bin/python hw/syn/run_synth.py "$TARGET" --seed "$s" \
            >/tmp/synth-sweep-$$.log 2>&1; then
        echo "FAILED" >&2
        tail -20 /tmp/synth-sweep-$$.log >&2
        rm -f /tmp/synth-sweep-$$.log
        exit 1
    fi
    # The floors are the only thing standing between a mapper bug and a
    # netlist that builds and does not work. Never let a sweep pass one.
    if grep -q "FAILED" /tmp/synth-sweep-$$.log; then
        echo "NETLIST CHECK FAILED" >&2
        grep "netlist check" /tmp/synth-sweep-$$.log >&2
        rm -f /tmp/synth-sweep-$$.log
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
rm -f /tmp/synth-sweep-$$.log

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
if spread > 4.8:
    print("  NOTE: spread exceeds the documented 4.8% — the design may have"
          "\n        become harder to place. That is a finding, not noise.")
PY
