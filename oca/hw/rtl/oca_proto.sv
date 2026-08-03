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
 * One command is processed to completion before the next is accepted, so
 * the engine is never busy when a command arrives. The request stream is
 * plain AXI-Stream for that: s_tready is high exactly while the engine
 * is in S_RX, the one state that stores what it accepts, so a byte is
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
    input  logic [  7:0] s_tdata,
    input  logic         s_tvalid,
    output logic         s_tready,
    input  logic         s_tlast,
    // stream out
    output logic [  7:0] m_tdata,
    output logic         m_tvalid,
    input  logic         m_tready,
    output logic         m_tlast,
    // receive buffer
    output logic         rx_wr_en,
    output logic [  7:0] rx_wr_data,
    output logic         rx_wr_clear,
    input  logic [ 11:0] rx_wr_count,
    input  logic         rx_wr_full,
    output logic [ 11:0] rx_rd_addr,
    input  logic [  7:0] rx_rd_data,
    // transmit buffer
    output logic         tx_wr_en,
    output logic [  7:0] tx_wr_data,
    output logic         tx_wr_clear,
    input  logic [ 11:0] tx_wr_count,
    output logic [ 11:0] tx_rd_addr,
    input  logic [  7:0] tx_rd_data,
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
        S_RX,        // accept bytes into the receive buffer
        S_PARSE,     // read header bytes 0..7
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
        S_RESP_FETCH,// one cycle for the registered buffer read
        S_RESPOND    // stream the response out
    } fsm_t;
    fsm_t state;

    // Combinational, but only on the state register: nothing of the
    // source's own handshake reaches it, so there is no tvalid-to-tready
    // path and the accepting cycle is the state that stores the byte.
    always_comb s_tready = rst_n && (state == S_RX);

    logic [ 7:0] opcode, slot, status;
    logic [15:0] req_id;
    logic [11:0] rx_len;          // bytes of request received
    logic [31:0] cnt_rx, cnt_drop, cnt_done, cnt_auth_fail;

    logic [ 63:0] hdr;            // header bytes 0..7, byte 0 in [7:0]
    logic [255:0] args;           // request bytes 8..39, byte 8 in [7:0]

    logic [511:0] blk;            // block being assembled for the engine
    logic [511:0] outbuf;         // block coming back, drained byte by byte
    logic [  6:0] out_left;
    logic [ 11:0] feed_addr;      // offset of the block being read
    logic [ 15:0] sec_left;       // bytes left in the section being fed
    logic         sec_aad;        // that section is the AAD
    logic [  6:0] blk_len;
    logic         blk_last;
    logic [127:0] resp_tag;
    logic         eng_done_seen;

    logic [11:0] resp_len, resp_idx, resp_body_len, body_start;

    // Shared sequential reader over the receive buffer. The buffer's read
    // port is registered, so a byte lands two edges after its address is
    // driven; issuing one address per cycle and consuming one byte per
    // cycle behind a two-deep valid pipeline keeps that at one byte per
    // cycle instead of the three a per-byte handshake would cost.
    logic [11:0] rd_ptr;
    logic [ 6:0] rd_left, rd_got;
    logic        rd_v, rd_v_d;

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

    // Response byte source: header from registers, then the tag for a
    // successful seal, then the transmit buffer. The tag is shifted out
    // rather than indexed so no byte select depends on resp_idx.
    logic [7:0] resp_byte;
    always_comb begin
        if (resp_idx < 12'(HDR_LEN)) begin
            case (resp_idx[2:0])
                3'd0: resp_byte = MAGIC[ 7:0];
                3'd1: resp_byte = MAGIC[15:8];
                3'd2: resp_byte = VERSION;
                3'd3: resp_byte = opcode;
                3'd4: resp_byte = req_id[ 7:0];
                3'd5: resp_byte = req_id[15:8];
                3'd6: resp_byte = slot;
                default: resp_byte = status;
            endcase
        end else if (resp_idx < body_start) begin
            resp_byte = resp_tag[7:0];
        end else begin
            resp_byte = tx_rd_data;
        end
    end

    logic [11:0] nxt_idx, nxt_body;
    always_comb nxt_idx  = resp_idx + 12'd1;
    always_comb nxt_body = (nxt_idx >= body_start) ? (nxt_idx - body_start)
                                                   : 12'd0;


    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state         <= S_RX;
            m_tdata       <= 8'd0;
            m_tvalid      <= 1'b0;
            m_tlast       <= 1'b0;
            rx_wr_en      <= 1'b0;
            rx_wr_data    <= 8'd0;
            rx_wr_clear   <= 1'b0;
            rx_rd_addr    <= 12'd0;
            tx_wr_en      <= 1'b0;
            tx_wr_data    <= 8'd0;
            tx_wr_clear   <= 1'b0;
            tx_rd_addr    <= 12'd0;
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
            cnt_rx        <= 32'd0;
            cnt_drop      <= 32'd0;
            cnt_done      <= 32'd0;
            cnt_auth_fail <= 32'd0;
            hdr           <= 64'd0;
            args          <= 256'd0;
            blk           <= 512'd0;
            outbuf        <= 512'd0;
            out_left      <= 7'd0;
            feed_addr     <= 12'd0;
            sec_left      <= 16'd0;
            sec_aad       <= 1'b0;
            blk_len       <= 7'd0;
            blk_last      <= 1'b0;
            resp_tag      <= 128'd0;
            eng_done_seen <= 1'b0;
            resp_len      <= 12'd0;
            resp_idx      <= 12'd0;
            resp_body_len <= 12'd0;
            body_start    <= 12'd0;
            rd_ptr        <= 12'd0;
            rd_left       <= 7'd0;
            rd_got        <= 7'd0;
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
            if (rd_left != 7'd0) begin
                rx_rd_addr <= rd_ptr;
                rd_ptr     <= rd_ptr  + 12'd1;
                rd_left    <= rd_left - 7'd1;
                rd_v       <= 1'b1;
            end else begin
                rd_v <= 1'b0;
            end
            rd_v_d <= rd_v;
            if (rd_v_d) rd_got <= rd_got + 7'd1;

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
                        rx_wr_en   <= 1'b1;
                        rx_wr_data <= s_tdata;
                        if (s_tlast) begin
                            cnt_rx  <= cnt_rx + 32'd1;
                            rd_ptr  <= 12'd0;
                            rd_left <= 7'(HDR_LEN);
                            rd_got  <= 7'd0;
                            state   <= S_PARSE;
                        end
                    end
                end

                S_PARSE: begin
                    if (rd_v_d) hdr <= {rx_rd_data, hdr[63:8]};
                    if (rd_got == 7'(HDR_LEN)) state <= S_DISPATCH;
                end

                S_DISPATCH: begin
                    opcode        <= hdr[31:24];
                    req_id        <= hdr[47:32];
                    slot          <= hdr[55:48];
                    ks_rd_slot    <= hdr[55:48];
                    rx_len        <= rx_wr_count;
                    resp_body_len <= 12'd0;
                    if (rx_wr_count < 12'(HDR_LEN)) begin
                        // not even a header: the only packet that gets no
                        // answer, because there is nothing to answer to
                        cnt_drop    <= cnt_drop + 32'd1;
                        rx_wr_clear <= 1'b1;
                        state       <= S_RX;
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
                        rd_ptr  <= 12'(HDR_LEN);
                        rd_left <= 7'd32;
                        rd_got  <= 7'd0;
                        state   <= S_ARGS;
                    end
                end

                S_ARGS: begin
                    if (rd_v_d) args <= {rx_rd_data, args[255:8]};
                    if (rd_got == 7'd32) begin
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
                            rd_ptr        <= data_off;
                            rd_left       <= 7'd64;
                            rd_got        <= 7'd0;
                            state         <= S_FEED;
                        end
                    end
                end

                // A block is always read out whole, whatever blk_len
                // says. The engine masks everything past in_len on its
                // way into Poly1305, so the trailing bytes are free, and
                // a fixed 64-byte shift keeps every byte at a constant
                // position instead of one that depends on the length.
                S_FEED: begin
                    if (rd_v_d) blk <= {rx_rd_data, blk[511:8]};
                    if (rd_got == 7'd64) state <= S_PRESENT;
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
                        tx_wr_en   <= 1'b1;
                        tx_wr_data <= outbuf[7:0];
                        outbuf     <= {8'd0, outbuf[511:8]};
                        out_left   <= out_left - 7'd1;
                    end else begin
                        state <= S_NEXTBLK;
                    end
                end

                S_NEXTBLK: begin
                    if (blk_last) begin
                        state <= S_WAIT_DONE;
                    end else begin
                        sec_aad   <= nx_aad;
                        sec_left  <= nx_left;
                        blk_len   <= nx_blk_len;
                        blk_last  <= nx_blk_last;
                        feed_addr <= feed_addr + {5'd0, blk_len};
                        rd_ptr    <= feed_addr + {5'd0, blk_len};
                        rd_left   <= 7'd64;
                        rd_got    <= 7'd0;
                        state     <= S_FEED;
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
                        tx_wr_en   <= 1'b1;
                        tx_wr_data <= outbuf[7:0];
                        outbuf     <= {8'd0, outbuf[511:8]};
                        out_left   <= out_left - 7'd1;
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
                    resp_idx   <= 12'd0;
                    tx_rd_addr <= 12'd0;
                    if ((opcode == OP_SEAL) && (status == ST_OK)) begin
                        body_start <= 12'(HDR_LEN + 16);
                        resp_len   <= 12'(HDR_LEN + 16) + resp_body_len;
                    end else begin
                        body_start <= 12'(HDR_LEN);
                        resp_len   <= 12'(HDR_LEN) + resp_body_len;
                    end
                    if (status == ST_OK) cnt_done <= cnt_done + 32'd1;
                    state <= S_RESP_FETCH;
                end

                S_RESP_FETCH: state <= S_RESPOND;

                S_RESPOND: begin
                    if (!m_tvalid) begin
                        m_tdata  <= resp_byte;
                        m_tvalid <= 1'b1;
                        m_tlast  <= (resp_idx == resp_len - 12'd1);
                    end else if (m_tready) begin
                        m_tvalid <= 1'b0;
                        m_tlast  <= 1'b0;
                        if (resp_idx == resp_len - 12'd1) begin
                            rx_wr_clear <= 1'b1;
                            tx_wr_clear <= 1'b1;
                            state       <= S_RX;
                        end else begin
                            if ((resp_idx >= 12'(HDR_LEN))
                                && (resp_idx < body_start))
                                resp_tag <= {8'd0, resp_tag[127:8]};
                            resp_idx   <= nxt_idx;
                            tx_rd_addr <= nxt_body;
                            state      <= S_RESP_FETCH;
                        end
                    end
                end

                default: state <= S_RX;
            endcase
        end
    end

endmodule
