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
`verilog-ethernet` (Alex Forencich, **MIT licence**), which has working
ECP5 support; it will arrive as a submodule when the board does. Writing
a MAC from scratch is weeks of work on well-trodden ground where every
bug presents as "the link does not come up". That choice sets the
project's RTL boundary: `verilog-ethernet` hands over the UDP payload as
an 8-bit AXI-Stream, and everything in `hw/rtl/` sits behind that
interface — which is why `oca_core` can be tested end to end with no
Ethernet in the simulation at all
(`docs/design/2026-08-03-host-protocol.md`).

## Environment rules

- **No system-wide installs without explicit permission.** Everything
  lives under this directory (`tools/`, `oca/.venv/`).
- Developed on CachyOS with Python 3.14. cocotb PyPI releases (<= 2.0.1)
  reject Python >= 3.14, so cocotb is installed from git master:
  `oca/.venv/bin/pip install "git+https://github.com/cocotb/cocotb.git@master"`
- To rebuild Verilator: clone `verilator/verilator` branch `stable`,
  `autoconf && ./configure --prefix=$PWD/../../tools/verilator` with
  `help2man` from `tools/help2man` in PATH, `make -j && make install`.
- To rebuild the ECP5 toolchain (sources in `tools/src/`, all installed
  with `-DCMAKE_INSTALL_PREFIX=tools/<name>`):
  - Eigen 3.4.0 — header-only, `cmake --install` into `tools/eigen`;
  - prjtrellis — cmake on `libtrellis/`; `pytrellis` builds against
    Python 3.14 with the bundled pybind11;
  - yosys — CMake (not the old Makefile), Ninja, submodules included
    (`abc`, `slang`);
  - nextpnr — `-DARCH=ecp5 -DECP5_DEVICES=45k -DBUILD_PYTHON=OFF
    -DTRELLIS_INSTALL_PREFIX=tools/trellis
    -DEigen3_DIR=tools/eigen/share/eigen3/cmake`. Building only the 45k
    chipdb keeps the build short; add devices when other boards appear.
- System dependencies used as-is (already present on the dev machine, no
  installs performed): boost 1.91 (incl. `libboost_python314`), tcl 8.6,
  readline, libffi, zlib.

## How to build and test

Software (Phase 1), from `oca/`:

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
ctest --test-dir build          # 114/114 vectors must pass
./build/oca_bench               # benchmarks
```

RTL (Phase 2), from `oca/`:

```sh
.venv/bin/python hw/sim/run_chacha20.py           # 5/5 pass
.venv/bin/python hw/sim/run_poly1305.py           # 4/4 pass
.venv/bin/python hw/sim/run_chacha20_poly1305.py  # 7/7 pass
.venv/bin/python hw/sim/run_dirty_pad.py          # 2/2 pass
.venv/bin/python hw/sim/run_keystore.py           # 4/4 pass
.venv/bin/python hw/sim/run_pktbuf.py             # 3/3 pass
.venv/bin/python hw/sim/run_oca_core.py           # 9/9 pass
```

The protocol model has no DUT, so it runs as plain Python rather than
through a simulator — pulling Verilator into a pure Python check would
be noise:

```sh
cd hw/sim && ../../.venv/bin/python test_proto_model.py   # prints "proto_model: OK"
```

`run_dirty_pad.py` is separate from the official-vector suite on
purpose: it drives random garbage into the bytes past `in_len` instead
of zeros, and is the only test that can fail when the engine's padding
masking is wrong. The suite above zero-pads and passes with that masking
removed entirely.

Lint (must stay clean, `-Wall`):

```sh
../tools/verilator/bin/verilator --lint-only -Wall hw/rtl/*.sv --top-module oca_core
```

ECP5 synthesis (Phase 2, from `oca/`), see `hw/syn/README.md`:

```sh
.venv/bin/python hw/syn/run_synth.py chacha20_poly1305   # ~2 min
.venv/bin/python hw/syn/run_synth.py oca_core            # ~3 min
```

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
- Git: work on branches; never commit directly on the default branch.

## Current status

- Phase 1: done, 114/114 vectors pass, zero warnings. Baseline on the
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
  **Corrected MVP target: ~1.26 Gbps**, i.e. two engines x 1.6
  bytes/cycle at 49.28 MHz = 158 MB/s, saturating one GbE port
  (125 MB/s) with **26% margin**. The board has two PHYs
  (`BOM-MVP.md`); **the second cannot be fed on this device** — a
  recorded limit, not an oversight. This supersedes the 1.97-2.07 Gbps
  three-engine projection and the >= 2 Gbps target (`SPEC.md`,
  `oca/hw/syn/README.md` "The occupancy study"). Note that
  `ROWS_PER_CYCLE` in `poly1305.sv` competes with replication for the
  same 72 multipliers — 2 rows per cycle costs 40 per engine, so one
  engine instead of two — while removing the one-cycle `p_blk` bubble
  is free.
- **The host protocol is implemented and verified** (design:
  `docs/design/2026-08-03-host-protocol.md`). Four new modules behind an
  8-bit AXI-Stream boundary: `oca_keystore.sv` (8 key slots, each with a
  loaded bit, cleared on reset), `oca_pktbuf.sv` (a 2048-byte byte-wide
  buffer), `oca_proto.sv` (the protocol FSM) and `oca_core.sv` (wiring
  only). Store and forward throughout: the request is buffered whole
  before the engine sees it and the response is built whole before a
  byte leaves, which is what lets a failed tag return no plaintext at
  all. Suites: keystore 4/4, pktbuf 3/3, oca_core 9/9, plus
  `test_proto_model.py` as plain Python. Lint `-Wall` clean with
  `--top-module oca_core`. **The security property has a test that can
  fail**: `test_corrupt_tag_yields_no_plaintext` asserts on the leak
  rather than on the status code, and was checked against a deliberately
  broken tag comparison.
- **`oca_core` synthesised: 11149 LUTs (25.4%), 10842 FF (24.7%), 20
  MULT18X18D (27.8%), 2 DP16KD (1.9%), Fmax 50.95 MHz at seed 1**
  (50.59 MHz mean over seeds 1-4). **Both packet buffers infer block
  RAM** — one DP16KD each, zero LUT RAM cells in the netlist — which was
  the open question, since 4096 bytes in LUTs would have been a serious
  regression. The protocol layer costs +3791 LUTs and +5104 FF over the
  engine, **no multipliers at all** (so it does not cost an engine), and
  is **not on the critical path**: every entry in nextpnr's report cites
  `chacha20.sv` lines 58-64, none cites the protocol modules. Against
  the engine's 52.83 MHz mean the -4.2% is inside the seed spread and is
  routing, not logic (+0.14 ns logic, +0.82 ns routing).
- **End-to-end throughput ~0.062 Gbps — 9% of the engine's own
  ~0.68 Gbps and 6% of the GbE link.** Measured differentially in
  simulation over seal commands of 4/8/12/16 blocks, a 64-byte block
  costs **415 cycles**, exactly linear: 64 in (1 byte/cycle) + 159
  through buffer/engine/buffer + **192 out (3 cycles/byte)**. The
  implementation plan predicted 168 and was 2.5x optimistic — it modelled
  only the middle term and omitted both stream transfers. The response
  path is the largest single cost and is a handshake, not a bandwidth
  limit: `oca_proto` spends a cycle fetching, a cycle asserting
  `m_tvalid` and a cycle completing, where the receive side holds
  `s_tready` for the whole of `S_RX` and streams at a byte per cycle.
  **Nothing here has run on silicon** — Verilator cycle counts and
  `--out-of-context` synthesis, with no IO, no pin constraints and no MAC.
- Next: **the Ethernet integration**, which needs the board (expected
  ~2026-08-17): `verilog-ethernet` as a submodule, the RGMII wrapper
  with its ECP5 DDR primitives, PLL, reset and the Colorlight i9 pin
  constraints. Cheapest RTL follow-up meanwhile is the response
  handshake — holding `m_tvalid` up and pipelining the buffer read, as
  the receive feed already does, takes a block from 415 cycles to 287
  and throughput to ~0.090 Gbps (+45%) for a rewrite of one state.
  **The 8-bit datapath inside `oca_core` is moving to 64 bits**: at 8
  bits the buffer needs 66 cycles to assemble a block the engine
  consumes in 40, so it cannot feed even one engine (amendment of
  2026-08-04 in `docs/design/2026-08-03-host-protocol.md`). The 8-bit
  AXI-Stream at the MAC boundary is unaffected.
