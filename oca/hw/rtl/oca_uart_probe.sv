// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * Which pin is the DAPLink's UART on? Nothing we hold says.
 *
 * The carrier carries a DAPLink that presents three things on USB: mass
 * storage, a HID interface that is the CMSIS-DAP we program through, and
 * a CDC pair, which is a serial port. That the serial port exists on the
 * USB side says nothing about whether it reaches the FPGA, and if it
 * does, nothing about where: the DAPLink is on the carrier and the
 * carrier is not documented anywhere in this project.
 *
 * litex-boards offers two candidates for this module, and offering two
 * is itself the evidence that neither is certain:
 *
 *   serial   J17 tx / H18 rx   bank 2, inherited from the v7.0 map
 *   serialx  E5  tx / F4  rx   bank 7, added for v7.2 and commented
 *                              "optional, alternative uart location"
 *
 * So both are driven, each with a payload naming its own pin, and the
 * answer is whichever string arrives. Same shape as the VCCIO probe: the
 * signal identifies itself rather than being looked up.
 *
 * ONLY THE tx PINS ARE DRIVEN. H18 and F4 are inputs to the FPGA, which
 * makes them outputs of the DAPLink, and driving into a driver is a
 * fight this design has no reason to pick. Finding the transmit pin also
 * settles the receive one: litex defines them as pairs on one connector,
 * so the partner follows.
 *
 * DRIVE=4 all the same, in the .lpf. That J17 and E5 are free is litex's
 * claim about a carrier litex has not seen either.
 *
 * BANK 2 AND BANK 7 ARE UNMEASURED, unlike bank 6. Both pins are
 * declared LVCMOS33 because that is what litex declares, and if either
 * bank is really at 2.5 V the pads output 2.5 V and the levels are off.
 * At 115200 baud that does not matter: the case that makes a wrong
 * declaration dangerous is receiver threshold against a DDR edge at
 * 125 MHz, four orders of magnitude from here. If nothing arrives, the
 * bank voltage is a hypothesis to test, not the first one.
 *
 * D2 TOGGLES ONCE PER MESSAGE, which couples the light to the thing
 * being debugged: a blinking LED and an empty terminal means the FPGA is
 * transmitting and the pin is wrong, which is a different problem from a
 * dark LED and an empty terminal.
 */
`default_nettype none

module oca_uart_probe (
    input  var logic clk25,
    output var logic led_n,
    output var logic uart_j17,
    output var logic uart_e5
);

    // 25 MHz over 115200. See oca_uart_tx for why 217 is close enough.
    localparam int DIV       = 217;
    // One message a second: enough to see it arrive, slow enough that a
    // terminal left open does not fill with it.
    localparam int SEND_TICK = 25_000_000;

    logic [24:0] tick_count;
    logic        send;

    always_ff @(posedge clk25) begin
        if (tick_count == SEND_TICK - 1) begin
            tick_count <= '0;
            send       <= 1'b1;
        end else begin
            tick_count <= tick_count + 25'd1;
            send       <= 1'b0;
        end
    end

    always_ff @(posedge clk25) begin
        if (send) begin
            led_n <= ~led_n;
        end
    end

    // "PIN=J17\n" and "PIN=E5 \n", eight bytes each so one parameter
    // width serves both. The trailing space on the shorter one keeps the
    // lengths equal without a second LEN.
    localparam logic [63:0] MSG_J17 = 64'h50_49_4E_3D_4A_31_37_0A;
    localparam logic [63:0] MSG_E5  = 64'h50_49_4E_3D_45_35_20_0A;

    // The entire method is that the two pins say DIFFERENT things. Equal
    // payloads would build, pack, load and transmit, and the bench would
    // read a name off whichever pin is wired and attribute it to both.
    // Nothing downstream catches that: the flip-flop census counts 52
    // either way, measured by making the two equal on purpose. So it is
    // caught here, where it costs nothing.
    if (MSG_J17 == MSG_E5) begin : gen_identical_payloads
        // One string literal: SystemVerilog does not concatenate
        // adjacent ones the way C does, and slang says "expected ','".
        $fatal(1, "oca_uart_probe: both probes carry the same payload, so neither pin can be told from the other");
    end

    oca_uart_tx #(
        .DIV (DIV),
        .LEN (8),
        .MSG (MSG_J17)
    ) u_tx_j17 (
        .clk  (clk25),
        .send (send),
        .busy (),
        .tx   (uart_j17)
    );

    oca_uart_tx #(
        .DIV (DIV),
        .LEN (8),
        .MSG (MSG_E5)
    ) u_tx_e5 (
        .clk  (clk25),
        .send (send),
        .busy (),
        .tx   (uart_e5)
    );

endmodule

`default_nettype wire
