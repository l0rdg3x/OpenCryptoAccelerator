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
 * THE MEASUREMENT POINT IS A 2.54 mm HEADER HOLE, not a pad on the
 * module. D2's own pads would be the shortest route, but they are an
 * 0603 pair a slipped probe can bridge, and the operator asked for a
 * target that cannot be shorted. Header holes cannot.
 *
 * WHICH HOLE. colorlight_i5.py names the physical connector above each
 * pmod pair: pmodc and pmodd on P2, pmodg and pmodh on P4, pmodi and
 * pmodj on P5, pmodk and pmodl on P6. This comment claimed that mapping
 * was undocumented until 2026-08-11, which was an artefact of the grep
 * that read the file: it captured the tuples and discarded the comment
 * lines between them.
 *
 * The probes toggle in step with D2 all the same, and it is still worth
 * it. A documented mapping fixes the connector; it does not say which
 * physical hole in a 2x13 header is a given pmod index, and nothing here
 * documents that. A reading that swings in time with a light the
 * operator can see identifies the hole itself, at the cost of nothing.
 *
 * beat[26] toggles every 2^26 cycles, which at 25 MHz is 2.684 s in each
 * state and 5.369 s round trip: long enough for a digital meter to
 * settle twice without hurry.
 *
 * EIGHT PROBES, ON TWO CONNECTORS. prjtrellis's iodb.json gives bank 6
 * as 33 balls on CABGA381 and colorlight_i9.lpf constrains seventeen. Of
 * the sixteen left, colorlight_i5.py routes NINE to headers; eight are
 * probed here and the ninth is K5, held out below. They land on two
 * carrier connectors, not four, because pmodg and pmodh share P4 and
 * pmodk and pmodl share P6:
 *
 *   P4                    P6
 *   F1 = pmodg 2          M4 = pmodk 1    N4 = pmodk 4    M1 = pmodl 3
 *   K4 = pmodh 6          L5 = pmodk 2    L4 = pmodk 5    N2 = pmodl 7
 *
 * Which makes the bench result read all the way back: two probes were
 * found swinging on P4, and F1 and K4 are the only two of the eight that
 * P4 carries.
 *
 * K5 is bank 6, free, and on pmodh, and is deliberately not among them:
 * it is VREF1_6, the bank's own reference input.
 *
 * DRIVE=4 in the .lpf. That these eight balls are free is litex's claim
 * and not a measurement, and if one is driven by something on the module
 * then this design fights it. The weakest drive the ECP5 offers keeps
 * that fight to a few milliamps.
 */
`default_nettype none

module oca_vccio (
    input  var logic clk25,
    output var logic led_n,
    output var logic probe_f1,
    output var logic probe_k4,
    output var logic probe_m4,
    output var logic probe_l5,
    output var logic probe_n4,
    output var logic probe_l4,
    output var logic probe_m1,
    output var logic probe_n2
);

    logic [26:0] beat;

    always_ff @(posedge clk25) begin
        beat <= beat + 27'd1;
    end

    always_comb led_n = beat[26];

    // High while D2 is lit, since led_n is active low. The operator
    // reads the meter against the LED, so the two must agree by eye.
    always_comb begin
        probe_f1 = ~led_n;
        probe_k4 = ~led_n;
        probe_m4 = ~led_n;
        probe_l5 = ~led_n;
        probe_n4 = ~led_n;
        probe_l4 = ~led_n;
        probe_m1 = ~led_n;
        probe_n2 = ~led_n;
    end

endmodule

`default_nettype wire
