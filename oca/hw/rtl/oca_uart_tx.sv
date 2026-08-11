// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * The smallest 8N1 transmitter that can carry a name, for finding out
 * which pin a UART is on when nothing documents it.
 *
 * No receiver, no flow control, no FIFO. It exists to answer one
 * question at the bench and the question is which of two candidate pins
 * a host is listening to, so the payload is a constant and the only
 * thing that varies between instances is what the constant says.
 *
 * DIV is the clock divisor. 25 MHz over 115200 is 217.01, and 217 gives
 * 115207 baud: 0.006% off, against the 2% or so a UART tolerates in
 * total across both ends, so the divisor is not what will make this
 * fail.
 *
 * MSG holds LEN bytes with the FIRST byte in the most significant
 * position, because that is the order a literal reads in: "PIN=J17\n"
 * written as a hex constant puts P at the top. Bytes go out LSB first
 * inside their frame, which is 8N1, but the byte ORDER is left to right.
 */
`default_nettype none

module oca_uart_tx #(
    parameter int          DIV = 217,
    parameter int          LEN = 8,
    parameter logic [63:0] MSG = 64'd0
) (
    input  var logic clk,
    // One cycle high starts the whole message. Ignored while busy, so a
    // send that arrives during a transmission is dropped rather than
    // corrupting the frame in flight.
    input  var logic send,
    output var logic busy,
    output var logic tx
);

    localparam int DIV_W = $clog2(DIV);
    localparam int IDX_W = $clog2(LEN + 1);

    logic [DIV_W-1:0] div_count;
    logic [3:0]       bit_index;
    logic [IDX_W-1:0] byte_index;
    logic [9:0]       shifter;
    logic             active;

    // Idle high is what a UART line does between frames, and what the
    // receiver needs to see a start bit as a falling edge.
    always_comb tx   = active ? shifter[0] : 1'b1;
    always_comb busy = active;

    // int unsigned rather than the index width, so the arithmetic below
    // happens at 32 bits and not at the four the caller happens to
    // carry. Verilator rejects the narrow form under -Wall, and it is
    // right to: 8*(LEN-1-i) in four bits wraps for any LEN over 2.
    function automatic logic [7:0] byte_at(input int unsigned i);
        byte_at = MSG[8*(LEN-1-i) +: 8];
    endfunction

    always_ff @(posedge clk) begin
        if (!active) begin
            if (send) begin
                active     <= 1'b1;
                byte_index <= '0;
                bit_index  <= 4'd0;
                div_count  <= '0;
                shifter    <= {1'b1, byte_at(32'd0), 1'b0};
            end
        end else if (div_count != DIV_W'(DIV - 1)) begin
            div_count <= div_count + DIV_W'(1);
        end else begin
            div_count <= '0;
            if (bit_index != 4'd9) begin
                shifter   <= {1'b1, shifter[9:1]};
                bit_index <= bit_index + 4'd1;
            end else if (byte_index != IDX_W'(LEN - 1)) begin
                byte_index <= byte_index + IDX_W'(1);
                bit_index  <= 4'd0;
                shifter    <= {1'b1, byte_at(32'(byte_index) + 32'd1), 1'b0};
            end else begin
                active <= 1'b0;
            end
        end
    end

endmodule

`default_nettype wire
