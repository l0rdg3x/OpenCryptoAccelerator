// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * Bring-up step 2: the smallest design that says something.
 *
 * A counter on the 25 MHz oscillator and the LED. No PLL, no reset
 * logic, no vendor modules -- when this does not blink, nothing after it
 * is diagnosable, so it must not contain anything that could be the
 * reason.
 *
 * WHICH IS WHY IT IS A FRESH COUNTER and not an existing top level with
 * its datapath taken out. The top level this project ran before drove
 * led_n from the AND of seven terms -- a beat, pll_locked, phy_ready,
 * link_up, link_full_duplex, link_speed != 3 and a sticky
 * delay-calibration flag -- so it needed a working PLL, a working PHY
 * and a link partner before it could toggle at all. And that last term
 * was not knowable: the delay primitive's calibration output is carried
 * as a bare blackbox by yosys, has no simulation model in prjtrellis and
 * is only placed by nextpnr, so whether it rests high is characterised
 * nowhere -- unknown, in a design that needed it true. So that top level
 * drove led_n high and steady when the clocking failed and could drive
 * it high and steady when everything worked, which is the one thing a
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
