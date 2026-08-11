// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * One byte, framed 8N1, from a port rather than from a parameter.
 *
 * oca_uart_tx sends a payload fixed at elaboration, which is what a pin
 * probe needs and what a console cannot use. This is the same framing
 * with the byte as an input.
 *
 * KNOWN RESIDUAL, recorded rather than fixed: the framing logic now
 * exists twice, here and in oca_uart_tx. oca_uart_tx is the tested one
 * and is on the board, so it is not being rewritten in the same change
 * that first needs a byte-level transmitter. The unification belongs
 * with the console that will make oca_uart_tx redundant; until then this
 * file carries its own tests rather than inheriting confidence.
 *
 * send is ignored while busy, so a byte offered during a frame is
 * dropped and not spliced into it. A dropped byte on a diagnostic
 * channel is a visible gap; a spliced one is a wrong answer.
 */
`default_nettype none

module oca_uart_tx8 #(
    parameter int DIV = 217
) (
    input  var logic       clk,
    input  var logic [7:0] data,
    input  var logic       send,
    output var logic       busy,
    output var logic       tx
);

    localparam int DIV_W = $clog2(DIV);

    logic [DIV_W-1:0] div_count;
    logic [3:0]       bit_index;
    logic [9:0]       shifter;
    logic             active;

    always_comb tx   = active ? shifter[0] : 1'b1;
    always_comb busy = active;

    always_ff @(posedge clk) begin
        if (!active) begin
            if (send) begin
                active    <= 1'b1;
                bit_index <= 4'd0;
                div_count <= '0;
                // Stop bit high, data LSB first, start bit low: shifted
                // out of bit 0, so the start bit leaves first.
                shifter   <= {1'b1, data, 1'b0};
            end
        end else if (div_count != DIV_W'(DIV - 1)) begin
            div_count <= div_count + DIV_W'(1);
        end else begin
            div_count <= '0;
            if (bit_index != 4'd9) begin
                shifter   <= {1'b1, shifter[9:1]};
                bit_index <= bit_index + 4'd1;
            end else begin
                active <= 1'b0;
            end
        end
    end

endmodule

`default_nettype wire
