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
.venv/bin/python hw/sim/run_console.py
.venv/bin/python hw/sim/run_fifo.py
.venv/bin/python hw/sim/run_uart_rx.py
.venv/bin/python hw/sim/run_uart_tx.py
.venv/bin/python hw/sim/run_uart_console.py
.venv/bin/python hw/sim/run_uart_echo.py
.venv/bin/python hw/sim/run_slip_rx.py
.venv/bin/python hw/sim/run_slip_tx.py
.venv/bin/python hw/sim/run_uart_crypto.py     # the crypto over the real UART
.venv/bin/python hw/sim/run_crypto_pll.py      # the board top: PLL, crypto, LED
.venv/bin/python hw/sim/run_keystore_gate.py   # on a synthesised netlist
.venv/bin/python hw/sim/run_proto_gate.py      # on a synthesised netlist
cd hw/sim && ../../.venv/bin/python test_proto_model.py   # plain Python
```

That is all 22 cocotb runners, and they give **173 passing executions
with no failures and no skips**, measured 2026-08-12 with the two crypto
suites re-measured 2026-08-15. More runs outside the
simulator and needs no toolchain: `pytest hw/host` (46 tests),
`pytest hw/syn/test_run_synth.py` (4), `test_proto_model.py` above (2),
and the 6-step `hw/host/cli.py --fake selftest` against an in-process
fake — all measured 2026-08-13 — plus the 126 known-answer checks
behind the `ctest` further up. Those are different units and are not
summed anywhere. There is no aggregate runner, so a suite missing from
these lists is a suite nobody runs.

The two `*_gate.py` runners need the project-local yosys as well: they
replay tests on ECP5 primitives, because every other suite elaborates
the SystemVerilog and cannot see what synthesis did to it. The last line
is not a simulation at all — the protocol model has no DUT and runs as
plain Python.

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
