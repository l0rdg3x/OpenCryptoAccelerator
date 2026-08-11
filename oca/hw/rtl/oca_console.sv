// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * A diagnostic console: one character in, one line out.
 *
 * SINGLE-CHARACTER COMMANDS, deliberately, and not a line parser. A line
 * parser needs a buffer, an editor, a length limit and a policy for what
 * happens when the limit is hit, and every one of those is a place to
 * put a bug into the only channel available for finding bugs. One
 * character needs none of it and can be typed into a terminal or piped
 * from a script identically.
 *
 *   p   the channel is alive          -> OCA\n
 *   s   the counters                  -> R=xxxx E=xxxx O=xxxx C=xxxx\n
 *   z   zero the counters             -> ok\n
 *   ?   what the commands are         -> psz?\n
 *   anything else                     -> ?\n
 *
 * WHAT THE COUNTERS ARE FOR. R is bytes the receiver delivered, E is
 * frames it refused, O is bytes the input FIFO had no room for, C is
 * commands run. E and O are the two ways this channel loses input, and
 * both are invisible without them: a wrong baud rate shows as E rising
 * with R flat, and a host talking faster than the console answers shows
 * as O rising. A console that dropped either in silence would be a
 * console whose silence has two meanings.
 *
 * COUNTERS SATURATE, they do not wrap. A counter that wraps reads as a
 * small number after a long run and is indistinguishable from a healthy
 * one, which is the failure this whole file exists to avoid. At 0xFFFF
 * they stop and stay, and `z` is how they start again.
 *
 * The response is emitted a byte at a time into whatever the caller
 * gives as the transmit side, and stalls without loss when that side is
 * full: tx_push is only raised when tx_ready is high.
 */
`default_nettype none

module oca_console (
    input  var logic       clk,
    input  var logic       rst_n,

    // Receive side: a byte is present when rx_valid is high, and it is
    // consumed by raising rx_pop for one cycle.
    input  var logic [7:0] rx_data,
    input  var logic       rx_valid,
    output var logic       rx_pop,

    // Events counted but not otherwise acted on.
    input  var logic       frame_error,
    input  var logic       rx_overflow,

    // Transmit side.
    output var logic [7:0] tx_data,
    output var logic       tx_push,
    input  var logic       tx_ready
);

    localparam int RESP_MAX = 28;

    logic [15:0] rx_count, err_count, ovf_count, cmd_count;
    logic [7:0]  command;
    logic [4:0]  resp_index;
    logic [4:0]  resp_len;
    logic        sending;

    function automatic logic [7:0] hex(input logic [3:0] n);
        hex = (n < 4'd10) ? (8'h30 + 8'(n)) : (8'h41 + 8'(n) - 8'd10);
    endfunction

    // The status line, position by position. Written out rather than
    // assembled from a loop because the offsets are the format: a reader
    // checking that the field widths match the labels can do it here.
    function automatic logic [7:0] status_at(input logic [4:0] i);
        case (i)
            5'd0:  status_at = "R";
            5'd1:  status_at = "=";
            5'd2:  status_at = hex(rx_count[15:12]);
            5'd3:  status_at = hex(rx_count[11:8]);
            5'd4:  status_at = hex(rx_count[7:4]);
            5'd5:  status_at = hex(rx_count[3:0]);
            5'd6:  status_at = " ";
            5'd7:  status_at = "E";
            5'd8:  status_at = "=";
            5'd9:  status_at = hex(err_count[15:12]);
            5'd10: status_at = hex(err_count[11:8]);
            5'd11: status_at = hex(err_count[7:4]);
            5'd12: status_at = hex(err_count[3:0]);
            5'd13: status_at = " ";
            5'd14: status_at = "O";
            5'd15: status_at = "=";
            5'd16: status_at = hex(ovf_count[15:12]);
            5'd17: status_at = hex(ovf_count[11:8]);
            5'd18: status_at = hex(ovf_count[7:4]);
            5'd19: status_at = hex(ovf_count[3:0]);
            5'd20: status_at = " ";
            5'd21: status_at = "C";
            5'd22: status_at = "=";
            5'd23: status_at = hex(cmd_count[15:12]);
            5'd24: status_at = hex(cmd_count[11:8]);
            5'd25: status_at = hex(cmd_count[7:4]);
            5'd26: status_at = hex(cmd_count[3:0]);
            default: status_at = "\n";
        endcase
    endfunction

    function automatic logic [7:0] resp_at(input logic [7:0] cmd,
                                           input logic [4:0] i);
        case (cmd)
            "p": case (i)
                     5'd0: resp_at = "O";
                     5'd1: resp_at = "C";
                     5'd2: resp_at = "A";
                     default: resp_at = "\n";
                 endcase
            "s": resp_at = status_at(i);
            "z": case (i)
                     5'd0: resp_at = "o";
                     5'd1: resp_at = "k";
                     default: resp_at = "\n";
                 endcase
            "?": case (i)
                     5'd0: resp_at = "p";
                     5'd1: resp_at = "s";
                     5'd2: resp_at = "z";
                     5'd3: resp_at = "?";
                     default: resp_at = "\n";
                 endcase
            default: case (i)
                         5'd0: resp_at = "?";
                         default: resp_at = "\n";
                     endcase
        endcase
    endfunction

    function automatic logic [4:0] resp_length(input logic [7:0] cmd);
        case (cmd)
            "p":     resp_length = 5'd4;
            "s":     resp_length = 5'(RESP_MAX);
            "z":     resp_length = 5'd3;
            "?":     resp_length = 5'd5;
            default: resp_length = 5'd2;
        endcase
    endfunction

    always_comb rx_pop  = rx_valid && !sending;
    always_comb tx_data = resp_at(command, resp_index);
    always_comb tx_push = sending && tx_ready;

    // Saturating, for the reason in the header: a wrapped counter reads
    // like a healthy one.
    function automatic logic [15:0] bump(input logic [15:0] c);
        bump = (c == 16'hFFFF) ? c : c + 16'd1;
    endfunction

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            rx_count   <= '0;
            err_count  <= '0;
            ovf_count  <= '0;
            cmd_count  <= '0;
            command    <= 8'd0;
            resp_index <= '0;
            resp_len   <= '0;
            sending    <= 1'b0;
        end else begin
            if (frame_error) err_count <= bump(err_count);
            if (rx_overflow) ovf_count <= bump(ovf_count);

            if (!sending) begin
                if (rx_valid) begin
                    rx_count   <= bump(rx_count);
                    cmd_count  <= bump(cmd_count);
                    command    <= rx_data;
                    resp_index <= '0;
                    resp_len   <= resp_length(rx_data);
                    sending    <= 1'b1;
                end
            end else if (tx_ready) begin
                if (resp_index == resp_len - 5'd1) begin
                    sending <= 1'b0;
                    // Zeroing happens at the END of the response, so the
                    // reply to `z` is not itself counted into the numbers
                    // it just cleared and `s` straight after reads C=0001.
                    if (command == "z") begin
                        rx_count  <= '0;
                        err_count <= '0;
                        ovf_count <= '0;
                        cmd_count <= '0;
                    end
                end else begin
                    resp_index <= resp_index + 5'd1;
                end
            end
        end
    end

endmodule

`default_nettype wire
