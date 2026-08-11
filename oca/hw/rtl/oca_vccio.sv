// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * Bring-up: measure bank 6's VCCIO through the FPGA instead of guessing
 * which capacitor on the module carries it.
 *
 * An LVCMOS output driven high sits at its bank's VCCIO. A multimeter is
 * ten megohms, so it draws under a microamp and the drop across the
 * driver is nothing a meter can show: the level read on an open pin is
 * VCCIO6 to within the meter's own accuracy. No BGA ball is probed and
 * no board layout has to be known.
 *
 * WHICH PINS. litex-boards' colorlight_i5.py routes four free bank 6
 * balls to one connector, pmodk: M4 at index 1, L5 at 2, N4 at 4 and
 * L4 at 5. Nothing else of bank 6 lands on a single header like that.
 * K5 is deliberately not among them: it is VREF1_6, the bank's own
 * reference input.
 *
 * WHY TWO HIGH AND TWO LOW, ALTERNATING. Which physical header on the
 * carrier is pmodk is not documented anywhere we hold, so the pins have
 * to identify themselves. A pin left unconstrained is not driven, and
 * what the ECP5 does with those is not something this tree states, so
 * "reads a voltage" cannot be the signature. Reading a hard 0 V next to
 * a hard VCCIO6, twice, in the order high-low-skip-high-low, is: an
 * undriven pin does not sit at a rail on command, and it does not do so
 * in a pattern.
 *
 * The high pins then give the number, and they give it twice, so a
 * single wrong reading cannot pass as the answer.
 *
 * DRIVE=4 in the .lpf. The claim that these four balls are free is
 * litex's, not a measurement, and if one of them is driven by something
 * on the module then this design fights it. The weakest drive the ECP5
 * offers keeps that fight to a few milliamps.
 *
 * D2 keeps blinking so the bitstream can be seen to be live while the
 * meter is on the header, and it blinks the same one eighth as
 * oca_blink so the reading is the one already characterised.
 */
`default_nettype none

module oca_vccio (
    input  var logic clk25,
    output var logic led_n,
    output var logic probe_m4,
    output var logic probe_l5,
    output var logic probe_n4,
    output var logic probe_l4
);

    logic [24:0] beat;

    always_ff @(posedge clk25) begin
        beat <= beat + 25'd1;
    end

    always_comb led_n = ~(beat[24:22] == 3'd0);

    always_comb probe_m4 = 1'b1;
    always_comb probe_l5 = 1'b0;
    always_comb probe_n4 = 1'b1;
    always_comb probe_l4 = 1'b0;

endmodule

`default_nettype wire
