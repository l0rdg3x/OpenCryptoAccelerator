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
  backend, 114/114 official test vectors passing, benchmark harness.
  In [oca/](oca/).
- **Phase 2 (in progress)** — SystemVerilog cores verified with
  cocotb + Verilator against the same official vectors: ChaCha20,
  Poly1305, AEAD ChaCha20-Poly1305 (encrypt + decrypt), plus the host
  protocol layer (`oca_core`) that turns a UDP payload into an AEAD
  operation and back. Next is the Ethernet integration, which needs the
  board.

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
.venv/bin/python hw/sim/run_keystore.py
.venv/bin/python hw/sim/run_pktbuf.py
.venv/bin/python hw/sim/run_oca_core.py
```

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
