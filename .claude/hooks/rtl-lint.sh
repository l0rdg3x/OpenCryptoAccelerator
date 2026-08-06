#!/usr/bin/env bash
# Lint the RTL after any edit to it. AGENTS.md requires -Wall to stay
# clean; this makes that requirement automatic rather than remembered.
#
# Reads the PostToolUse payload on stdin and does nothing unless the file
# edited was one of the SystemVerilog sources. Exits 2 on a lint failure
# so the message reaches the assistant rather than only the transcript.
set -uo pipefail

ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
VERILATOR="$ROOT/tools/verilator/bin/verilator"

path=$(jq -r '.tool_input.file_path // empty' 2>/dev/null) || exit 0
[[ $path == *"/oca/hw/rtl/"*.sv ]] || exit 0

# A toolchain that is not built is not a lint failure — say so once and
# get out of the way, rather than failing every edit.
[[ -x $VERILATOR ]] || {
    echo "rtl-lint: $VERILATOR not built; skipping (scripts/build-toolchain.sh)" >&2
    exit 0
}

out=$(cd "$ROOT/oca" && "$VERILATOR" --lint-only -Wall hw/rtl/*.sv \
        --top-module oca_core 2>&1)
rc=$?
if ((rc != 0)); then
    echo "rtl-lint FAILED (-Wall, top oca_core):" >&2
    echo "$out" >&2
    exit 2
fi
