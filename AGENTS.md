# AGENTS.md — OpenCrypto Accelerator (OCA)

Open-source FPGA crypto accelerator. Canonical project specification:
`SPEC.md` (read it first — it drives all phase work).
MVP development BOM: `BOM-MVP.md`.

## Project shape

- **Phase 1 (done)**: abstract crypto API in C11 + OpenSSL 3 software
  backend + official test vectors + benchmarks. In `oca/`.
- **Phase 2 (in progress)**: FPGA cores in SystemVerilog, verified with
  cocotb + Verilator against the same official vectors. In `oca/hw/`.
- **MVP target**: Lattice ECP5 (Colorlight i9 v7.2) + GbE, open toolchain
  (yosys/nextpnr-ecp5). PCIe phase later: Artix-7 + LitePCIe (Vivado is
  the documented exception to the open-toolchain rule).
- Linux/WireGuard integration via kernel crypto API — **no kernel
  patches**. FreeBSD if_wg needs a minimal patch or is out of scope v1.

## Repository layout

```
SPEC.md                     project specification
BOM-MVP.md                  MVP development bill of materials
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
  .venv/                    python venv (cocotb) — NOT committed
tools/                      local tool builds — NOT committed
  verilator/                Verilator 5.050 install (built from source, branch stable)
  help2man/                 help2man install (needed to build Verilator)
  yosys/                    yosys 0.67+ install (CMake build, slang frontend)
  trellis/                  prjtrellis install (ECP5 database + ecppack)
  nextpnr/                  nextpnr-ecp5 install (45k chipdb only)
  eigen/                    Eigen 3.4.0 headers (nextpnr analytic placer)
  src/                      upstream sources for the above (shallow clones)
```

**The Ethernet MAC is an external dependency, not project RTL.** The
1G MAC, the RGMII interface and the IP/ARP/UDP stack come from
`verilog-ethernet` (Alex Forencich, **MIT licence**); it will arrive as
a submodule. Writing a MAC from scratch is weeks of work on well-trodden
ground where every bug presents as "the link does not come up". That
choice sets the project's RTL boundary: the stack hands over the UDP
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
primitives and the receive delay — is therefore ours to write, behind
the wrapper SPEC.md's portability rule requires**, and it now exists as
`oca/hw/rtl/oca_rgmii.sv`. (This entry said "the RX clock delay and its
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
under a permissive licence, so verilog-ethernet at MIT stays the choice.
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
  the exact revisions every number in this repository was measured on:

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

- `tools/` is gitignored, so the script and the patch beside it are the
  only record of how to get one. **A yosys built without
  `oca/hw/syn/patches/yosys-cmp2lut-signed-negative-constant.patch`
  silently deletes the key store from the netlist** — the script applies
  it, and `--check` proves the result with the same behavioural probe
  `run_synth.py` refuses to synthesise without: `$signed(a) >= -8` is a
  tautology and must map to an all-ones LUT. `techlibs/common/cmp2lut.v`
  is read at run time from `tools/yosys/share/yosys/`, so an
  already-built yosys is repaired by copying the patched file there, no
  rebuild.

- Pins, for reference: help2man 1.49.3, Verilator `3d2421f3` (v5.050),
  Eigen `3147391d` (3.4.0), prjtrellis `56bb1704`, yosys `41a4b5a0`
  (0.67+), nextpnr `89454078`, openFPGALoader `85be4fa0` (v1.1.1),
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
.venv/bin/python hw/sim/run_rgmii.py              # 10/10 pass
.venv/bin/python hw/sim/run_eth_mac.py            # 8/8 pass, needs the vendor patches
.venv/bin/python hw/sim/run_udp_seam.py           # 10/10 pass, twice, at two HDR_Q_DEPTH
.venv/bin/python hw/sim/run_oca_path.py           # 7/7 pass, the whole path, needs the vendor patches
.venv/bin/python hw/sim/run_keystore_gate.py      # 4/4 pass, post-synthesis
.venv/bin/python hw/sim/run_proto_gate.py         # 2/2 pass, post-synthesis
```

123 RTL tests, twenty-two of them run a second time at a non-default
parameter — five for `chacha20` at `ROUNDS_PER_CYCLE` = 2, four for
`poly1305` at `ROWS_PER_CYCLE` = 5, three for `oca_pktbuf` at the
smallest `BYTES` it accepts and all ten of `oca_udp_seam` at
`HDR_Q_DEPTH` = 2 — plus 6 on a synthesised netlist.

`run_eth_mac.py` and `run_oca_path.py` read the patched vendor tree, so
`hw/vendor/vendor_patches.py build` has to have run; both say so and
exit non-zero rather than testing the wrong sources.

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
  synthesis bug is invisible to every suite we have. Stock yosys
  (0.67+, and upstream `main` as of 2026-08-04) mis-maps signed
  comparisons against negative constants in
  `techlibs/common/cmp2lut.v`, which `synth_ecp5` runs unconditionally;
  it deleted the entire key store from `oca_core` — 2048 key bits, 8
  loaded bits — while all 72 tests stayed green and the build reported
  success. Fixed by
  `hw/syn/patches/yosys-cmp2lut-signed-negative-constant.patch`, which
  **must be applied to any freshly built yosys**; `run_synth.py` probes
  for the defect and refuses to run without it. The lesson generalises:
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
  doubles (22.94 -> **52.68 MHz**). **AEAD Fmax was unchanged**
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
  flip-flop. A 64-byte block now costs **57 cycles** (measured), so
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
  **Corrected MVP target: two ports at 56% of line rate each, not one
  port saturated** (56% being that figure at the 48.16 MHz of the day)
  — superseded in turn on 2026-08-05, when `d4ee09f` measured the cost
  of an Ethernet port and two ports turned out not to fit; the standing
  target is in the two-core bullet below. The board has two PHYs
  (`BOM-MVP.md`) and
  `oca_dual` wires the two engines as two independent AXI-Stream pairs,
  one per core — so this is **0.569 Gbps per port at a 1500-byte MTU,
  1.138 Gbps aggregated across both** on the committed pair's 48.89 MHz,
  and **neither port is saturated**. That 48.89 is an out-of-context
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
- **`oca_core` as committed: 12308 LUTs (28.1%), 12033 FF (27.4%), 20
  MULT18X18D (27.8%), 4 DP16KD (3.7%)**, and **Fmax 47.93 / 50.91 /
  51.03 / 49.76, mean 49.91 MHz** over four placer seeds (measured
  2026-08-09; area identical on all four, as it must be). The figures
  `run_synth.py oca_core` reproduces today, on a netlist whose key
  store is present (see the `cmp2lut` bullet below). The spread is
  **6.5%** — wider than the pair's 4.8%, which is why a single seed
  from this design settles nothing. This entry carried only seed 1
  (47.93) until the sweep was run.

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
- **Two cores measured, and the standing target: one core on one port.**
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
  the committed pair (48.89 MHz mean, below) one core per port is
  **0.569 Gbps at a 1500-byte MTU — 56.9% of line rate — and 0.226 Gbps
  on 64-byte packets**, with 1.138 Gbps aggregated across both ports.
  Neither port is saturated. (This read 0.561 / 0.222 / 1.121 until
  2026-08-09: those are the same formula at 48.16 MHz, the
  pre-zeroisation pair, left standing when the clock above was
  corrected to 48.89.)
  Saturating one would need both cores behind it, which needs a
  distributor and a collector that do not exist, and which the per-core
  key store makes non-trivial: a slot is loaded into one core and only
  that core can use it.

  **With the secret zeroisation merged — the pair as committed — four
  seeds give 24602 LUTs (56.1%), 24066 FF (54.9%), 40 MULT18X18D, 8
  DP16KD, Fmax 50.37 / 48.12 / 48.05 / 49.03, mean 48.89 MHz**
  (spread 4.8%), measured 2026-08-05 in `d4ee09f`. The 2026-08-09 audit
  reports re-running all four and getting them exactly; what survives
  in `hw/syn/build/` is the last of them, `oca_dual.report.json` at
  02:22 that day reading 49.029 MHz, which matches seed 4. That is
  +1411 LUTs over the
  pre-zeroisation pair — 705 per core, against the 718 measured on one
  core alone — and the clock is 1.5% *better*, which is inside the seed
  spread and means the zeroisation costs area and not time.

  **And one Ethernet port costs 8422 LUTs, 19.2% of the device**,
  measured out-of-context on this toolchain rather than estimated:
  `udp_complete_64` 7147, `eth_mac_1g_rgmii_fifo` at 64 bits 1214, and
  ~61 for the RGMII front end. The MAC figure is that module as
  measured; the build now uses `eth_mac_1g_fifo`, which is the same
  wrapper without the `rgmii_phy_if` the ~61 accounts for, so the total
  does not move. **Two modules are missing from that figure**:
  `eth_axis_rx` and `eth_axis_tx`, which `udp_complete_64` does not
  instantiate and which `oca_top` reads and needs
  (`docs/design/2026-08-05-ethernet-integration.md`), so 8422 is a floor
  for a port and not its cost. What that leaves:

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
  estimates of unknown tightness in the same direction. **On the current
  RTL the MVP that fits is one core on one port, 0.581 Gbps at MTU** —
  the single core's own mean of 49.91 MHz through the same cycle model,
  58.1% of line rate, 0.230 Gbps on 64-byte packets. That clock is the
  core placed **alone and out of context**: no MAC beside it, no IO, no
  PLL, so it is the ceiling that configuration could reach and not a
  measurement of it.

  **And the ceiling is not the clock the board runs.** `oca_top`
  instantiates `oca_clkrst`, which delivers `clk_sys` at 625/13 =
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
  rung above is **untested rather than unreachable**, and moot until the
  receive clock closes.
  The device does carry four PLLs and this design uses one, so a second
  one for `clk_sys` is still a door nobody has opened — though it would
  not help until the receive clock closes.

  **Read verilog-ethernet with `read_verilog`, never `read_slang`.**
  Measured on the same modules: `axis_async_fifo` is 169 LUTs and 3
  DP16KD through `read_verilog` and 6454 LUTs with no block RAM at all
  through `read_slang`; `eth_mac_1g_fifo` is 1185 against 12620. Slang
  does not infer these memories and spills them into logic, so a mixed
  design read entirely through slang would measure an order of magnitude
  too large and be abandoned for the wrong reason.

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
  already dead, as 2056 self-holding registers. Fixed by
  `oca/hw/syn/patches/yosys-cmp2lut-signed-negative-constant.patch`
  (reported upstream 2026-08-05 as YosysHQ/yosys#6085, its text kept in
  `oca/hw/syn/patches/README.md`); `run_synth.py` now refuses an
  unpatched toolchain and asserts the key store's storage against the
  netlist, and `run_keystore_gate.py` replays the key store tests on the
  synthesised netlist — 2 of its 4 fail without the patch. The same net
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
- **The Ethernet integration is merged** (`c153934`), designed in
  `docs/design/2026-08-05-ethernet-integration.md`. Everything it needed
  to be built is written and tested, section 8's whole-path testbench
  included. **The board arrived on 2026-08-11**, six days before it was
  expected, and bring-up is what is next: everything that could be
  settled without it has been.

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
  the short flash and not its complement, which settles three things at
  once: **the LED is active low**, litex's `user_led_n` being right on a
  point no source had measured; the 25 MHz oscillator is on P3, since
  the period is the predicted 1.34 s; and the bitstream path runs end to
  end. `oca_top_stub` cannot do this job and its LED comment claimed it
  could; both are corrected.

  **There is no `oca_top` bitstream to load, and that is deliberate.**
  It misses 125 MHz on all 32 seeds, and `pack()` refuses to write a
  bitstream for a design that missed its clock, so the bench cannot get
  past the MAC on the real top until the receive path closes.

  What exists in the tree: `verilog-ethernet` as a submodule at
  `oca/hw/vendor/verilog-ethernet` (77320a94); `oca_rgmii.sv`, the RGMII
  front end around the ECP5 DDR primitives, with the receive delay
  movable at run time rather than fixed in the bitstream (10 tests);
  `oca_clkrst.sv`, one PLL and three clock domains with a reset
  synchroniser each (7 tests); `ecp5_prims.sv`; `colorlight_i9.lpf`;
  the parameter-fixing wrappers under `oca/hw/rtl/vendor/` that the
  8422-LUT port measurement was taken on; and `oca_udp_seam.sv`, the
  join between the UDP stack and `oca_core` (10 tests, run at two queue
  depths).

  **The 8-to-64-bit width conversion is not in our clock domain**: at
  ~48 MHz an 8-bit stream carries 384 Mbps, under the port it is meant
  to feed, so it happens on the 125 MHz side inside `eth_mac_1g_fifo` at
  `AXIS_DATA_WIDTH = 64`, which does the conversion and the clock domain
  crossing in one instance. Upstream's testbench does not exercise that
  configuration, so it has one of ours (`run_eth_mac.py`, 8 tests). The
  whole path from a synthetic frame back out to one has `run_oca_path.py`
  (7 tests), below.

  **A pinned place & route now runs**, on `oca_top_stub`: 17 TRELLIS_IO
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

  **`oca_top` places and routes, and as of 2026-08-11 it does NOT close
  timing.** The whole chain is in one design — pads, `oca_rgmii`, the
  MAC, the Ethernet header parse and build, ARP/IP/UDP, `oca_udp_seam`
  and `oca_core` — and `run_synth.py oca_top` exits 1 and packs nothing.

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
  lever. `oca_top`'s DESIGNS entry records 10 because it is the best
  measured, not because it works.

  **This is the top open item.** The fix stays: a board that closes
  timing and stops receiving on the first ping is worth less than one
  that misses a clock, and the ICMP defect is proved by test while the
  clock is a number in a report. What it needs is less logic competing
  for the fabric around the receive path — the same conclusion the
  occupancy study reached, now with a harder constraint. Not yet tried:
  a vendor patch that discards non-UDP frames inside `udp_complete_64`
  without exposing the raw-IP port at all, which would consume the frame
  without keeping its datapath alive.

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
  the same module alone says it louder: rebuilt on this toolchain,
  `oca_top_mac` reaches **146.35 MHz** on `rgmii_rx_clk`, 17% clear of
  the target, against 124.22 for the same path in the whole design. Those
  22 MHz are what the rest of the design costs it. (This entry said
  132.98 MHz until 2026-08-11, measured on an earlier state of that
  target.) Nothing about the MAC, the FIFO or the placer settings will
  return them; what is left is the conclusion the occupancy study kept
  reaching, that there is too much logic on this device.

  **Two vendor defects had to be patched to get here, and both were
  blocking.** They live in `hw/vendor/patches/`, applied to an extracted
  copy of the pin by `hw/vendor/vendor_patches.py`; the submodule is
  never written and `run_synth.py` and the runners refuse to build
  without them.

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

  `oca/hw/sim/run_eth_mac.py` is the MAC's testbench, written before the
  patches and against the unpatched module: 8 tests, and it is what makes
  the `tkeep` patch provable — reverted, it goes 7/8. The FCS patch is
  observably inert by construction, so what the suite proves about it is
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
  **And right now it packs nothing for `oca_top`**, because that design
  misses three constraints. The gate is doing its job rather than
  failing: the only `oca_top.bit` this project ever had was 527142
  bytes with a header reading `Part: LFE5U-45F-6CABGA381`, built from
  the netlist before the ICMP fix, and it no longer exists. What packs
  today is `oca_top_stub`, 163854 bytes, which carries no crypto and no
  MAC.

  **The whole path is tested, and testing it found a defect that would
  have killed the board.** `run_oca_path.py` (7 tests) generates a
  harness holding `oca_eth_mac_1g_fifo_64`, both halves of
  `oca_eth_axis_64`, `oca_udp_complete_64`, `oca_udp_seam` and
  `oca_core`, wired as `oca_top` wires them, and drives `gmii_rxd` /
  reads `gmii_txd`. A synthetic frame goes in and a frame comes out:
  ARP answered, a stats request, a seal and its open compared byte for
  byte against `aead_model`, a corrupted tag proved to put **no
  plaintext on the wire** — asserted on the 60 bytes that leave the
  board — and two peers in flight each answered at their own address.

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

  **Still not done, and none of it is simulation:** that bitstream has
  been loaded onto nothing, and nothing has run on hardware.

  What the board alone can settle is listed in the design document: the
  RGMII delay value and the IO bank voltages above all. One trap is
  written down in `oca_rgmii.sv` and in the bring-up skill: the receive
  delay sits on the data lines by LiteEth precedent and not by geometry,
  and a one-unit-interval misalignment cannot be repaired by any tap
  value. `link_up` low while the PHY's own link LED is lit is the tell.
