// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * The crypto console asked for the 50.00 MHz rung: oca_crypto_pll with
 * its divider set overridden to a 500 MHz VCO, on the same four pins.
 *
 * CLKI_DIV 1, CLKFB_DIV 5, CLKOP_DIV 4, CLKOS_DIV 10 puts the VCO at
 * 500 MHz, clk_tx at exactly 125 MHz and clk_sys at exactly 50.000 MHz.
 * Every guard in oca_clkrst recomputes from these parameters and
 * accepts the set — the same dividers the exp/clk-sys-50mhz experiment
 * ran through those guards on the old Ethernet top — and
 * oca_crypto_pll's own guard proves the CLK_SYS_HZ below is what the
 * dividers make before anything derives from it.
 *
 * THE UART DIVISOR COMES OUT BY DERIVATION, NOT BY EDIT. CLK_SYS_HZ
 * reaches oca_uart_crypto as CLK_HZ, so DIV is 50_000_000 / 115_200 =
 * 434 (434.03 truncated), and the stop-bit sampler guard there computes
 * the sample landing +6304 to +8608 ppm of a bit time after 9.5 bit
 * times — inside its 25_000 ppm budget. $clog2(434) is 9, the same as
 * the shipping 417, so the divisor counters do not gain a bit and the
 * per-file flip-flop floors match oca_crypto_pll's exactly
 * (hw/syn/run_synth.py, NETLIST_FF_FLOOR).
 *
 * MEASURED 2026-08-16, four placer seeds on commit b0a94db: clk_sys
 * 51.83 / 51.91 / 50.48 / 49.61 MHz against the 50.00 required --
 * three seeds close, the fourth misses by 0.78% (docs/RECORD.md, the
 * PLL ladder). By the project's rule that is NOT closure, so this
 * variant is measured and not shipped: the board top stays at
 * 48.0769, where every seed clears. This file remains in the tree as
 * the measured baseline for any future seed hunt or faster netlist.
 * Nothing here has run on silicon.
 *
 * A thin instantiation and nothing else, so the design under this name
 * is provably oca_crypto_pll at another operating point rather than a
 * copy that can drift. It shares colorlight_i9_crypto.lpf: same four
 * ports on the same four balls, and nextpnr derives the 50.00 MHz
 * clk_sys constraint from the dividers in the netlist, so no .lpf line
 * moves — the reasons are the DESIGNS entry's in hw/syn/run_synth.py.
 */
`default_nettype none

module oca_crypto_pll_50 #(
    // Forwarded to the heartbeat counter; 25 is the board, a simulation
    // elaborates it small (oca_crypto_pll.sv).
    parameter int LED_BITS = 25
) (
    input  var logic clk25,
    output var logic led_n,
    output var logic uart_tx,
    input  var logic uart_rx
);

    // 500 MHz VCO over CLKOS_DIV 10. run_synth.py's check_clk_sys_const
    // reads this constant back and compares it against the dividers in
    // the built netlist.
    localparam int CLK_SYS_HZ = 50_000_000;

    oca_crypto_pll #(
        .LED_BITS   (LED_BITS),
        .CLK_SYS_HZ (CLK_SYS_HZ),
        .CLKI_DIV   (1),
        .CLKFB_DIV  (5),
        .CLKOP_DIV  (4),
        .CLKOS_DIV  (10)
    ) u_top (
        .clk25   (clk25),
        .led_n   (led_n),
        .uart_tx (uart_tx),
        .uart_rx (uart_rx)
    );

endmodule

`default_nettype wire
