# OCA — MVP Development BOM

Hardware required to develop and test the MVP (Phase 2: ECP5 + GbE
prototype, see `SPEC.md`). Prices are indicative
(checked 2026-08-02); marketplaces change them without notice.

## Required

| Item | Spec / notes | Price |
|------|--------------|-------|
| Colorlight i9 v7.2 (Lattice ECP5 LFE5U-45F) | 44K LUTs, 8 MB SDRAM, W25Q64 flash, 2× GbE PHY (Broadcom B50612D). Get **Kit 2**: module + carrier board with DAPLink debugger (CMSIS-DAP JTAG, supported by openFPGALoader) and 6× dual-PMOD. ~40-55 € on AliExpress, ~69 € on Amazon. **Avoid listings labeled "i9+ XC7A50T"**: that is an Artix-7 board, not ECP5, incompatible with the open toolchain. The genuine i9+ (85K LUTs, LFE5U-85F) is fine but found mainly on AliExpress | 40-69 € |
| USB-C PSU 5 V / 3 A | e.g. official Raspberry Pi 4 PSU (5.1 V / 3 A) | ~14 € |
| 2× Ethernet cables, Cat5e or better | GbE link to the host | ~5 € |

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
- Host with a free GbE port
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
- Totals: ~60-90 € for the core kit (i9 Kit 2 + PSU + cables),
  ~110-165 € including debug tools.
