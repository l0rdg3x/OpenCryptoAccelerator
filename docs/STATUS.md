<!-- SPDX-License-Identifier: MIT -->
# Project status

**Updated 2026-08-15.** Where the project is, in one page. Update this at
every merge and at every design gate; a tracker that drifts is worse than
none.

This is deliberately short. `AGENTS.md` carries the measurements, their
limits and the history of how each was arrived at, and it is the document
to read before touching anything. This one answers only: what is done,
what is being worked on, what is next.

## Done

**Phase 1 — software reference.** The C API over OpenSSL, 126/126 checks,
113 of them driven by official vectors.

**Phase 2 — RTL.** ChaCha20, Poly1305 and the AEAD engine, verified
against RFC 8439. The host protocol — key store, packet buffers,
protocol FSM — behind a 64-bit AXI-Stream pair. **177 passing
executions over 21 cocotb runners, six of them on a synthesised
netlist**, measured 2026-08-12 after the Ethernet removal below: no
failures, 2 skips, and every runner exits 0. That figure is the
simulator's alone. Outside it, and outside every count this page gave
before 2026-08-13: **46 tests in `hw/host/`, 4 in
`hw/syn/test_run_synth.py`, 2 in `hw/sim/test_proto_model.py`** and the
126 known-answer checks of Phase 1 above. They are not one unit and are
not summed here. Nothing in any of them has run on hardware. No suite
needs a vendored tree to build any more. `verilator
--lint-only -Wall` is clean. The two skips are `oca_uart_crypto`'s
heartbeat pair, which needs `LED_BITS` small enough to simulate and so
runs only on that suite's second build.

This read 222 executions over 25 runners until the removal, and the
difference is exactly the four Ethernet suites, all four of which
passed while they existed: `run_rgmii` 10, `run_udp_seam` 20 (running
twice at two `HDR_Q_DEPTH` values), `run_eth_mac` 8 and `run_oca_path`
7. So 222 − 45 = 177. None of the four is on `main` any more.

**In a fresh clone the before-figure is 207 over 23 runners instead, and
that is not a contradiction.** The 222-execution figure includes `run_eth_mac`
and `run_oca_path`, which built from the patched vendor tree at `oca/hw/vendor/build/`,
gitignored and now deleted. To reproduce that figure, check out fd3059c (just before
the Ethernet removal), where `vendor_patches.py build` and the tree still exist.
The current tree measures 177; both figures are real and differ by that precondition.

**The open ECP5 toolchain**, built locally in `tools/`: yosys with the
slang frontend, nextpnr-ecp5, prjtrellis, Verilator, openFPGALoader.

**Bring-up on silicon**, six readings taken on the Colorlight i9 v7.2
that arrived 2026-08-11. The ladder in `.claude/skills/bringup` numbers
three steps; steps 4 and 5 were Ethernet and are retired.

| rung | result |
|---|---|
| IDCODE over the DAPLink | `0x41112043`, LFE5U-45 — settles the die, not the package |
| package | read off the chip: `6BG381C`, caBGA381, speed grade 6 |
| LED and oscillator | D2 gives the short flash, so the LED is active low; P3 is clocking |
| bank 6 VCCIO | 3.28 V measured on a driven pad, so `LVCMOS33` is right |
| PLL | locked, 1 Hz off a `clk_tx` counter — a frequency, not a lock flag |
| serial console | `PIN=J17` returned at 115200 8N1; the diagnostic console answers |

**The crypto on the FPGA, built and packed** (this is what the
`feat/crypto-console` work delivered). `oca_uart_crypto` is the AEAD core
behind SLIP framing on the board's DAPLink serial line, one clock domain
at 25 MHz, no PLL:

| | |
|---|---|
| LUT | 13030 — 29.7% of the device |
| flip-flops | 12518 — 28.5% |
| block RAM | 6 of 108 |
| multipliers | 20 of 72 |
| Fmax | 49.85 MHz against the 25.00 required — 1.99x |
| bitstream | 423971 bytes, packed |

Seed 1, `colorlight_i9_crypto.lpf`, re-measured 2026-08-14 on yosys
`f77ddfb87`, the day the toolchain was rebuilt on it. On the previous pin the same design read 13043 LUTs,
50.55 MHz and 423213 bytes: the flip-flops, block RAM and multipliers
did not move at all, and neither did the LUT percentage. The figures
above are what `run_synth.py oca_uart_crypto` reproduces; `build/`
holds one pass, since a rebuild overwrites the last, so re-run it
rather than trusting this table.

## Not established

**No bitstream containing crypto has been loaded onto the board.** What
is proved is that it builds, closes its clock at 1.99x the frequency it
needs, and answers the protocol correctly in simulation down to UART bit
timing — including a forged tag returning no plaintext, proved by
mutation rather than asserted.

**Banks 2 and 7 are unmeasured.** J17 and H18 live in bank 2. The console
has been talking through them at 115200 since 2026-08-11, so this is not
a blocker; the `oca_vccio` method applies if anything faster ever lands
there.

**The PLL's 1 Hz was never timed with a stopwatch**, so what is
established is lock and the absence of a gross error, not 125.0 MHz to
three digits.

**Throughput over the serial line is about 11.5 KB/s.** That is a figure
about the transport and never about the accelerator, which is aimed at
gigabit. Any rate measured over this link must say which of the two it
describes.

## Next

1. **Load `oca_uart_crypto` onto the board and run the vectors through
   it** with `oca/hw/host/cli.py selftest`. This is the step the whole
   project exists to reach: the crypto answering on silicon.

   **Watch D2 for a few seconds before the host opens the port.** The
   fast heartbeat latches on any malformed UART frame, and the edge a
   host puts on the line when it opens `/dev/ttyACM0` is enough to
   produce one. Fast before any traffic is line noise; fast only after
   traffic is the reading the rate is for. `oca_uart_crypto.sv`'s header
   has the full table.
2. Time the PLL with a stopwatch, and measure bank 2 while the meter is
   out.

## Closed

**Ethernet, 2026-08-12.** No RJ45 socket on this kit, and the die is an
LFE5U with no SERDES, so this part can never be the PCIe platform either.
`SPEC.md` PHASE 2 records both reasons.

**And the code is gone, deleted the same day.** The condition set for
removing it was that the console carry an equivalent end-to-end test,
and `run_uart_crypto` does: a request shifted in bit by bit over the
real UART and the response recovered the same way. What went: the RTL
(`oca_rgmii.sv`, `oca_udp_seam.sv`, `oca_top.sv`, `oca_top_mac.sv`,
`oca_top_stub.sv`, and under `oca/hw/rtl/vendor/` the three
parameter-fixing wrappers — `oca_eth_axis_64.v`,
`oca_eth_mac_1g_fifo_64.v`, `oca_udp_complete_64.v` — plus the two
synthesis probes `oca_eth_mac_1g_fifo_64_probe.sv` and
`oca_udp_complete_64_probe.sv`, which were not wrappers but
frontend-compatibility checks that also report area — **not** the
apparatus the 8422-LUT figure was measured with, which is a nextpnr
out-of-context run and is not reproduced by any script in this tree;
see `docs/design/2026-08-12-ethernet-measurement-provenance.md`), the four
runner/testbench pairs, the whole
`oca/hw/vendor/` tree — the `verilog-ethernet` submodule, its patches
and `vendor_patches.py` — the three `oca_top*` synthesis targets with
their netlist census tables, and the four DDR and delay primitives from
`ecp5_prims.sv`, which keeps `EHXPLLL`. **The measurements are kept**,
in `AGENTS.md` and `oca/hw/syn/README.md`; the code they were taken on
is not.

**Three pieces of it survive on purpose, and this is where they are
declared.**

- **`oca_clkrst.sv` keeps its B50612D PHY reset sequencer**, and
  `test_clkrst.py` keeps the tests that hold it to the datasheet's
  Table 86 minimums and their order. `oca_clkrst` sits inside `oca_pll`,
  a design already measured on silicon at bring-up step 3, and reopening
  it would put a validated design at risk for no functional gain.
  **Declared with it: the file's comments are now full of dangling
  references, and they stay.** Its header describes a wiring that exists
  nowhere in the tree. `:36` and `:196` name `oca_rgmii.sv`; `:89-90`
  name `oca_top`; `:106-107` name `verilog-ethernet`, `eth_mac_1g_fifo`
  and `udp_complete_64`; `:209-214` is a WIRING block routing clocks to
  `udp_complete_64`, `eth_mac_1g_fifo` and `oca_rgmii`; `:245` reads
  "The same three inverted, for verilog-ethernet" above ports that are
  still live. This is a known, bounded limitation, recorded here rather
  than fixed: correcting the comments means editing a
  silicon-validated design, which is the one thing this bullet exists to
  avoid.
- **`oca/hw/syn/colorlight_i9.lpf` is kept verbatim**, its fifteen-pin
  RGMII/PHY pinout included — twelve `rgmii_*` plus `phy_mdc`,
  `phy_mdio` and `phy_rst_n`; the file has seventeen `LOCATE`s in all
  and calls them "these seventeen pins" without separating out `clk25`
  and `led_n` — because 270 of its 327 lines are ECP5
  analysis that surviving code cites: `run_synth.py` cites it in three
  places, all seven of the other `.lpf` files take their balls and
  IO_TYPEs from it, and `.claude/skills/bringup/SKILL.md` rests on its
  bank-consistency argument. **No surviving design uses it as a
  constraint file** — every pinned design carries its own, from two pins
  for `oca_blink` to ten for `oca_vccio`.
- **The submodule's object store under `.git/modules` was deliberately
  not purged.** Upstream is public and still serves the pin (`77320a94`)
  as the head of master, so this is insurance rather than rescue: the
  commit is literally its "Add deprecation notice", the author has moved
  to `taxi` and the repository has not advanced since 2025-02-27. 6.3 MB
  is the price of not depending on a deprecated third-party repository
  staying up, and every published Ethernet figure was measured on those
  sources.
