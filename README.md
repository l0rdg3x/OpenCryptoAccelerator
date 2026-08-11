# OpenCrypto Accelerator (OCA)

Open-source FPGA cryptographic accelerator, developed fully in the open:
software, HDL and — in later phases — open-hardware board designs.

- **MVP**: Lattice ECP5 board (Colorlight i9) + GbE host link, fully
  open-source toolchain (yosys/nextpnr-ecp5, Verilator, cocotb,
  SymbiYosys).
- **Crypto**: ChaCha20-Poly1305, AES-GCM, BLAKE2s, SHA-256, HMAC —
  targeting WireGuard, IPsec and OpenVPN/TLS offload.
- **Roadmap**: software reference → ECP5 prototype → PCIe platform
  (Artix-7 + LitePCIe) → custom open-hardware boards (OCA-10/OCA-50).

The full project specification is in [SPEC.md](SPEC.md).

## Repository status

- **Phase 1 (done)** — abstract crypto API in C11 + OpenSSL 3 software
  backend, 126/126 checks passing — 113 driven by official test
  vectors, plus a tamper case and twelve bad-argument cases, which have
  no vector to come from — and a benchmark harness. In [oca/](oca/).
- **Phase 2 (in progress)** — SystemVerilog cores verified with
  cocotb + Verilator against the same official vectors: ChaCha20,
  Poly1305, AEAD ChaCha20-Poly1305 (encrypt + decrypt), plus the host
  protocol layer (`oca_core`) that turns a UDP payload into an AEAD
  operation and back. The Ethernet integration is merged: `oca_top`
  reaches from the RGMII pads to `oca_core` and back, and a whole-path
  testbench drives a synthetic Ethernet frame through it and checks the
  frame that comes out. **It does not close timing**: the receive clock
  misses 125 MHz on all 32 placer seeds tried, the best reaching 124.22,
  so no bitstream is produced. That is the project's first open item —
  `AGENTS.md` carries the measurements and what has been ruled out.
  Nothing here has run on a board.

## Quick start

Software tests (requires cmake, a C11 compiler and OpenSSL 3):

```sh
cd oca
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
ctest --test-dir build --output-on-failure
./build/oca_bench
```

RTL simulation (requires Verilator and a Python virtualenv with cocotb —
exact tool setup in [AGENTS.md](AGENTS.md)):

```sh
cd oca
.venv/bin/python hw/sim/run_chacha20.py
.venv/bin/python hw/sim/run_poly1305.py
.venv/bin/python hw/sim/run_chacha20_poly1305.py
.venv/bin/python hw/sim/run_dirty_pad.py
.venv/bin/python hw/sim/run_secret_zeroise.py
.venv/bin/python hw/sim/run_keystore.py
.venv/bin/python hw/sim/run_pktbuf.py
.venv/bin/python hw/sim/run_oca_core.py
.venv/bin/python hw/sim/run_attack.py
.venv/bin/python hw/sim/run_clkrst.py
.venv/bin/python hw/sim/run_rgmii.py
.venv/bin/python hw/vendor/vendor_patches.py build        # before the next one
.venv/bin/python hw/sim/run_eth_mac.py
.venv/bin/python hw/sim/run_udp_seam.py
.venv/bin/python hw/sim/run_oca_path.py        # the whole path, frame in to frame out
.venv/bin/python hw/sim/run_keystore_gate.py   # on a synthesised netlist
.venv/bin/python hw/sim/run_proto_gate.py      # on a synthesised netlist
cd hw/sim && ../../.venv/bin/python test_proto_model.py   # plain Python
```

The two `*_gate.py` runners need the project-local yosys as well: they
replay tests on ECP5 primitives, because every other suite elaborates
the SystemVerilog and cannot see what synthesis did to it. `run_eth_mac.py`
and `run_oca_path.py` read a patched copy of the pinned
`verilog-ethernet` tree rather than the submodule, which is why
`vendor_patches.py build` comes first; each
refuses to run against an unpatched tree instead of testing sources the
board will not carry. The last line is not a simulation at all — the
protocol model has no DUT and runs as plain Python.

## Documentation

- [SPEC.md](SPEC.md) — project specification: goals, phases, constraints
- [BOM-MVP.md](BOM-MVP.md) — hardware required for MVP development
- [Security.md](Security.md) — threat scope, side-channel limits,
  caller obligations, known limitations
- [oca/README.md](oca/README.md) — code layout, tests, benchmarks,
  security notes
- [AGENTS.md](AGENTS.md) — contributor/agent guide: environment rules,
  build and test commands, hard rules

## Contributing

Contributions are welcome. Read [AGENTS.md](AGENTS.md) first: it
documents the environment rules, the test workflow and the hard rules
(official test vectors only, lint-clean HDL, branch-based workflow).
Every contribution must keep the software test suite (ctest) and the RTL
simulations passing.

## License

- **Software** (everything under `oca/` except `oca/hw/rtl/`): dual
  licensed under [MIT](LICENSE-MIT) OR
  [Apache-2.0](LICENSE-APACHE-2.0), at your option.
- **Hardware / HDL** (`oca/hw/rtl/` and future board designs):
  [CERN-OHL-P v2](LICENSE-CERN-OHL-P-2.0).

Files carry SPDX license identifiers accordingly.
