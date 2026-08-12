// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * SLIP encoder, RFC 1055: oca_core's response stream in, a byte stream out.
 *
 * The other half of the console bridge. It walks the bytes of each beat
 * in the order the response was built -- byte 0 in tdata[7:0], the same
 * order oca_proto assembles its header in -- escapes the two framing
 * values, and emits END when the beat carrying tlast has been walked.
 *
 * TKEEP IS THE LENGTH, and nothing else is. oca_proto masks the final
 * beat's bytes past the response length to zero (oca_proto.sv:556-564)
 * because at 64 bits those bytes are AEAD engine output -- keystream over
 * the tail of a partial block -- and tkeep alone would leave them for a
 * downstream MAC to honour or not. They are therefore zero, but they are
 * zero rather than absent: a walk that stopped at the first zero byte
 * would agree with the mask today and disagree the first time a response
 * legitimately contains one. The count comes from popcount(tkeep) and
 * from no other source. That reading is exact rather than lucky because
 * the same lines build the keep as a contiguous right-justified run, so
 * popcount and "highest bit set plus one" are the same number here.
 *
 * WHY BOTH VALUES ARE ESCAPED, and why an unescaped one is worse than a
 * dropped byte. A 0xC0 inside a response ends the frame at that byte, and
 * the host reads a short reply that decodes cleanly and parses as a
 * header -- a truncation with nothing to mark it as one. 0xDB is the same
 * failure a step removed: it turns the byte after it into an escape and
 * shifts the rest of the reply.
 *
 * TX_PUSH ONLY WITH TX_READY, which is oca_console's rule and the same
 * reason: a push into a full FIFO is a byte deleted from the middle of a
 * reply, and the reply that arrives is a shorter valid-looking one.
 *
 * ONE END, AT THE END. RFC 1055 also has senders emit a leading END to
 * flush line noise, and this does not -- the frame the host must parse is
 * the same either way. If a later revision adds one it costs nothing on
 * the receiving side: oca_slip_rx discards an empty frame in silence
 * rather than counting it.
 *
 * A beat is accepted in one cycle and walked in the cycles after it, so
 * s_axis_tready is low while a beat is being emitted and there is one
 * idle cycle between beats. The byte sink is a UART at 115200 baud --
 * 2170 clocks a byte against at most two clocks a byte here -- so the
 * pace is set by tx_ready throughout and never by this handshake.
 *
 * A beat whose tkeep is zero emits nothing, and its END if it carries
 * tlast. oca_proto never produces one: its final beat carries 1..8 bytes
 * because the response is never shorter than its header.
 */
`default_nettype none

module oca_slip_tx (
    input  var logic        clk,
    input  var logic        rst_n,

    // One response per tlast.
    input  var logic [63:0] s_axis_tdata,
    input  var logic [ 7:0] s_axis_tkeep,
    input  var logic        s_axis_tvalid,
    output var logic        s_axis_tready,
    input  var logic        s_axis_tlast,

    // Byte stream out: oca_fifo's write port. tx_push is raised only
    // with tx_ready high, so no byte is ever offered to a full queue.
    output var logic [ 7:0] tx_data,
    output var logic        tx_push,
    input  var logic        tx_ready
);

    localparam logic [7:0] END     = 8'hC0;
    localparam logic [7:0] ESC     = 8'hDB;
    localparam logic [7:0] ESC_END = 8'hDC;
    localparam logic [7:0] ESC_ESC = 8'hDD;

    typedef enum logic [1:0] {
        T_IDLE,   // ready for a beat
        T_BYTE,   // emitting the byte at idx, or the ESC in front of it
        T_ESC,    // emitting the escaped form of the byte at idx
        T_END     // emitting the frame terminator
    } state_e;

    state_e      state;
    logic [63:0] word;
    logic [ 3:0] nbytes;    // popcount of the accepted beat's tkeep, 0..8
    logic [ 3:0] idx;       // byte being emitted, 0..nbytes-1
    logic        last;

    logic [3:0] keep_bytes;
    always_comb keep_bytes = 4'($countones(s_axis_tkeep));

    // idx is below nbytes whenever this is read, and nbytes is at most
    // eight, so three bits address the word and the select is always in
    // range.
    logic [7:0] cur_byte;
    logic       needs_esc;
    always_comb cur_byte  = word[{idx[2:0], 3'b000} +: 8];
    always_comb needs_esc = (cur_byte == END) || (cur_byte == ESC);

    // Off the state register alone: no path from tvalid to tready.
    always_comb s_axis_tready = (state == T_IDLE);

    always_comb begin
        case (state)
            T_BYTE:  tx_data = needs_esc ? ESC : cur_byte;
            T_ESC:   tx_data = (cur_byte == END) ? ESC_END : ESC_ESC;
            default: tx_data = END;
        endcase
    end

    always_comb tx_push = (state != T_IDLE) && tx_ready;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state  <= T_IDLE;
            word   <= '0;
            nbytes <= '0;
            idx    <= '0;
            last   <= 1'b0;
        end else begin
            case (state)
                T_IDLE: if (s_axis_tvalid) begin
                    word   <= s_axis_tdata;
                    nbytes <= keep_bytes;
                    last   <= s_axis_tlast;
                    idx    <= 4'd0;
                    if (keep_bytes != 4'd0)  state <= T_BYTE;
                    else if (s_axis_tlast)   state <= T_END;
                end

                T_BYTE: if (tx_ready) begin
                    if (needs_esc) begin
                        state <= T_ESC;
                    end else begin
                        idx <= idx + 4'd1;
                        if (idx + 4'd1 == nbytes)
                            state <= last ? T_END : T_IDLE;
                    end
                end

                T_ESC: if (tx_ready) begin
                    idx <= idx + 4'd1;
                    if (idx + 4'd1 == nbytes)
                        state <= last ? T_END : T_IDLE;
                    else
                        state <= T_BYTE;
                end

                default: if (tx_ready) state <= T_IDLE;
            endcase
        end
    end

endmodule

`default_nettype wire
