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
.venv/bin/python hw/sim/run_chacha20.py           # 3/3 pass
.venv/bin/python hw/sim/run_poly1305.py           # 4/4 pass
.venv/bin/python hw/sim/run_chacha20_poly1305.py  # 3/3 pass
```

Lint (must stay clean, `-Wall`):

```sh
../tools/verilator/bin/verilator --lint-only -Wall hw/rtl/*.sv --top-module chacha20_poly1305
```

ECP5 synthesis (Phase 2, from `oca/`), see `hw/syn/README.md`:

```sh
.venv/bin/python hw/syn/run_synth.py chacha20_poly1305   # ~2 min
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
- Git: work on branches; never commit directly on the default branch.

## Current status

- Phase 1: done, 114/114 vectors pass, zero warnings. Baseline on the
  dev machine: AES-128-GCM 26.6 GB/s (AES-NI), ChaCha20-Poly1305
  5.9 GB/s (large blocks).
- Phase 2: `chacha20.sv`, `poly1305.sv`, `chacha20_poly1305.sv` (AEAD,
  encrypt + decrypt) written and verified against RFC 8439 vectors
  (2.3.2, 2.4.2, 2.5.2, A.3 #1-4, 2.8.2, A.5). Lint `-Wall` clean.
  Poly1305 additionally has a reference model validated on the official
  vectors, digit-boundary edge cases and 200 randomised messages.
- Open ECP5 toolchain built locally (yosys, prjtrellis, nextpnr-ecp5).
  Baseline synthesis of the AEAD engine on the LFE5U-45F was 25% of the
  LUTs, **90% of the multipliers**, Fmax **26.77 MHz**, critical path in
  the single-cycle 130x130 multiply of `poly1305.sv`.
- `poly1305.sv` reworked into a 26-bit limb datapath (five digits, the
  mod-2^130-5 reduction folded into the accumulation, parameter
  `ROWS_PER_CYCLE`). Result: the AEAD engine drops from **65 to 20**
  multipliers (90% -> 28%) and standalone Poly1305 Fmax more than
  doubles (22.94 -> **52.68 MHz**). **AEAD Fmax is unchanged**
  (26.77 -> 26.10 MHz, inside place & route noise) and throughput fell
  ~40% to ~0.28 Gbps — a 64-byte block costs 47 cycles instead of 29,
  measured in simulation — because a Poly1305 block now takes 9 cycles
  instead of 3 and the critical path moved into `chacha20.sv`
  (`oca/hw/syn/README.md`).
- Next: rework `chacha20.sv` — it now owns the critical path
  (two full rounds per cycle, 38.31 ns in the AEAD engine) and it is
  what keeps the engine clock-bound at ~26 MHz. Then recover throughput
  (`ROWS_PER_CYCLE`, deeper pipelining) and build the streaming/packet
  interface toward the GbE MVP.
