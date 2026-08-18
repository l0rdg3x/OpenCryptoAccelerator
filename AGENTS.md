# AGENTS.md — OpenCrypto Accelerator (OCA)

Open-source FPGA crypto accelerator. Canonical project specification:
`SPEC.md` (read it first — it drives all phase work).
MVP development BOM: `BOM-MVP.md`.

## Project shape

- **Phase 1 (done)**: abstract crypto API in C11 + OpenSSL 3 software
  backend + official test vectors + benchmarks. In `oca/`.
- **Phase 2 (in progress)**: FPGA cores in SystemVerilog, verified with
  cocotb + Verilator against the same official vectors. In `oca/hw/`.
- **MVP target**: Lattice ECP5 (Colorlight i9 v7.2), host interface
  over the board's DAPLink USB serial (J17/H18, 115200 8N1,
  `/dev/ttyACM0`), open toolchain (yosys/nextpnr-ecp5). The board is a
  vehicle for proving the core on silicon, not a prototype of the
  product (`SPEC.md`, PHASE 2); the Ethernet route is retired
  (`docs/RECORD.md`). PCIe phase later, on other hardware (Vivado is
  the documented exception to the open-toolchain rule).
- Linux/WireGuard integration via kernel crypto API — **no kernel
  patches**. FreeBSD if_wg needs a minimal patch or is out of scope v1.

## Repository layout

```
SPEC.md                     project specification
BOM-MVP.md                  MVP development bill of materials
docs/STATUS.md              where the project stands, one page
docs/RECORD.md              the measurement record, long form
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
tools/                      local tool builds — NOT committed; one
                            directory per tool, sources under tools/src/,
                            all of it from scripts/build-toolchain.sh
```

Paths that are not here because the code is gone: the Ethernet route's
history is in `docs/RECORD.md`.

## Environment rules

- **No system-wide installs without explicit permission.** Everything
  lives under this directory (`tools/`, `oca/.venv/`); system libraries
  are used as found, never installed. Developed on CachyOS, Python 3.14;
  the system libraries the published builds linked are recorded in
  `docs/RECORD.md`, "The build environment".
- The whole toolchain is built by `scripts/build-toolchain.sh`, pinned
  to exact revisions — and since `tools/` is gitignored, the script is
  the only record of how to get one: the pins are its top-of-file
  constants, its header documents the three things it leaves outside
  the repository, and the cocotb-from-git reason (PyPI releases
  <= 2.0.1 refuse Python 3.14, and `@master` drifts under a pin) sits
  at its cocotb step. The slang frontend that implements
  `read_slang` is a yosys submodule, pinned and rebuilt with it. Since
  the yosys bump of 2026-08-15 the revisions the script builds are no
  longer the ones every number in this repository was measured on
  (`oca/hw/syn/README.md`, "Results").

  ```sh
  scripts/build-toolchain.sh --check   # what is present, and probe yosys
  scripts/build-toolchain.sh           # build all of it, hours
  scripts/build-toolchain.sh yosys     # or one component
  ```

- **A yosys older than `f77ddfb87` silently deletes the key store from
  the netlist** (`docs/RECORD.md`, "The key store was missing"). Both
  `--check` and `run_synth.py` prove the fix with a behavioural probe —
  `$signed(a) >= -8` is a tautology and must map to an all-ones LUT —
  rather than trusting the pin. `techlibs/common/cmp2lut.v` is read at
  run time from `tools/yosys/share/yosys/`, so a stale copy there
  defeats a correct build and is repaired by copying the source file
  over it, no rebuild.
- openFPGALoader does not install `99-openfpgaloader.rules`, and there
  is no CMake option to ask it to. A JTAG cable is reachable either
  after copying that rule to `/etc/udev/rules.d/` by hand or by running
  the tool with sudo.
- The `tools/verilator` and `tools/help2man` in this working copy were
  not built here — they are copies of artefacts from another project,
  which is why `verilator_bin` carries that project's path internally.
  The Perl wrapper resolves `VERILATOR_ROOT` from its own location, so
  the suites are unaffected; a toolchain built by the script does not
  have the discrepancy.

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
.venv/bin/python hw/sim/run_oca_core.py           # 31/31 pass
.venv/bin/python hw/sim/run_aead_cycles.py        # 3/3 pass, differential cycle cost
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
.venv/bin/python hw/sim/run_uart_crypto.py        # 5/5 pass
.venv/bin/python hw/sim/run_uart_crypto_dual.py   # 1/1 pass, two cores over the real UART
.venv/bin/python hw/sim/run_dual_fabric.py        # 6/7 pass, 1 skipped by design
.venv/bin/python hw/sim/run_crypto_pll.py         # 3/3 pass, its one build at LED_BITS=8
.venv/bin/python hw/sim/run_keystore_gate.py      # 4/4 pass, post-synthesis
.venv/bin/python hw/sim/run_proto_gate.py         # 2/2 pass, post-synthesis
```

`run_dual_fabric.py`'s fail-closed test — the one of its seven that
does not pass on this tree — needs RTL whose cores CAN diverge, so it
is skipped by default and **`--divergent` alone is not enough**: it
also needs a source copy that makes them differ:

```sh
mkdir -p /tmp/div && sed 's/oca_core #(.BYTES (BYTES)) u_core1 (/oca_core #(.BYTES (BYTES), .NUM_SLOTS (4)) u_core1 (/' \
    hw/sim/oca_dual_harness.sv > /tmp/div/oca_dual_harness.sv
.venv/bin/python hw/sim/run_dual_fabric.py --divergent --src-override /tmp/div
```

That run reads 6/7 as well, but not the same 6: on the pristine tree
the divergence test is **skipped**, everything else passes and the
runner exits 0; on the copy the divergence test passes and
`test_clean_broadcast_keeps_trouble_low` **fails**, because those
cores can no longer answer a slot-6 load-key identically, so the
runner exits 1. The two properties are mutually exclusive by
construction and neither result is a defect. Both were run on
2026-08-16.

That is every RTL runner in the repository, and the list has to be
complete because **there is no aggregate RTL runner here at all** —
`oca/CMakeLists.txt` registers one ctest and it is the C vectors
binary, so every suite is invoked by name and a suite missing from this
list is a suite nobody runs.

**The 25 cocotb runners measure 162 tests over 186 executions, 185 of
them passing and one skipped by design** (the fail-closed test above),
six of the passing ones on a synthesised netlist from the two gate
runners. Counted by running every runner in the tree on 2026-08-16,
not by adding up this list. Beyond them, four more populations run and
can fail: **69
tests in `hw/host/`, 4 in `hw/syn/test_run_synth.py`, 4 in
`hw/sim/test_proto_model.py`** and the C backend's **126 known-answer
checks** behind the single ctest above; the 6-step `cli.py --fake
selftest` carries an exit-code contract as well. **No grand total is
given**: the populations share no unit, and a headline number would
have to pick one and mislead about the rest. The decomposition of the
figures, and how they moved — the 222 and 207 before-figures, the two
177s that were never one measurement, the 123 that missed six suites —
is `docs/RECORD.md`, "The test counts".

**A parameter with one tested value is a parameter that does not
work.** `ROUNDS_PER_CYCLE` and `ROWS_PER_CYCLE` switch the datapath
rather than sizing it, and both went untested outside their default
until 2026-08-09. What the second value catches is not hypothetical:
mutating `poly1305.sv`'s `CSH` to a constant is a no-op at one value
and wrong at the other, in both directions, and each mutation is caught
only by the run it breaks.

`run_keystore_gate.py` and `run_proto_gate.py` are the only suites that
run on a synthesised netlist rather than on the RTL; they exist because
everything above them is blind to synthesis, and the tag comparison
they cover is combinational, invisible to the flip-flop floors in
`run_synth.py`. Only those two tops can be replayed that way — a
netlist carrying `poly1305`'s multipliers does not elaborate in
Verilator, and one carrying the packet buffers runs green over a memory
that returns nothing; `run_proto_gate.py`'s header carries the
mechanics.

`run_attack.py` drives the same DUT as `run_oca_core.py` but from the
other side: written to break the four-stage overlap rather than confirm
it, watching `oca_proto`'s internal registers where a payload assertion
would need the traffic to hit the exposing alignment.

The protocol model has no DUT, so it runs as plain Python rather than
through a simulator:

```sh
cd hw/sim && ../../.venv/bin/python test_proto_model.py   # prints "proto_model: OK"
```

The suites that need neither simulator nor RTL toolchain — sub-second,
so there is no excuse for skipping them:

```sh
.venv/bin/python -m pytest -q hw/host                  # 73 pass, host link + fake board
.venv/bin/python -m pytest -q hw/syn/test_run_synth.py # 4 pass, yosys and nextpnr argv shapes
.venv/bin/python hw/host/cli.py --fake selftest        # 6/6 steps, in-process fake, no wire
```

`run_dirty_pad.py` is separate from the official-vector suite on
purpose: it drives random garbage into the bytes past `in_len` where
that suite zero-pads, which is the only way to see the engine's padding
mask on the **decrypt** path (the mask-removal measurement is
`docs/RECORD.md`, "The masking change").

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
  `tests/vectors/sources/` — never hand-typed expected values.** If a
  vector fails, suspect the parser before the implementation, and
  cross-check with an independent implementation (OpenSSL CLI / Python
  reference) before touching RTL; the RFC-hexdump pitfalls this paid
  for are documented in the parsers themselves.
- Licenses: software MIT/Apache-2.0, RTL CERN-OHL-P-2.0 (SPDX header in
  every file), hardware docs CERN-OHL-P v2.
- **Two frontends, and the split is not symmetric.** Our SystemVerilog
  goes through `read_slang` — the Verilog-2005 frontend rejects it —
  and **third-party Verilog through `read_verilog`, never slang**,
  which spills vendored memories into logic and measures the design an
  order of magnitude too large; quote the ratio, never the absolute
  numbers (`docs/RECORD.md`, "Third-party Verilog goes through
  `read_verilog`"). A mixed design reads each side with its own
  frontend.
- **Never invoke `yosys` or `nextpnr-ecp5` directly. Go through
  `hw/syn/run_synth.py`**, which bounds every stage with a hard
  wall-clock timeout and kills the whole process group when one is hit.
  If a build genuinely needs longer, raise it with `--timeout`; do not
  route around the bound. It has taken two runaway builds to learn this
  (`docs/RECORD.md`, "Two builds ran away").
- cocotb gotchas: runner import is `cocotb_tools.runner` on cocotb 2.x
  (fallback from `cocotb.runner`); when polling a DUT status signal in
  a loop, `await RisingEdge` **before** reading — reading right after
  the edge that consumed your stimulus returns the stale value.
- **An AXI-Stream driver samples the handshake before the transfer
  edge, not after it**: `await ReadOnly()`, read `tready`, then
  `await RisingEdge` — and only advance to the next byte if `tready`
  was high, holding `tdata`/`tvalid`/`tlast` stable until it is. The
  other order trains the RTL to drop one byte per packet
  (`docs/RECORD.md`, "The AXI-Stream driver read `tready` after the
  edge").
- **Proposals from per-file analysis are not additive.** Before
  applying two proposals that touch opposite ends of one handshake,
  measure the combination; never infer it from the parts
  (`docs/RECORD.md`, "Two per-file proposals").
- When mutating a Python model or testbench to prove a test can fail,
  delete `hw/sim/__pycache__` before re-running: Python invalidates its
  cache on mtime and size at one-second granularity, so a same-size
  edit reverted within the same second keeps executing the mutated
  bytecode. Cost one confusing debug session on 2026-08-03.
- **A green simulation says nothing about the netlist.** Verilator
  never runs yosys, so a synthesis bug is invisible to every suite here
  — one deleted the whole key store while every test stayed green
  (`docs/RECORD.md`, "The key store was missing"). Whatever only
  synthesis decides is asserted against the netlist in
  `NETLIST_FF_FLOOR`, and `run_synth.py` proves the yosys fix
  behaviourally rather than trusting the revision number.
- **An undriven input is not an input reading zero.** yosys may take
  whichever value deletes the most logic — an unconnected port removed
  a vendored cache whole while the design linted, placed and closed its
  clock (`docs/RECORD.md`, "What happened, and it is neither a lost
  seed nor a cost"). Connect every port of a third-party module
  deliberately, and prove the storage with a netlist census, not a lint
  pass.
- **A test runner's exit code is the contract, not its log.** Every
  cocotb runner here once exited 0 whatever its tests did
  (`docs/RECORD.md`, "Every cocotb runner exited 0"); they now parse
  `results.xml` and exit 1 on any failure, and on no tests at all.
  Prove the check by mutation — a deliberately red test must exit
  non-zero — not by reading the code.
- **Pointer and length are validated as a pair, for every pointer, at
  the API boundary.** NULL with a non-zero length is
  `OCA_ERR_INVALID_ARG`, never a silent empty input, and a new pointer
  argument to the C API arrives with its `(!p && len)` guard and its
  bad-args test in the same change (`docs/RECORD.md`, "`aad = NULL`").
- **A correction edits the figure in place, everywhere it stands, in
  the same commit** — this project's errors of record were right
  figures written next to stale ones (`docs/RECORD.md`, "The
  documentation errors of record"). When a number changes, grep for it
  and for everything derived from it — `docs/RECORD.md` first, where
  the figures live, then `AGENTS.md`, `SPEC.md`, both READMEs,
  `hw/syn/README.md` and `docs/STATUS.md` — and amend every occurrence,
  dated. Do not stop at the documents:
  `.claude/skills/synth-sweep/SKILL.md` transcribes the per-seed values
  and spreads of the committed designs, and `sweep.sh` prints the
  widest of them in the note it emits past its 8% threshold — neither
  gates on those numbers, but both are read as current.
- **A LUT figure in this project is a post-pack `TRELLIS_COMB` count
  from nextpnr, over the device's 43848 — never a yosys cell count.**
  The two are not comparable and neither converts to the other; a
  percentage of 43848, or an Fmax beside the number, is how a figure
  declares which it is. Record which counter produced a number when you
  write it down — unpicking one that did not took its own document
  (`docs/design/2026-08-12-ethernet-measurement-provenance.md`).
- **Equal cell counts are not the same netlist, and an Fmax belongs to
  the commit it was measured on.** Two netlists have matched on every
  per-type total and still placed differently at the same seed, and
  nextpnr at a fixed seed is deterministic (`docs/RECORD.md`, "The
  audit blamed the toolchain"). Treat a disagreement with a recorded
  figure as a finding to resolve: diff the RTL before blaming the
  toolchain, and quote an Fmax with its commit, and re-measure before publishing.
- **An out-of-context Fmax is a ceiling, not the clock the board runs,
  and areas measured that way do not add.** The board gets what the
  PLL's dividers deliver — 625/13 = 48.0769 MHz — so a throughput
  divided out of an Fmax is a cycle budget and has to say so, and a sum
  of parts measured alone double-counts shared logic: an estimate of
  unknown tightness until built (`docs/RECORD.md`, "Two cores
  measured").
- **A bring-up indicator has to be able to say the wrong thing.** Drive
  it from one counter and nothing else, reporting a frequency rather
  than a flag; a diagnostic counter saturates rather than wraps, and
  two counters sharing a register agree tautologically
  (`docs/RECORD.md`, "Step 2 of the ladder", "Step 3 of the ladder",
  "The diagnostic console runs on the board").
- Git: work on branches; never commit directly on the default branch.

## The measurement record

The long-form record — every figure this project has taken, how it was
arrived at, and what it does NOT establish — is `docs/RECORD.md`, which
also carries the stories behind the hard rules above and the history of
the test counts. **Read it before quoting any figure or claiming a
result**, and write every new bench number into it. For where the
project stands rather than how it was measured, `docs/STATUS.md` is one
page.
