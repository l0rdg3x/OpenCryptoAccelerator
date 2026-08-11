// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * Bring-up: does the DAPLink's UART reach the FPGA in BOTH directions?
 *
 * oca_uart_probe answered the transmit half on 2026-08-11: the host sees
 * J17. The receive half was litex's pairing and nothing more, so H18 was
 * a guess until a byte travelled it.
 *
 * ANSWERED THE SAME DAY: H18 is the receive pin. Eight bytes sent one
 * every 300 ms came back in order and byte exact, 4f 43 41 00 01 55 aa
 * ff, which includes the values a reversed shifter or a lost frame
 * mangles. Sent back to back instead, alternate bytes are dropped:
 * "OCA" returns "OA". That is this design's documented behaviour and
 * what test_a_byte_arriving_mid_echo_is_dropped_not_spliced asserts, so
 * the bench and the simulation agree on the failure as well as on the
 * success. A console has to do better than that, and the fix is a
 * holding register or a FIFO, not a change here.
 *
 * WHY AN ECHO AND NOT A COUNTER OR A BANNER. What has to be
 * distinguished is a receive path that works from one that does not, and
 * an echo is the only shape where the operator supplies the expected
 * value. A design that transmits something of its own on receiving
 * anything proves a byte arrived; it does not prove WHICH byte, so a
 * receiver that decodes every input as 0xFF passes it. Typing "OCA" and
 * reading back "OCA" cannot be passed by a receiver that is not
 * receiving. Type the printable range and it is a decoder test as well.
 *
 * D2 TOGGLES PER BYTE RECEIVED, which separates two failures the
 * terminal cannot: nothing echoed with D2 dead means the byte never
 * arrived, and nothing echoed with D2 toggling means it arrived and the
 * way back is broken. The transmit path is known good, so the second
 * would point at this module rather than at the wiring.
 *
 * FRAMING ERRORS ARE NOT ECHOED AND NOT COUNTED. oca_uart_rx raises
 * frame_error instead of valid, so a misframed byte simply produces no
 * echo and no toggle. That is a silent drop and it is deliberate here:
 * the reading is "what I typed came back", and a channel that answered
 * garbage to a bad frame would make a baud-rate mismatch look like a
 * decoder bug. It is a bring-up design, not the console.
 */
`default_nettype none

module oca_uart_echo (
    input  var logic clk25,
    output var logic led_n,
    output var logic uart_tx,
    input  var logic uart_rx
);

    localparam int DIV = 217;

    logic [7:0] rx_data;
    logic       rx_valid;
    logic       rx_frame_error;
    logic       tx_busy;

    oca_uart_rx #(.DIV (DIV)) u_rx (
        .clk         (clk25),
        .rx          (uart_rx),
        .data        (rx_data),
        .valid       (rx_valid),
        .frame_error (rx_frame_error)
    );

    // A byte arriving while the previous one is still going out is
    // dropped, not queued. At 115200 in and 115200 out that needs the
    // host to send with no gap at all, and a dropped byte shows as a
    // short echo rather than as a wrong one.
    oca_uart_tx8 #(.DIV (DIV)) u_tx (
        .clk  (clk25),
        .data (rx_data),
        .send (rx_valid && !tx_busy),
        .busy (tx_busy),
        .tx   (uart_tx)
    );

    always_ff @(posedge clk25) begin
        if (rx_valid) begin
            led_n <= ~led_n;
        end
    end

endmodule

`default_nettype wire
