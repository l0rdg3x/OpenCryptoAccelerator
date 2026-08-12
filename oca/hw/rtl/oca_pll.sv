// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * Bring-up step 3: the PLL, and whether its output is the frequency the
 * whole design is constrained against.
 *
 * oca_clkrst is the real module rather than a copy, so the PLL under
 * test is the one later steps depend on. Two of its connections are NOT
 * what a board top level would drive, and one of them matters:
 * ext_rst_n is tied high here, so arst_n is pll_locked alone instead of
 * por_n && pll_locked, and a top that gates it with a power-on-reset
 * counter releases reset by a path this step never exercises. clk_rx is
 * tied to clk25 rather than to a recovered receive clock, and reaches
 * nothing this design consumes. Most of the module is not under test
 * either: this design consumes pll_locked and rst_n_tx and nothing
 * else, so the PHY reset timer and the sys and rx synchronisers are
 * optimised out and only two flip-flops of oca_clkrst survive. Those
 * two are the tx reset synchroniser. What this bitstream says about the
 * rest of oca_clkrst is nothing at all; test_clkrst.py is where the
 * rest is proven, and it is proven in simulation only.
 *
 * THE READING IS A FREQUENCY, NOT A LIGHT. A lock flag on an LED says
 * only that EHXPLLL asserted LOCK, which it will do around a wrong
 * frequency as readily as around the right one: LOCK reports that the
 * loop closed, not what it closed on. So D2 is driven from a counter on
 * clk_tx sized to halve 125 MHz exactly:
 *
 *   62_500_000 cycles at 125 MHz = 0.5 s, so a full period is 1.000 s
 *
 * and 1 Hz is checkable to a fraction of a percent with a phone
 * stopwatch over a minute, which no eyeballed blink rate is. If clk25
 * were 24 MHz rather than 25, clk_tx would be 120 MHz and the LED would
 * run 4% slow: 2.4 s of drift over a minute, visible without equipment.
 * That also tightens step 2's result, which only ever established the
 * oscillator to the precision of a blink counted by eye.
 *
 * THREE STATES, THREE READINGS, and no two of them may look alike. The
 * rule was paid for by an earlier top level whose led_n was the AND of
 * seven terms -- a beat, a lock flag, PHY readiness, three link terms
 * and a sticky delay-calibration flag -- so it went high and steady
 * both when the clocking failed and when everything worked. An
 * indicator that shows a dead board and a healthy one the same way
 * reports nothing, whichever of the two it is looking at.
 *
 *   D2 at 1 Hz, symmetric   PLL locked and clk_tx is 125 MHz
 *   D2 flickering, ~3 Hz    bitstream live, clk25 running, PLL NOT
 *                           locked. The fast beat is counted on clk25
 *                           itself, so it survives everything the PLL
 *                           can do.
 *   D2 static               no bitstream, or no clk25. Step 2 already
 *                           separated those two.
 *
 * The fast beat carries no reset. ECP5 flip-flops come out of
 * configuration cleared, which is the same start a power-on reset would
 * give it, and it must not depend on rst_n_sys: that is gated on
 * pll_locked, so a reset-driven fast beat would be dead in exactly the
 * case it exists to report.
 *
 * clk_rx is tied to clk25. The receive clock does not exist until a link
 * does, and oca_clkrst wants a clock on that port to synchronise a reset
 * it hands back; nothing here consumes rst_n_rx. ext_rst_n is tied high
 * for the reason oca_clkrst's own comment gives: with no reset pin the
 * PLL lock is the only reset root, which brings the design up and leaves
 * no way to restart it short of reconfiguring. That is acceptable for a
 * bring-up step whose restart is a reload.
 */
`default_nettype none

module oca_pll (
    input  var logic clk25,
    output var logic led_n
);

    logic clk_sys, clk_tx, pll_locked;
    logic rst_n_sys, rst_n_tx, rst_n_rx;
    logic rst_sys, rst_tx, rst_rx;
    logic phy_rst_n, phy_ready;

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

    // Half of 125 MHz. Counting to a decimal target rather than letting a
    // power of two roll over is the whole point: 2^26 cycles would be
    // 0.537 s and 0.93 Hz, which is not a number a stopwatch can hold
    // the design to.
    localparam int unsigned HALF_SECOND_TX = 62_500_000;

    logic [25:0] tx_count;
    logic        tx_beat;

    always_ff @(posedge clk_tx or negedge rst_n_tx) begin
        if (!rst_n_tx) begin
            tx_count <= '0;
            tx_beat  <= 1'b0;
        end else if (tx_count == HALF_SECOND_TX - 1) begin
            tx_count <= '0;
            tx_beat  <= ~tx_beat;
        end else begin
            tx_count <= tx_count + 26'd1;
        end
    end

    // 2^22 cycles at 25 MHz is 0.168 s, so this beats at 2.98 Hz: fast
    // enough to read as a flicker beside a one-second blink, slow enough
    // to count.
    logic [22:0] slow_count;

    always_ff @(posedge clk25) begin
        slow_count <= slow_count + 23'd1;
    end

    always_comb led_n = pll_locked ? ~tx_beat : ~slow_count[22];

endmodule

`default_nettype wire
