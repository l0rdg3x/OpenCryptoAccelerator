<!-- SPDX-License-Identifier: MIT -->
# Project status

**Updated 2026-08-12.** Where the project is, in one page. Update this at
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
protocol FSM — behind a 64-bit AXI-Stream pair. 177 RTL tests across 25
runners; 207 passing executions measured on 2026-08-12, no failures.

**The open ECP5 toolchain**, built locally in `tools/`: yosys with the
slang frontend, nextpnr-ecp5, prjtrellis, Verilator, openFPGALoader.

**Bring-up on silicon**, four rungs, on the Colorlight i9 v7.2 that
arrived 2026-08-11:

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
| LUT | 13043 — 29.7% of the device |
| flip-flops | 12518 — 28.5% |
| block RAM | 6 of 108 |
| multipliers | 20 of 72 |
| Fmax | 50.55 MHz against the 25.00 required — 2.02x |
| bitstream | 423213 bytes, packed |

Seed 1, `colorlight_i9_crypto.lpf`. Measured twice independently.

## Not established

**No bitstream containing crypto has been loaded onto the board.** What
is proved is that it builds, closes its clock with a doubling of margin,
and answers the protocol correctly in simulation down to UART bit
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
2. Time the PLL with a stopwatch, and measure bank 2 while the meter is
   out.
3. Decide what happens to the retired Ethernet RTL — `oca_rgmii`,
   `oca_udp_seam`, `oca_top` and the vendored stack. They cost nothing
   where they sit and no aggregate runner invokes them; the console now
   carries an equivalent end-to-end test, which was the condition set for
   removing them.

## Closed

**Ethernet, 2026-08-12.** No RJ45 socket on this kit, and the die is an
LFE5U with no SERDES, so this part can never be the PCIe platform either.
`SPEC.md` PHASE 2 records both reasons. Nothing in the tree asks for
Ethernet work any more; what was measured is kept because it was
measured.
