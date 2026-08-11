#!/usr/bin/env bash
# Sweep for build processes that outlived the work that started them.
#
# run_synth.py bounds anything it starts and kills the whole process
# group, so nothing that goes through it can end up here. This is the
# net under the cases that do not: a yosys invoked directly, a subagent
# that wrote its own timeout, an orphaned shell that relaunched a job
# after its children were killed. All three have happened.
#
# Runs at the end of a turn. Reports every project build process it
# finds, and kills the ones past the ceiling — a runaway can therefore
# survive at most one turn instead of an afternoon.
set -uo pipefail

ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"

# Past this, a build is not working, it is stuck. Deliberately above the
# largest bound run_synth.py will use so a legitimate long build reaches
# its own bound first and reports properly.
#
# 3600 until 2026-08-10, which was below the project's own flagship
# build: oca_top's synthesis measures 3941 s, so this net would have
# killed it 341 s from the end and called it a runaway. oca_top records
# a 7200 s bound; this sits above it.
CEILING_SECONDS=${OCA_BUILD_CEILING:-7500}

# Only ever this project's toolchain: never anything else the user is
# running.
TOOLS=$(readlink -f "$ROOT/tools" 2>/dev/null) || exit 0
[[ -n $TOOLS ]] || exit 0

# Identify by the executable /proc says is running, not by argv. The
# runaway this hook exists for was launched as `../tools/yosys/bin/yosys`
# from inside oca/, so its command line carried a relative path and any
# match against the absolute one missed it entirely. /proc/PID/exe is
# already resolved and cannot be spelled two ways.
rows=()
for d in /proc/[0-9]*; do
    pid=${d#/proc/}
    [[ $pid == "$$" ]] && continue
    exe=$(readlink -f "$d/exe" 2>/dev/null) || continue
    [[ $exe == "$TOOLS"/* ]] || continue
    read -r pgid secs < <(ps -o pgid=,etimes= -p "$pid" 2>/dev/null) || continue
    [[ -n ${secs:-} ]] || continue
    rows+=("$pid $pgid $secs $(basename "$exe")")
done
((${#rows[@]})) || exit 0

killed=0
report=""
for row in "${rows[@]}"; do
    read -r pid pgid secs prog <<<"$row"
    [[ $pid =~ ^[0-9]+$ ]] || continue
    if ((secs > CEILING_SECONDS)); then
        # The group, not the process. Killing the leader alone is what
        # left helpers running with nobody watching them.
        kill -TERM "-$pgid" 2>/dev/null
        sleep 2
        kill -KILL "-$pgid" 2>/dev/null
        killed=$((killed + 1))
        report+="  KILLED pid $pid (group $pgid) $prog, running ${secs}s"$'\n'
    else
        report+="  still running: pid $pid $prog, ${secs}s"$'\n'
    fi
done

if ((killed > 0)); then
    printf 'no-runaway-builds: killed %d process group(s) past the %ds ceiling.\n%s' \
        "$killed" "$CEILING_SECONDS" "$report" >&2
    printf 'Nothing they were doing produced a result. Do not simply restart them: find out why the bound was reached.\n' >&2
    exit 2
fi

# Nothing killed, but say what is alive so it is never a surprise.
printf 'no-runaway-builds: %d project build process(es) alive.\n%s' \
    "${#rows[@]}" "$report" >&2
exit 0
