# OpenCrypto Accelerator (OCA)

Open-source FPGA cryptographic accelerator, developed fully in the open:
software, HDL and — in later phases — open-hardware board designs.

- **MVP**: Lattice ECP5 board (Colorlight i9) + USB serial host link,
  fully open-source toolchain (yosys/nextpnr-ecp5, Verilator, cocotb,
  SymbiYosys). This line read "+ GbE host link" until 2026-08-12; the
  board has no Ethernet socket, and SPEC.md PHASE 2 says why.
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
  protocol layer (`oca_core`) that turns a request payload into an AEAD
  operation and back. It takes that payload on a 64-bit AXI-Stream and
  has never known what carried it, which is why retiring the Ethernet
  route on 2026-08-12 left it untouched. The host interface is the board's DAPLink USB
  serial (J17/H18, 115200 8N1), on which a diagnostic console has run on
  silicon. None of the cores above has run on a board.

**The Ethernet route is retired, 2026-08-12.** The Colorlight i9 v7.2
carries both PHYs on the module but no RJ45 socket, so there is nothing
to plug a cable into, and the die is an LFE5U with no SERDES, so it
cannot be the PCIe platform either ([SPEC.md](SPEC.md), PHASE 2). What
that route measured is kept; the code that measured it is not. `oca_top`
reached from the RGMII pads to `oca_core` and back, a whole-path
testbench drove a synthetic Ethernet frame through it and checked the
frame that came out, and it never closed timing -- the receive clock
missed 125 MHz on all 32 placer seeds tried, the best reaching 124.22, so
no bitstream was produced. `docs/RECORD.md` carries those measurements.
**The RTL, the four testbenches and the vendored `verilog-ethernet` tree
were deleted the same day**; `docs/STATUS.md` lists what deliberately
stayed behind and why. `oca_core`'s ports are two 64-bit AXI-Stream and
it never knew about Ethernet, so the crypto, the keystore, the packet
buffer and the protocol are unaffected.

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

The full registry — all 23 cocotb runners with their expected counts,
plus the host, synthesis and protocol-model suites — lives in
[AGENTS.md](AGENTS.md) "## How to build and test", the single list a
suite must appear on to exist. Today it measures **178 passing
executions with no failures and no skips** (2026-08-12, the crypto
suites re-measured 2026-08-15 for the PLL and 2026-08-16 for the
bubble-and-bench commit); outside the simulator, `pytest hw/host`
(60), `pytest hw/syn/test_run_synth.py` (4), the protocol model (4),
the 6-step `--fake` selftest, and the 126 known-answer checks behind
`ctest` — different units, never summed.

The two `*_gate.py` runners in that registry need the project-local
yosys as well: they replay tests on ECP5 primitives, because every
other suite elaborates the SystemVerilog and cannot see what synthesis
did to it.

Four further suites belonged to the Ethernet route and were deleted with
it on 2026-08-12: `run_rgmii`, `run_eth_mac`, `run_udp_seam` and
`run_oca_path`, along with the vendored `verilog-ethernet` tree and the
`vendor_patches.py` that patched it. Nothing here needs a vendored tree
to build any more. `run_uart_crypto` is the whole-path test that
replaced `run_oca_path`, over the serial line instead of over a frame.

## Documentation

- [SPEC.md](SPEC.md) — project specification: goals, phases, constraints
- [BOM-MVP.md](BOM-MVP.md) — hardware required for MVP development
- [Security.md](Security.md) — threat scope, side-channel limits,
  caller obligations, known limitations
- [oca/README.md](oca/README.md) — code layout, tests, benchmarks,
  security notes
- [AGENTS.md](AGENTS.md) — contributor/agent guide: environment rules,
  build and test commands, hard rules
- [docs/STATUS.md](docs/STATUS.md) — where the project stands, one page:
  done, not established, next
- [docs/RECORD.md](docs/RECORD.md) — the long-form record: every figure
  taken, how it was arrived at, and what it does NOT establish

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
