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
 * wave looks the same whichever way round the diode sits.
 *
 * So the beat is one eighth on and seven eighths off: led_n low for
 * 168 ms and high for 1174 ms. Active low shows a short flash every
 * 1.34 s, active high the complementary pattern, and the two are
 * unmistakable across the room without an instrument.
 *
 * Run on the board 2026-08-11, D2 showed the short flash. The LED is
 * ACTIVE LOW and litex's user_led_n is right. Until then it was an
 * assumption: the pin had moved from U16 to L2 in the v7.2 revision and
 * nothing in either source was a measurement.
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
