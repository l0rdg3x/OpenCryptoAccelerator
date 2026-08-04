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
(`docs/design/2026-08-03-host-protocol.md`). Since 2026-08-04 `oca_core`
itself is **64 bits wide with `tkeep`**, so a width converter belongs
between the MAC and it; the 8-bit boundary at the MAC is unchanged.

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
    (`abc`, `slang`). **Apply
    `oca/hw/syn/patches/yosys-cmp2lut-signed-negative-constant.patch`**
    (`git apply` in `tools/src/yosys`) — without it synthesis silently
    deletes the key store. `techlibs/common/cmp2lut.v` is read at run
    time from `tools/yosys/share/yosys/cmp2lut.v`, so an already-built
    yosys is fixed by copying the patched file there, no rebuild
    needed;
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
.venv/bin/python hw/sim/run_pktbuf.py             # 9/9 pass
.venv/bin/python hw/sim/run_oca_core.py           # 26/26 pass
.venv/bin/python hw/sim/run_attack.py             # 15/15 pass
.venv/bin/python hw/sim/run_keystore_gate.py      # 4/4 pass, post-synthesis
```

`run_keystore_gate.py` is the only suite that runs on a synthesised
netlist rather than on the RTL; it exists because everything above it
is blind to synthesis.

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
  `docs/design/2026-08-03-host-protocol.md`). Four new modules behind a
  64-bit AXI-Stream boundary with `tkeep`: `oca_keystore.sv` (8 key
  slots, each with a loaded bit, cleared on reset), `oca_pktbuf.sv` (a
  2048-byte buffer, 256 x 64 with a 1..8 byte count on writes),
  `oca_proto.sv` (the protocol FSM) and `oca_core.sv` (wiring
  only). Store and forward throughout: the request is buffered whole
  before the engine sees it and the response is built whole before a
  byte leaves, which is what lets a failed tag return no plaintext at
  all. Suites: keystore 4/4, pktbuf 5/5, oca_core 10/10, plus
  `test_proto_model.py` as plain Python. Lint `-Wall` clean with
  `--top-module oca_core`. **The security property has a test that can
  fail**: `test_corrupt_tag_yields_no_plaintext` asserts on the leak
  rather than on the status code, and was checked against a deliberately
  broken tag comparison. Two more properties the 64-bit datapath
  introduced are covered the same way: `recv_packet` asserts the bytes
  past `tkeep` are zero, so every test witnesses the final-beat mask
  (removing it fails 6 of the 10), and
  `test_partial_keep_mid_packet_fails_closed` sends a short beat before
  `tlast` and asserts status 05 with `cnt_drop` unmoved — a length
  error is not a header drop.
- **`oca_core` synthesised at 64 bits: 11429 LUTs (26.1%), 11228 FF
  (25.6%), 20 MULT18X18D (27.8%), 4 DP16KD (3.7%), Fmax 51.71 MHz at
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
  port is 36 bits, so a 64-bit word spans two blocks, and since 36-bit
  mode is 512 x 36 with only 256 words used, `BYTES` could double for
  free. The protocol layer still adds **no multipliers** (so it does not
  cost an engine) and is still **not on the critical path**: seeds 1, 3
  and 4 cite no RTL file but `chacha20.sv`, lines 58-64; seed 2, the
  slowest, lands on `poly1305.sv:140`. **No protocol module appears on
  any of the four.**
- **End-to-end throughput: 415 cycles per 64-byte block down to 64.**
  Measured differentially in simulation over seal commands of 4/8/12/16
  blocks, exactly linear across all four spans: **8 in (8 bytes/cycle) +
  48 through buffer/engine/buffer + 8 out (8 bytes/cycle)**. That is
  6.5x overall, and it is **below** 8x because the engine's cost never
  scaled with the host datapath: of the 48 middle cycles, **40 are the
  engine** (re-measured on this RTL), so only 24 of the remaining 64 are
  protocol at all. The individual phases did better than the total — the
  request path scaled exactly 8x (64 -> 8) and the response path **beat**
  8x (192 -> 8 where width alone gives 24), because the three-cycle
  handshake was replaced by a clock-enabled pipeline at the same time.
  Of the 351 cycles saved, the response accounts for 184, the middle for
  111 and the request for 56.
- **The MVP target is missed by 38%, and the clock is not the reason.**
  Two `oca_core` instances place and route at 22891 LUTs (52.2%), 22456
  FF, 40 MULT (55.6%), 8 DP16KD, **48.53 MHz** — replication is linear
  to +33 LUTs of glue and costs nothing in clock (the 8-bit pair was
  49.28). But 64 cycles per 64-byte block is **exactly one byte per
  cycle per core**, so two cores are 97.1 MB/s = **~0.78 Gbps**: 78% of
  a bare GbE port (125 MB/s), and **62% of the ~1.26 Gbps the target
  actually asks for — one port saturated *with margin*.** 22% is the
  distance to breaking even with the wire; **38% is the shortfall against
  the target** and is the honest headline. Two cores would need 62.5 MHz
  even to break even, above anything `chacha20.sv` has reached. The reason is
  that the 64 cycles are **serialised** by store and forward — 8 + 48 + 8
  in strict sequence, nothing overlapping. **Packet-level pipelining is
  the next step**: overlapping receive, process and transmit across
  successive packets makes a block cost `max(8, 48, 8) = 48` and two
  cores ~1.04 Gbps — which clears the port by 2-5%, *less than the noise
  band of the Fmax it is computed from*. **Necessary, not sufficient**:
  the margin only appears when the 8 feed/drain cycles also leave the
  loop, giving the engine's own 40 and ~1.24 Gbps (24% margin) — which is
  the ~1.26 Gbps of crypto capacity `SPEC.md` already records for two
  engines. Cost of pipelining: a second buffer per direction per core,
  4 -> 8 DP16KD per core, 16 of 108 for the pair. The security property
  survives — it is a statement about one packet, and each packet is still
  received whole, then processed whole, then transmitted.
  **Two caveats.** Two of the four placer seeds for the two-core build
  **did not route**: restarted alone after the other two finished, then
  stopped after 3 h 22 min each with the remaining arc count oscillating
  rather than descending over their last 200 router reports (seed 2
  between 53 and 2292, seed 3 between 77 and 2180). So
  the 48.53 MHz mean is over two seeds and is weaker evidence than the
  other Fmax figures here — and the two were stopped, not shown to
  diverge. This is a narrowed routability margin, not the three-core
  failure mode of the occupancy study (roughly 50000 flat arcs, unmoved
  by relaxing the constraint). And **nothing here has run
  on silicon** — Verilator cycle counts and `--out-of-context` synthesis,
  with no IO, no pin constraints, no MAC and no PLL.
- **The key store was missing from every netlist this project ever
  produced**, and is now present: a mis-mapping in yosys's
  `cmp2lut.v` folded `oca_keystore.sv`'s index bounds check to constant
  false, so all 2048 key bits and 8 loaded bits were optimised away and
  a bitstream would have answered "bad slot" to every seal and open.
  Not a regression — synthesising `95c81f7` shows the same key store
  already dead, as 2056 self-holding registers. Fixed by
  `oca/hw/syn/patches/yosys-cmp2lut-signed-negative-constant.patch`
  (upstream report drafted, not filed); `run_synth.py` now refuses an
  unpatched toolchain and asserts the key store's storage against the
  netlist, and `run_keystore_gate.py` replays the key store tests on the
  synthesised netlist — 2 of its 4 fail without the patch. Cost:
  8620 -> 11590 TRELLIS_COMB and 8311 -> 12043 TRELLIS_FF, DP16KD and
  MULT18X18D unchanged at 4 and 20, Fmax 49.31 -> 48.84 MHz mean
  (-1.0%, inside a 4.8% seed spread over five seeds). This is the price
  of having a key store at all, not a regression in area — what it does
  cost is router effort, at least 2.5x. See `oca/hw/syn/README.md`,
  "The cmp2lut trap".
- Next: **the Ethernet integration**, which needs the board (expected
  ~2026-08-17): `verilog-ethernet` as a submodule, the RGMII wrapper
  with its ECP5 DDR primitives, PLL, reset and the Colorlight i9 pin
  constraints, plus the **8-to-64-bit width conversion at the MAC
  boundary** — the 1G MAC hands over an 8-bit AXI-Stream and `oca_core`
  is now 64 bits, so the conversion belongs there and not inside the
  buffers. Cheapest RTL follow-up meanwhile is the packet-level
  pipelining above.
