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

- MVP (ECP5): aggregate crypto-core throughput in the FPGA fabric of
  ~1.26 Gbps from two parallel AEAD engines, measured in simulation and
  on hardware. **That is an aggregate cycle budget and not a saturated
  port.** This bullet read "high enough to saturate one GbE host port
  with margin" until 2026-08-06, which added the two engines together
  against a single port: `oca_dual` gives each core its own stream, so a
  port sees one core and carries 0.569 Gbps at a 1500-byte MTU on yosys
  `41a4b5a03`, 0.577 on `f77ddfb87` — itself
  an Fmax over a cycle count rather than a rate, see the 2026-08-10
  amendment below. The
  2026-08-05 correction further down this bullet is the one that holds.
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
  of crypto capacity**, against the 125 MB/s a GbE port carries. That is
  what the fabric holds, so that is where the target sits — as a sum
  over both engines. The 49.28 MHz is the 2026-08-04 occupancy study's
  two-core clock, measured before the key store was restored; the
  committed pair's 48.89 MHz on yosys `41a4b5a03` puts the same sum at
  ~1.25 Gbps, and its 49.61 on `f77ddfb87` at ~1.27, which is
  why this target is left where it is rather than chased by a
  hundredth. This paragraph read "one port saturated with **26%
  margin**" until 2026-08-06; the sum only clears a port if both engines
  sit behind that port, which `oca_dual` does not do.
  **Corrected 2026-08-04, then amended the same day: the cycle budget
  now clears the port in simulation, and what is unmeasured is whether
  the pair still fits.** The datapath inside `oca_core` was widened from
  8 to 64 bits that day, taking a 64-byte block from 415 cycles to **64
  end to end** — 8 to receive at 8 bytes/cycle, 48 through buffer,
  engine and buffer, 8 to transmit. Those 64 were serialised because
  `oca_core` was store and forward on a single pair of buffers, and two
  scheduling reworks removed the serialisation: overlapping feed,
  compute and drain inside a command took the block to 56 cycles, and
  overlapping four packet stages across successive commands took it to
  **40** — the engine's own figure, with the protocol layer no longer
  adding anything to it. Measured differentially in simulation and
  exactly linear: 231, 391, 551 and 711 cycles for 4, 8, 12 and 16
  blocks, marginal 40.00.
  **What that is worth end to end is a projection, not a result** — the
  cycles and the clock are both measured, but nothing has run on
  silicon. At 1.6 bytes/cycle and the 48.53 MHz measured for the last
  64-bit pair, two cores are 155 MB/s = **~1.24 Gbps** added together,
  which is a cycle budget and not a port cleared; the 2026-08-05
  correction below is what it comes to per port. That clock was in any
  case measured on RTL two rewrites older than this one, in a build
  whose key store synthesis had silently deleted. **Two cores of the
  current RTL have since been placed and routed, over four placer seeds
  and with every seed routing** — `oca_dual` builds them, and the
  paragraph below carries what they measure. One core of the current RTL
  measures **12330 LUTs, 12033 FF, 4 DP16KD, 20 multipliers and 49.77
  MHz mean over four seeds** (50.12 / 48.74 / 48.61 / 51.62, measured
  2026-08-15 on yosys `f77ddfb87`; on the previous pin it was 12308 and
  49.91 mean, and before the secret zeroisation 11590 / 12043 / 48.52),
  against the 11429 LUTs the two-core figure was scaled from — and that
  pair was already the tightest configuration in the study: two of its
  four placer seeds never routed, each stopped after 3 h 22 min still
  bouncing between 50 and 2300 unrouted arcs rather than descending. The
  cycle budget is measured, and so now are the clock and the fit at two
  cores; what was marginal on that older pair was routability rather
  than the clock.
  **Both PHYs can be fed; neither can be saturated. Corrected
  2026-08-05, having previously read that the second PHY could not be
  fed at all.** The Colorlight i9 v7.2 carries two PHYs (`BOM-MVP.md`),
  so 2 Gbps of wire is present and >= 2 Gbps was the honest thing to aim
  at. Two cores of the current RTL place and route at 49.61 MHz mean
  over four seeds (24621 LUTs, 56.1% of the device, measured 2026-08-15
  on yosys `f77ddfb87`; on the previous pin it was 24602 and 48.89 mean,
  and 48.16 for the pair from before the secret zeroisation), and
  `oca_dual` wires them as two independent
  streams — one core per port. Through the measured cycle model (1031
  cycles for a 1500-byte MTU) the previous pin's 48.89 gives **0.569
  Gbps per port, 56.9% of line rate, 1.138 Gbps across both**; on
  `f77ddfb87` 49.61 gives 0.577 / 1.155, and at 48.16 it was
  0.561 / 1.121. Each of those pairs
  is the arithmetic of the clock beside it and nothing more. **48.89 and
  49.61 MHz are out-of-context Fmax figures and no PLL divider produces
  either**, so these are
  cycle budgets and not rates — the 2026-08-10 amendment below gives the
  clock a pinned build gets. Saturating
  a single port needs both cores behind it, which needs a distributor, a
  collector and an answer to the per-core key store; saturating both
  needs four cores, and three do not route. Full line rate on any port
  stays out of reach until the Artix-7 phase.
  **Amended 2026-08-09: the MAC question the next paragraph leaves open
  is settled, and it settles against two ports.** One GbE port costs
  8422 LUTs measured (19.2% of the device), so two cores with two ports
  would take 94.5% and two cores behind one port 75.3% — against the
  76.4% at which this device stopped routing in the study above. **The
  MVP that fits the current RTL is one core on one port, 0.581 Gbps at
  MTU** — the single core's own four-seed mean of 49.91 MHz (2026-08-09,
  yosys `41a4b5a03`; 49.77 and 0.579 on `f77ddfb87`)
  rather than the pair's, since only one core is in that build. The
  1.138 Gbps — 1.155 on the current pin — stands as the fabric's cycle
  budget, not a configuration the board carries. The Ethernet code was deleted on 2026-08-12, so the
  8422 no longer has a probe in the tree behind it;
  `docs/design/2026-08-12-ethernet-measurement-provenance.md` records the
  commit, the vendor pin and the commands it was measured with, and what
  a re-measurement did and did not reproduce.
  **Amended 2026-08-10: the MAC fits, it has been placed beside a core,
  and the delivered figure is lower than either projection.** `oca_top`
  was one core and one full port against the real pin map, with IO and
  the PLL: **18719 LUTs, 42.7% of the device**. (It was deleted from the
  tree on 2026-08-12 with the rest of the route; the measurement stands,
  the design does not exist.) **0.581 Gbps was an Fmax
  divided into a cycle count**, and the design cannot run at that clock:
  `oca_clkrst` delivers `clk_sys` at 625/13 = **48.0769 MHz**, and
  because `clk_tx` divides the same VCO the ladder is coarse — nothing
  between 48.08 and 50.00, and 50.00 has since been built and does not
  close (48.22 against 50.00). Through the same cycle model the design
  delivers **0.560 Gbps at MTU, 56.0% of line rate**.

  **Amended 2026-08-11: it does not close timing at all.** Connecting
  the raw-IP ready pins on the UDP stack, which the board needs or one
  non-UDP frame stops reception for good, came with a third connection
  that restored 881 LUTs and 400 flip-flops of deleted ARP logic
  around the receive path; across 32 placer seeds `rgmii_rx_clk` clears
  125 MHz on none, the best being 124.22. The 0.560 Gbps above is
  therefore what this design delivers if a closing placement is found,
  and **nobody is looking for one**: this read "finding one is the
  project's first open item" until 2026-08-12, when the route closed for
  want of an RJ45 socket and the search stopped being an item at all
  (`AGENTS.md`). The two-port and two-core
  rows above remain sums that nothing has built: every term in them was
  measured `--out-of-context`, with no IO and no pin constraints, and
  the port was measured apart from the cores rather than placed beside
  them — which is exactly the addition that came in 2011 LUTs high on
  the one row that has since been built. (That figure was 2928 against
  the netlist before `54a2df8`, which restored 917 LUTs the earlier
  build had deleted — 881 of them ARP logic brought back by the
  `clear_arp_cache` connection, the other 36 the cost of the two raw-IP
  ready pins.)
  Nothing has run on a board. The host
  datapath is no longer the limit — it costs the
  engine's 40 cycles per 64-byte block and nothing on top, see
  `docs/design/2026-08-03-host-protocol.md`. Measurements and method:
  `oca/hw/syn/README.md`.
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
  projection. On the MVP board the figure is in any case only observable
  in simulation: this said "the host link being GbE" until 2026-08-12,
  and the host link is now the DAPLink USB serial at 115200 8N1 — about
  11.5 KB/s, against the 125 MB/s a GbE port carries.
- Declared note, **inverted 2026-08-09**: v1 end-to-end throughput is
  limited by the fabric, not by the host interface. This line read "v1
  end-to-end throughput is limited by the host interface (GbE)", which
  was true when this bullet asked for >= 2 Gbps from three engines
  against a 1 Gbps port. The MVP that fits is one core on one port at
  0.560 Gbps, **56.0% of line rate**, so the port idles 44% of the time
  and the GbE link is the larger number, not the smaller one. (This read
  0.581 and 58.1% until 2026-08-10: that figure divides an Fmax the PLL
  cannot deliver into the cycle model. The conclusion is unchanged and
  the margin is wider.) What
  bounds v1 is what fits and routes on the LFE5U-45F: three cores do
  not route at 76.4% occupancy (congestion, not timing), and one
  Ethernet port costs 8422 LUTs, which puts two cores with two ports at
  94.5% of the device. Whether two cores behind one port would route at
  75.3% is **unmeasured** — that configuration has never been built,
  and it would need a distributor, a collector and an answer to the
  per-core key store besides. The host interface stops being the
  smaller number only from Phase 3.
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
host interface over the board's USB serial. NO PCIe in this phase, and
NO Ethernet either: that is a change from what this line said until
2026-08-12, and the two reasons are hardware, not preference.

The Colorlight i9 v7.2 carries both B50612D PHYs on the module and
routes their MDI pairs to the SO-DIMM edge, but the RJ45 sockets and
the magnetics live on a carrier this kit does not include and no
carrier sold with it has. There is no socket to put a cable in.

And this part cannot become the PCIe platform of step 7 in any case.
The die is an LFE5U, IDCODE 0x41112043 read on silicon, and the ECP5
family puts the SERDES only in the UM and UM5G variants: prjtrellis
gives LFE5U-45F twenty-four DCU tiles, every one of them a VCIB_DCU*
fabric interface with nothing behind it, against LFE5UM-45F's
forty-two — the same twenty-four grid locations, named CIB_DCU* there,
plus eighteen DCU0...DCU8 tiles in two runs of nine, which are the
transceiver this die does not have. No SERDES, no PCIe. (The count and
the conclusion are unchanged; this named all forty-two CIB_DCU* until
2026-08-12, and only twenty-four of them are.)

So this board is a vehicle for proving the core on silicon, not a
prototype of the product, and the transport it uses only has to be
good enough to carry a test vector. The clause below already allowed
that: the USB FIFO was always the alternative.

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
6. Host integration on ECP5 over USB serial (MVP). Was "ECP5/Ethernet"
   until 2026-08-12; see PHASE 2 for why the Ethernet route is closed on
   this hardware. Correctness on silicon is what this step proves, and
   the transport's throughput is not a figure about the accelerator.
7. PCIe platform (Artix-7 + LitePCIe)
8. FreeBSD and Linux drivers
9. WireGuard and OpenSSL integration
10. OCA-10: KiCad design, prototype, gateware+driver porting
11. OCA-50: Gen3 PoC on dev board, board design, validation
12. Final benchmarks on both models, public release

Each phase must produce working, testable code before moving to the next.
