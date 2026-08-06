#!/usr/bin/env bash
# Delete hw/sim/__pycache__ after any edit to a testbench.
#
# AGENTS.md carries this as a hard rule because it has already cost a
# debugging session: Python invalidates a cached .pyc by size and mtime,
# so an edit of the same size reverted within one second keeps executing
# the stale bytecode. The symptom is a test that reports behaviour the
# source cannot produce, and nothing about it looks like a caching
# problem.
set -uo pipefail

ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"

path=$(jq -r '.tool_input.file_path // empty' 2>/dev/null) || exit 0
[[ $path == *"/oca/hw/sim/"*.py ]] || exit 0

rm -rf "$ROOT/oca/hw/sim/__pycache__"
