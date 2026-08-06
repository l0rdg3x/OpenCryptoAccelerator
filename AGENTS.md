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
primitives, the RX clock delay and its ECLK routing — is therefore ours
to write, behind the wrapper SPEC.md's portability rule requires.**
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
than in our clock domain. `eth_mac_1g_rgmii_fifo` with
`AXIS_DATA_WIDTH = 64` does the width conversion and the clock domain
crossing in one instance, on the correct side of each — that
configuration is not exercised by the upstream testbench, so it needs
one of ours.

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

  It fetches into `tools/src/<name>`, installs into `tools/<name>`, and
  writes nothing else. It never installs a system package: missing build
  tools are reported together and it stops.

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
  (0.67+), nextpnr `89454078`, cocotb `82d0eed5`. cocotb comes from git
  because its PyPI releases (<= 2.0.1) reject Python >= 3.14, and it is
  pinned to a commit rather than `@master`, which drifts under the pin
  without saying so.

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
ctest --test-dir build          # 114/114 vectors must pass
./build/oca_bench               # benchmarks
```

RTL (Phase 2), from `oca/`:

```sh
.venv/bin/python hw/sim/run_chacha20.py           # 5/5 pass
.venv/bin/python hw/sim/run_poly1305.py           # 4/4 pass
.venv/bin/python hw/sim/run_chacha20_poly1305.py  # 7/7 pass
.venv/bin/python hw/sim/run_dirty_pad.py          # 2/2 pass
.venv/bin/python hw/sim/run_secret_zeroise.py     # 2/2 pass
.venv/bin/python hw/sim/run_keystore.py           # 4/4 pass
.venv/bin/python hw/sim/run_pktbuf.py             # 12/12 pass, + 3 at BYTES=16
.venv/bin/python hw/sim/run_oca_core.py           # 29/29 pass
.venv/bin/python hw/sim/run_attack.py             # 16/16 pass
.venv/bin/python hw/sim/run_keystore_gate.py      # 4/4 pass, post-synthesis
.venv/bin/python hw/sim/run_proto_gate.py         # 2/2 pass, post-synthesis
```

81 RTL tests, three of them run a second time at the smallest BYTES
oca_pktbuf accepts, plus 6 on a synthesised netlist.

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
  **Corrected MVP target: two ports at 56% of line rate each, not one
  port saturated.** The board has two PHYs (`BOM-MVP.md`) and
  `oca_dual` wires the two engines as two independent AXI-Stream pairs,
  one per core — so this is **0.561 Gbps per port at a 1500-byte MTU,
  1.121 Gbps aggregated across both**, and **neither port is
  saturated**. Both PHYs can be fed; saturating one of them would need
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
  MULT18X18D (27.8%), 4 DP16KD (3.7%)**, 47.93 MHz at seed 1 — the
  figures `run_synth.py oca_core` reproduces today, on a netlist whose
  key store is present (see the `cmp2lut` bullet below).

  **What secret zeroisation cost**, measured seed 1 against the same
  toolchain, one step at a time from 11590 / 12043 / 48.52 MHz:
  clearing the engines' secret registers is **+670 LUTs (+5.8%)**, −30
  FF, 49.65 MHz; walking the packet memory adds **+48 LUTs (+0.4%)**,
  +20 FF, 47.93 MHz. Together **+718 LUTs (+6.2%)** and no change in
  multipliers or block RAM. On this device it costs no DSP — synth_ecp5
  maps through `dsp_map_18x18.v`, which connects no clock or reset, so
  those registers were already in fabric — but the LUT bill is real and
  it is logic, not routing. Fmax moves in both directions across the
  three points and stays inside the 4.8% seed spread documented below,
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
  appears on any of the four** — but on the committed netlist at seed 1
  the worst path is `oca_proto`'s `data_off` adder, dominated by one
  route across the die. First time the protocol layer has shown up
  there; one seed, so watch it rather than conclude from it
  (`hw/syn/README.md`, "Where the committed design stands").
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
- **The MVP target: two ports at 56% of line rate each, not one port
  saturated.** `run_synth.py oca_dual` builds two `oca_core` and four
  placer seeds give **23191 LUTs (52.9%), 24086 FF (54.9%), 40
  MULT18X18D (55.6%), 8 DP16KD, Fmax 47.07 / 49.61 / 47.99 / 47.98,
  mean 48.16 MHz** (spread 5.4%). Replication is linear to eleven LUTs
  of glue against 2 x 11590, and the second core costs 0.7% of clock,
  inside that spread.

  **What that buys depends on how the cores are wired to the ports, and
  `oca_dual` answers it: two independent AXI-Stream pairs, one per
  core.** One core per port is therefore **0.561 Gbps at a 1500-byte
  MTU — 56% of line rate — and 0.222 Gbps on 64-byte packets**, with
  1.121 Gbps aggregated across both ports. Neither port is saturated.
  Saturating one would need both cores behind it, which needs a
  distributor and a collector that do not exist, and which the per-core
  key store makes non-trivial: a slot is loaded into one core and only
  that core can use it.

  **With the secret zeroisation merged, four seeds give 24602 LUTs
  (56.1%), 24066 FF (54.9%), 40 MULT18X18D, 8 DP16KD, Fmax 50.37 /
  48.12 / 48.05 / 49.03, mean 48.89 MHz.** That is +1411 LUTs over the
  pre-zeroisation pair — 705 per core, against the 718 measured on one
  core alone — and the clock is 1.5% *better*, which is inside the seed
  spread and means the zeroisation costs area and not time.

  **And one Ethernet port costs 8422 LUTs, 19.2% of the device**,
  measured out-of-context on this toolchain rather than estimated:
  `udp_complete_64` 7147, `eth_mac_1g_rgmii_fifo` at 64 bits 1214, and
  ~61 for the RGMII front end. What that leaves:

  | configuration | LUTs | of device |
  |---|---|---|
  | two cores, two ports | 41446 | **94.5%** |
  | two cores, one port | 33024 | **75.3%** |
  | one core, one port | 20730 | 47.3% |

  Two ports are not merely tight, they are out. Two cores behind one
  port land at 75.3%, against the 76.4% at which this device stopped
  routing in the occupancy study — and would additionally need a
  distributor, a collector and an answer to the per-core key store. **On
  the current RTL the MVP that fits is one core on one port, 0.561 Gbps
  at MTU.**

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

  Two caveats. These figures **predate the secret zeroisation**, which
  costs 718 LUTs per core — two cores should land near 24600 (56% of
  the device), and the two-core Fmax with it is not measured. And
  **nothing here has run on silicon**: Verilator cycle counts and
  `--out-of-context` synthesis, with no IO, no pin constraints, no MAC
  and no PLL.
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
  MULT18X18D unchanged at 4 and 20, Fmax 49.31 -> 48.84 MHz mean
  (-1.0%, inside a 4.8% seed spread over five seeds). This is the price
  of having a key store at all, not a regression in area — what it does
  cost is router effort, at least 2.5x. See `oca/hw/syn/README.md`,
  "The cmp2lut trap".
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
- Next: **the Ethernet integration**, designed in
  `docs/design/2026-08-05-ethernet-integration.md` and needing the board
  (expected ~2026-08-17): `verilog-ethernet` as a submodule, the RGMII
  front end written by us around the ECP5 DDR primitives with its RX
  delay as a parameter rather than a constant, PLL, reset and the
  Colorlight i9 pin constraints. **The 8-to-64-bit width conversion is
  not in our clock domain**: at ~48 MHz an 8-bit stream carries
  384 Mbps, under the port it is meant to feed, so it happens on the
  125 MHz side inside `eth_mac_1g_rgmii_fifo` at `AXIS_DATA_WIDTH = 64`,
  which does the conversion and the clock domain crossing in one
  instance. Upstream's testbench does not exercise that configuration,
  so it needs one of ours — as do the RGMII wrapper and the whole path
  from a synthetic frame back out to one, all three of them buildable
  without the board. `openFPGALoader` is not in `tools/` yet and there
  is no programmer of any kind in the tree. What the board alone can
  settle is listed in that document: the RGMII delay value and the IO
  bank voltages above all.
