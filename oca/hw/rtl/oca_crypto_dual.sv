// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * The dual-core crypto console on the PLL: oca_clkrst plus
 * oca_uart_crypto_dual, on the same four pins every serial design here
 * uses.
 *
 * This file is oca_crypto_pll with the datapath swapped: everything the
 * two tops share — why the PLL is out here and not in the datapath, the
 * two connections of oca_clkrst an Ethernet top would drive
 * differently, why D2 is counted on clk25, the lock term's deliberate
 * non-stickiness and its recorded residual, the eighth cause, what D2
 * still cannot say, the reading trap once a host opens the port, and
 * what a simulation of this file can and cannot show — is documented
 * there, at length, and holds here unchanged. What differs is below.
 *
 * THE FAST RATE NOW HAS NINE CAUSES, not eight. To oca_crypto_pll's
 * eight — PLL not locked, datapath never left reset, and the six sticky
 * datapath faults — this design adds a ninth, sticky like the six:
 * oca_collect's fault bit, which latches when the two cores answered
 * one broadcast with different status bytes OR when an expectation
 * queue overflowed. Identical RTL on one clock, fed identical beats,
 * cannot diverge, and the queues cannot overflow while the dispatcher
 * stalls on tag_full, so either firing is a fault latch, not a status
 * (oca_collect.sv). It travels
 * inside the datapath's one `trouble` wire, so the three synchronisers
 * and the OR below are oca_crypto_pll's untouched: nine causes, still
 * one LED, still deliberately coarse.
 *
 * THIS TOP HAS BEEN BUILT AND SWEPT, AND IT DOES NOT CLOSE. Four
 * placer seeds on 2026-08-16 read 50.28 / 50.66 / 46.23 / 51.15 MHz
 * against the 48.0769 MHz clk_sys asks for: three clear it by 4.6 to
 * 6.4% and the third misses by 3.85%, so by this project's rule the
 * constraint is not closed and this top is measured, not shipped.
 * 26290 LUTs and 25124 flip-flops, 60.0% of the device, and the
 * spread across the four seeds is 10.6% — the widest recorded here.
 * The warning this header used to carry was right for the reason it
 * gave: oca_dual's out-of-context sweep had a seed at 47.68 MHz, and
 * an out-of-context Fmax is a ceiling rather than the clock a board
 * runs. docs/RECORD.md carries the sweep and what it does not settle;
 * the rung below, 625/14 = 44.643 MHz, has never been measured.
 */
`default_nettype none

module oca_crypto_dual #(
    // Heartbeat counter width, on clk25: oca_crypto_pll's parameter,
    // same default, same floor, same reason a simulation elaborates it
    // small.
    parameter int LED_BITS = 25
) (
    input  var logic clk25,
    output var logic led_n,
    output var logic uart_tx,
    input  var logic uart_rx
);

    localparam int SLOW = LED_BITS - 1;
    localparam int FAST = LED_BITS - 4;

    if (LED_BITS < 5) begin : gen_illegal_led_bits
        $fatal(1, "oca_crypto_dual: LED_BITS must be at least 5 (got %0d)",
               LED_BITS);
    end

    // oca_clkrst's CLK_SYS_HZ = VCO_HZ / CLKOS_DIV = 625e6 / 13. A
    // third writing-down of this number (oca_clkrst keeps it as a
    // localparam, oca_crypto_pll copies it for the same reason): a
    // CLKOS_DIV edit there must find every copy, or the UART divisor is
    // derived from a frequency the board does not run at — a mute
    // serial line, not a build failure.
    localparam int CLK_SYS_HZ = 48_076_923;

    logic clk_sys, pll_locked, rst_n_sys;

    /*
     * Eight of oca_clkrst's outputs belong to a route this design does
     * not have; named so a forgotten pin still fails the build, waived
     * rather than ORed for oca_crypto_pll's reason (clk_tx is a global
     * clock net). The waiver covers these four declarations and nothing
     * else.
     */
    /* verilator lint_off UNUSEDSIGNAL */
    logic clk_tx;
    logic rst_n_tx, rst_n_rx;
    logic rst_sys, rst_tx, rst_rx;
    logic phy_rst_n, phy_ready;
    /* verilator lint_on UNUSEDSIGNAL */

    oca_clkrst u_clkrst (
        .clk_in     (clk25),
        .ext_rst_n  (1'b1),
        .clk_rx     (clk25),
        .clk_sys    (clk_sys),
        .clk_tx     (clk_tx),
        .pll_locked (pll_locked),
        .rst_n_sys  (rst_n_sys),
        .rst_n_tx   (rst_n_tx),
        .rst_n_rx   (rst_n_rx),
        .rst_sys    (rst_sys),
        .rst_tx     (rst_tx),
        .rst_rx     (rst_rx),
        .phy_rst_n  (phy_rst_n),
        .phy_ready  (phy_ready)
    );

    logic trouble, rst_n_core;

    oca_uart_crypto_dual #(.CLK_HZ (CLK_SYS_HZ)) u_crypto (
        .clk        (clk_sys),
        .rst_n      (rst_n_sys),
        .uart_tx    (uart_tx),
        .uart_rx    (uart_rx),
        .rst_n_core (rst_n_core),
        .trouble    (trouble)
    );

    // ------------------------------------------------------------------
    // D2
    // ------------------------------------------------------------------
    logic [LED_BITS-1:0] beat;
    logic [1:0]          trouble_sync, locked_sync, started_sync;

    // verilator lint_off SYNCASYNCNET
    //
    // pll_locked and rst_n_core are asynchronous reset roots read
    // synchronously; this is the synchronous end of both nets, two
    // flops deep, driving nothing but an LED (oca_crypto_pll.sv).
    always_ff @(posedge clk25) begin
        beat         <= beat + LED_BITS'(1);
        trouble_sync <= {trouble_sync[0], trouble};
        locked_sync  <= {locked_sync[0], pll_locked};
        started_sync <= {started_sync[0], rst_n_core};
        // Active low, settled on the board 2026-08-11 by oca_blink's
        // asymmetric duty cycle.
        led_n <= ~((trouble_sync[1] || !locked_sync[1] || !started_sync[1])
                   ? beat[FAST] : beat[SLOW]);
    end
    // verilator lint_on SYNCASYNCNET

endmodule

`default_nettype wire
