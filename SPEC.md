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
  enough to saturate one GbE host port with margin — target
  ~1.26 Gbps aggregate from two parallel AEAD engines, measured in
  simulation and on hardware.
  This bullet asked for ">= 2 Gbps aggregate (multiple parallel cores)"
  until 2026-08-04, on the estimate that an LFE5U-45F would hold three
  engines. The estimate did not survive being placed. The occupancy
  study of 2026-08-04 built the configurations instead of multiplying
  one of them (yosys + nextpnr-ecp5, LFE5U-45F, CABGA381, speed 6,
  `--out-of-context`, four placer seeds each):
  - one `oca_core`: 11149 of 43848 LUTs (25.4%), 20 of 72 multipliers
    (27.8%), 50.59 MHz mean;
  - two `oca_core`: 22313 LUTs (50.9%), 40 multipliers (55.6%),
    49.28 MHz mean — replication costs 2.6% of the clock, which is
    inside the seed spread of a single core;
  - three `oca_core`: 33484 LUTs (76.4%), 60 multipliers (83.3%), and
    **it does not route**. One seed fails placement; six others were
    still routing after 55 minutes each with roughly 50000 arcs
    unrouted, and the same roughly 50000 remain whether the constraint
    is 100, 45, 40 or 35 MHz. That is congestion, not timing: asking
    for a slower clock buys nothing.
  Two engines at 49.28 MHz and 40 cycles per 64-byte block (1.6
  bytes/cycle each, measured in simulation) are 158 MB/s = **~1.26 Gbps
  of crypto capacity**, against the 125 MB/s a GbE port carries: one
  port saturated with **26% margin**. That is what the fabric holds, so
  that is where the target sits.
  **Corrected again 2026-08-04: the design does not currently meet this
  target, and the gap is the host datapath, not the engines.** The
  datapath inside `oca_core` was widened from 8 to 64 bits that day and
  a 64-byte block now costs **64 cycles end to end** instead of 415 —
  8 to receive at 8 bytes/cycle, 48 through buffer, engine and buffer,
  8 to transmit — measured differentially in simulation and exactly
  linear. 64 cycles per 64 bytes is **exactly one byte per cycle per
  core**. Two 64-bit `oca_core` instances place and route at 22891 LUTs
  (52.2%), 40 multipliers (55.6%), 8 DP16KD and **48.53 MHz**, so they
  deliver 2 x 48.53 MB/s = 97.1 MB/s = **~0.78 Gbps end to end**. That is
  78% of a bare GbE port and **62% of the ~1.26 Gbps this bullet asks
  for**, so the target is **missed by 38%**; the 22% figure that also
  appears in the measurement notes is only the distance to breaking even
  with the wire, before any margin. The clock cannot close it —
  62.5 MHz would be needed even to break even, above anything
  `chacha20.sv` has reached.
  **What closes it is that the 64 cycles are serialised**, 8 + 48 + 8 in
  strict sequence, because `oca_core` is store and forward on a single
  pair of buffers. Overlapping receive, process and transmit across
  successive packets takes a block to `max(8, 48, 8) = 48` cycles and
  two cores to ~1.04 Gbps, which passes 125 MB/s by 2-5% — less than the
  spread of the Fmax it is computed from, so **necessary but not
  sufficient**. A real margin only returns when the 8 cycles of feed and
  drain also leave the loop, leaving the engine's own 40 cycles and
  ~1.24 Gbps — **24% over the port**, at the 48.53 MHz measured for the
  64-bit pair. That is the same figure as the ~1.26 Gbps and 26% above,
  which was computed at the 8-bit pair's 49.28 MHz; the two clocks differ
  by 1.5% and so do the two margins.
  The target therefore stands at ~1.26 Gbps and is **not met today**; two
  further steps of work stand between the two figures, and both are
  scheduling rather than datapath. Two qualifications: the 48.53 MHz is
  the mean of the **two** placer seeds that routed — the other two were
  stopped after 3 h 22 min each, still bouncing between 50 and 2300
  unrouted arcs rather than descending, so two 64-bit cores fit this
  device but are no longer comfortable on it — and the 40-cycle engine
  figure assumes a protocol layer that costs nothing on top of the
  engine, which is a design that does not exist yet.
  **The board's second GbE PHY cannot be fed, and this is recorded
  rather than overlooked.** The Colorlight i9 v7.2 carries two PHYs
  (`BOM-MVP.md`), so 2 Gbps of wire is present on the board and
  >= 2 Gbps was the honest thing to aim at; the fabric holds ~1.26 Gbps
  of crypto. The second port is out of reach on this silicon and stays
  out of reach until the Artix-7 phase.
  Both figures are projections from measurement rather than silicon
  results: the builds are `--out-of-context`, with no IO, no pin
  constraints and no Ethernet MAC, and the MAC still has to fit
  alongside. The engines are also not the current end-to-end limit —
  the host datapath is, at 64 cycles per 64-byte block against the
  engine's 40, see `docs/design/2026-08-03-host-protocol.md`.
  Measurements and method: `oca/hw/syn/README.md`.
- Phase 3 (Artix-7 + LitePCIe): aggregate crypto-core throughput
  >= 10 Gbps, measured in simulation and on hardware.
  This target was originally set for the MVP. The first ECP5 synthesis
  of the cores (2026-08-03) showed it is out of reach on the LFE5U-45F
  without architectural work well beyond the MVP scope: one AEAD engine
  took 90% of the device multipliers at 26.77 MHz, and even after the
  datapath reworks — which brought one engine to 20 multipliers and
  ~0.67 Gbps — the device holds two engines, not the three earlier
  versions of this bullet assumed, for the ~1.26 Gbps the MVP bullet
  above records. See `oca/hw/syn/README.md` for the measurements and
  `docs/design/2026-08-03-poly1305-datapath.md` for the earlier
  projection. On the MVP board the figure would in any case only be
  observable in simulation, the host link being GbE.
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
