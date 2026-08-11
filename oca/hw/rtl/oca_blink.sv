// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * Bring-up step 2: the smallest design that says something.
 *
 * A counter on the 25 MHz oscillator and the LED. No PLL, no RGMII, no
 * vendor logic -- when this does not blink, nothing after it is
 * diagnosable, so it must not contain anything that could be the reason.
 *
 * It is not oca_top_stub with the Ethernet removed. The stub cannot do
 * this job: its led_n is the AND of seven terms -- beat[24], pll_locked,
 * phy_ready, link_up, link_full_duplex, link_speed != 3 and
 * dly_cflag_seen -- so it needs a working PLL, a working PHY and a link
 * partner before it can toggle at all. And the last term is one this
 * design cannot make true or prove false: see the comment on that
 * always_comb, which is where the argument is written out. The stub
 * drives led_n high and steady when the clocking failed and can drive it
 * high and steady when everything worked, which is the one thing a
 * bring-up indicator may not do.
 *
 * WHY THE DUTY CYCLE IS NOT 50%. The step is meant to prove three things,
 * and a symmetric blink proves only two of them: that the oscillator is
 * on P3 at the frequency we think, and that the bitstream reached the
 * device. It cannot prove the third, the LED's polarity, because a square
 * wave looks the same whichever way round the diode sits -- and the
 * polarity is an assumption we have never checked on silicon. litex's
 * user_led_n says active low; the pin moved from U16 to L2 in the v7.2
 * revision and nothing in either source is a measurement.
 *
 * So the beat is one eighth on and seven eighths off. Under the active-
 * low assumption the LED shows a short flash every 1.34 s. If it is
 * actually active high, the same bitstream shows a long lit period with a
 * short gap -- the complementary pattern, unmistakable across the room
 * and needing no instrument.
 */
`default_nettype none

module oca_blink (
    input  var logic clk25,
    output var logic led_n
);

    // 2^25 cycles at 25 MHz is 1.342 s per period; the top three bits
    // select the eighth. No reset: ECP5 flip-flops come out of
    // configuration cleared, which is the same start this counter would
    // get from a power-on reset built to give it one.
    logic [24:0] beat;

    always_ff @(posedge clk25) begin
        beat <= beat + 25'd1;
    end

    always_comb led_n = ~(beat[24:22] == 3'd0);

endmodule

`default_nettype wire
