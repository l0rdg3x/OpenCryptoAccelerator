# OCA — MVP Development BOM

Hardware required to develop and test the MVP (Phase 2: ECP5 prototype
driven over the board's USB serial, see `SPEC.md`; this line read
"ECP5 + GbE prototype" until 2026-08-12). Prices are indicative
(checked 2026-08-02); marketplaces change them without notice.

## Required

| Item | Spec / notes | Price |
|------|--------------|-------|
| Colorlight i9 v7.2 (Lattice ECP5 LFE5U-45F) | 44K LUTs, 8 MB SDRAM, W25Q64 flash, 2× GbE PHY (Broadcom B50612D) **and no Ethernet socket** -- the PHYs are on the module and their MDI pairs go to the SO-DIMM edge, but the RJ45s and the magnetics are on a carrier no kit sold with this module includes. This line listed the PHYs as if they were ports until 2026-08-12; a PHY is the chip, not the socket. The project does not need them: see SPEC.md PHASE 2. Get **Kit 2**: module + carrier board with DAPLink debugger (CMSIS-DAP JTAG, supported by openFPGALoader, and a USB CDC serial that reaches the FPGA on J17/H18 -- that is the host interface) and 6× dual-PMOD. ~40-55 € on AliExpress, ~69 € on Amazon. **Avoid listings labeled "i9+ XC7A50T"**: that is an Artix-7 board, not ECP5, incompatible with the open toolchain. The genuine i9+ (85K LUTs, LFE5U-85F) is fine but found mainly on AliExpress | 40-69 € |
| USB-C PSU 5 V / 3 A | e.g. official Raspberry Pi 4 PSU (5.1 V / 3 A) | ~14 € |

## JTAG plan B (buy only if needed)

| Item | Spec / notes | Price |
|------|--------------|-------|
| CJMCU FT232H module | Supported by openFPGALoader for ECP5. Needed only if the Kit-2 DAPLink does not work at first flash | ~17 € |

## Recommended for bring-up and debug (not strictly required)

| Item | Spec / notes | Price |
|------|--------------|-------|
| USB logic analyzer, 8 ch / 24 MHz | FX2 clone compatible with sigrok/PulseView | ~15 € |
| TRMS multimeter | Rail checks (3.3 V / 1.8 V); e.g. KAIWEETS KM601S, any 6000-count TRMS works | 30-60 € |

## Assumed already available (do not buy)

- Linux development machine
- Host with a free USB port: the carrier's DAPLink is both the JTAG
  programmer and the host serial link, on one cable. This line read
  "Host with a free GbE port" until 2026-08-12
- Optional: a Raspberry Pi can act as JTAG programmer via openFPGALoader
  GPIO, replacing the FT232H

## Software — zero cost

yosys, nextpnr-ecp5, openFPGALoader, Verilator, cocotb, SymbiYosys:
all open source. Test vectors: RFC 8439, NIST CAVP (public).

## Notes

- The Colorlight i9 is a recycled LED-panel controller reused as a dev
  board: community documentation only, no vendor support. It is the
  de-facto standard for low-cost ECP5 development.
- AliExpress orders: 2-4 weeks shipping, possible VAT on delivery.
- Totals: ~55-85 € for the core kit (i9 Kit 2 + PSU), ~100-160 €
  including debug tools — the sums of the rows above (54-83 and 99-158),
  each end rounded to the nearest five. This said "rounded outward"
  until 2026-08-12, and both low ends are rounded inward.
  These read ~60-90 and ~110-165 until 2026-08-12, when the two Ethernet
  cables left the required list: there is no socket to put one in, and
  the host interface is the DAPLink USB serial that comes with Kit 2.
  Dropping a ~5 € row does not account for all of the change; the old
  debug-tools low end was already about 6 € above its own rows.
