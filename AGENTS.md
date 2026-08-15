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

**The Ethernet route is retired, 2026-08-12, and everything from here to
the end of this section is history.** The board has no RJ45 socket -- the
B50612D PHYs are on the module and their MDI pairs go to the SO-DIMM
edge, but the sockets and the magnetics are on a carrier no kit sold
with the module includes -- and the die is an LFE5U with no SERDES, so
it can never be the PCIe platform either (`SPEC.md`, PHASE 2). None of
it is work to do.

**The code was deleted the same day; the numbers are kept.** This
sentence used to say the material was kept "because it was measured and
because the code is still in the tree", and half of that premise has
gone: `oca_rgmii.sv`, `oca_udp_seam.sv`, `oca_top.sv`, `oca_top_mac.sv`,
`oca_top_stub.sv`, the three wrappers and two synthesis probes under
`oca/hw/rtl/vendor/`, the four
runner/testbench pairs, the whole `oca/hw/vendor/` tree with the
`verilog-ethernet` submodule and its patches, and the three `oca_top*`
synthesis targets are all removed. What stays is what was measured, and
it stays here because a figure whose sources are gone is worth more with
its provenance written down than deleted along with them. **Read every
Ethernet passage in this file as a record, not as a description of the
tree**: no path, module or runner it names still exists unless this
document says so explicitly. `docs/STATUS.md` lists the three pieces
that deliberately survive.

**The Ethernet MAC was an external dependency, not project RTL.** The
1G MAC, the RGMII interface and the IP/ARP/UDP stack come from
`verilog-ethernet` (Alex Forencich, **MIT licence**); it arrived as
a submodule. Writing a MAC from scratch is weeks of work on well-trodden
ground where every bug presents as "the link does not come up". That
choice set the project's RTL boundary, and it is the reason retiring the
route cost no crypto: the stack hands over the UDP
payload as an AXI-Stream and everything in `hw/rtl/` sits behind that
interface, which is why `oca_core` can be tested end to end with no
Ethernet in the simulation at all
(`docs/design/2026-08-03-host-protocol.md`).

**This entry used to say that verilog-ethernet "has working ECP5
support". It does not, and the correction matters because the missing
piece is the one nearest the pins.** Checked 2026-08-05 against the
repository: all 25 directories under `example/` target Xilinx or Intel,
a code search for `ecp5`, `lattice`, `colorlight` and `trellis` returns
nothing, and `rgmii_phy_if.v` accepts only `SIM`, `GENERIC`, `XILINX`
and `ALTERA` — an unrecognised value falls through to `GENERIC` without
a warning. `GENERIC` is not merely unoptimised on this device: `oddr.v`
drives one register from two `always` blocks on opposite edges, and
`synth_ecp5` reports conflicting drivers on every bit rather than
inferring `ODDRX1F`. `iddr.v` does elaborate, into fabric flip-flops on
both edges instead of `IDDRX1F`. **The RGMII front end — DDR
primitives and the receive delay — was therefore ours to write, behind
the wrapper SPEC.md's portability rule requires**, and it was written as
`oca/hw/rtl/oca_rgmii.sv`, deleted with the route. (This entry said "the RX clock delay and its
ECLK routing" until 2026-08-08. `IDDRX1F` has no `ECLK` port — its port
list is `D, SCLK, RST, Q0, Q1` — and `ECLK` belongs to the x2 gearing
primitives, which this design rejects. The delay is on the five data
lines, not on the clock, so that the recovered clock keeps its dedicated
path to a global buffer.)
(`example/RV901T` is a Linsn RV901T, a Spartan-6 board, not a
Colorlight.)

Two further facts recorded before they are rediscovered. The repository
is **deprecated by its author** in favour of `taxi`, and has not moved
since 2025-02-27; taxi is CERN-OHL-S 2.0 strongly reciprocal or
commercial, which is not compatible with keeping this project's design
under a permissive licence, so verilog-ethernet at MIT stayed the choice.
And the stack has a **64-bit variant** (`udp_complete_64` and the `_64`
modules below it) alongside the 8-bit one, which changes where the width
conversion belongs: at 48 MHz an 8-bit stream carries 384 Mbps, under
the port, so the conversion has to happen on the 125 MHz side rather
than in our clock domain. `eth_mac_1g_fifo` with
`AXIS_DATA_WIDTH = 64` does the width conversion and the clock domain
crossing in one instance, on the correct side of each — that
configuration is not exercised by the upstream testbench, so it needs
one of ours. (This said `eth_mac_1g_rgmii_fifo` until 2026-08-08. That
one **embeds `rgmii_phy_if`**, the module with no ECP5 target, so it
cannot take our front end without editing a pinned vendor tree.
`eth_mac_1g_fifo` is the same wrapper one layer down, taking GMII plus
its two clocks, and it carries the same pair of `axis_async_fifo_adapter`
instances. Its user side is already in our domain, so no further
asynchronous FIFO is needed between the MAC and the UDP stack.)

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
- yosys reads these cores with `read_slang`, not `read_verilog -sv`:
  the Verilog-2005 frontend rejects functions with `return` and
  concatenation assignments, which the RTL uses throughout.
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
  everything derived from it — `AGENTS.md`, `SPEC.md`, both READMEs,
  `hw/syn/README.md` — and amend every occurrence, dated. Same audit.
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
- Git: work on branches; never commit directly on the default branch.

## Current status

**For where the project stands, read `docs/STATUS.md` first.** It is one
page and it is the answer to "what is done, what is next". What follows
here is the long form: every measurement, what it does NOT establish, and
how each was arrived at. It is a record, not a summary, and it is far too
long to serve as one — which is why the tracker exists as of 2026-08-12.

- Phase 1: done, 126/126 checks pass, zero warnings — 113 of them
  driven by official vectors, plus one tamper case and twelve
  argument-validation cases, which have no vector to come from.
  Baseline on the
  dev machine: AES-128-GCM 26.6 GB/s (AES-NI), ChaCha20-Poly1305
  5.9 GB/s (large blocks).
- Phase 2: `chacha20.sv`, `poly1305.sv`, `chacha20_poly1305.sv` (AEAD,
  encrypt + decrypt) written and verified against RFC 8439 vectors
  (2.3.2, 2.4.2, 2.5.2, A.3 #1-4, 2.8.2, A.5). Lint `-Wall` clean.
  All three have a reference model validated on the official vectors
  before it is trusted: ChaCha20 with 100 randomised blocks (counter
  randomised over its full 32 bits), Poly1305 with digit-boundary edge
  cases and 200 randomised messages, the AEAD engine with 40 randomised
  encryptions and 40 randomised decryptions over AAD and message lengths
  chosen around the 64-byte block and 16-byte MAC boundaries.
- Open ECP5 toolchain built locally (yosys, prjtrellis, nextpnr-ecp5).
  Baseline synthesis of the AEAD engine on the LFE5U-45F was 25% of the
  LUTs, **90% of the multipliers**, Fmax **26.77 MHz**, critical path in
  the single-cycle 130x130 multiply of `poly1305.sv`.
- `poly1305.sv` reworked into a 26-bit limb datapath (five digits, the
  mod-2^130-5 reduction folded into the accumulation, parameter
  `ROWS_PER_CYCLE`). Result: the AEAD engine drops from **65 to 20**
  multipliers (90% -> 28%) and standalone Poly1305 Fmax more than
  doubles (22.94 -> **52.68 MHz** as measured that day on `1205c68`;
  **55.41 MHz** rebuilt 2026-08-12 on the netlist `8dd9cab` left behind,
  see `oca/hw/syn/README.md`). **AEAD Fmax was unchanged**
  (26.77 -> 26.10 MHz, inside place & route noise) and throughput fell
  ~40% to ~0.28 Gbps — a 64-byte block costs 47 cycles instead of 29,
  measured in simulation — because a Poly1305 block now takes 9 cycles
  instead of 3 and the critical path moved into `chacha20.sv`
  (`oca/hw/syn/README.md`).
- `chacha20.sv` reworked to compute one round per cycle (parameter
  `ROUNDS_PER_CYCLE`, 22 cycles per block instead of 12). Result:
  standalone Fmax 28.66 -> **53.11 MHz** (+85%), level with Poly1305's
  52.68 MHz, and **AEAD Fmax 26.10 -> 37.87 MHz** (+41% over the
  baseline), for +799 LUTs standalone / +487 in the engine and one
  flip-flop. (Both Fmax figures are of that day and neither is current:
  `3e4619e` later took this core to one datapath at 52.09 seed 1 / 52.76
  mean, and Poly1305 rebuilt gives 55.41 on 2026-08-12 and 54.83 on
  yosys `f77ddfb87`. Nothing has rebuilt the
  two together, so **no ordering between them is claimed**.) A 64-byte block now costs **57 cycles** (measured), so
  throughput is **~0.34 Gbps**: above the ~0.28 Gbps of the previous
  state, still **28% below the ~0.47 Gbps baseline** — Fmax gained 41%
  while cycles per block grew 97% across the two reworks.
- The wrapper's byte mask in `chacha20_poly1305.sv` then stopped building
  itself with `(512'd1 << (len * 8)) - 512'd1` — one 512-bit carry chain,
  which had become the critical path — and builds it per byte instead,
  64 independent 7-bit compares. **AEAD Fmax 37.87 -> 50.08 MHz** (+32%)
  for +514 LUTs, cycles unchanged, so throughput reaches **~0.45 Gbps**:
  level with the ~0.47 Gbps baseline, on 20 multipliers instead of 65
  and at nearly twice the clock. The critical path is back inside
  `chacha20.sv` (one quarter round, 19.97 ns), where the engine is now
  within 6% of the standalone core.
- The AEAD FSM was then split in two, joined by a one-block buffer, so
  the phases overlap: the input FSM accepts a block, runs ChaCha20 and
  emits ciphertext while the MAC FSM drains the buffer into Poly1305, and
  block N is authenticated while block N+1 is encrypted. A 64-byte block
  costs **40 cycles instead of 57** (measured), for **-540 LUTs** and
  **+13 flip-flops** — area went down, because the buffer replaces the
  old `src` register one for one and the 512-bit multiplexer in front of
  it loses one source. AEAD Fmax 50.08 -> 52.58 MHz is +5%, inside the
  place & route noise band, as it must be: the netlist's carry chains are
  unchanged and the critical path is still the one quarter round inside
  `chacha20.sv` (19.02 ns), now within 1% of that core standalone.
  Throughput **~0.67 Gbps: +50% on the previous state and +42% on the
  ~0.47 Gbps original baseline**, on 20 multipliers instead of 65. This
  is the first point in the series where the engine is faster than where
  it started.
- The 40 cycles are the MAC FSM alone: 4 sub-blocks x (9 Poly1305 cycles
  + 1 for the registered `p_blk` handshake). ChaCha20's 22 cycles are
  fully hidden — proved rather than assumed, by measuring an AAD block,
  which never runs ChaCha20 and costs exactly the same 40 cycles.
- An **area pass** then took the engine from **10041 to 7358 LUTs
  (-26.7%)** with flip-flops (5738), multipliers (20) and cycles per
  block (40, measured differentially: 227 cycles for 4 blocks, 387 for 8)
  all unchanged. Two independent changes: `chacha20.sv` carries **one**
  round datapath instead of two — a diagonal round is a column round on a
  row-rotated state and rotating by a constant is wiring, so 16 of the 32
  adders and the multiplexer choosing between them are deleted (4368 ->
  3125 standalone, exactly -256 CCU2C) — and `chacha20_poly1305.sv` masks
  the padding on the 16-byte sub-block Poly1305 reads instead of on the
  512-bit buses feeding it, replacing two full-width masking stages with
  one quarter-width one (-1454 LUTs). **No speed is claimed**, and one
  seed could not settle whether any was there: over seeds 1-4 the engine
  means 50.72 -> 52.83 MHz (+4.2%) while the standalone core shows no
  effect at all (51.50 -> 52.76, distributions overlapping). The critical
  path is structurally the same quarter round inside `chacha20.sv`, so
  the engine's separation is recorded as a plausible congestion effect
  and kept out of the throughput figures. The masking change is covered
  by `hw/sim/test_dirty_pad.py`, without which no test in the project
  could see it (`oca/hw/syn/README.md`).
- **Engine replication: two, not three, and the reason is the router.**
  Placed and routed on 2026-08-04 rather than projected from one core
  (four seeds each, `--out-of-context`): 1 `oca_core` 11149 LUTs
  (25.4%), 20 MULT (27.8%), 50.59 MHz; 2 `oca_core` 22313 LUTs (50.9%),
  40 MULT (55.6%), **49.28 MHz** (-2.6%, inside the seed spread); 3
  engines + 1 protocol layer 25983 LUTs (59.3%), 60 MULT (83.3%),
  42.80 MHz; **3 `oca_core` 33484 LUTs (76.4%), 60 MULT (83.3%) — does
  not route.** One seed fails placement, six more were still routing
  after 55 minutes each, and roughly 50000 arcs stay unrouted whether
  the constraint is 100, 45, 40 or 35 MHz: **congestion, not timing**,
  so a slower clock buys nothing. Neither multipliers nor LUTs are the
  binding constraint — both fit — **routability is**, which no
  multiplication of a single-core report could have predicted. With
  three engines the critical path also leaves `chacha20.sv` for
  `poly1305.sv:140` (the registered DSP products), routing-dominated
  because the third engine fills 83% of the DSP columns.
  **The port figures that follow are retired, 2026-08-12** -- the board
  has no socket, so none of them is a target any more. They are kept
  because they were measured and because the occupancy conclusion above
  does not depend on the transport.
  **Corrected MVP target: two ports at 56% of line rate each, not one
  port saturated** (56% being that figure at the 48.16 MHz of the day)
  — superseded in turn on 2026-08-05, when `d4ee09f` measured the cost
  of an Ethernet port and two ports turned out not to fit; the port
  target that stood until the route closed is in the two-core bullet
  below. The board has two PHYs
  (`BOM-MVP.md`) and
  `oca_dual` wires the two engines as two independent AXI-Stream pairs,
  one per core — so this is **0.569 Gbps per port at a 1500-byte MTU,
  1.138 Gbps aggregated across both** on the committed pair's 48.89 MHz
  as it read on yosys `41a4b5a03`, and **neither port is saturated**.
  On `f77ddfb87` that pair means 49.61 MHz and the same formula gives
  0.577 / 1.155. That 48.89 is an out-of-context
  Fmax and no PLL divider produces it; the two-core bullet below gives
  the clock a pinned build gets. Both PHYs can be fed in cycle
  budget; whether two MACs
  fit beside the cores is settled below — they do not. Saturating one of them would need
  both cores behind it, hence a distributor and a collector that do not
  exist (the two-core bullet below, and commit 23742dc, which retracted
  the "one port saturated with margin" reading this passage carried).
  This supersedes the 1.97-2.07 Gbps three-engine projection and the
  >= 2 Gbps target (`SPEC.md`, `oca/hw/syn/README.md` "The occupancy
  study"). Note that `ROWS_PER_CYCLE` in `poly1305.sv` competes with
  replication for the same 72 multipliers — 2 rows per cycle costs 40
  per engine, so one engine instead of two — while removing the
  one-cycle `p_blk` bubble is free.
- **The host protocol is implemented and verified** (design:
  `docs/design/2026-08-03-host-protocol.md`). Four new modules behind a
  64-bit AXI-Stream boundary with `tkeep`: `oca_keystore.sv` (8 key
  slots, each with a loaded bit, cleared on reset), `oca_pktbuf.sv` (two
  banks of `BYTES` = 2048, 512 x 64 in one pair of block RAMs, with a
  1..8 byte count on writes), `oca_proto.sv` (the protocol FSM) and
  `oca_core.sv` (wiring only). Store and forward throughout: the request
  is buffered whole before the engine sees it and the response is built
  whole before a byte leaves, which is what lets a failed tag return no
  plaintext at all. Suites: keystore 4/4, pktbuf 12/12, oca_core 29/29, attack 16/16,
  plus `test_proto_model.py` as plain Python. Lint `-Wall` clean with
  `--top-module oca_core`. **The security property has two tests that
  can fail**: `test_corrupt_tag_yields_no_plaintext` asserts on the leak
  rather than on the status code, and `test_every_tag_byte_is_compared`
  pins the width of the comparison by flipping one bit in each of the
  sixteen tag bytes — without it, a comparison of 120 bits passes both
  suites, because every other tag corruption in them touches byte 0 or
  byte 15. Two more properties the 64-bit datapath introduced are
  covered the same way: `recv_packet` asserts the bytes past `tkeep` are
  zero, so every test witnesses the final-beat mask. Re-measured
  2026-08-06: deleting that mask from `oca_proto` fails **15 of the 29
  and 9 of the 16**, and deleting the assertion as well takes both
  suites straight back to 29/29 and 16/16 — that one assertion is the
  only thing in either suite that can see the leak, because
  `recv_packet` reads the response through `tkeep` and would otherwise
  discard the unmasked bytes in silence. Beside it,
  `test_partial_keep_mid_packet_fails_closed` sends a short beat before
  `tlast` and asserts status 05 with `cnt_drop` unmoved — a length
  error is not a header drop.
- **`oca_core` as committed: 12330 LUTs (28.1%), 12033 FF (27.4%), 20
  MULT18X18D (27.8%), 4 DP16KD (3.7%)**, and **Fmax 50.12 / 48.74 /
  48.61 / 51.62, mean 49.77 MHz** over four placer seeds (measured
  2026-08-15 on yosys `f77ddfb87`; area identical on all four, as it
  must be). The figures `run_synth.py oca_core` reproduces today, on a
  netlist whose key store is present (see the `cmp2lut` bullet below).
  The spread is **6.2%**, against the pair's 7.3% on the same pin — both
  wide enough that a single seed from either settles nothing, and one
  four-seed draw of each is not enough to order them as a property of
  the designs. This entry carried only seed 1 until the first sweep was
  run.

  **The 2026-08-15 yosys bump did not move this design's mean
  materially.** On
  `41a4b5a03` the same sweep read 12308 LUTs and 47.93 / 50.91 / 51.03
  / 49.76, mean 49.91 MHz, spread 6.5%. Area went up by 22 LUTs, 0.2%,
  and the mean fell by 0.14 MHz — a twentieth of the spread, which is
  to say nothing at all. Both readings are four-seed sweeps, so they
  are comparable; neither is a single seed.

  **What secret zeroisation cost**, measured seed 1 against the same
  toolchain, one step at a time from 11590 / 12043 / 48.52 MHz:
  clearing the engines' secret registers is **+670 LUTs (+5.8%)**, −30
  FF, 49.65 MHz; walking the packet memory adds **+48 LUTs (+0.4%)**,
  +20 FF, 47.93 MHz. Together **+718 LUTs (+6.2%)** and no change in
  multipliers or block RAM. On this device it costs no DSP — synth_ecp5
  maps through `dsp_map_18x18.v`, which connects no clock or reset, so
  those registers were already in fabric — but the LUT bill is real and
  it is logic, not routing. Fmax moves in both directions across the
  three points and stays inside the seed spread documented below,
  so there is no clock signal in it either way; a multi-seed sweep would
  be needed to claim otherwise.

  Everything in
  the rest of this bullet is the 64-bit widening step that came before
  the packet overlap and before that fix, kept because the comparison
  with the 8-bit core is only meaningful against it: **11429 LUTs
  (26.1%), 11228 FF (25.6%), 20 MULT18X18D, 4 DP16KD, Fmax 51.71 MHz at
  seed 1** (50.69 MHz mean over seeds 1-4). Against the 8-bit version
  (11149 / 10842 / 20 / 2, 50.59 MHz mean) the widening costs **+280
  LUTs (+2.5%), +386 FF (+3.6%), no multipliers and no clock** — the
  Fmax means differ by +0.2% with the distributions overlapping. The
  plan estimated +530 LUTs and +325 FF; the LUT figure came in at about
  half, which is trap 1 of the plan paying off (every next-state
  multiplexer in `oca_proto` is a `case` on a registered selector, which
  the plan measured on a synthetic 64-bit 3:1 mux at 129 LUT4 against
  771 for an `if / else if` chain — **a factor of six**, and 642 LUTs on
  that one mux, more than twice this design's whole measured increase;
  that is the plan's synthetic figure, not a measurement of
  `oca_proto`). **Both packet buffers still
  infer block RAM** in pseudo dual-port mode, zero LUT RAM cells in the
  netlist; **2 -> 4 DP16KD is width, not capacity** — a DP16KD's widest
  port is 36 bits, so a 64-bit word spans two blocks, and 36-bit mode is
  512 x 36 where one bank used 256 words. That spare half is what the
  second bank was later built in, at no extra block RAM; it is **not**
  room for a larger `BYTES`. 4096 does not fit the 12-bit byte counters
  the protocol layer carries — `12'(BYTES)` truncates to zero and both
  full flags jam high — and anything that is not eight times a power of
  two puts the upper bank off the end of the array. `oca_pktbuf` now
  refuses both at elaboration; the legal range is 16 to 2048. The
  protocol layer still adds **no multipliers** (so it does not
  cost an engine) and was **not on the critical path** of that build:
  seeds 1, 3 and 4 cite no RTL file but `chacha20.sv`, lines 58-64; seed
  2, the slowest, lands on `poly1305.sv:140`. **No protocol module
  appears on any of the four**. The protocol layer did reach the worst
  path once — `oca_proto`'s `data_off` adder, dominated by one route
  across the die — but on the pre-zeroisation netlist, not the committed
  one: at seed 1 the committed netlist's worst path is back inside the
  engine, `poly1305.sv:159`'s multiply. This entry previously attributed
  the `data_off` sighting to the committed netlist; the record it cited
  (`hw/syn/README.md`, "Where the committed design stands") says the
  opposite. One seed either way is a placement result, not a property of
  the design.
- **End-to-end throughput: 415 cycles per 64-byte block down to 40**,
  which is the engine's own cost — the protocol layer now adds nothing
  on top of it. Three steps, each measured differentially in simulation
  over seal commands of 4/8/12/16 blocks and exactly linear across every
  span. The 64-bit datapath took 415 to **64**: 8 in (8 bytes/cycle) +
  48 through buffer/engine/buffer + 8 out, serialised because the core
  was store and forward on one pair of buffers. Overlapping feed,
  compute and drain inside a command took it to **56**, and four packet
  stages overlapping across commands took it to **40** — 231, 391, 551,
  711 cycles for 4, 8, 12, 16 blocks, marginal 40.00. Of the 48 middle
  cycles at the 64-cycle stage, 40 were already the engine, which is why
  40 is the floor and why the remaining work was scheduling rather than
  datapath.
- **Two cores measured, and the port target the Ethernet route carried
  until it was retired on 2026-08-12.** The cell counts, the clocks and
  the cycle model in this bullet are measurements and stand; every
  figure in it that names a port, an MTU or a percentage of line rate
  describes a configuration the board cannot carry, because there is no
  socket to carry it (`SPEC.md`, PHASE 2).
  `run_synth.py oca_dual` builds two `oca_core`. **On the RTL of
  `c1c6556` (2026-08-05), before the secret zeroisation**, four placer
  seeds gave **23191 LUTs (52.9%), 24086 FF (54.9%), 40 MULT18X18D
  (55.6%), 8 DP16KD, Fmax 47.07 / 49.61 / 47.99 / 47.98, mean 48.16
  MHz** (spread 5.4%). Replication is linear to eleven LUTs of glue
  against 2 x 11590, and the second core costs 0.7% of clock, inside
  that spread. **That row is superseded by the committed pair below**
  and is kept because the replication argument was measured on it.

  **What that buys depends on how the cores are wired to the ports, and
  `oca_dual` answers it: two independent AXI-Stream pairs, one per
  core.** Throughput follows from the measured cycle model — 40 cycles
  per 64-byte block plus 71 per packet, so 1031 cycles for a 1500-byte
  MTU and 111 for a 64-byte packet — divided into the clock of the
  netlist being described, **and it moves when that clock does** —
  which makes every figure below a cycle budget, since the clock in
  question is an Fmax and `oca_clkrst`'s PLL delivers 625/13 =
  48.0769 MHz whatever an out-of-context build reaches. On
  the committed pair (48.89 MHz mean on yosys `41a4b5a03`, below) one
  core per port is
  **0.569 Gbps at a 1500-byte MTU — 56.9% of line rate — and 0.226 Gbps
  on 64-byte packets**, with 1.138 Gbps aggregated across both ports;
  on `f77ddfb87`'s 49.61 MHz the same model gives 0.577 / 0.229 / 1.155.
  Neither port is saturated. (This read 0.561 / 0.222 / 1.121 until
  2026-08-09: those are the same formula at 48.16 MHz, the
  pre-zeroisation pair, left standing when the clock above was
  corrected to 48.89.)
  Saturating one would need both cores behind it, which needs a
  distributor and a collector that do not exist, and which the per-core
  key store makes non-trivial: a slot is loaded into one core and only
  that core can use it.

  **With the secret zeroisation merged — the pair as committed — four
  seeds give 24621 LUTs (56.1%), 24066 FF (54.9%), 40 MULT18X18D, 8
  DP16KD, Fmax 50.41 / 51.18 / 47.68 / 49.19, mean 49.61 MHz**
  (spread 7.3%), measured 2026-08-15 on yosys `f77ddfb87`.

  On the previous pin `41a4b5a03` the same sweep read 24602 LUTs and
  50.37 / 48.12 / 48.05 / 49.03, mean 48.89 MHz, spread 4.8%, measured
  2026-08-05 in `d4ee09f`; the 2026-08-09 audit re-ran all four and got
  them exactly. Area moved by 19 LUTs and the mean by +1.5%, which is
  inside either spread. **Of the three the spread moved furthest**,
  4.8% to 7.3%: still under the 8% at which
  `.claude/skills/synth-sweep` says to treat placement difficulty as
  the finding, but half again as wide, on the densest design here — and
  four seeds on each pin measure those two draws, not a property of the
  design.

  Against the pre-zeroisation pair, both measured on `41a4b5a03`, the
  zeroisation cost +1411 LUTs — 705 per core, against the 718 measured
  on one core alone — and the clock was 1.5% *better*, inside the seed
  spread, so it cost area and not time. That comparison is left on the
  toolchain it was taken on: the pre-zeroisation netlist has never been
  built on `f77ddfb87` and re-deriving it from the 24621 above would
  compare two different mappers.

  **And one Ethernet port costs 8422 LUTs, 19.2% of the device**,
  measured out-of-context on this toolchain rather than estimated:
  `udp_complete_64` 7147, `eth_mac_1g_rgmii_fifo` at 64 bits 1214, and
  ~61 for the RGMII front end. The MAC figure is that module as
  measured; the build used `eth_mac_1g_fifo`, which is the same
  wrapper without the `rgmii_phy_if` the ~61 accounts for, so the total
  does not move. **Two modules are missing from that figure**:
  `eth_axis_rx` and `eth_axis_tx`, which `udp_complete_64` does not
  instantiate and which `oca_top` read and needed
  (`docs/design/2026-08-05-ethernet-integration.md`), so 8422 is a floor
  for a port and not its cost. The probes that produced these numbers
  were deleted on 2026-08-12 with the rest of the route;
  `docs/design/2026-08-12-ethernet-measurement-provenance.md` carries the
  commit, the vendor pin, the two yosys commands and the result of
  re-running them before the deletion. What that leaves:

  | configuration | LUTs | of device |
  |---|---|---|
  | two cores, two ports | 41446 | **94.5%** |
  | two cores, one port | 33024 | **75.3%** |
  | one core, one port | 20730 | 47.3% — **built: 18719, 42.7%** |

  Two ports are not merely tight, they are out. Two cores behind one
  port land at 75.3%, against the 76.4% at which this device stopped
  routing in the occupancy study — and would additionally need a
  distributor, a collector and an answer to the per-core key store.
  **The one row that has since been built came in 2011 LUTs under its
  sum**, because adding a core measured alone to a port measured alone
  counts twice the logic the optimiser shares. (It was 2928 under until
  `54a2df8`, whose `clear_arp_cache` connection restored 881 LUTs of ARP
  logic the earlier netlist had deleted.) The two rows above it are
  sums of the same kind and neither has been built, so they are
  estimates of unknown tightness in the same direction. **The one
  configuration that fitted was one core on one port, 0.581 Gbps at
  MTU** — the single core's own mean of 49.91 MHz on yosys `41a4b5a03`
  through the same cycle model, 58.1% of line rate, 0.230 Gbps on
  64-byte packets; on `f77ddfb87`'s 49.77 the same model gives 0.579 and
  0.230. That clock is
  the core placed **alone and out of context**: no MAC beside it, no IO,
  no PLL, so it is the ceiling that configuration could reach and not a
  measurement of it. It stopped being a target on 2026-08-12, when the
  route closed for want of a socket.

  **And the ceiling is not the clock the board runs.** `oca_top`
  instantiated `oca_clkrst`, which delivers `clk_sys` at 625/13 =
  **48.0769 MHz**. Fmax only says whether that clock closes, and **as of
  2026-08-11 it does not**: the best of 32 seeds reaches 47.40 on the
  seed that comes closest overall, and `clk_sys` clears its target on 20
  of the 32 without any of them carrying the two 125 MHz clocks as well.
  So **0.560 Gbps at MTU, 56.0% of line rate, and 0.222 Gbps on 64-byte
  packets** is what this design delivers *if* a placement is found that
  closes it — not what a build produces today. Every *throughput* figure
  above it is an Fmax divided into a cycle count — what a core could
  reach if a PLL could give it that clock; this one is what the board
  gets. (The Gbps figures that are wire rates or targets, 1 and 2, are
  neither.)

  **What this PLL can offer instead is a coarse ladder, and the next
  rung up is unmeasured.** `clk_tx` is an integer division of the same
  VCO, so the VCO must be a multiple of 125 MHz, and the 400-800 MHz
  band leaves exactly 500, 625 and 750. From those, `clk_sys` near this
  range can be 45.45, 46.88, **48.08**, 50.00 and 52.08 — nothing
  between 48.08 and 50.00. **50.00 has now been asked for, at one seed,
  and that placement reached 48.22**: `CLKOP_DIV` 4 with `CLKOS_DIV` 10
  gives a 500 MHz VCO, `clk_tx` exactly 125 and `clk_sys` exactly
  50.000. One placement is not a sweep, and this project's own rule
  about seeds cuts both ways: on the 48.08-constrained sweep `clk_sys`
  reaches 50.44 at best and clears 50.00 on three of 32 seeds. So the
  rung above is **untested rather than unreachable**. It was moot while
  `rgmii_rx_clk` had to close alongside it; that route was retired on
  2026-08-12 and no design has asked for the rung since.
  The device does carry four PLLs and this design uses one, so a second
  one for `clk_sys` is still a door nobody has opened.

  **Third-party Verilog goes through `read_verilog`, never
  `read_slang`, and this is a frontend lesson rather than an Ethernet
  one.** It was measured on `verilog-ethernet`, which is no longer in the
  tree, and it applies to the next vendored Verilog just as it applied to
  that one. On the same two modules: `axis_async_fifo` is 169 LUTs and 3
  DP16KD through `read_verilog` and 6454 LUTs with no block RAM at all
  through `read_slang`; `eth_mac_1g_fifo` is 1185 against 12620. Slang
  does not infer those memories and spills them into logic, so a mixed
  design read entirely through slang measures **38x on the FIFO and
  10.6x on the MAC** too large and gets abandoned for the wrong reason.
  Our own cores need `read_slang` for the SystemVerilog they use (see
  the hard rule above), so a design mixing the two reads each side with
  its own frontend.

  **Which counter produced those four figures is not recorded, so take
  them as an order-of-magnitude warning and nothing finer.** They
  entered at `d4ee09f` (2026-08-05) in a paragraph that quotes no
  percentage of 43848 and no Fmax — the two tells this file uses
  everywhere else to mark a figure as post-pack — and nothing here
  reproduces the run: neither module was ever a `DESIGNS` entry in
  `run_synth.py`, and `.gitmodules` does not exist before `5c886d7`
  (2026-08-08), so `verilog-ethernet` was outside version control on
  the day these were published. That commit's "out-of-context", which
  is nextpnr's flag, belongs to its 8422 sentence and not to this one.
  Nor is the ratio itself a constant to lean on: 38x on the FIFO
  against 10.6x on the MAC, from one paragraph, is a spread wide enough
  that the two pairs were probably not measured the same way. A fresh
  measurement is possible — the upstream sources are reachable and the
  pin is recorded from `5c886d7` onwards — but it would be a new pair
  with a recorded counter, not a confirmation of these four.

  This corrects the figure this entry carried until 2026-08-05, which
  read "one gigabit port is saturated at MTU with 12% of margin". That
  summed both cores' cycle budgets against a single port — a topology
  the RTL does not implement and has no path to without new logic. The
  synthesis numbers above are unaffected; only what they were claimed to
  deliver was wrong.

  **All four seeds routed.** The previous two-core reading had two of
  its four fail to route at all, stopped after 3 h 22 min each with the
  arc count oscillating rather than descending; this RTL is slightly
  larger at 23191 LUTs against that build's 22891 and routes on every
  seed. Why is not established here, and the earlier pair was measured
  on RTL from before the packet overlap in a build whose key stores
  yosys had deleted, so the two are not a controlled comparison. What
  is established: both key stores are present in this netlist, 4626
  live flip-flops attributed to `oca_keystore.sv`, exactly twice 2313.

  One caveat stands. **Nothing here has run on silicon**: Verilator
  cycle counts and `--out-of-context` synthesis, with no IO, no pin
  constraints, no MAC and no PLL.
- **The key store was missing from every netlist this project ever
  produced**, and is now present: a mis-mapping in yosys's
  `cmp2lut.v` folded `oca_keystore.sv`'s index bounds check to constant
  false, so all 2048 key bits and 8 loaded bits were optimised away and
  a bitstream would have answered "bad slot" to every seal and open.
  Not a regression — synthesising `95c81f7` shows the same key store
  already dead, as 2056 self-holding registers. Fixed by a local patch
  carried here from 2026-08-04 in `bf3930f`, reported upstream the next
  day as YosysHQ/yosys#6085 (its text kept in
  `oca/hw/syn/patches/README.md`), and landed upstream unchanged as
  PR #6114 on 2026-08-14; the pin moved to `f77ddfb87` on 2026-08-15
  and the patch went with it. `run_synth.py` still refuses a toolchain
  that fails the probe and asserts the key store's storage against the
  netlist, and `run_keystore_gate.py` replays the key store tests on the
  synthesised netlist — 2 of its 4 fail on a yosys older than
  `f77ddfb87`. The same net
  now covers `oca_proto` as well: a floor of 3600 live flip-flops
  attributed to it (3645 measured, and `check_netlist` prints the census
  per file so the number can be re-measured), and `run_proto_gate.py`
  for the tag comparison, which is combinational and so invisible to any
  cell count. Cost:
  8620 -> 11590 TRELLIS_COMB and 8311 -> 12043 TRELLIS_FF, DP16KD and
  MULT18X18D unchanged at 4 and 20. **Two Fmax readings exist and they
  are not the same measurement.** The one this entry carried (49.31 ->
  48.84 MHz mean, -1.0%) was taken 2026-08-04 on the RTL of `bf3930f`,
  the stock row over seeds 1-2 and the patched row over five. Re-run
  2026-08-09 on `ee54b06` with five seeds on both rows: 50.96 -> 49.33
  MHz mean (-3.2%, inside the stock row's 46.84-54.08 spread). What
  separates them is the RTL and the seed sets, not the tools: `tools/`
  holds one nextpnr, built 2026-08-03 and never rebuilt, and `5492e3a`
  had already measured these two netlists placing differently at the
  same seed while being cell for cell identical. Both readings support
  the same conclusion, which is the point: having a key store costs
  area, not clock. Area is unaffected either way — yosys is
  deterministic, 11590 / 12043 reproduce exactly, and the stock row is
  8616 / 8311 where it read 8620 (nextpnr's packing, not yosys). What
  the change does cost is router effort, at least 2.5x. See
  `oca/hw/syn/README.md`, "The cmp2lut trap".
- **The AEAD engine is guarded too, by a floor on the whole netlist**
  (2026-08-06). The two per-file floors left `chacha20.sv`,
  `poly1305.sv` and `chacha20_poly1305.sv` with no netlist assertion at
  all, and neither gate runner can reach them, so the same mapping
  defect landing on the block state or the accumulator would have been
  invisible to the entire flow. `NETLIST_FF_TOTAL` in `run_synth.py`
  requires 11900 live flip-flops for `oca_core` (12033 measured) and
  23800 for `oca_dual` (24066). A total rather than three more per-file
  floors because yosys's attribution moves: across the
  secret-zeroisation merge `poly1305.sv` went 391 -> 1789 while the
  unattributed bucket fell 1753 -> 324, the module having gained reset
  branches and no new state. Non-vacuous — deleting the flip-flops
  attributed to any one of the three engine files fails it, and the
  per-file floors report ok in all three cases.
- **The Ethernet integration was merged** (`c153934`), designed in
  `docs/design/2026-08-05-ethernet-integration.md`, **and the route is
  retired as of 2026-08-12** because the board has no RJ45 socket and
  the die has no SERDES (`SPEC.md`, PHASE 2; that design document now
  opens with a closed header). Everything it needed
  to be built is written and tested, section 8's whole-path testbench
  included, and none of it is work to do. **The board arrived on
  2026-08-11**, six days before it was expected, and the bring-up ladder
  below is where it went. The host interface is the DAPLink USB serial
  on J17/H18, 115200 8N1, `/dev/ttyACM0`.

  **Step 1 of the ladder is done.** `openFPGALoader --detect -c
  cmsisdap` reads `idcode 0x41112043`, LFE5U-45, over the carrier's
  DAPLink. That excludes a wrong die and not a wrong package, since
  prjtrellis lists six packages against that one code with `caBGA256`
  among them, so the package was settled the only way it can be, off
  the chip: the marking reads `LFE5U-45F` / `6BG381C`, speed grade 6,
  caBGA381, commercial. Both halves now agree with `BOM-MVP.md` and
  with the `--package CABGA381 --speed 6` the build targets, so every
  LOCATE in `colorlight_i9.lpf` rests on the right ball map.

  **Step 2 of the ladder is done, on the board.** `oca_blink.sv` and its
  two-pin `colorlight_i9_blink.lpf`: 25 flip-flops, two IO, floored at
  all 25, and the counting proof on the .lpf shown non-vacuous by
  renaming a LOCATE and watching the build stop. It is one eighth on and
  seven eighths off so that the duty cycle settles the LED polarity,
  which a symmetric blink cannot. Loaded into SRAM 2026-08-11, D2 gave
  the short flash and not its complement, which settles two things and
  bounds a third: **the LED is active low**, litex's `user_led_n` being
  right on a point no source had measured; the bitstream path runs end
  to end; and something is clocking P3 at roughly the rate expected,
  since the blink was neither absent nor obviously fast or slow. **No
  period was ever timed**, at step 2 or at step 3, so the oscillator's
  frequency is bounded by eye and by nothing else. `oca_top_stub` could
  not do this job and its LED comment claimed it could — an indicator
  gated on seven terms reads the same when the PLL never locked as when
  everything worked. Both were corrected before that file was deleted
  with the route, and the lesson is why `oca_blink` and `oca_pll` each
  drive D2 from one counter and nothing else.

  **Step 3 of the ladder is half done on the board**, and the half that
  is missing is the half that makes it a measurement. `oca_pll.sv` with its
  own two-pin `colorlight_i9_pll.lpf`: the real `oca_clkrst`, though two
  of its fourteen connections are not the top's (`ext_rst_n` tied high
  where the top passes `por_n`, `clk_rx` tied to `clk25` where it passes
  `gmii_rx_clk`), and D2 driven from a counter on `clk_tx` that counts
  62,500,000 to halve 125 MHz on a decimal boundary, so the reading is a
  **frequency** rather than a lock flag. `EHXPLLL` raises LOCK when the
  loop closed, not when it closed on the right frequency, so a lock LED
  reports a PLL multiplying by four exactly as it reports one
  multiplying by five. Three states, three readings: 1 Hz symmetric is
  locked at 125 MHz, a ~3 Hz flicker counted on `clk25` and carrying no
  reset is live-but-unlocked, static is no bitstream or no clock.
  Observed 2026-08-11: **1 Hz**, so the PLL locks and `clk_tx` runs. The
  stopwatch check that would tighten that to 0.3% has not been done, so
  what is established is lock and the absence of a gross error, not
  125.0 MHz to three digits.

  `check_prims` used to return early and silently for a top absent from
  `NETLIST_PRIM_COUNT`, and `check_pll` is called from nowhere else, so
  `oca_pll` built once with its PLL parameters unverified and said
  nothing about it. It now warns the way the flip-flop census does. With
  the entry added the check runs and passes: `CLKI_DIV 1`, `CLKFB_DIV
  5`, `CLKOP_DIV 5`, `CLKOS_DIV 13`, VCO 625.00 MHz, `clk_tx` 125.0000,
  `clk_sys` 48.0769. `clk_sys` is not measured on the board and does not
  need to be: it is that same VCO over a divisor now checked in the
  netlist.

  **There is no Ethernet connector on this hardware, and there is a
  serial console.** The module carries both B50612D PHYs and routes
  their MDI pairs to the SO-DIMM edge; the RJ45s and the magnetics
  belong on an extension board this kit does not include, so steps 4 and
  5 have nowhere to plug a cable. What the carrier does have is a
  DAPLink presenting a CDC pair beside the HID that carries CMSIS-DAP.
  `oca_uart_probe` drove both of litex's candidate transmit pins with
  payloads naming themselves, and `/dev/ttyACM0` at 115200 8N1 returned
  **`PIN=J17`** once a second: the DAPLink is on litex's `serial`, the
  v7.0 pair, not on the `serialx` at E5/F4 that the v7.2 section adds.
  **The diagnostic console runs on the board.** `oca_uart_console`:
  `oca_uart_rx` and `oca_uart_tx8` with an `oca_fifo` on each side, 16
  in and 32 out, and `oca_console` between them. Single-character
  commands, deliberately, because a line parser needs a buffer, an
  editor, a length limit and a policy for the limit, and each is a place
  to put a bug into the only channel available for finding bugs. `p`
  answers `OCA`, `?` lists `psz?`, `z` zeroes, anything unknown answers
  `?`, and `s` gives `R=xxxx E=xxxx O=xxxx C=xxxx`: bytes the receiver
  delivered, frames it refused, bytes the input FIFO had no room for,
  commands run. R is taken at the receiver and C at the command, so
  `R >= C` and the difference is what the queue holds plus what O
  refused. They were one register until 2026-08-11, bumped in the same
  branch, which made them equal for every input and the status line
  three numbers wearing four labels.
  The counters saturate rather than wrap, since a wrapped counter reads
  like a healthy one. Run 2026-08-11 the board answered every command
  and reported `R=0006 E=0000 O=0000 C=0006` for the six sent, and `pp`
  with no gap produced two complete answers, which is the exact failure
  `oca_uart_echo` had before the FIFOs existed. **The output FIFO is
  what fixes it**; the input one is insurance and is not load bearing at
  this baud, since the console empties a response into the output queue
  in a few cycles and is free again long before the next byte lands.
  That figure was measured with R and C sharing a register, so their
  agreement in it is tautological: what says nothing was lost is
  `E=0000 O=0000`.

  **The receive pin is H18, confirmed the same day** by `oca_uart_echo`:
  eight bytes sent one every 300 ms came back in order and byte exact,
  `4f 43 41 00 01 55 aa ff`. Sent back to back, alternate bytes are
  dropped and "OCA" returns "OA", which is this design's documented
  behaviour and what its testbench asserts, so bench and simulation
  agree on the failure as well as on the success. **The channel works in
  both directions**; a console needs a holding register or a FIFO before
  it can take a line at speed. Bank 2's
  VCCIO stays unmeasured: a clean decode says the DAPLink accepted the
  levels, which a 2.5 V swing into a 3.3 V input generally is.

  **Bank 6 is at 3.3 V, measured 2026-08-11.** The rail is a plane
  inside the module and no capacitor on it is identifiable from anything
  we hold, so it was read off a driven pad instead: an LVCMOS output
  driven high sits at its own bank's VCCIO, and a ten megohm meter loads
  it with under a microamp. `oca_vccio` drives eight free bank 6 balls
  toggling in step with D2, so a swinging reading beside a visible
  blinking LED identifies which hole carries which. `colorlight_i5.py`
  does name the connector each pmod sits on, contrary to what this
  paragraph said until 2026-08-11, but nothing names the hole within it.
  Two surfaced on **connector P4** (not ball P4, which is the PHY
  reset), holes 8 and 25: **3.28 V** driven high, 0 V driven low. They
  are **F1 and K4**, the only two of the eight probes that P4 carries. `LVCMOS33` throughout `colorlight_i9.lpf` is
  therefore right, and the silent case that file documents at length,
  RGMII inputs declared `LVCMOS33` in a 2.5 V bank, does not apply here.

  **There is no `oca_top` bitstream to load, and there will not be
  one.** It misses 125 MHz on all 32 seeds, and `pack()` refuses to
  write a bitstream for a design that missed its clock -- so the gate
  held, and then the route it belongs to was retired on 2026-08-12 for a
  reason no placement could have fixed.

  What that route left in the tree, and what it no longer does. Deleted
  on 2026-08-12: `verilog-ethernet`, which sat as a submodule at
  `oca/hw/vendor/verilog-ethernet` (77320a94), together with
  `vendor_patches.py` and `patches/`; `oca_rgmii.sv`, the RGMII front end
  around the ECP5 DDR primitives, with the receive delay movable at run
  time rather than fixed in the bitstream (10 tests); `oca_udp_seam.sv`,
  the join between the UDP stack and `oca_core` (10 tests, run at two
  queue depths); `oca_top.sv`, `oca_top_mac.sv` and `oca_top_stub.sv`
  with their `run_synth.py` entries; and, under `oca/hw/rtl/vendor/`,
  the three parameter-fixing wrappers (`oca_eth_axis_64.v`,
  `oca_eth_mac_1g_fifo_64.v`, `oca_udp_complete_64.v`) together with the
  two synthesis probes (`oca_eth_mac_1g_fifo_64_probe.sv`,
  `oca_udp_complete_64_probe.sv`). Both probes were re-run before being
  deleted and their cell counts are recorded in
  `docs/design/2026-08-12-ethernet-measurement-provenance.md` — which
  also corrects the belief that they were what the 8422-LUT figure was
  measured on. They are not: 8422 is a `TRELLIS_COMB` count from a
  nextpnr out-of-context run, the probes report yosys cells before
  packing, and no script in this repository reproduces the former.
  `ecp5_prims.sv` lost `DELAYF`, `DELAYG`, `IDDRX1F` and `ODDRX1F`
  and keeps `EHXPLLL`, which `oca_clkrst` needs.

  **Two things stayed, and both are deliberate.** `oca_clkrst.sv` keeps
  its B50612D PHY reset sequencer, and `test_clkrst.py` keeps the tests
  that hold it to the datasheet's Table 86 minimums and their order,
  because `oca_clkrst` sits inside `oca_pll` — a design measured on
  silicon at bring-up step 3 — and reopening it would risk a validated
  design for no functional gain. **The price of freezing it is that its
  comments now point at code that does not exist**: `docs/STATUS.md`
  lists the dangling references line by line, and they are a recorded
  limitation, not an oversight. And `colorlight_i9.lpf` is kept
  verbatim, its fifteen RGMII and PHY pins included — twelve `rgmii_*`
  plus `phy_mdc`, `phy_mdio` and `phy_rst_n`, which is the file's
  seventeen `LOCATE`s less `clk25` and `led_n`, a distinction the file
  itself does not draw: 270 of its 327 lines are
  ECP5 analysis that surviving code cites, `run_synth.py` in three places
  and the bring-up skill's bank argument among them, while **no
  surviving design uses it as a constraint file** — every pinned design
  carries its own small `.lpf`. `docs/STATUS.md` states both, and the
  third residue: the submodule's object store under `.git/modules` was
  not purged. Upstream is deprecated rather than archived — public, and
  still serving the pin as the head of master, but unmoved since
  2025-02-27 and superseded by `taxi`, the pin being its own "Add
  deprecation notice" commit — so 6.3 MB is what this project pays for
  not depending on a third-party repository it does not control staying
  up with the sources these figures were measured on.

  **The 8-to-64-bit width conversion is not in our clock domain**: at
  ~48 MHz an 8-bit stream carries 384 Mbps, under the port it was meant
  to feed, so it happens on the 125 MHz side inside `eth_mac_1g_fifo` at
  `AXIS_DATA_WIDTH = 64`, which does the conversion and the clock domain
  crossing in one instance. Upstream's testbench did not exercise that
  configuration, so it had one of ours (`run_eth_mac.py`, 8 tests). The
  whole path from a synthetic frame back out to one had `run_oca_path.py`
  (7 tests), below. Both were deleted with the route.

  **A pinned place & route ran**, on `oca_top_stub`: 17 TRELLIS_IO
  (every pad the `.lpf` names, and the flow passes no
  `--lpf-allow-unconstrained`, so 17 is the proof), 1 EHXPLLL, 11
  IOLOGIC, and four clocks all constrained for real — `clk_sys` 260.69
  MHz against 48.08 required, `clk_tx` 347.34 against 125,
  `rgmii_rx_clk` 283.53 against 125, `clk25` 488.76 against 25. Those are
  the committed stub, rebuilt by `3421a20` after `oca_rgmii` stopped
  being reset from the wrong clock domain in it; the figures this entry
  carried until 2026-08-10 (243 / 315 / 332 / 417) belong to `24b90e6`,
  the RTL before that fix. The stub carries no crypto: it exists so that
  the clocking and the pads are known to place before the real top is
  written.

  **`oca_top` placed and routed, and as of 2026-08-11 it did NOT close
  timing.** The whole chain was in one design — pads, `oca_rgmii`, the
  MAC, the Ethernet header parse and build, ARP/IP/UDP, `oca_udp_seam`
  and `oca_core` — and `run_synth.py oca_top` exited 1 and packed
  nothing. That target no longer exists: `run_synth.py` rejects the name.

  | | measured, seed 10 |
  |---|---|
  | TRELLIS_COMB | 18719, **42.7%** of the device |
  | TRELLIS_FF | 17249, 39.3% |
  | DP16KD | 13, 12.0% |
  | MULT18X18D | 20, 27.8% |
  | TRELLIS_IO | 17, every pad the `.lpf` names |
  | `rgmii_rx_clk` | **124.22 MHz against 125 required — FAILED** |
  | `clk_tx` | 122.91 against 125 — FAILED |
  | `clk_sys` | 47.40 against 48.08 — FAILED |

  **What happened, and it is neither a lost seed nor a cost.** Until
  `54a2df8` this design closed on seed 6 at 129.87 / 130.07 / 49.41,
  measuring 17802 and 16849 — **and it had no ARP cache in it**.
  `arp_cache.v` contributed zero live flip-flops to that netlist, with
  `arp.v` at 225 against 353 and `ip_64.v` at 8 against 55. A board
  built from it receives frames and can answer nobody.

  What brought them back is one connection, attributed by building both
  ways: `clear_arp_cache`. With the two `m_ip_*` ready pins connected
  and that one left unconnected the netlist is 16849 flip-flops with
  `arp_cache.v` still at 0; connecting it gives 17249 with the cache at
  130. An undriven input is not an input reading zero — yosys may treat
  it as don't-care and take the value that simplifies most, here "the
  cache is permanently clearing", which kills its ports and its storage.
  The two ready pins, which fix the ICMP wedge, cost nothing.

  So **+881 of the 917 TRELLIS_COMB and all 400 TRELLIS_FF are not what
  the ICMP fix cost — the ready pair costs 36 LUTs and no flip-flops.
  They are the design being complete for the first time**, and the
  129.87 MHz belongs to a netlist that was missing part of it. There is
  no regression to recover from: there is a whole design that has never
  closed, and a partial one that did.

  **32 placer seeds, and `rgmii_rx_clk` clears 125 MHz on none of
  them.** Best 124.22 (seed 10, short by 0.63%), second best 117.32, and
  the bulk between 105 and 117 — so the best is a tail event and not a
  cluster near the target. `clk_tx` clears on 18 of 32 and `clk_sys` on
  20, but no seed carries all three. The seed lottery is spent as a
  lever. `oca_top`'s DESIGNS entry recorded 10 because it was the best
  measured, not because it worked; that entry was deleted with the
  design.

  **This was the top open item until 2026-08-12, and it is not an item
  any more**: the route closed for want of an RJ45 socket, which no
  placement, no vendor patch and no seed could have supplied. The fix
  that cost the clock stayed to the end, and for the right reason -- a
  board that closes timing and stops receiving on the first ping is
  worth less than one that misses a clock, and the ICMP defect is proved
  by test while the clock is a number in a report. What it would have
  needed is less logic competing for the fabric around the receive path,
  the same conclusion the occupancy study reached, with a harder
  constraint; nothing was found that delivered it.

  42.7% against the 47.3% the area sum predicted: adding a core measured
  alone to a port measured alone still counts twice the logic the
  optimiser shares, though by less than before.

  Superseded, and kept because the comparison is the finding: on the RTL
  before the fix, `rgmii_rx_clk` cleared its target on two seeds of
  thirteen, `clk_tx` on six, and they coincided once. The whole sweep is
  in `hw/syn/README.md`, with the levers that have been ruled out and
  the measurements that ruled them out.

  **The failing path is entirely inside the MAC's receive FIFO** —
  `rx_fifo`, its async FIFO and the width adapter beside it — 64%
  routing and 30% logic, which says congestion rather than depth, and
  the same module alone said it louder: rebuilt on this toolchain,
  `oca_top_mac` reached **146.35 MHz** on `rgmii_rx_clk`, 17% clear of
  the target, against 124.22 for the same path in the whole design. Those
  22 MHz are what the rest of the design costs it. (This entry said
  132.98 MHz until 2026-08-11, measured on an earlier state of that
  target.) Nothing about the MAC, the FIFO or the placer settings will
  return them; what is left is the conclusion the occupancy study kept
  reaching, that there is too much logic on this device.

  **Two vendor defects had to be patched to get here, and both were
  blocking.** They lived in `hw/vendor/patches/`, applied to an extracted
  copy of the pin by `hw/vendor/vendor_patches.py` — the submodule was
  never written, and `run_synth.py` and the runners refused to build
  without them. All of that was deleted on 2026-08-12; the two defects
  are described below because they are properties of the upstream
  sources, which anyone reopening this route would meet again.

  1. **`tkeep` was zero on every receive beat.** `axis_adapter`'s upsize
     branch ignored `S_KEEP_ENABLE` where its bypass branch honours it,
     and `eth_mac_1g_fifo` ties that port to zero having set the
     parameter to 0. `eth_axis_rx` computes which bytes are valid from
     that signal, so no byte was valid anywhere downstream: the board
     would not have received anything, at any clock.
  2. **The FCS comparison sat on the 125 MHz critical path**, between
     `crc_next` and its register. Moving it to the registered
     `crc_state` one cycle later took the receive path from 102.59 to
     115.77 MHz and took the CRC out of the critical path entirely.

  `oca/hw/sim/run_eth_mac.py` was the MAC's testbench, written before the
  patches and against the unpatched module: 8 tests, and it is what made
  the `tkeep` patch provable — reverted, it went 7/8. The FCS patch is
  observably inert by construction, so what the suite proved about it is
  that it changed nothing.

  **The flow packs a bitstream**, and only where one can mean something:
  `run_synth.py` runs `ecppack` after `check_timing` has passed, and only
  for a design that carries an `.lpf`. An `--out-of-context` build still
  stops at the report — it has no IO buffers, so a bitstream from one
  would configure a device that drives no pin — and a design that misses
  a constraint exits 1 and writes no `.bit`. **Nor does one outlive the
  report it was built with**: the bitstream is removed as soon as
  nextpnr has replaced that report, so `build/` never holds a `.bit`
  from one placement beside the numbers of another. A run that fails
  before or during place & route takes nothing, because nextpnr writes
  no report unless it succeeds and the old pairing still holds.
  **It packed nothing for `oca_top`**, because that design missed three
  constraints. The gate was doing its job rather than failing: the only
  `oca_top.bit` this project ever had was 527142 bytes with a header
  reading `Part: LFE5U-45F-6CABGA381`, built from the netlist before the
  ICMP fix, and it no longer exists. The design that demonstrated the
  passing side was `oca_top_stub`, 163854 bytes, carrying no crypto and
  no MAC. Both were deleted on 2026-08-12; the design that packs a
  bitstream with crypto in it is `oca_uart_crypto`, 423971 bytes on
  yosys `f77ddfb87` and 423213 on `41a4b5a03`, and it reaches
  the host over the serial line instead.

  **The whole path was tested, and testing it found a defect that would
  have killed the board.** `run_oca_path.py` (7 tests) generated a
  harness holding `oca_eth_mac_1g_fifo_64`, both halves of
  `oca_eth_axis_64`, `oca_udp_complete_64`, `oca_udp_seam` and
  `oca_core`, wired as `oca_top` wires them, and drives `gmii_rxd` /
  reads `gmii_txd`. A synthetic frame goes in and a frame comes out:
  ARP answered, a stats request, a seal and its open compared byte for
  byte against `aead_model`, a corrupted tag proved to put **no
  plaintext on the wire** — asserted on the whole reply frame, padding
  included, beside an assertion that the body is empty — and two peers
  in flight each answered at their own address.

  `docs/design/2026-08-05-ethernet-integration.md` called that leak
  assertion defect 1, "could not fail", **and that entry is withdrawn**
  as of 2026-08-12, in the document itself. Its argument holds only once
  the body is known empty: that is when the reply is 60 bytes and the
  only place left to hide is ten bytes of padding, which 48 bytes of
  plaintext cannot fit in — and an empty body is what the assertion
  beside it establishes, so the reasoning was circular. `rsp["frame"]`
  has no fixed length: `oca_udp_seam.sv:421` drives `tx_length = 16'd0`
  because the checksum generator recomputes it from the payload that
  arrives, and the same assertion runs over a 114-byte seal reply six
  lines earlier in the same test. Break the tag comparison — the
  mutation the test's own docstring names — and the body carries the
  plaintext, the frame grows past 60, and that assertion is the first to
  fail. What is true is that the two assertions overlap while the body
  is empty. Neither is inert, and defect 1 was the one of the four
  backed by arithmetic rather than by a mutation that was run.

  Not at the pads, and the reason is structural rather than a cost:
  `EHXPLLL` in `ecp5_prims.sv` is a blackbox with an empty body, so
  under Verilator `CLKOP`, `CLKOS` and `LOCK` never move, `pll_locked`
  stays 0 and every reset in the design is held asserted forever
  (`oca_clkrst.sv:186-198` says so).

  What it found on its first run: **`oca_top` left `m_ip_hdr_ready` and
  `m_ip_payload_axis_tready` unconnected**, so the first IPv4 frame that
  is not UDP — an ICMP echo, a stray TCP segment, one IGMP report — was
  never consumed, `ip_eth_rx_64` held it, and the board stopped
  receiving. Every one of the nineteen status and error wires read zero
  while it happened. The wrapper had predicted it in writing
  (`oca_udp_complete_64.v:29-44`) and the yosys log carried the warning,
  one of forty on that instance, and nothing gated on it. Fixed, and the
  fix is proved load-bearing: reverting it fails only the new test,
  while the other six pass unchanged.

  **None of the Ethernet path ever ran on hardware, and none of it
  will**: no `oca_top` bitstream was ever loaded, and the route was
  retired on 2026-08-12. What did run on silicon is the ladder above --
  the IDCODE, the blink, the PLL and the serial console.

  What the board alone could have settled is listed in that design
  document, which is now marked closed: the RGMII delay value above all,
  the other item on the list -- the IO bank voltages -- having been
  closed on 2026-08-11 by the bank 6 measurement above. The delay trap
  is written down there, which is now the only place it survives:
  `oca_rgmii.sv` was deleted with the route and the bring-up skill's
  steps 4 and 5 point at that document. It is kept as history rather
  than as a sweep to run: the receive delay
  sits on the data lines by LiteEth precedent and not by geometry, and a
  one-unit-interval misalignment cannot be repaired by any tap value,
  with `link_up` low while the PHY's own link LED is lit as the tell.
