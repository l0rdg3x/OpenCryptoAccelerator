# AGENTS.md — OpenCrypto Accelerator (OCA)

Open-source FPGA crypto accelerator. Canonical project specification:
`SPEC.md` (read it first — it drives all phase work).
MVP development BOM: `BOM-MVP.md`.

## Project shape

- **Phase 1 (done)**: abstract crypto API in C11 + OpenSSL 3 software
  backend + official test vectors + benchmarks. In `oca/`.
- **Phase 2 (in progress)**: FPGA cores in SystemVerilog, verified with
  cocotb + Verilator against the same official vectors. In `oca/hw/`.
- **MVP target**: Lattice ECP5 (Colorlight i9 v7.2), host interface over
  the board's DAPLink USB serial (J17/H18, 115200 8N1, `/dev/ttyACM0`),
  open toolchain (yosys/nextpnr-ecp5). This read "+ GbE" until
  2026-08-12: the board has no RJ45 socket and the die has no SERDES, so
  it is a vehicle for proving the core on silicon and not a prototype of
  the product (`SPEC.md`, PHASE 2). PCIe phase later, on other hardware:
  Artix-7 + LitePCIe (Vivado is the documented exception to the
  open-toolchain rule).
- Linux/WireGuard integration via kernel crypto API — **no kernel
  patches**. FreeBSD if_wg needs a minimal patch or is out of scope v1.

## Repository layout

```
SPEC.md                     project specification
BOM-MVP.md                  MVP development bill of materials
docs/STATUS.md              where the project stands, one page
docs/design/                design records, dated
scripts/build-toolchain.sh  builds everything under tools/
oca/                        the code
  include/oca/oca.h         public API
  src/                      API + OpenSSL backend
  tests/                    test_vectors.c + vectors/
  tests/vectors/sources/    official vector sources (RFC texts, KATs, wycheproof)
  tests/vectors/gen_vectors.py  generates vectors.h from sources/
  bench/                    benchmark harness
  hw/rtl/                   SystemVerilog cores (SPDX CERN-OHL-P-2.0)
  hw/sim/                   cocotb testbenches + runners (SPDX MIT)
  hw/syn/                   ECP5 synthesis flow + results (SPDX MIT)
  hw/host/                  host-side test tooling, not the driver (SPDX MIT)
  .venv/                    python venv (cocotb) — NOT committed
tools/                      local tool builds — NOT committed
  verilator/                Verilator 5.050 install (built from source, branch stable)
  help2man/                 help2man install (needed to build Verilator)
  yosys/                    yosys 0.68+ install (CMake build, slang frontend)
  trellis/                  prjtrellis install (ECP5 database + ecppack)
  nextpnr/                  nextpnr-ecp5 install (45k chipdb only)
  eigen/                    Eigen 3.4.0 headers (nextpnr analytic placer)
  openFPGALoader/           openFPGALoader v1.1.1 (every bring-up step ends in it)
  src/                      upstream sources for the above (shallow clones)
```

Paths that are not here because the code is gone: the Ethernet route's
history — the vendored stack, why it was taken as a dependency and what
its removal left behind — is in `docs/RECORD.md`.

## Environment rules

- **No system-wide installs without explicit permission.** Everything
  lives under this directory (`tools/`, `oca/.venv/`).
- The whole toolchain is built by `scripts/build-toolchain.sh`, pinned to
  exact revisions — the ones it builds today, which since the yosys bump
  of 2026-08-15 are no longer the ones every number in this repository
  was measured on (`oca/hw/syn/README.md`, "Results"):

  ```sh
  scripts/build-toolchain.sh --check   # what is present, and probe yosys
  scripts/build-toolchain.sh           # build all of it, hours
  scripts/build-toolchain.sh yosys     # or one component
  ```

  It fetches into `tools/src/<name>` and installs into `tools/<name>`.
  It never installs a system package: missing build tools are reported
  together and it stops. Outside the repository it leaves three things,
  none of them a package: a scratch directory under `/tmp` removed on
  exit, pip's download cache, and one entry in the user's CMake package
  registry (`~/.cmake/packages/Eigen3/`), which eigen's own
  `export(PACKAGE Eigen3)` writes at configure time and nothing removes
  — it points into `tools/src/eigen/build` and outlives it.

- `tools/` is gitignored, so the script is the only record of how to get
  one. **A yosys older than `f77ddfb87` silently deletes the key store
  from the netlist**: it mis-maps a signed comparison against a negative
  constant, which is what this project's index bounds checks are. That
  was carried as a local patch here until 2026-08-15 and is now upstream
  (PR #6114). Both `--check` and `run_synth.py` still prove the result
  with a behavioural probe rather than trusting the pin: `$signed(a) >=
  -8` is a tautology and must map to an all-ones LUT.
  `techlibs/common/cmp2lut.v` is read at run time from
  `tools/yosys/share/yosys/`, so a stale copy there defeats a correct
  build and is repaired by copying the source file over it, no rebuild.

- Pins, for reference: help2man 1.49.3, Verilator `3d2421f3` (v5.050),
  Eigen `3147391d` (3.4.0), prjtrellis `56bb1704`, yosys `f77ddfb87`
  (0.68+, with submodules abc `0bd9c3ea`, slang `e222e7dc` and sv-elab
  `ce388355` at `frontends/slang/lib`, which is what implements
  `read_slang` — all three pinned by the yosys revision and moved by a
  rebuild with it),
  nextpnr `89454078`, openFPGALoader `85be4fa0` (v1.1.1),
  cocotb `82d0eed5`. cocotb comes from git because its PyPI releases
  (<= 2.0.1) reject Python >= 3.14, and it is pinned to a commit rather
  than `@master`, which drifts under the pin without saying so.
  openFPGALoader links libftdi1, hidapi, libusb, zlib and libudev from
  the system through pkg-config. **It does not install
  `99-openfpgaloader.rules`**, and there is no CMake option to ask it
  to: upstream has no CMake-driven udev rule install at all, and its own
  documentation has the operator copy the file to `/etc/udev/rules.d/`
  by hand. So a JTAG cable is reachable either after copying that rule
  or by running the tool with sudo.

- Developed on CachyOS with Python 3.14. System dependencies are used as
  found and never installed: boost 1.91, ICU 78, tcl 8.6, readline,
  ncursesw, libffi, system fmt 12, zlib, bzip2, lzma, zstd, jemalloc,
  libatomic, and a C++20 compiler.

- The `tools/verilator` and `tools/help2man` in this working copy were
  not built here — they are copies of artefacts from another project on
  the same machine, which is why `verilator_bin` carries that project's
  path internally. The Perl wrapper resolves `VERILATOR_ROOT` from its
  own location, so the suites are unaffected; a toolchain built by the
  script does not have the discrepancy.

## How to build and test

Software (Phase 1), from `oca/`:

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
ctest --test-dir build          # one test, and it wraps 126 checks
./build/test_vectors            # prints the 126/126 that must pass
./build/oca_bench               # benchmarks
```

RTL (Phase 2), from `oca/`:

```sh
.venv/bin/python hw/sim/run_chacha20.py           # 5/5 pass, + 5 at ROUNDS_PER_CYCLE=2
.venv/bin/python hw/sim/run_poly1305.py           # 4/4 pass, + 4 at ROWS_PER_CYCLE=5
.venv/bin/python hw/sim/run_chacha20_poly1305.py  # 7/7 pass
.venv/bin/python hw/sim/run_dirty_pad.py          # 2/2 pass
.venv/bin/python hw/sim/run_secret_zeroise.py     # 2/2 pass
.venv/bin/python hw/sim/run_keystore.py           # 4/4 pass
.venv/bin/python hw/sim/run_pktbuf.py             # 12/12 pass, + 3 at BYTES=16
.venv/bin/python hw/sim/run_oca_core.py           # 29/29 pass
.venv/bin/python hw/sim/run_attack.py             # 16/16 pass
.venv/bin/python hw/sim/run_clkrst.py             # 7/7 pass
.venv/bin/python hw/sim/run_console.py            # 8/8 pass
.venv/bin/python hw/sim/run_fifo.py               # 4/4 pass
.venv/bin/python hw/sim/run_uart_rx.py            # 4/4 pass
.venv/bin/python hw/sim/run_uart_tx.py            # 5/5 pass
.venv/bin/python hw/sim/run_uart_console.py       # 4/4 pass
.venv/bin/python hw/sim/run_uart_echo.py          # 3/3 pass
.venv/bin/python hw/sim/run_slip_rx.py            # 12/12 pass, + 12 at BYTES=64
.venv/bin/python hw/sim/run_slip_tx.py            # 7/7 pass
.venv/bin/python hw/sim/run_uart_crypto.py        # 5/7 pass + 2 skip, then 7/7 at LED_BITS=8
.venv/bin/python hw/sim/run_keystore_gate.py      # 4/4 pass, post-synthesis
.venv/bin/python hw/sim/run_proto_gate.py         # 2/2 pass, post-synthesis
```

That is every RTL runner in the repository, and the list has to be
complete because **there is no aggregate RTL runner here at all** —
`oca/CMakeLists.txt` registers one ctest and it is the C vectors binary,
so every suite is invoked by name and a suite missing from this list is
a suite nobody runs.

**The 21 cocotb runners alone measure 148 tests over 177 passing executions**,
six of them on a synthesised netlist from the two gate runners. Twenty-nine tests
run a second time at a non-default parameter — five for `chacha20` at
`ROUNDS_PER_CYCLE` = 2, four for `poly1305` at `ROWS_PER_CYCLE` = 5,
three for `oca_pktbuf` at the smallest `BYTES` it accepts, all twelve of
`oca_slip_rx` at `BYTES` = 64 and five of `oca_uart_crypto` at
`LED_BITS` = 8 — which is what separates the two figures: 142 tests
outside the gate runners, plus 29 re-runs, plus their 6.

Beyond the cocotb runners, four more things here run and can fail, and
none of them is inside the 148 or the 177: **46 tests in `hw/host/`, 4
in `hw/syn/test_run_synth.py`, 2 in `hw/sim/test_proto_model.py`** —
measured 2026-08-13, all sub-second, none needing Verilator, yosys or a
board — and the C backend's **126 known-answer checks** behind the
single ctest above. The 6-step `cli.py --fake selftest` carries an
exit-code contract as well.

**This file gives each population and no grand total**, because there
is no unit they share: a simulator test case, a simulator execution, a
Python test case, a KAT check and a selftest step are five different
things, and a single headline number would have to pick one and
mislead about the rest. That is how "148 tests" came to be read as the
repository's total in three documents at once.

Measured by running every one of them on 2026-08-12, after the Ethernet
removal: **177 passing executions, no failures, and every runner exits
0.** Two tests skip — `oca_uart_crypto`'s heartbeat pair, which needs
`LED_BITS` small enough to simulate and so runs only on the second
build, exactly as `oca_blink`'s does.

**This read 222 executions over 25 runners, and 183 tests, until that
removal** — 177 of those tests over the 23 runners that are not the gate
pair, plus the 6 on a netlist, which is how the old figure was written
and why the coincidence of that 177 with today's 177 executions is worth
naming, so nobody reads a stale figure as a current one. Deleted with
the route: `run_rgmii` (10 tests, 10 executions), `run_udp_seam` (10
tests, 20 executions, running twice at two `HDR_Q_DEPTH` values),
`run_eth_mac` (8) and `run_oca_path` (7) — 35 tests and 45 executions.
So 222 − 45 = 177 executions, and 183 − 35 = 148 tests over 21 runners.
Nothing in the tree needs `vendor_patches.py build` any more; that
script and the tree it patched are gone.

**Two before-figures are both true and they differed by a
precondition.** `run_eth_mac` and `run_oca_path` built from the patched
vendor tree at `oca/hw/vendor/build/`, which `oca/.gitignore` excludes:
it was present only where `vendor_patches.py build` had already run, and
absent from a fresh checkout or a new worktree. Where it was present all
four Ethernet suites passed and `main` read **222 executions over 25
runners**. Where it was absent those two refused to build and exited
non-zero, and the same `main` read **207 executions over the 23
producing runners** — the figure this file carried until 2026-08-12.
Neither was wrong; only one of them was the whole tree. **Neither
reproduces on `main` today**: the four runners, the vendor tree and
`vendor_patches.py` were all deleted on 2026-08-12, so reaching either
figure means checking out `fd3059c` first.

This count read **123** until 2026-08-12, and that gap was never
arithmetic: the sum was exactly right over the fourteen suites it named,
and six suites were missing from the list. The console and UART chain —
`run_console` 8, `run_fifo` 4, `run_uart_console` 4, `run_uart_echo` 3,
`run_uart_rx` 4, `run_uart_tx` 5, 28 tests — was written on 2026-08-11
and appeared in no document at all, while being the only host channel
the board has. The serial bridge and the crypto console add 26 more:
`run_slip_rx` 12, `run_slip_tx` 7, `run_uart_crypto` 7. The list above
now carries all of them, which is the actual fix.

**A parameter with one tested value is a parameter that does not work.**
Both of these switch the datapath rather than sizing it, and both went
untested outside their default until 2026-08-09. What the second value
catches is not hypothetical: mutating `poly1305.sv`'s `CSH` to a
constant is a no-op at one value and wrong at the other, in both
directions, and each mutation is caught only by the run it breaks.

`run_keystore_gate.py` and `run_proto_gate.py` are the only suites that
run on a synthesised netlist rather than on the RTL; they exist because
everything above them is blind to synthesis. The first replays the key
store's own tests on a mapped `oca_keystore`; the second maps
`oca_proto` and drops it into an otherwise unmodified `oca_core`, then
replays a round trip and the sixteen tag bytes — the tag comparison is
combinational, so the flip-flop floors in `run_synth.py` cannot see it
and only a simulation of the cells can. The whole core cannot be
replayed that way, for two different reasons. `MULT18X18D` is declared
only in `cells_bb.v`, which these runners do not read, so a netlist
carrying `poly1305`'s multipliers will not elaborate in Verilator at
all. The packet buffers fail more quietly: they map through
`$__PDPW16KD_` but emit `DP16KD`, which `cells_sim.v` does declare, as a
blackbox with no behaviour — that netlist elaborates and the memory
reads nothing, so the run would be green over a buffer that never
returns a byte. `oca_proto` infers neither primitive.

`run_attack.py` drives the same DUT as `run_oca_core.py` but from the
other side: its tests are written to break the four-stage overlap
rather than to confirm it, and several of them watch oca_proto's
internal registers instead of the wire, because a descriptor field
moving under a pending hand-off is invisible to any payload assertion
until the traffic happens to hit the alignment that exposes it.

The protocol model has no DUT, so it runs as plain Python rather than
through a simulator — pulling Verilator into a pure Python check would
be noise:

```sh
cd hw/sim && ../../.venv/bin/python test_proto_model.py   # prints "proto_model: OK"
```

The suites that need neither simulator nor RTL toolchain. They are
sub-second, so there is no excuse for skipping them:

```sh
.venv/bin/python -m pytest -q hw/host                  # 46 pass, host link + fake board
.venv/bin/python -m pytest -q hw/syn/test_run_synth.py # 4 pass, yosys and nextpnr argv shapes
.venv/bin/python hw/host/cli.py --fake selftest        # 6/6 steps, in-process fake, no wire
```

`run_dirty_pad.py` is separate from the official-vector suite on
purpose: it drives random garbage into the bytes past `in_len` where
that suite zero-pads, which is the only way to see the engine's padding
mask on the **decrypt** path. Measured 2026-08-06 with the mask removed
entirely: `run_dirty_pad.py` fails both its tests and
`run_chacha20_poly1305.py` fails **3 of its 7** — exactly the three that
encrypt, because on encryption the padding is XORed with keystream
before it reaches Poly1305 and is therefore not zero. **The decrypt
tests all still pass**, since the zero padding the testbench supplies
goes into Poly1305 unchanged whether it is masked or not
(`oca/hw/syn/README.md`, "After the area pass").

Lint (must stay clean, `-Wall`):

```sh
../tools/verilator/bin/verilator --lint-only -Wall hw/rtl/*.sv --top-module oca_core
```

ECP5 synthesis (Phase 2, from `oca/`), see `hw/syn/README.md`:

```sh
.venv/bin/python hw/syn/run_synth.py chacha20_poly1305   # ~2 min
.venv/bin/python hw/syn/run_synth.py oca_core            # 4-10 min
```

The `oca_core` figure is routing, and it varies: 4 min 14 s for the run
behind the current numbers, 519 s of `Router1 time` alone for the one
before it. The "~3 min" this line used to say was measured on the 8-bit
core and never updated as the design grew by 3000 LUTs.

## Hard rules

- **Test vectors are parsed from official sources in
  `tests/vectors/sources/` — never hand-typed expected values.**
  RFC hexdump pitfalls already hit twice: the ASCII gutter can start
  with hex-looking characters (cap at 16 bytes/line) and page breaks
  interrupt dumps. If a vector fails, suspect the parser before the
  implementation, and cross-check with an independent implementation
  (OpenSSL CLI / Python reference) before touching RTL.
- Licenses: software MIT/Apache-2.0, RTL CERN-OHL-P-2.0 (SPDX header in
  every file), hardware docs CERN-OHL-P v2.
- **Two frontends, and the split is not symmetric.** yosys reads these
  cores with `read_slang`, not `read_verilog -sv`: the Verilog-2005
  frontend rejects functions with `return` and concatenation
  assignments, which the RTL uses throughout. **Third-party Verilog goes
  through `read_verilog`, never slang.** Slang did not infer the
  memories of the one vendored design this was measured on and spilled
  them into logic — 38x the area on a FIFO, 10.6x on a MAC, and no
  block RAM at all — so a vendored design read through slang measures far
  too large and gets abandoned for the wrong reason. A design mixing the
  two reads each side with its own frontend. **Which counter produced
  those figures was never recorded**, so the absolute numbers are not
  quotable at all; and the spread between 38x and 10.6x is itself wide
  enough that the two pairs were probably not measured the same way, so
  the ratio is an order-of-magnitude warning and nothing finer
  (`docs/RECORD.md`, "Third-party Verilog goes through `read_verilog`").
- **Never invoke `yosys` or `nextpnr-ecp5` directly. Go through
  `hw/syn/run_synth.py`**, which bounds every stage with a hard
  wall-clock timeout and kills the whole process group when one is hit.
  A build that has produced nothing after half an hour will not produce
  anything by carrying on, and a synthesis nobody is watching is worse
  than no synthesis: it saturates a core and hides whether anything is
  progressing. This has happened twice — a stalled yosys that outlived
  the agent that started it, and a caller that wrote its own two-hour
  timeout and then had its orphaned shells relaunch the job after its
  children were killed. If a build genuinely needs longer, raise it with
  `--timeout`; do not route around the bound. The `Stop` hook in
  `.claude/hooks/no-runaway-builds.sh` is the net under the cases that
  bypass this anyway: it reports every live build at the end of a turn
  and kills anything past an hour, identifying processes by
  `/proc/PID/exe` because the command line may carry a relative path.
- cocotb gotchas: runner import is `cocotb_tools.runner` on cocotb 2.x
  (fallback from `cocotb.runner`); when polling a DUT status signal in
  a loop, `await RisingEdge` **before** reading — reading right after
  the edge that consumed your stimulus returns the stale value.
- **An AXI-Stream driver samples the handshake before the transfer
  edge, not after it**: `await ReadOnly()`, read `tready`, then
  `await RisingEdge` — and only advance to the next byte if `tready`
  was high, holding `tdata`/`tvalid`/`tlast` stable until it is.
  Polling `tready` after the edge reads what the slave offered
  *before* the transfer, which is the stale read above wearing a
  different hat, and RTL adapted to such a source grows a `tready`
  that outlives the state which consumes the byte: against a
  conforming master that silently drops one byte per packet, with
  nothing in simulation to show it. Cost the `s_tready` rework of
  2026-08-03.
- **Proposals from per-file analysis are not additive.** The area pass
  of 2026-08-03 came out of a workflow that read the three RTL files
  independently, and two of its proposals — an early `blk_ready` in
  `poly1305.sv`, a combinational `p_blk` in the wrapper — were each
  correct against the file their author had read and silently corrupted
  the authentication tag when combined: both attacked the same one-cycle
  handshake bubble, each assuming the *other* side kept its signal
  registered. Per-file review cannot see this by construction, and the
  official-vector suite is not a safety net for a data-dependent
  handshake failure. Before applying two proposals that touch opposite
  ends of one handshake, measure the combination; never infer it from
  the parts.
- When mutating a Python model or testbench to prove a test can fail,
  delete `hw/sim/__pycache__` before re-running. Python invalidates its
  cache on mtime and size at one-second granularity, so a same-size edit
  reverted within the same second keeps executing the mutated bytecode —
  the restore looks broken, or worse, a later run silently keeps the
  mutation. Cost one confusing debug session on 2026-08-03.
- **A green simulation says nothing about the netlist.** Verilator
  elaborates the SystemVerilog directly and never runs yosys, so a
  synthesis bug is invisible to every suite we have. Yosys before
  `f77ddfb87` mis-maps signed comparisons against negative constants in
  `techlibs/common/cmp2lut.v`, which `synth_ecp5` runs unconditionally;
  it deleted the entire key store from `oca_core` — 2048 key bits, 8
  loaded bits — while all 72 tests stayed green and the build reported
  success. Reported as YosysHQ/yosys#6085 on 2026-08-05, carried here as
  a local patch, and fixed upstream by PR #6114 merged 2026-08-14; the
  pin moved to it on 2026-08-15 and the patch is gone. **The probe is
  not**: `run_synth.py` still refuses to synthesise on a toolchain that
  fails it, because what has to hold is the behaviour, not the revision
  number. The lesson generalises:
  whenever correctness depends on something only synthesis decides,
  assert it against the netlist in `NETLIST_FF_FLOOR`, because no test
  in `hw/sim/` can. Cost the MVP bitstream; see `hw/syn/README.md`.
- **An undriven input is not an input reading zero.** yosys may treat it
  as don't-care and take whichever value deletes the most logic: an
  unconnected `clear_arp_cache` on a vendored instance was read as "the
  cache is permanently clearing" and took that cache's ports and all of
  its storage out of the netlist — zero live flip-flops attributed to
  `arp_cache.v` — while the design elaborated, linted, placed and closed
  its clock. Two more unconnected ports on the same instance stopped
  reception after one non-UDP frame — found in simulation, on a path
  that never ran on hardware; the yosys log carried the warning among
  forty and nothing gated on it. Connect every port of a
  third-party module deliberately, and prove the storage is there with a
  netlist census, not with a lint pass (`docs/RECORD.md`, "What
  happened, and it is neither a lost seed nor a cost").
- **A test runner's exit code is the contract, not its log.** cocotb's
  `runner.test()` only inspects `results.xml` under pytest, and
  Verilator exits 0 on `$finish` even with red tests — every cocotb
  runner in `hw/sim/` once returned 0 whatever the tests did, and
  anything driving the suites by exit code would have called a red
  suite green. (`run_pktbuf.py` could already exit non-zero, but only
  from its elaboration guard, never from a result; `run_synth.py` is
  not a cocotb runner and always propagated its failures.) They now
  parse
  `results.xml` and exit 1 on any failure (and on no tests at all: a
  suite that ran nothing is not a pass). Prove the check by mutation —
  a deliberately red test must exit non-zero — not by reading the code.
  Found by audit on 2026-08-09.
- **Pointer and length are validated as a pair, for every pointer, at
  the API boundary.** A NULL with a non-zero length is
  `OCA_ERR_INVALID_ARG`, never a silent empty input: `aad = NULL` with
  `aad_len > 0` produced a valid tag covering no AAD at all, and no
  vector caught it because none passes the pair — `in` had its guard,
  `aad` did not. A new pointer argument to the C API arrives with its
  `(!p && len)` guard and its bad-args test in the same change. Same
  audit.
- **A correction edits the figure in place, everywhere it stands, in
  the same commit.** This project's documentation errors of record were
  not wrong measurements but right ones written next to the stale ones:
  the two-port target corrected in one bullet and still current in the
  next, the same netlist's seed-1 Fmax recorded as 48.52 in one file
  and 49.76 in another, a "not yet measured" caveat left standing below
  the measurement. When a number changes, grep for it and for
  everything derived from it — `docs/RECORD.md` first, where the figures
  now live, then `AGENTS.md`, `SPEC.md`, both READMEs, `hw/syn/README.md`
  and `docs/STATUS.md` — and amend every occurrence, dated. Do not stop
  at the documents: `.claude/skills/synth-sweep/SKILL.md` transcribes
  the per-seed values and spreads of the committed designs, and
  `sweep.sh` prints the widest of them in the note it emits past its 8%
  threshold. Neither gates anything on those numbers — the threshold is
  a literal — but both are read as current, which is enough. Same audit.
- **A LUT figure in this project is a post-pack `TRELLIS_COMB` count
  from nextpnr, over the device's 43848 — never a yosys cell count.**
  The two are not comparable and neither converts to the other: yosys
  `stat` counts cells before packing and produces no Fmax, so a
  percentage of 43848, or an Fmax beside the number, is how a figure
  here declares which one it is. This has already gone wrong in writing
  — two yosys-cell probes were described as the measurement apparatus
  for a nextpnr figure, and unpicking that took its own document
  (`docs/design/2026-08-12-ethernet-measurement-provenance.md`). Record
  which counter produced a number when you write it down; one that says
  neither is not a LUT count until somebody re-measures it.
- **Equal cell counts are not the same netlist, and an Fmax belongs to
  the commit it was measured on.** Treat a disagreement between a
  recorded figure and a fresh run as a finding to resolve, never as
  noise to average over — but resolve it by finding what changed, not
  by naming the first suspect. The 2026-08-09 audit found the cmp2lut
  table's clocks (2026-08-04, RTL `bf3930f`) irreproducible on a re-run
  at `ee54b06` and concluded "the nextpnr behind them was another
  binary". It was not: `tools/nextpnr` holds one binary, built
  2026-08-03 and never rebuilt, and the pin (`49691a4`) landed six
  hours after that table on the same day, recording the revisions of an
  already-built toolchain. What differed was the RTL — `oca_pktbuf.sv`
  and `oca_proto.sv` moved between those two commits — and `5492e3a`
  had already measured exactly that: two netlists **matching on every
  per-type cell total (7768 LUT4, 12043 TRELLIS_FF, 1687 CCU2C, 4
  DP16KD, 20 MULT18X18D) and still placing differently at the same
  seed**, the worst path moving out of `chacha20.sv` into `oca_proto`.
  Equal totals are not an equal netlist — the connectivity differs, or
  the path could not move — so matching area is no evidence that
  placement should repeat, and it is the step from "the cell counts
  match" to "therefore the tools changed" that has to be refused.
  nextpnr at a fixed seed is deterministic; that is why a seed sweep
  measures anything at all. Quote an Fmax with the commit it was taken
  on, and diff the RTL before blaming the toolchain.
- **An out-of-context Fmax is a ceiling, not the clock the board runs,
  and areas measured that way do not add.** `--out-of-context` places no
  IO, no PLL and nothing beside the design, so what it reports is what
  that design could reach alone; the board gets what the PLL's dividers
  deliver — `oca_clkrst` gives 625/13 = 48.0769 MHz whatever the report
  says — and an Fmax only says whether that clock closes. Every
  throughput figure divided out of an Fmax is therefore a cycle budget
  and not a measurement, and a Gbps figure has to say which of the two
  it is. Sums are the same trap in the area dimension: the one
  configuration this project summed and then built came in 2011 LUTs
  under its sum, because adding a core measured alone to a port measured
  alone counts twice the logic the optimiser shares. A sum is an
  estimate of unknown tightness until it is built (`docs/RECORD.md`,
  "Two cores measured").
- **A bring-up indicator has to be able to say the wrong thing.** Drive
  it from one counter and nothing else, and have it report a frequency
  rather than a flag: `EHXPLLL` raises LOCK when the loop closed, not
  when it closed on the right frequency, so a lock LED reports a PLL
  multiplying by four exactly as it reports one multiplying by five, and
  an indicator gated on seven terms reads the same when the PLL never
  locked as when everything worked. Downstream of it the same rule: a
  diagnostic counter saturates rather than wraps, because a wrapped
  counter reads like a healthy one, and two counters that share a
  register agree tautologically — the console's status line was three
  numbers wearing four labels until they were split (`docs/RECORD.md`,
  "Step 2 of the ladder" and "Step 3 of the ladder" for the indicator,
  "The diagnostic console runs on the board" for the counters).
- Git: work on branches; never commit directly on the default branch.

## The measurement record

The long-form record — every figure this project has taken, how it was
arrived at, and what it does NOT establish — is `docs/RECORD.md`. It was
this file's "Current status" section until 2026-08-15 and moved out
whole, the Ethernet route's history with it, because it is a record and
this file is a set of rules. **Read it before quoting any figure or
claiming a result**, and write every new bench number into it. For where
the project stands rather than how it was measured, `docs/STATUS.md` is
one page.
