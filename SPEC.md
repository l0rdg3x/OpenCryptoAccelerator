# OpenCrypto Accelerator (OCA) — Project Prompt

Act as a team composed of:

- FPGA Hardware Architect
- Digital Design Engineer
- FreeBSD Kernel Developer
- Linux Kernel Developer
- Cryptography Engineer
- OpenSSL Developer
- WireGuard Developer
- PCIe/DMA Engineer
- Embedded Firmware Developer
- Open Source Project Maintainer

You must design a fully open-source cryptographic accelerator, initially
developed on a tight budget using a commercial FPGA.

The project must be named: **OpenCrypto Accelerator (OCA)**

# ================================================== LICENSES

- Software (drivers, API, provider, simulator): MIT or Apache-2.0
- Hardware (HDL, schematics, PCB, board documentation): CERN-OHL-P v2
- The project must be commercially usable, with no mandatory proprietary
  IP cores in the HDL.
- Toolchain transparency (to be stated in Hardware.md): all sources are
  100% open and reproducible by third parties; the MVP phase (ECP5) uses
  a fully open-source toolchain; the Xilinx FPGA phases require Vivado
  (proprietary, free-of-charge tool) because no mature open alternative
  exists for 7-series and UltraScale+. This exception is limited to the
  build chain, never to the sources.

# ================================================== VERSION 1 GOALS (MVP)

Hardware accelerator for:

1. WireGuard: ChaCha20, Poly1305, BLAKE2s
   (Curve25519 deferred to a later phase)
2. IPsec: AES-GCM, SHA-256, HMAC
3. OpenVPN/TLS: AES-GCM, ChaCha20-Poly1305

# ================================================== PERFORMANCE TARGETS

- MVP (ECP5): aggregate crypto-core throughput in the FPGA fabric high
  enough to saturate the GbE host link with margin — target >= 2 Gbps
  aggregate, measured in simulation and on hardware (multiple parallel
  cores).
- Phase 3 (Artix-7 + LitePCIe): aggregate crypto-core throughput
  >= 10 Gbps, measured in simulation and on hardware.
  This target was originally set for the MVP. The first ECP5 synthesis
  of the cores (2026-08-03) showed it is out of reach on the LFE5U-45F
  without architectural work well beyond the MVP scope: one AEAD engine
  took 90% of the device multipliers at 26.77 MHz, and even after the
  planned datapath rework the device holds roughly three engines for an
  estimated ~4 Gbps. See `docs/design/2026-08-03-poly1305-datapath.md`.
  On the MVP board the figure would in any case only be observable in
  simulation, the host link being GbE.
- Declared note: v1 end-to-end throughput is limited by the host
  interface (GbE); host-facing throughput scales up in later phases.
- Final OCA-50 board: 50 Gbps aggregate multi-core/multi-session, with
  reproducible benchmarks (scripts + documented host configuration).
- All results must be reproducible by third parties using public test
  vectors (RFC 8439, NIST CAVP).

# ================================================== CONSTRAINTS

- Fully open-source sources, documented, reproducible.
- HDL in SystemVerilog.
- MVP toolchain fully open source: yosys + nextpnr-ecp5,
  Verilator + cocotb for simulation, SymbiYosys for formal verification.
- Linux and FreeBSD as primary platforms.
- Security: Poly1305 and tag comparison must be constant-time;
  advanced side-channel resistance (DPA) is explicitly OUT OF SCOPE
  for v1 and must be stated as such in Security.md.
- Vendor-independent HDL: dedicated wrappers for RAM/PLL/SERDES/PCIe
  primitives, for portability across FPGAs and toward a future ASIC.

# ================================================== PHASE 1: SOFTWARE SIMULATION

Before any hardware:

- Abstract crypto API: software backend / FPGA backend interchangeable
  without application changes;
- software backend based on an existing library (libsodium/OpenSSL)
  used as test reference;
- automated tests with official test vectors; benchmarks.

# ================================================== PHASE 2: FPGA PROTOTYPE (MVP, low budget)

Hardware: Lattice ECP5 (e.g., Colorlight i9 or equivalent, ~40 EUR),
host interface via Ethernet (GbE). NO PCIe in this phase.

Implement in SystemVerilog:

- ChaCha20, Poly1305, AES-GCM hardware;
- BLAKE2s, SHA-256, HMAC;
- simple command queue to the host (documented protocol over UDP
  or USB FIFO);
- cocotb testbenches, Verilator simulation, SymbiYosys formal
  verification where applicable.

# ================================================== PHASE 3: PCIe DEVELOPMENT PLATFORM

- Commercial Artix-7 board + LitePCIe (e.g., LiteFury/Acorn):
  PCIe Gen2, DMA engine, MSI-X, interrupts;
- synthesis with Vivado (documented exception to the toolchain
  constraint);
- the command/DMA protocol defined here must remain STABLE: it is the
  contract on which drivers and later custom boards depend.

# ================================================== PHASE 4: DRIVERS

FreeBSD:

- PCIe driver: DMA, MSI-X, OCF (opencrypto) provider, sysctl
  monitoring.

Linux:

- PCIe driver: DMA, registration in the kernel crypto API
  (skcipher/aead), so that any crypto API consumer (including
  WireGuard) uses the accelerator WITHOUT patches.

# ================================================== WIREGUARD

- Linux: NO modifications to WireGuard. Integration happens by
  registering the algorithms in the kernel crypto API (Phase 4).
  Verify that WireGuard actually uses the hardware provider and
  document the procedure.
- FreeBSD: if_wg has internal crypto; evaluate and document the
  minimal patch to route ChaCha20-Poly1305 through OCF, or declare
  wg/FreeBSD support out of scope for v1.
- Algorithms: ChaCha20, Poly1305, BLAKE2s.

# ================================================== OPENSSL

- OpenSSL 3 Provider (provider interface, not the deprecated engine);
- AES-GCM and ChaCha20-Poly1305 routed to the device;
- test with `openssl speed -provider` and TLS interoperability.

# ================================================== PHASE 5: CUSTOM OPEN-HARDWARE BOARDS (FINAL LINEUP)

Two models, same parameterized gateware, same command/DMA protocol,
single driver for both:

- OCA-10 (entry): Artix-7 200T, PCIe Gen2 x4, target ~10-14 Gbps
  end-to-end. 6-layer PCB in KiCad. Vivado Standard (free).
- OCA-50 (server): Kintex UltraScale+ (part number decided at design
  phase, after verifying free-tool coverage), PCIe Gen3 x8, target
  50 Gbps aggregate. 10-12 layer PCB, factory-assembled.

Requirements:

- schematics, PCB (KiCad), BOM, and fabrication files published under
  CERN-OHL-P v2: anyone must be able to have the board manufactured;
- validate LitePCIe Gen3 x8 support with a proof-of-concept on a
  commercial dev board BEFORE designing the OCA-50 board;
- OCA-10 also serves to build the signal/power integrity skills
  required for OCA-50.

# ================================================== DOCUMENTATION

README.md, Architecture.md, Hardware.md, FPGA.md, Driver.md,
FreeBSD.md, Linux.md, WireGuard.md, OpenSSL.md, Benchmark.md,
Contribution.md, Security.md (threat scope, side-channel limits).
Diagrams in versionable text format (Mermaid or AsciiDoc).

# ================================================== DEVELOPMENT METHOD

Do NOT generate everything at once. Proceed with:

1. Complete architecture and diagrams
2. API and command/DMA protocol specifications
3. Software simulator + tests
4. First FPGA algorithm (ChaCha20) with testbench
5. Remaining crypto cores
6. Host integration on ECP5/Ethernet (MVP)
7. PCIe platform (Artix-7 + LitePCIe)
8. FreeBSD and Linux drivers
9. WireGuard and OpenSSL integration
10. OCA-10: KiCad design, prototype, gateware+driver porting
11. OCA-50: Gen3 PoC on dev board, board design, validation
12. Final benchmarks on both models, public release

Each phase must produce working, testable code before moving to the next.
