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
 * R and C measure different points on purpose and R >= C always: R is
 * taken at the receiver, C at the command. They were the same register
 * until 2026-08-11, incremented in one branch, which made them equal at
 * every cycle for every input and the difference between them
 * unreadable. The difference is the interesting part -- it is what the
 * queue is holding plus what O refused.
 *
 * WHICH IS WHY z DOES NOT SET R TO ZERO. The clear runs when the z is
 * accepted, and anything typed ahead of it is already counted into R
 * and still sitting in the queue behind it; those bytes bump C after
 * the clear has run. R zeroed outright therefore ends up below C and
 * the published queue depth reads as a negative number -- `s s s z s`
 * typed at speed answered R=0000 C=0001, and R - C - O was -1. So the
 * clear leaves R holding what R - C - O said an instant earlier, which
 * is exactly the byte count those queued commands are about to consume.
 * R = C + O + queued is the identity, and setting R to that queue depth
 * with the other three at zero is the only clear that keeps it. Bytes
 * arriving and overflows pulsing in that same cycle are folded in,
 * because the clear overrides their bumps and they would otherwise be
 * counted on one side of the identity and not the other. A caller whose
 * queue loses bytes in some way O does not see makes the estimate high,
 * never low, so the invariant survives a wiring this module cannot
 * inspect.
 *
 * THE STATUS LINE IS A SNAPSHOT, taken in the cycle the command is
 * accepted and held in four registers of its own. Read live it would be
 * sixteen samples and not one: each hex digit is a separate read, one
 * per beat, and a beat costs a drain of whatever is downstream -- 86.8
 * us on this board with the transmit side full. A counter crossing a
 * nibble boundary between two beats prints a value it never held, and
 * 0x000F followed by 0x0010 came out as 0000. C made it worse rather
 * than better: it cannot move during a response while the other three
 * can, so the four fields described four different instants.
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
    //
    // rx_delivered is a separate port from rx_valid ON PURPOSE, and the
    // first version of this module did not have it: R was bumped in the
    // same branch as C, which made the two provably equal at every
    // cycle for every input and the status line three numbers wearing
    // four labels. R is meant to be what the RECEIVER delivered, which
    // is upstream of the queue, so a byte the input FIFO had no room
    // for now shows in R and in O and never reaches C. R minus C minus
    // O is what is still waiting.
    input  var logic       rx_delivered,
    input  var logic       frame_error,
    input  var logic       rx_overflow,

    // Transmit side.
    output var logic [7:0] tx_data,
    output var logic       tx_push,
    input  var logic       tx_ready
);

    localparam int RESP_MAX = 28;

    logic [15:0] rx_count, err_count, ovf_count, cmd_count;
    logic [15:0] rx_snap, err_snap, ovf_snap, cmd_snap;
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
    //
    // Off the snapshot, never off the counters: see the header. Sixteen
    // reads of a live counter are sixteen instants, and the beats
    // between them are UART frames.
    function automatic logic [7:0] status_at(input logic [4:0] i);
        case (i)
            5'd0:  status_at = "R";
            5'd1:  status_at = "=";
            5'd2:  status_at = hex(rx_snap[15:12]);
            5'd3:  status_at = hex(rx_snap[11:8]);
            5'd4:  status_at = hex(rx_snap[7:4]);
            5'd5:  status_at = hex(rx_snap[3:0]);
            5'd6:  status_at = " ";
            5'd7:  status_at = "E";
            5'd8:  status_at = "=";
            5'd9:  status_at = hex(err_snap[15:12]);
            5'd10: status_at = hex(err_snap[11:8]);
            5'd11: status_at = hex(err_snap[7:4]);
            5'd12: status_at = hex(err_snap[3:0]);
            5'd13: status_at = " ";
            5'd14: status_at = "O";
            5'd15: status_at = "=";
            5'd16: status_at = hex(ovf_snap[15:12]);
            5'd17: status_at = hex(ovf_snap[11:8]);
            5'd18: status_at = hex(ovf_snap[7:4]);
            5'd19: status_at = hex(ovf_snap[3:0]);
            5'd20: status_at = " ";
            5'd21: status_at = "C";
            5'd22: status_at = "=";
            5'd23: status_at = hex(cmd_snap[15:12]);
            5'd24: status_at = hex(cmd_snap[11:8]);
            5'd25: status_at = hex(cmd_snap[7:4]);
            5'd26: status_at = hex(cmd_snap[3:0]);
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

    // What the input queue is still holding, from R = C + O + queued.
    // Only read on the accept of a `z`, and the terms are what that
    // cycle makes of the identity: the byte being popped right now is
    // accounted for, a byte arriving right now is not yet in rx_count
    // but is already on its way into the queue, and an overflow pulsing
    // right now belongs to a byte rx_count counted a cycle ago. Both
    // same-cycle terms exist because the clear overrides their bumps.
    //
    // 18 bits so the sum of two saturated counters cannot carry out of
    // it, and clamped at zero: past saturation C + O may exceed a
    // rx_count that stopped at 0xFFFF, and a borrow there would print a
    // queue of tens of thousands of bytes on a channel holding none.
    logic [17:0] delivered_now, accounted_now;
    logic [15:0] queued;
    always_comb delivered_now = 18'(rx_count) + 18'(rx_delivered);
    always_comb accounted_now = 18'(cmd_count) + 18'(ovf_count)
                              + 18'(rx_overflow) + 18'd1;
    always_comb queued = (delivered_now > accounted_now)
                       ? 16'(delivered_now - accounted_now) : 16'd0;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            rx_count   <= '0;
            err_count  <= '0;
            ovf_count  <= '0;
            cmd_count  <= '0;
            rx_snap    <= '0;
            err_snap   <= '0;
            ovf_snap   <= '0;
            cmd_snap   <= '0;
            command    <= 8'd0;
            resp_index <= '0;
            resp_len   <= '0;
            sending    <= 1'b0;
        end else begin
            if (rx_delivered) rx_count  <= bump(rx_count);
            if (frame_error)  err_count <= bump(err_count);
            if (rx_overflow)  ovf_count <= bump(ovf_count);

            if (!sending) begin
                if (rx_valid) begin
                    cmd_count  <= bump(cmd_count);
                    command    <= rx_data;
                    resp_index <= '0;
                    resp_len   <= resp_length(rx_data);
                    sending    <= 1'b1;
                    // The instant the status line reports: the counters
                    // as they stand at the end of this cycle, this
                    // cycle's events included, so all four fields come
                    // from one edge. C is written from the same bump as
                    // the counter, which is why the line counts the
                    // command that asked for it.
                    rx_snap  <= rx_delivered ? bump(rx_count)  : rx_count;
                    err_snap <= frame_error  ? bump(err_count) : err_count;
                    ovf_snap <= rx_overflow  ? bump(ovf_count) : ovf_count;
                    cmd_snap <= bump(cmd_count);
                    // Zeroing happens HERE, on the accept, and not at the
                    // end of the response. Ending it there left a window
                    // the length of the reply in which a frame_error or
                    // an overflow was counted by the branch above and
                    // then wiped by the clear, silently: the two are
                    // non-blocking assignments to one register in one
                    // always_ff and the later wins. Clearing on the
                    // accept means everything from the next cycle counts,
                    // and the only event that can be lost is one in the
                    // same cycle as the z itself, which is the cycle the
                    // operator asked to be the new zero.
                    //
                    // These override the bumps above by being later, so
                    // the z is not counted into what it just cleared.
                    //
                    // R goes to what the queue is still holding rather
                    // than to zero, because the bytes behind the z were
                    // counted into R before it ran and will count into
                    // C after: see the header.
                    if (rx_data == "z") begin
                        rx_count  <= queued;
                        err_count <= '0;
                        ovf_count <= '0;
                        cmd_count <= '0;
                    end
                end
            end else if (tx_ready) begin
                if (resp_index == resp_len - 5'd1) begin
                    sending <= 1'b0;
                end else begin
                    resp_index <= resp_index + 5'd1;
                end
            end
        end
    end

endmodule

`default_nettype wire
