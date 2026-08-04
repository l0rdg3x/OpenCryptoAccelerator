// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * Protocol engine for the OCA host interface.
 *
 * Parses the fixed 8-byte header out of the receive buffer, runs the
 * command, and builds the response. Knows nothing about cryptography
 * beyond driving chacha20_poly1305 and comparing its tag.
 *
 * Store and forward: the whole request is buffered before the engine
 * sees any of it, and the whole response is built before a byte leaves.
 * That is what lets a failed tag return no plaintext at all.
 *
 * The stream is 64 bits wide with a byte-enable, because at 8 bits the
 * buffer needed 66 cycles to assemble a block the engine consumes in 40
 * and so could not feed even one engine (amendment of 2026-08-04 in
 * docs/design/2026-08-03-host-protocol.md). The 8-bit AXI-Stream that
 * verilog-ethernet hands over is unchanged; the width conversion belongs
 * at that boundary, not inside here.
 *
 * tkeep is decoded by a priority encoder, and a non-last beat carrying a
 * partial keep fails the packet closed with status 05. Only the final
 * write of a packet may be partial (oca_pktbuf): a partial write
 * mid-stream leaves the byte count off a word boundary and the next
 * write lands back on the word just issued, so the buffer would hold
 * something other than what arrived while every length check still
 * agreed. Nothing read out of it past that beat can be trusted,
 * including the magic, which is why the check precedes the header ones.
 *
 * Every fixed field is word-aligned, so the header is one read and the
 * arguments four. The single misaligned boundary in the protocol is the
 * one between the AAD and the message, whose distance apart is
 * aad_len bytes: a funnel shifter over two consecutive buffer words
 * carries it, with feed_addr kept as a byte address so that the word
 * address and the shift both fall out of it and the section logic needs
 * no knowledge of either.
 *
 * One command is processed to completion before the next is accepted, so
 * the engine is never busy when a command arrives. The request stream is
 * plain AXI-Stream for that: s_tready is high exactly while the engine
 * is in S_RX, the one state that stores what it accepts, so a beat is
 * never taken and discarded. A source with the next packet ready one
 * cycle after tlast simply waits.
 *
 * The four stats counters are: packets received (every packet that
 * reaches tlast), packets dropped for an invalid header (too short to
 * hold one, bad magic, bad version, unknown opcode — the first is the
 * only packet that gets no answer at all), commands completed with
 * status 00, and authentication failures. The snapshot is taken before
 * the stats command itself completes, so it never counts itself.
 *
 * A load-key command whose length is not exactly 40 bytes is refused
 * with status 05. Without that check the 32 key bytes would be read from
 * buffer positions the packet never wrote, which after a previous
 * command means loading a key an attacker chose the bytes of.
 *
 * The wire format is docs/design/2026-08-03-host-protocol.md.
 */
module oca_proto #(
    parameter int NUM_SLOTS = 8,
    parameter int BYTES     = 2048
) (
    input  logic         clk,
    input  logic         rst_n,
    // stream in
    input  logic [ 63:0] s_tdata,
    input  logic [  7:0] s_tkeep,
    input  logic         s_tvalid,
    output logic         s_tready,
    input  logic         s_tlast,
    // stream out
    output logic [ 63:0] m_tdata,
    output logic [  7:0] m_tkeep,
    output logic         m_tvalid,
    input  logic         m_tready,
    output logic         m_tlast,
    // receive buffer
    output logic         rx_wr_en,
    output logic [ 63:0] rx_wr_data,
    output logic [  3:0] rx_wr_bytes,
    output logic         rx_wr_clear,
    input  logic [ 11:0] rx_wr_count,
    input  logic         rx_wr_full,
    output logic [  8:0] rx_rd_addr,
    input  logic [ 63:0] rx_rd_data,
    // transmit buffer
    output logic         tx_wr_en,
    output logic [ 63:0] tx_wr_data,
    output logic [  3:0] tx_wr_bytes,
    output logic         tx_wr_clear,
    input  logic [ 11:0] tx_wr_count,
    output logic [  8:0] tx_rd_addr,
    input  logic [ 63:0] tx_rd_data,
    // key store
    output logic         ks_wr_en,
    output logic [  7:0] ks_wr_slot,
    output logic [255:0] ks_wr_key,
    output logic [  7:0] ks_rd_slot,
    input  logic [255:0] ks_rd_key,
    input  logic         ks_rd_valid,
    // AEAD engine
    output logic         eng_start,
    output logic         eng_dec,
    output logic [255:0] eng_key,
    output logic [ 95:0] eng_nonce,
    output logic         eng_in_valid,
    output logic         eng_in_aad,
    output logic         eng_in_last,
    output logic [  6:0] eng_in_len,
    output logic [511:0] eng_in_data,
    input  logic         eng_in_ready,
    input  logic         eng_out_valid,
    input  logic [511:0] eng_out_data,
    input  logic [  6:0] eng_out_len,
    input  logic         eng_done,
    input  logic [127:0] eng_tag,
    input  logic         eng_err
);

    localparam logic [15:0] MAGIC   = 16'h434F;   // "OC", byte 0 = 0x4F
    localparam logic [ 7:0] VERSION = 8'h01;
    localparam int HDR_LEN = 8;

    localparam logic [7:0] OP_LOAD_KEY = 8'h01;
    localparam logic [7:0] OP_SEAL     = 8'h02;
    localparam logic [7:0] OP_OPEN     = 8'h03;
    localparam logic [7:0] OP_STATS    = 8'h04;

    localparam logic [7:0] ST_OK          = 8'h00;
    localparam logic [7:0] ST_BAD_MAGIC   = 8'h01;
    localparam logic [7:0] ST_BAD_VERSION = 8'h02;
    localparam logic [7:0] ST_BAD_OPCODE  = 8'h03;
    localparam logic [7:0] ST_BAD_SLOT    = 8'h04;
    localparam logic [7:0] ST_BAD_LENGTH  = 8'h05;
    localparam logic [7:0] ST_AUTH_FAIL   = 8'h06;
    localparam logic [7:0] ST_ENGINE_ERR  = 8'h07;

    typedef enum logic [3:0] {
        S_RX,        // accept beats into the receive buffer
        S_PARSE,     // read the header word
        S_DISPATCH,  // decide, or fail with a status
        S_LOADKEY,   // write the slot
        S_ARGS,      // read nonce, lengths and tag, validate them
        S_FEED,      // read the next 64-byte block out of the buffer
        S_PRESENT,   // hand that block to the engine
        S_WAIT_OUT,  // wait for the block the engine gives back
        S_DRAIN,     // write out_data into the transmit buffer
        S_NEXTBLK,   // advance section and offset
        S_WAIT_DONE, // wait for the tag
        S_CHECK,     // compare the tag (open only)
        S_STATS,     // write the counters into the transmit buffer
        S_BUILD,     // size the response
        S_RESPOND    // stream the response out
    } fsm_t;
    fsm_t state;

    // Combinational, but only on the state register: nothing of the
    // source's own handshake reaches it, so there is no tvalid-to-tready
    // path and the accepting cycle is the state that stores the beat.
    always_comb s_tready = rst_n && (state == S_RX);

    // tkeep semantics are not documented upstream; verilog-axis's
    // axis_adapter produces a right-justified contiguous keep, and this
    // priority encoder reads the highest bit set. A keep that is neither
    // right-justified nor contiguous cannot be honoured by a buffer
    // written sequentially, so the only safe reading of a partial keep
    // before the last beat is that the packet is malformed.
    logic [3:0] keep_bytes;
    always_comb begin
        keep_bytes = 4'd0;
        for (int i = 0; i < 8; i++)
            if (s_tkeep[i]) keep_bytes = 4'(i) + 4'd1;
    end

    logic [ 7:0] opcode, slot, status;
    logic [15:0] req_id;
    logic [11:0] rx_len;          // bytes of request received
    logic        rx_keep_bad;     // a non-last beat carried a partial keep
    logic [31:0] cnt_rx, cnt_drop, cnt_done, cnt_auth_fail;

    // Request bytes 8..39, byte 8 in [7:0], filled a word at a time. The
    // header word arrives through the same register and is read out of
    // the top of it, then leaves as the four argument words shift in, so
    // it needs no register of its own. Only bytes 0..6 are aliased: byte
    // 7 is the status field, which a request does not carry.
    logic [255:0] args;
    logic [ 55:0] hdr;
    always_comb hdr = args[247:192];

    logic [511:0] blk;            // block being assembled for the engine
    logic [511:0] outbuf;         // block coming back, drained a word at a time
    logic [  6:0] out_left;
    logic [ 11:0] feed_addr;      // byte offset of the block being read
    logic [  2:0] feed_shift;     // feed_addr within its word
    logic [ 63:0] feed_prev;      // the word before the one on rx_rd_data
    logic [ 15:0] sec_left;       // bytes left in the section being fed
    logic         sec_aad;        // that section is the AAD
    logic [  6:0] blk_len;
    logic         blk_last;
    logic [127:0] resp_tag;
    logic         eng_done_seen;

    // Response pipeline, three stages deep: stage 1 walks the word index
    // and says where that word comes from, stage 2 is the transmit
    // buffer's registered read with the metadata that travels beside it,
    // stage 3 is the output register.
    logic [11:0] resp_body_len, resp_left;
    logic [ 8:0] resp_widx, body_start_w;
    logic [ 1:0] resp_sel;
    logic        resp_v;
    logic [ 3:0] beat_bytes;
    logic [ 1:0] beat_sel;
    logic        beat_last, beat_v;

    // Shared sequential reader over the receive buffer. The buffer's read
    // port is registered, so a word lands two edges after its address is
    // driven; issuing one address per cycle and consuming one word per
    // cycle behind a two-deep valid pipeline keeps that at one word per
    // cycle instead of the three a per-word handshake would cost.
    logic [8:0] rd_ptr;
    logic [3:0] rd_left, rd_got;
    logic       rd_v, rd_v_d;

    // Request fields, sliced out of the two shift registers above.
    logic [15:0] aad_len, msg_len;
    logic [95:0] nonce;
    always_comb nonce   = args[ 95:  0];   // bytes 8..19
    always_comb aad_len = args[111: 96];   // bytes 20..21
    always_comb msg_len = args[127:112];   // bytes 22..23

    logic [17:0] want_len;
    always_comb want_len = 18'(HDR_LEN) + 18'd16
                           + ((opcode == OP_OPEN) ? 18'd16 : 18'd0)
                           + {2'd0, aad_len} + {2'd0, msg_len};

    logic len_bad;
    always_comb len_bad = rx_wr_full || (want_len > 18'(BYTES))
                          || (want_len != {6'd0, rx_len});

    // Offset of the first payload byte: the open command carries the
    // received tag between the lengths and the data.
    logic [11:0] data_off;
    always_comb data_off = (opcode == OP_OPEN) ? 12'(HDR_LEN + 32)
                                               : 12'(HDR_LEN + 16);

    // Block sequencing. The AAD is fed first and the message follows it
    // in the buffer, so one offset walks both sections; only the section
    // flag and the remaining count have to change at the boundary.
    logic        first_aad, first_last;
    logic [15:0] first_left;
    logic [ 6:0] first_len;
    always_comb begin
        first_aad  = (aad_len != 16'd0);
        first_left = first_aad ? aad_len : msg_len;
        first_len  = (first_left >= 16'd64) ? 7'd64 : first_left[6:0];
        first_last = first_aad ? ((first_left <= 16'd64) && (msg_len == 16'd0))
                               : (first_left <= 16'd64);
    end

    logic        nx_aad, nx_blk_last;
    logic [15:0] nx_left;
    logic [ 6:0] nx_blk_len;
    always_comb begin
        if (sec_aad && (sec_left <= 16'd64)) begin
            nx_aad  = 1'b0;
            nx_left = msg_len;
        end else begin
            nx_aad  = sec_aad;
            nx_left = sec_left - {9'd0, blk_len};
        end
        nx_blk_len  = (nx_left >= 16'd64) ? 7'd64 : nx_left[6:0];
        nx_blk_last = nx_aad ? ((nx_left <= 16'd64) && (msg_len == 16'd0))
                             : (nx_left <= 16'd64);
    end

    // A block produces ciphertext only when it carries message bytes: an
    // AAD block and the empty-message marker both come back silently.
    logic blk_is_msg;
    always_comb blk_is_msg = !sec_aad && (blk_len != 7'd0);

    // Where the next block starts, as a byte offset. Only the last block
    // of the AAD advances it by something other than a whole number of
    // words, which is the one boundary the funnel below exists for.
    logic [11:0] nx_feed;
    always_comb nx_feed = feed_addr + {5'd0, blk_len};

    // The funnel: one word of payload out of the two the block spans,
    // written as a case over the registered shift rather than a variable
    // shift, because a barrel shifter built out of comparators on
    // feed_addr costs six times the LUTs of this at 64 bits.
    logic [63:0] feed_word;
    always_comb begin
        case (feed_shift)
            3'd0: feed_word = feed_prev;
            3'd1: feed_word = {rx_rd_data[ 7:0], feed_prev[63: 8]};
            3'd2: feed_word = {rx_rd_data[15:0], feed_prev[63:16]};
            3'd3: feed_word = {rx_rd_data[23:0], feed_prev[63:24]};
            3'd4: feed_word = {rx_rd_data[31:0], feed_prev[63:32]};
            3'd5: feed_word = {rx_rd_data[39:0], feed_prev[63:40]};
            3'd6: feed_word = {rx_rd_data[47:0], feed_prev[63:48]};
            default: feed_word = {rx_rd_data[55:0], feed_prev[63:56]};
        endcase
    end

    // Draining a block into the transmit buffer. Only the last word of
    // the last block of the message can be partial, which is the
    // invariant oca_pktbuf requires of its writer.
    logic [3:0] out_bytes;
    logic [6:0] nx_out_left;
    always_comb out_bytes   = (out_left >= 7'd8) ? 4'd8 : {1'b0, out_left[2:0]};
    always_comb nx_out_left = (out_left >= 7'd8) ? (out_left - 7'd8) : 7'd0;

    // The security property of the whole design. One 128-bit equality
    // between the tag the engine computed and the sixteen bytes the host
    // sent: fixed-width combinational logic, so it is constant-time by
    // construction, and both outcomes below cost the same single cycle.
    // Never a byte loop, and never a comparison delegated to the host
    // (docs/design/2026-08-03-host-protocol.md section 5).
    logic [127:0] rx_tag;
    always_comb rx_tag = args[255:128];   // bytes 24..39
    logic tag_match;
    always_comb tag_match = (resp_tag == rx_tag);

    // Response word source: the header, the two tag words of a
    // successful seal, then the transmit buffer. Selected by a
    // registered two-bit code rather than by comparing the word index
    // here — the same multiplexer written as an if / else chain over
    // comparators costs 771 LUT4 at 64 bits against 129 for this,
    // measured 2026-08-04. The tag is indexed rather than shifted out
    // because a shift register would have to be advanced by the same
    // enable as the stage reading it, and a second thing to freeze is
    // what the pipeline below exists to avoid.
    logic [63:0] resp_hdr, resp_word;
    always_comb resp_hdr = {status, slot, req_id, opcode, VERSION, MAGIC};
    always_comb begin
        case (beat_sel)
            2'd0:    resp_word = resp_hdr;
            2'd1:    resp_word = resp_tag[ 63: 0];
            2'd2:    resp_word = resp_tag[127:64];
            default: resp_word = tx_rd_data;
        endcase
    end

    // The last beat carries up to seven bytes of engine output past the
    // response length. At 8 bits they could not physically be emitted;
    // at 64 they can, and tkeep alone would leave them on the wire for
    // the downstream MAC to honour or not. Mask them here.
    logic [ 7:0] beat_keep;
    logic [63:0] beat_mask;
    always_comb begin
        for (int i = 0; i < 8; i++) begin
            beat_keep[i]        = (4'(i) < beat_bytes);
            beat_mask[8*i +: 8] = {8{4'(i) < beat_bytes}};
        end
    end

    // The response body starts at a whole word in both directions — the
    // header is one word and the tag two — so the transmit buffer is
    // read straight, with no funnel on this side. Below body_start_w the
    // word index is its own source code: word 0 is the header, words 1
    // and 2 are the tag where a successful seal carries one.
    logic [ 8:0] nxt_widx;
    logic [ 1:0] nx_sel;
    logic        resp_last;
    logic [ 3:0] resp_bytes;
    logic [11:0] nx_resp_left;
    always_comb nxt_widx     = resp_widx + 9'd1;
    always_comb nx_sel       = (nxt_widx >= body_start_w) ? 2'd3
                                                          : nxt_widx[1:0];
    always_comb resp_last    = (resp_left <= 12'd8);
    always_comb resp_bytes   = resp_last ? resp_left[3:0] : 4'd8;
    always_comb nx_resp_left = resp_last ? 12'd0 : (resp_left - 12'd8);

    // One enable freezes all three stages together, which is what lets a
    // sink lowering tready cost exactly one cycle with no skid buffer
    // behind the output register.
    logic go;
    always_comb go = !m_tvalid || m_tready;

    // The word address the transmit buffer is shown this cycle. Its read
    // is unconditional — one word lands on tx_rd_data every edge,
    // whatever the pipeline is doing — so freezing the stages is not
    // enough on its own: with the address left at the word stage 1 is
    // fetching, the edge ending a stalled cycle would overwrite the word
    // stage 2 still owes the output register. Showing it the stage-2
    // word instead makes that edge re-read what is already there, and is
    // the one place go has to reach combinationally. resp_widx counts
    // stage 1, so stage 2 is the word below it.
    always_comb tx_rd_addr = (go ? resp_widx : (resp_widx - 9'd1))
                             - body_start_w;


    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state         <= S_RX;
            m_tdata       <= 64'd0;
            m_tkeep       <= 8'd0;
            m_tvalid      <= 1'b0;
            m_tlast       <= 1'b0;
            rx_wr_en      <= 1'b0;
            rx_wr_data    <= 64'd0;
            rx_wr_bytes   <= 4'd0;
            rx_wr_clear   <= 1'b0;
            rx_rd_addr    <= 9'd0;
            tx_wr_en      <= 1'b0;
            tx_wr_data    <= 64'd0;
            tx_wr_bytes   <= 4'd0;
            tx_wr_clear   <= 1'b0;
            ks_wr_en      <= 1'b0;
            ks_wr_slot    <= 8'd0;
            ks_wr_key     <= 256'd0;
            ks_rd_slot    <= 8'd0;
            eng_start     <= 1'b0;
            eng_dec       <= 1'b0;
            eng_key       <= 256'd0;
            eng_nonce     <= 96'd0;
            eng_in_valid  <= 1'b0;
            eng_in_aad    <= 1'b0;
            eng_in_last   <= 1'b0;
            eng_in_len    <= 7'd0;
            eng_in_data   <= 512'd0;
            opcode        <= 8'd0;
            slot          <= 8'd0;
            status        <= ST_OK;
            req_id        <= 16'd0;
            rx_len        <= 12'd0;
            rx_keep_bad   <= 1'b0;
            cnt_rx        <= 32'd0;
            cnt_drop      <= 32'd0;
            cnt_done      <= 32'd0;
            cnt_auth_fail <= 32'd0;
            args          <= 256'd0;
            blk           <= 512'd0;
            outbuf        <= 512'd0;
            out_left      <= 7'd0;
            feed_addr     <= 12'd0;
            feed_shift    <= 3'd0;
            feed_prev     <= 64'd0;
            sec_left      <= 16'd0;
            sec_aad       <= 1'b0;
            blk_len       <= 7'd0;
            blk_last      <= 1'b0;
            resp_tag      <= 128'd0;
            eng_done_seen <= 1'b0;
            resp_left     <= 12'd0;
            resp_widx     <= 9'd0;
            resp_sel      <= 2'd0;
            resp_v        <= 1'b0;
            beat_bytes    <= 4'd0;
            beat_sel      <= 2'd0;
            beat_last     <= 1'b0;
            beat_v        <= 1'b0;
            resp_body_len <= 12'd0;
            body_start_w  <= 9'd0;
            rd_ptr        <= 9'd0;
            rd_left       <= 4'd0;
            rd_got        <= 4'd0;
            rd_v          <= 1'b0;
            rd_v_d        <= 1'b0;
        end else begin
            rx_wr_en     <= 1'b0;
            rx_wr_clear  <= 1'b0;
            tx_wr_en     <= 1'b0;
            tx_wr_clear  <= 1'b0;
            ks_wr_en     <= 1'b0;
            eng_start    <= 1'b0;
            eng_in_valid <= 1'b0;

            // shared sequential reader
            if (rd_left != 4'd0) begin
                rx_rd_addr <= rd_ptr;
                rd_ptr     <= rd_ptr  + 9'd1;
                rd_left    <= rd_left - 4'd1;
                rd_v       <= 1'b1;
            end else begin
                rd_v <= 1'b0;
            end
            rd_v_d <= rd_v;
            if (rd_v_d) rd_got <= rd_got + 4'd1;

            // `done` is one cycle wide and the MAC of the last block
            // finishes while its ciphertext is still being drained into
            // the transmit buffer, so it is caught here rather than in
            // S_WAIT_DONE: for a full final block the pulse lands 60-odd
            // cycles before that state is reached. `out_valid` needs no
            // such latch — a block cannot come back before S_WAIT_OUT,
            // which is entered the cycle after the block is handed over,
            // and ChaCha20 alone takes 22.
            if (eng_done) begin
                eng_done_seen <= 1'b1;
                resp_tag      <= eng_tag;
            end

            case (state)
                S_RX: begin
                    if (s_tvalid && s_tready) begin
                        rx_wr_en    <= 1'b1;
                        rx_wr_data  <= s_tdata;
                        rx_wr_bytes <= keep_bytes;
                        if (!s_tlast && (s_tkeep != 8'hFF))
                            rx_keep_bad <= 1'b1;
                        if (s_tlast) begin
                            cnt_rx  <= cnt_rx + 32'd1;
                            rd_ptr  <= 9'd0;
                            rd_left <= 4'd1;
                            rd_got  <= 4'd0;
                            state   <= S_PARSE;
                        end
                    end
                end

                S_PARSE: begin
                    if (rd_v_d) args <= {rx_rd_data, args[255:64]};
                    if (rd_got == 4'd1) state <= S_DISPATCH;
                end

                S_DISPATCH: begin
                    opcode        <= hdr[31:24];
                    req_id        <= hdr[47:32];
                    slot          <= hdr[55:48];
                    ks_rd_slot    <= hdr[55:48];
                    rx_len        <= rx_wr_count;
                    resp_body_len <= 12'd0;
                    rx_keep_bad   <= 1'b0;
                    if (rx_wr_count < 12'(HDR_LEN)) begin
                        // not even a header: the only packet that gets no
                        // answer, because there is nothing to answer to
                        cnt_drop    <= cnt_drop + 32'd1;
                        rx_wr_clear <= 1'b1;
                        state       <= S_RX;
                    end else if (rx_keep_bad) begin
                        // the write pointer is not where the byte count
                        // says it is, so the magic below would be read
                        // out of a word the packet never filled
                        status <= ST_BAD_LENGTH;
                        state  <= S_BUILD;
                    end else if (hdr[15:0] != MAGIC) begin
                        status   <= ST_BAD_MAGIC;
                        cnt_drop <= cnt_drop + 32'd1;
                        state    <= S_BUILD;
                    end else if (hdr[23:16] != VERSION) begin
                        status   <= ST_BAD_VERSION;
                        cnt_drop <= cnt_drop + 32'd1;
                        state    <= S_BUILD;
                    end else if (hdr[31:24] != OP_LOAD_KEY
                              && hdr[31:24] != OP_SEAL
                              && hdr[31:24] != OP_OPEN
                              && hdr[31:24] != OP_STATS) begin
                        status   <= ST_BAD_OPCODE;
                        cnt_drop <= cnt_drop + 32'd1;
                        state    <= S_BUILD;
                    end else begin
                        rd_ptr  <= 9'(HDR_LEN / 8);
                        rd_left <= 4'd4;
                        rd_got  <= 4'd0;
                        state   <= S_ARGS;
                    end
                end

                S_ARGS: begin
                    if (rd_v_d) args <= {rx_rd_data, args[255:64]};
                    if (rd_got == 4'd4) begin
                        if (opcode == OP_LOAD_KEY) begin
                            if (slot >= 8'(NUM_SLOTS)) begin
                                status <= ST_BAD_SLOT;
                                state  <= S_BUILD;
                            end else if (rx_len != 12'(HDR_LEN + 32)) begin
                                status <= ST_BAD_LENGTH;
                                state  <= S_BUILD;
                            end else begin
                                state <= S_LOADKEY;
                            end
                        end else if (opcode == OP_STATS) begin
                            // packets received, packets dropped for an
                            // invalid header, commands completed,
                            // authentication failures — little-endian,
                            // in that order
                            outbuf   <= {384'd0, cnt_auth_fail, cnt_done,
                                         cnt_drop, cnt_rx};
                            out_left <= 7'd16;
                            state    <= S_STATS;
                        end else if (!ks_rd_valid) begin
                            status <= ST_BAD_SLOT;
                            state  <= S_BUILD;
                        end else if (len_bad) begin
                            status <= ST_BAD_LENGTH;
                            state  <= S_BUILD;
                        end else begin
                            eng_start     <= 1'b1;
                            eng_dec       <= (opcode == OP_OPEN);
                            eng_key       <= ks_rd_key;
                            eng_nonce     <= nonce;
                            eng_done_seen <= 1'b0;
                            sec_aad       <= first_aad;
                            sec_left      <= first_left;
                            blk_len       <= first_len;
                            blk_last      <= first_last;
                            feed_addr     <= data_off;
                            feed_shift    <= data_off[2:0];
                            rd_ptr        <= data_off[11:3];
                            rd_left       <= 4'd9;
                            rd_got        <= 4'd0;
                            state         <= S_FEED;
                        end
                    end
                end

                // A block is always read out whole, whatever blk_len
                // says. The engine masks everything past in_len on its
                // way into Poly1305, so the trailing bytes are free, and
                // a fixed 64-byte shift keeps every byte at a constant
                // position instead of one that depends on the length.
                //
                // Nine reads for eight words: the first only primes the
                // funnel. Keeping that uniform costs one cycle a block
                // and spares the control a special case at shift zero,
                // where the ninth word is read and discarded.
                S_FEED: begin
                    if (rd_v_d) begin
                        feed_prev <= rx_rd_data;
                        if (rd_got != 4'd0) blk <= {feed_word, blk[511:64]};
                    end
                    if (rd_got == 4'd9) state <= S_PRESENT;
                end

                S_PRESENT: begin
                    if (eng_err) begin
                        status        <= ST_ENGINE_ERR;
                        resp_body_len <= 12'd0;
                        tx_wr_clear   <= 1'b1;
                        state         <= S_BUILD;
                    end else if (eng_in_ready) begin
                        eng_in_valid <= 1'b1;
                        eng_in_aad   <= sec_aad;
                        eng_in_last  <= blk_last;
                        eng_in_len   <= blk_len;
                        eng_in_data  <= blk;
                        state        <= S_WAIT_OUT;
                    end
                end

                S_WAIT_OUT: begin
                    if (eng_err) begin
                        status        <= ST_ENGINE_ERR;
                        resp_body_len <= 12'd0;
                        tx_wr_clear   <= 1'b1;
                        state         <= S_BUILD;
                    end else if (!blk_is_msg) begin
                        state <= S_NEXTBLK;
                    end else if (eng_out_valid) begin
                        outbuf   <= eng_out_data;
                        out_left <= eng_out_len;
                        state    <= S_DRAIN;
                    end
                end

                S_DRAIN: begin
                    if (out_left != 7'd0) begin
                        tx_wr_en    <= 1'b1;
                        tx_wr_data  <= outbuf[63:0];
                        tx_wr_bytes <= out_bytes;
                        outbuf      <= {64'd0, outbuf[511:64]};
                        out_left    <= nx_out_left;
                    end else begin
                        state <= S_NEXTBLK;
                    end
                end

                S_NEXTBLK: begin
                    if (blk_last) begin
                        state <= S_WAIT_DONE;
                    end else begin
                        sec_aad    <= nx_aad;
                        sec_left   <= nx_left;
                        blk_len    <= nx_blk_len;
                        blk_last   <= nx_blk_last;
                        feed_addr  <= nx_feed;
                        feed_shift <= nx_feed[2:0];
                        rd_ptr     <= nx_feed[11:3];
                        rd_left    <= 4'd9;
                        rd_got     <= 4'd0;
                        state      <= S_FEED;
                    end
                end

                S_WAIT_DONE: begin
                    if (eng_err) begin
                        status        <= ST_ENGINE_ERR;
                        resp_body_len <= 12'd0;
                        tx_wr_clear   <= 1'b1;
                        state         <= S_BUILD;
                    end else if (eng_done_seen) begin
                        if (opcode == OP_OPEN) begin
                            state <= S_CHECK;
                        end else begin
                            status        <= ST_OK;
                            resp_body_len <= tx_wr_count;
                            state         <= S_BUILD;
                        end
                    end
                end

                // Store and forward is what makes this possible: the
                // plaintext is already whole in the transmit buffer, so a
                // failed tag simply never addresses it.
                S_CHECK: begin
                    if (tag_match) begin
                        status        <= ST_OK;
                        resp_body_len <= tx_wr_count;
                    end else begin
                        status        <= ST_AUTH_FAIL;
                        resp_body_len <= 12'd0;
                        tx_wr_clear   <= 1'b1;
                        cnt_auth_fail <= cnt_auth_fail + 32'd1;
                    end
                    state <= S_BUILD;
                end

                S_STATS: begin
                    if (out_left != 7'd0) begin
                        tx_wr_en    <= 1'b1;
                        tx_wr_data  <= outbuf[63:0];
                        tx_wr_bytes <= out_bytes;
                        outbuf      <= {64'd0, outbuf[511:64]};
                        out_left    <= nx_out_left;
                    end else begin
                        status        <= ST_OK;
                        resp_body_len <= 12'd16;
                        state         <= S_BUILD;
                    end
                end

                S_LOADKEY: begin
                    ks_wr_en   <= 1'b1;
                    ks_wr_slot <= slot;
                    ks_wr_key  <= args;
                    status     <= ST_OK;
                    state      <= S_BUILD;
                end

                // The tag precedes the payload so the host finds it at a
                // fixed offset without first computing lengths.
                S_BUILD: begin
                    resp_widx <= 9'd0;
                    resp_sel  <= 2'd0;
                    resp_v    <= 1'b1;
                    beat_v    <= 1'b0;
                    if ((opcode == OP_SEAL) && (status == ST_OK)) begin
                        body_start_w <= 9'((HDR_LEN + 16) / 8);
                        resp_left    <= 12'(HDR_LEN + 16) + resp_body_len;
                    end else begin
                        body_start_w <= 9'(HDR_LEN / 8);
                        resp_left    <= 12'(HDR_LEN) + resp_body_len;
                    end
                    if (status == ST_OK) cnt_done <= cnt_done + 32'd1;
                    state <= S_RESPOND;
                end

                // One beat per cycle: the three stages step together
                // under go, so the two cycles the buffer's registered
                // read costs are paid once at the head of the response
                // and never again. resp_widx keeps counting after the
                // last word has been issued because the stalled address
                // above is derived from it.
                S_RESPOND: begin
                    if (go) begin
                        if (m_tvalid && m_tlast) begin
                            m_tvalid    <= 1'b0;
                            m_tlast     <= 1'b0;
                            rx_wr_clear <= 1'b1;
                            tx_wr_clear <= 1'b1;
                            state       <= S_RX;
                        end else begin
                            m_tdata  <= resp_word & beat_mask;
                            m_tkeep  <= beat_keep;
                            m_tvalid <= beat_v;
                            m_tlast  <= beat_v && beat_last;

                            beat_v     <= resp_v;
                            beat_sel   <= resp_sel;
                            beat_bytes <= resp_bytes;
                            beat_last  <= resp_last;

                            resp_widx <= nxt_widx;
                            if (resp_v) begin
                                resp_sel  <= nx_sel;
                                resp_left <= nx_resp_left;
                                resp_v    <= !resp_last;
                            end
                        end
                    end
                end

                default: state <= S_RX;
            endcase
        end
    end

endmodule
