#!/usr/bin/env bash
# SPDX-License-Identifier: CERN-OHL-P-2.0
#
# Build the FPGA toolchain this project synthesises with, pinned to
# exact upstream revisions -- which are not all the revisions the
# published numbers were measured on. The yosys pin moved from
# 41a4b5a03 to f77ddfb87 on 2026-08-15; oca/hw/syn/README.md records,
# row by row, which figures were measured on which.
#
# tools/ is not committed, so a fresh clone has none of this. Everything
# is fetched into tools/src/<name> and installed into tools/<name>;
# nothing is installed system-wide. System libraries are used as found
# and never installed. Outside the repository it leaves a scratch
# directory under /tmp, removed on exit, pip's download cache, and one
# entry in ~/.cmake/packages/Eigen3/ -- written by eigen's own
# export(PACKAGE Eigen3) at configure time, pointing into
# tools/src/eigen/build and removed by nothing here.
#
# The pins are the point. A yosys older than the one pinned here
# silently deletes the key store from the netlist -- fixed upstream in
# f77ddfb87 and carried as a local patch before that -- and a toolchain
# that merely "works" is not enough: the probe at the end proves this
# yosys maps a signed comparison correctly before any result from it is
# trusted. The probe tests the behaviour, not the revision, which is
# why it outlived the patch.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# TOOLS is overridable so the fetch paths can be exercised against a
# throwaway prefix without touching a working toolchain.
TOOLS="${TOOLS:-${REPO_ROOT}/tools}"
SRC="${TOOLS}/src"
VENV="${REPO_ROOT}/oca/.venv"

readonly HELP2MAN_VERSION=1.49.3
readonly HELP2MAN_URL="https://ftp.gnu.org/gnu/help2man/help2man-${HELP2MAN_VERSION}.tar.xz"
readonly HELP2MAN_SHA256=4d7e4fdef2eca6afe07a2682151cea78781e0a4e8f9622142d9f70c083a2fd4f

readonly VERILATOR_URL=https://github.com/verilator/verilator.git
readonly VERILATOR_REV=3d2421f3bf8cda84b49d8f739e39bce73c93cc46   # v5.050

readonly EIGEN_URL=https://gitlab.com/libeigen/eigen.git
readonly EIGEN_REV=3147391d946bb4b6c68edd901f2add6ac1f31f8c       # 3.4.0

readonly TRELLIS_URL=https://github.com/YosysHQ/prjtrellis.git
readonly TRELLIS_REV=56bb17047cd8b062f784de8666ceb3f90f77f77a

readonly YOSYS_URL=https://github.com/YosysHQ/yosys.git
readonly YOSYS_REV=f77ddfb87534da157f399a37a02665ced59e0c61       # 0.68+, PR #6114

readonly NEXTPNR_URL=https://github.com/YosysHQ/nextpnr.git
readonly NEXTPNR_REV=8945407874c3031f13a5453598e9923268259698

readonly OPENFPGALOADER_URL=https://github.com/trabucayre/openFPGALoader.git
readonly OPENFPGALOADER_REV=85be4fa02b2dd6a83716d7dfac3d25bbd260ff7b   # v1.1.1

readonly COCOTB_URL=https://github.com/cocotb/cocotb.git
readonly COCOTB_REV=82d0eed5521349b74d6397ccbf138f15130bc9a2

readonly ALL=(help2man verilator eigen prjtrellis yosys nextpnr openfpgaloader cocotb)

MODE=build
JOBS="$(nproc)"
WANT=()

usage() {
    cat <<EOF
Usage: ${0##*/} [--check|--fetch-only] [--jobs N] [component ...]

  --check       report what is present and probe yosys; build nothing
  --fetch-only  fetch the top-level sources at the pinned revisions;
                build nothing. Submodules are not fetched here: yosys's
                abc and slang, and prjtrellis's database, are
                initialised by their builds
  --jobs N      parallel jobs (default: $(nproc))

Components, in dependency order: ${ALL[*]}
With none named, all are built. Sources go to tools/src, installs to
tools/<name>; the Python venv is oca/.venv.
EOF
}

die() { echo "${0##*/}: $*" >&2; exit 1; }
say() { echo "==> $*"; }

# One scratch directory for the whole run, released on exit. Not a RETURN
# trap, which bash keeps armed and fires again when any later function
# returns, by then out of scope; and not created on demand behind a
# function, because $(...) would make it in a subshell that deletes it
# on the way out.
SCRATCH=$(mktemp -d)
trap 'rm -rf "$SCRATCH"' EXIT

parse_args() {
    while (($#)); do
        case $1 in
        --check)      MODE=check ;;
        --fetch-only) MODE=fetch ;;
        --jobs)       [[ ${2:-} ]] || die "--jobs needs a number"
                      JOBS=$2; shift ;;
        -h|--help)    usage; exit 0 ;;
        -*)           usage >&2; die "unknown option: $1" ;;
        *)            [[ " ${ALL[*]} " == *" $1 "* ]] || die "unknown component: $1"
                      WANT+=("$1") ;;
        esac
        shift
    done
    ((${#WANT[@]})) || WANT=("${ALL[@]}")
}

wanted() { [[ " ${WANT[*]} " == *" $1 "* ]]; }

# Report every missing tool at once: finding them one build at a time
# wastes an hour of compiling to discover the second one.
preflight() {
    local missing=() c
    for c in git curl tar cmake ninja make g++ autoconf flex bison perl python3; do
        command -v "$c" >/dev/null || missing+=("$c")
    done
    ((${#missing[@]} == 0)) || die "missing build tools: ${missing[*]}
Install them with your distribution's package manager, then re-run.
This script never installs anything outside ${TOOLS}."
}

# Fetch a pinned commit without cloning history. GitHub and GitLab both
# serve an unadvertised SHA to a depth-1 fetch, so this costs seconds.
fetch_git() {
    local dir=$1 url=$2 rev=$3 shallow=${4:-shallow}
    if [[ -e $dir/.git ]] && [[ "$(git -C "$dir" rev-parse HEAD)" == "$rev" ]]; then
        return 0
    fi
    [[ -e $dir ]] && die "$dir exists but is not at $rev; move it aside and re-run"
    mkdir -p "$dir"
    if [[ $shallow == full ]]; then
        # Verilator derives its version string from `git describe`, so a
        # tagless shallow clone makes it report "rev vUNKNOWN".
        git clone --quiet "$url" "$dir"
        git -C "$dir" checkout --quiet "$rev"
    else
        git -C "$dir" init --quiet
        git -C "$dir" remote add origin "$url"
        git -C "$dir" fetch --quiet --depth 1 origin "$rev"
        git -C "$dir" checkout --quiet FETCH_HEAD
    fi
}

fetch_help2man() {
    local dir=$SRC/help2man tmp
    [[ -e $dir ]] && return 0
    tmp=$SCRATCH
    curl -fsSL "$HELP2MAN_URL" -o "$tmp/h.tar.xz"
    echo "${HELP2MAN_SHA256}  $tmp/h.tar.xz" | sha256sum -c --status \
        || die "help2man tarball failed its checksum"
    mkdir -p "$SRC"
    tar -xf "$tmp/h.tar.xz" -C "$tmp"
    mv "$tmp/help2man-${HELP2MAN_VERSION}" "$dir"
}

fetch_all() {
    wanted help2man   && { say "fetch help2man ${HELP2MAN_VERSION}"; fetch_help2man; }
    wanted verilator  && { say "fetch verilator ${VERILATOR_REV:0:9}"
                           fetch_git "$SRC/verilator" "$VERILATOR_URL" "$VERILATOR_REV" full; }
    wanted eigen      && { say "fetch eigen ${EIGEN_REV:0:9}"
                           fetch_git "$SRC/eigen" "$EIGEN_URL" "$EIGEN_REV"; }
    wanted prjtrellis && { say "fetch prjtrellis ${TRELLIS_REV:0:9}"
                           fetch_git "$SRC/prjtrellis" "$TRELLIS_URL" "$TRELLIS_REV"; }
    wanted yosys      && { say "fetch yosys ${YOSYS_REV:0:9}"
                           fetch_git "$SRC/yosys" "$YOSYS_URL" "$YOSYS_REV"; }
    wanted nextpnr    && { say "fetch nextpnr ${NEXTPNR_REV:0:9}"
                           fetch_git "$SRC/nextpnr" "$NEXTPNR_URL" "$NEXTPNR_REV"; }
    wanted openfpgaloader && { say "fetch openFPGALoader ${OPENFPGALOADER_REV:0:9}"
                           fetch_git "$SRC/openFPGALoader" "$OPENFPGALOADER_URL" "$OPENFPGALOADER_REV"; }
    return 0
}

build_help2man() {
    [[ -x $TOOLS/help2man/bin/help2man ]] && return 0
    say "build help2man"
    (cd "$SRC/help2man" \
        && ./configure --prefix="$TOOLS/help2man" \
        && make -j"$JOBS" \
        && make install)
}

build_verilator() {
    [[ -x $TOOLS/verilator/bin/verilator ]] && return 0
    say "build verilator"
    # verilator's Makefile runs help2man to generate its man page, so
    # help2man must already be built and on PATH.
    (cd "$SRC/verilator" \
        && PATH="$TOOLS/help2man/bin:$PATH" autoconf \
        && PATH="$TOOLS/help2man/bin:$PATH" ./configure --prefix="$TOOLS/verilator" \
        && PATH="$TOOLS/help2man/bin:$PATH" make -j"$JOBS" \
        && PATH="$TOOLS/help2man/bin:$PATH" make install)
}

build_eigen() {
    [[ -d $TOOLS/eigen/include/eigen3 ]] && return 0
    say "install eigen headers"
    # Header-only: CMAKE_BUILD_TYPE is deliberately unset.
    cmake -S "$SRC/eigen" -B "$SRC/eigen/build" \
        -DCMAKE_INSTALL_PREFIX="$TOOLS/eigen" -DBUILD_TESTING=OFF
    cmake --install "$SRC/eigen/build"
}

build_prjtrellis() {
    [[ -x $TOOLS/trellis/bin/ecppack ]] && return 0
    say "build prjtrellis"
    # The bitstream database is a submodule; without it ecppack has
    # nothing to pack against.
    git -C "$SRC/prjtrellis" submodule update --init --depth 1 database
    # The CMake project is libtrellis/, not the repository root.
    cmake -S "$SRC/prjtrellis/libtrellis" -B "$SRC/prjtrellis/libtrellis/build" \
        -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$TOOLS/trellis"
    cmake --build "$SRC/prjtrellis/libtrellis/build" -j "$JOBS"
    cmake --install "$SRC/prjtrellis/libtrellis/build"
}

# The installed revision, read out of the binary itself rather than out
# of the source tree it was built from: `yosys -V` prints
# "Yosys 0.68+ (git sha1 f77ddfb87, Release, GNU /usr/bin/c++ 16.1.1)".
yosys_installed_rev() {
    [[ -x $TOOLS/yosys/bin/yosys ]] || return 1
    "$TOOLS/yosys/bin/yosys" -V 2>/dev/null |
        sed -n 's/.*git sha1 \([0-9a-f]\{7,\}\).*/\1/p' | head -1
}

# yosys -V prints an abbreviated sha and YOSYS_REV is full length, so
# the two are compared over whichever is shorter. Not over a fixed
# width: git widens the abbreviation as a repository grows, and a
# comparison pinned to today's length would read the same revision as a
# different one.
rev_matches() {
    local a=$1 b=$2 n
    [[ -n $a && -n $b ]] || return 1
    n=${#a}
    if ((${#b} < n)); then n=${#b}; fi
    [[ ${a:0:n} == "${b:0:n}" ]]
}

build_yosys() {
    local rev
    rev=$(yosys_installed_rev || true)
    rev_matches "$rev" "$YOSYS_REV" && return 0
    # An installed yosys at another revision is neither skipped nor built
    # over: the same rule fetch_git applies to a source tree that is not
    # at its pin. Skipping is what made this a silent trap -- cmp2lut.v
    # as merged upstream is byte-identical to the patch this project
    # carried before it, so an installation from before the pin passes
    # the probe below and goes on measuring on the old revision with
    # nothing to say so.
    if [[ -n $rev ]]; then
        die "$TOOLS/yosys is at $rev, not ${YOSYS_REV:0:9}; move it aside and re-run"
    fi
    say "build yosys"
    # abc and the slang frontend are submodules; slang is what reads this
    # project's SystemVerilog, the Verilog-2005 frontend rejects it.
    git -C "$SRC/yosys" submodule update --init --depth 1 --recursive
    cmake -S "$SRC/yosys" -B "$SRC/yosys/build" -G Ninja \
        -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$TOOLS/yosys"
    cmake --build "$SRC/yosys/build" -j "$JOBS"
    cmake --install "$SRC/yosys/build"
}

build_nextpnr() {
    [[ -x $TOOLS/nextpnr/bin/nextpnr-ecp5 ]] && return 0
    say "build nextpnr-ecp5"
    # One chipdb, for the 45k on the Colorlight i9. Every other device
    # is build time and disk spent on a board this project does not have.
    cmake -S "$SRC/nextpnr" -B "$SRC/nextpnr/build" -G Ninja \
        -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$TOOLS/nextpnr" \
        -DARCH=ecp5 -DECP5_DEVICES=45k -DBUILD_PYTHON=OFF \
        -DTRELLIS_INSTALL_PREFIX="$TOOLS/trellis" \
        -DEigen3_DIR="$TOOLS/eigen/share/eigen3/cmake"
    cmake --build "$SRC/nextpnr/build" -j "$JOBS"
    cmake --install "$SRC/nextpnr/build"
}

build_openfpgaloader() {
    [[ -x $TOOLS/openFPGALoader/bin/openFPGALoader ]] && return 0
    say "build openFPGALoader"
    # libftdi1, hidapi, libusb, zlib and libudev are found through
    # pkg-config on the system as-is; none of them are built here.
    # `cmake --install` never touches /etc/udev — this project has no
    # CMake option to gate that, because it has no CMake-driven udev
    # rule install at all: 99-openfpgaloader.rules ships as a plain file
    # at the repository root and upstream's own docs have the operator
    # copy it by hand. So this build does the same as any other install
    # of this tool: to use it without sudo, run
    #   sudo cp tools/src/openFPGALoader/99-openfpgaloader.rules /etc/udev/rules.d/
    #   sudo udevadm control --reload-rules && sudo udevadm trigger
    # or invoke openFPGALoader itself with sudo.
    cmake -S "$SRC/openFPGALoader" -B "$SRC/openFPGALoader/build" \
        -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$TOOLS/openFPGALoader"
    cmake --build "$SRC/openFPGALoader/build" -j "$JOBS"
    cmake --install "$SRC/openFPGALoader/build"
}

# The installed commit, read from what pip recorded, not from a marker
# this script writes: a marker only proves the script ran once.
cocotb_installed_rev() {
    local f
    for f in "$VENV"/lib/python*/site-packages/cocotb-*.dist-info/direct_url.json; do
        [[ -f $f ]] || continue
        python3 - "$f" <<'PY'
import json, sys
print(json.load(open(sys.argv[1])).get("vcs_info", {}).get("commit_id", ""))
PY
        return 0
    done
    return 1
}

build_cocotb() {
    [[ "$(cocotb_installed_rev 2>/dev/null || true)" == "$COCOTB_REV" ]] && return 0
    say "install cocotb ${COCOTB_REV:0:9} into oca/.venv"
    # cocotb's PyPI releases refuse Python 3.14, so it comes from git —
    # pinned to a commit, because @master drifts under the pin silently.
    [[ -x $VENV/bin/python ]] || python3 -m venv "$VENV"
    "$VENV/bin/pip" install --quiet "cocotb @ git+${COCOTB_URL}@${COCOTB_REV}"
}

# The same probe run_synth.py refuses to synthesise without, kept
# identical to it on purpose: $signed(a) >= -8 is a tautology and must
# map to an all-ones LUT. A yosys older than the pin maps it to constant
# false, which deletes the key store and says nothing.
probe_yosys() {
    local yosys=$TOOLS/yosys/bin/yosys tmp out lut
    [[ -x $yosys ]] || { echo "yosys not built"; return 1; }
    tmp=$SCRATCH
    cat >"$tmp/probe.il" <<'RTLIL'
module \top
  wire width 4 input 1 \a
  wire output 2 \y
  cell $ge \c
    parameter \A_SIGNED 1
    parameter \B_SIGNED 1
    parameter \A_WIDTH 4
    parameter \B_WIDTH 4
    parameter \Y_WIDTH 1
    connect \A \a
    connect \B 4'1000
    connect \Y \y
  end
end
RTLIL
    out=$("$yosys" -q -p "read_rtlil $tmp/probe.il; \
techmap -map +/cmp2lut.v -D LUT_WIDTH=4; write_rtlil" 2>&1) || true
    lut=$(grep -oE "parameter \\\\LUT 16'[01]{16}" <<<"$out" | head -1)
    if [[ -z $lut ]]; then
        echo "cmp2lut probe: yosys did not map the comparison; cannot vouch"
        echo "for this toolchain."
        return 1
    fi
    if [[ ${lut: -16} != 1111111111111111 ]]; then
        echo "cmp2lut probe FAILED: \$signed(a) >= -8 mapped to 16'b${lut: -16},"
        echo "expected all ones. This yosys mis-synthesises signed comparisons"
        echo "against negative constants and will delete the key store."
        echo "The fix is upstream in yosys f77ddfb87 (PR #6114, 2026-08-14),"
        echo "so a yosys older than that is the cause. Check that"
        echo "$SRC/yosys is at $YOSYS_REV, then move $TOOLS/yosys aside and"
        echo "re-run: a build refuses to install over a yosys at another"
        echo "revision rather than leave the old binary standing. If both are"
        echo "already at the pin, $TOOLS/yosys/share/yosys/cmp2lut.v is stale"
        echo "— it is read at run time, so copying the source file over it is"
        echo "enough."
        return 1
    fi
}

check() {
    local ok=0 rev
    report() { if [[ -e $2 ]]; then echo "  present  $1"; else echo "  MISSING  $1"; ok=1; fi; }
    echo "toolchain under $TOOLS"
    report help2man       "$TOOLS/help2man/bin/help2man"
    report verilator      "$TOOLS/verilator/bin/verilator"
    report eigen          "$TOOLS/eigen/include/eigen3"
    report prjtrellis     "$TOOLS/trellis/bin/ecppack"
    # yosys by revision and not by presence, the way cocotb is reported
    # below. The probe at the end tests the mapping, and the mapping is
    # right on an installation from before the pin too, so a green probe
    # is no evidence of which revision the figures came from.
    rev=$(yosys_installed_rev || true)
    if rev_matches "$rev" "$YOSYS_REV"; then
        echo "  present  yosys ${YOSYS_REV:0:9}"
    elif [[ -x $TOOLS/yosys/bin/yosys ]]; then
        echo "  DRIFTED  yosys is at ${rev:-an unreadable revision}, pinned at ${YOSYS_REV:0:9}"
        ok=1
    else
        echo "  MISSING  yosys"
        ok=1
    fi
    report nextpnr-ecp5   "$TOOLS/nextpnr/bin/nextpnr-ecp5"
    report openFPGALoader "$TOOLS/openFPGALoader/bin/openFPGALoader"

    rev=$(cocotb_installed_rev 2>/dev/null || true)
    if [[ $rev == "$COCOTB_REV" ]]; then
        echo "  present  cocotb ${COCOTB_REV:0:9}"
    elif [[ -n $rev ]]; then
        echo "  DRIFTED  cocotb is at ${rev:0:9}, pinned at ${COCOTB_REV:0:9}"
        ok=1
    else
        echo "  MISSING  cocotb"
        ok=1
    fi

    if [[ -x $TOOLS/yosys/bin/yosys ]]; then
        if probe_yosys; then
            echo "  ok       yosys cmp2lut probe"
        else
            ok=1
        fi
    fi
    return $ok
}

versions() {
    say "installed"
    [[ -x $TOOLS/help2man/bin/help2man ]] &&
        echo "  $("$TOOLS/help2man/bin/help2man" --version | head -1)"
    [[ -x $TOOLS/verilator/bin/verilator ]] &&
        echo "  $("$TOOLS/verilator/bin/verilator" --version | head -1)"
    [[ -x $TOOLS/yosys/bin/yosys ]] &&
        echo "  $("$TOOLS/yosys/bin/yosys" -V | head -1)"
    [[ -x $TOOLS/nextpnr/bin/nextpnr-ecp5 ]] &&
        echo "  $("$TOOLS/nextpnr/bin/nextpnr-ecp5" --version 2>&1 | head -1)"
    [[ -x $TOOLS/trellis/bin/ecppack ]] &&
        echo "  $("$TOOLS/trellis/bin/ecppack" --version 2>&1 | head -1)"
    [[ -x $TOOLS/openFPGALoader/bin/openFPGALoader ]] &&
        echo "  $("$TOOLS/openFPGALoader/bin/openFPGALoader" --Version 2>&1 | head -1)"
    return 0
}

main() {
    parse_args "$@"

    [[ $MODE == check ]] && { check; return $?; }

    preflight
    fetch_all
    [[ $MODE == fetch ]] && { say "sources ready under $SRC"; return 0; }

    wanted help2man   && build_help2man
    wanted verilator  && build_verilator
    wanted eigen      && build_eigen
    wanted prjtrellis && build_prjtrellis
    wanted yosys      && build_yosys
    wanted nextpnr    && build_nextpnr
    wanted openfpgaloader && build_openfpgaloader
    wanted cocotb     && build_cocotb

    if wanted yosys; then
        probe_yosys || die "the yosys just built fails the cmp2lut probe"
        say "yosys cmp2lut probe ok"
    fi
    versions
}

main "$@"
