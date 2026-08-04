// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * Protocol engine for the OCA host interface.
 *
 * Parses the fixed 8-byte header out of the receive buffer, runs the
 * command, and builds the response. Knows nothing about cryptography
 * beyond driving chacha20_poly1305 and comparing its tag.
 *
 * Store and forward, per packet: the whole request is buffered before the
 * engine sees any of it, and the whole response is built before a byte
 * leaves. That is what lets a failed tag return no plaintext at all, and
 * it survives the pipelining below unchanged, because each packet is
 * still received whole, processed whole and transmitted whole.
 *
 * The stream is 64 bits wide with a byte-enable, because at 8 bits the
 * buffer needed 66 cycles to assemble a block the engine consumes in 40
 * and so could not feed even one engine (amendment of 2026-08-04 in
 * docs/design/2026-08-03-host-protocol.md). The 8-bit AXI-Stream that
 * verilog-ethernet hands over is unchanged; the width conversion belongs
 * at that boundary, not inside here.
 *
 * FOUR STAGES, FOUR STATE REGISTERS, ONE OWNER PER PORT
 *
 * RX receives packet N+2 into one bank of the receive buffer, PROC parses
 * and feeds packet N+1 out of the other, DRAIN writes packet N's engine
 * output into one bank of the transmit buffer and publishes its result,
 * and TX streams packet N-1's response out of the other. Each memory port
 * has exactly one owner, which is what keeps both buffers at one write
 * port and one read port and so at 2 DP16KD each: parse deliberately
 * stays inside PROC, sharing the receive buffer's single reader with the
 * block feed, because giving it a stage of its own would add a second
 * read port and double the block RAM.
 *
 * A block costs the engine's 40 cycles instead of 64 (measured
 * 2026-08-04, 8 ingress + 48 compute + 8 egress before, marginal 40
 * after) because the three phases now belong to different stages.
 *
 * WHAT HOLDS THE SECURITY PROPERTY, NOW THAT ONE PACKET AT A TIME NO
 * LONGER DOES
 *
 * Every field a packet is judged on is copied into a per-packet
 * descriptor at the stage boundary, and no other packet's stage can write
 * it. This matters most for the received tag: it used to be a
 * combinational slice of `args`, the single shift register the parser
 * fills for every packet, so letting a successor parse during a
 * predecessor's tag check would have handed an attacker both operands of
 * the comparison that releases plaintext — send open(ct, wrong tag)
 * followed by a packet whose bytes 24..39 are the right tag, recorded in
 * the clear from an earlier seal, and the comparison becomes T == T. The
 * descriptor copy is what makes that impossible, not the ordering of the
 * stages.
 *
 * The engine is owned by exactly one packet, from its `eng_start` to the
 * cycle its result is published, and the token is explicit rather than a
 * property of the state encoding. chacha20_poly1305 holds one key, one
 * nonce, one counter and one accumulator, and honours `start` only from
 * its idle state, so a start arriving while it is busy would be discarded
 * in silence; held as a level instead, it would begin a second message
 * under the same one-time (r, s), which is Security.md section 4 item 3.
 * Overlap is therefore RX || compute || TX and never compute || compute.
 *
 * Holding the token until publication also means the engine cannot be
 * producing ciphertext for packet N+1 while DRAIN is still finishing
 * packet N, so `out_valid` — a one-cycle pulse with no back-pressure —
 * always finds the drain idle. It costs nothing at steady state: the next
 * packet's start waits on the previous response leaving, and the response
 * of packet N-1 leaves while packet N computes, so the period is still
 * the larger of compute and transmit.
 *
 * DRAIN is the single point where a result becomes a response. Every
 * completion goes through it — crypto, statistics, load-key, header
 * error, engine error — with no shortcut for the commands that finish in
 * one cycle, because a bad-magic packet answering ahead of the 1400-byte
 * seal in front of it is exactly the reordering the wire format does not
 * promise. Responses therefore leave in arrival order structurally, with
 * no reorder buffer and no sequence tags. The one packet that gets no
 * answer at all, being shorter than a header, never becomes a descriptor
 * and so cannot disturb its neighbours either.
 *
 * TIMING. The tag comparison is one 128-bit equality in a one-cycle
 * state, the same cost either way, so latency depends on lengths and
 * handshakes and not on data values (Security.md section 3). One residual
 * is recorded honestly rather than papered over: a failed tag shortens
 * the response, which shifts when the packet behind it is published. That
 * signal is the pass/fail bit, which status 06 puts on the wire in the
 * clear regardless, so nothing secret is added to what an observer on the
 * segment already has.
 *
 * tkeep is decoded by a priority encoder, and a non-last beat carrying a
 * partial keep fails the packet closed with status 05. Only the final
 * write of a packet may be partial (oca_pktbuf): a partial write
 * mid-stream leaves the byte count off a word boundary and the next
 * write lands back on the word just issued, so the buffer would hold
 * something other than what arrived while every length check still
 * agreed. Nothing read out of it past that beat can be trusted,
 * including the magic, which is why the check precedes the header ones.
 * The flag is per bank, because RX is a packet ahead of PROC and a
 * partial keep in the packet still arriving must not fail the packet
 * being processed.
 *
 * Every fixed field is word-aligned, so the header is one read and the
 * arguments four. The single misaligned boundary in the protocol is the
 * one between the AAD and the message, whose distance apart is
 * aad_len bytes: a funnel shifter over two consecutive buffer words
 * carries it, with feed_addr kept as a byte address so that the word
 * address and the shift both fall out of it and the section logic needs
 * no knowledge of either. Bank bases are whole banks, so they stay
 * 8-byte aligned by construction and the funnel never sees a base that
 * makes its word address and its shift disagree.
 *
 * The request stream is plain AXI-Stream: s_tready is high exactly while
 * RX is in the one state that stores what it accepts, so a beat is never
 * taken and discarded, and nothing of the source's handshake reaches the
 * signal.
 *
 * The four stats counters are: packets received (every packet that
 * reaches tlast and is looked at), packets dropped for an invalid header
 * (too short to hold one, bad magic, bad version, unknown opcode — the
 * first is the only packet that gets no answer at all), commands
 * completed with status 00, and authentication failures. Each has exactly
 * one writer: the first two belong to PROC and the last two to DRAIN,
 * both of which handle packets strictly in arrival order, so a snapshot
 * is a prefix of the command sequence and never a timing-dependent set.
 * PROC's two are sampled when the stats command is parsed and DRAIN's two
 * when it is published, which is the cycle before the increment that
 * would have counted the command itself. Its sixteen bytes travel through
 * the same response field the tag of a seal uses, not through the
 * transmit buffer: the two cannot both be present.
 *
 * A load-key command whose length is not exactly 40 bytes is refused
 * with status 05. Without that check the 32 key bytes would be read from
 * buffer positions the packet never wrote, which after a previous
 * command means loading a key an attacker chose the bytes of. A load-key
 * is also ordered against the commands behind it for free: PROC is a
 * single in-order stage, so the slot write commits before the next
 * packet's key lookup is even issued, and a seal behind a re-key cannot
 * encrypt under the key it replaced.
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
    output logic         rx_wr_bank,
    output logic         rx_wr_en,
    output logic [ 63:0] rx_wr_data,
    output logic [  3:0] rx_wr_bytes,
    output logic         rx_wr_clear,
    output logic         rx_rd_bank,
    output logic [  8:0] rx_rd_addr,
    input  logic [ 63:0] rx_rd_data,
    input  logic [ 11:0] rx_rd_count,
    input  logic         rx_rd_full,
    // transmit buffer
    output logic         tx_wr_bank,
    output logic         tx_wr_en,
    output logic [ 63:0] tx_wr_data,
    output logic [  3:0] tx_wr_bytes,
    output logic         tx_wr_clear,
    input  logic [ 11:0] tx_wr_count,
    output logic         tx_rd_bank,
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
    input  logic         eng_busy,
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

    // ------------------------------------------------------------------
    // Stage hand-offs.
    //
    // Each is a pair of parity bits with one writer apiece, rather than a
    // flag one stage sets and another clears: a register written from two
    // always_ff blocks is not a register. The predicate is their xor, so
    // "full" and "taken" cost one flip-flop each and no arbitration.
    // ------------------------------------------------------------------
    logic [1:0] fill_par, free_par;      // RX filled a bank / PROC freed it
    logic [1:0] bank_full;
    always_comb bank_full = fill_par ^ free_par;

    logic pd_put_par, pd_get_par;        // PROC handed a descriptor / DRAIN took it
    logic pd_valid;
    always_comb pd_valid = pd_put_par ^ pd_get_par;

    logic eng_take_par, eng_rel_par;     // PROC took the engine / DRAIN released it
    logic eng_owned;
    always_comb eng_owned = eng_take_par ^ eng_rel_par;

    logic rsp_pub_par, rsp_done_par;     // DRAIN published / TX finished streaming
    logic rsp_pending;
    always_comb rsp_pending = rsp_pub_par ^ rsp_done_par;

    // DRAIN has nothing left from any earlier packet. The engine may not
    // start until this holds: `out_valid` is a one-cycle pulse with no
    // back-pressure, and DRAIN is the only thing that can catch it, so a
    // message must not begin producing ciphertext while DRAIN is still
    // finishing or publishing its predecessor. The engine token alone
    // does not say this — a load-key or a header error takes no token at
    // all, yet still occupies DRAIN for as long as its response takes to
    // be accepted.
    // (assigned below, next to the register it reads: read_slang requires
    // the declaration to precede the use, where Verilator does not)
    logic drain_empty;

    // ------------------------------------------------------------------
    // RX: one bank of the receive buffer, the request stream, nothing else
    // ------------------------------------------------------------------
    typedef enum logic [1:0] {
        R_CLEAR,     // zero the byte count of the bank about to be used
        R_ACCEPT,    // take beats
        R_LAST,      // the final beat's write is landing
        R_WAIT       // the next bank is still being read by PROC
    } rx_fsm_t;
    rx_fsm_t rx_state;

    logic [1:0] keep_bad;   // per bank: a non-last beat carried a partial keep

    // Combinational, but only on the state register: nothing of the
    // source's own handshake reaches it, so there is no tvalid-to-tready
    // path and the accepting cycle is the state that stores the beat.
    always_comb s_tready = rst_n && (rx_state == R_ACCEPT);

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

    // ------------------------------------------------------------------
    // PROC: the other bank of the receive buffer, the parse, the key
    // lookup and write, and the engine's input side
    // ------------------------------------------------------------------
    typedef enum logic [3:0] {
        P_IDLE,      // wait for a full bank
        P_PARSE,     // read the header word
        P_DISPATCH,  // decide, or fail with a status
        P_ARGS,      // read nonce, lengths and tag, validate them
        P_LOADKEY,   // write the slot
        P_START,     // take the engine and start the message
        P_FEED,      // read the next 64-byte block out of the buffer
        P_PRESENT,   // hand that block to the engine
        P_NEXTBLK,   // advance section and offset
        P_ENDREQ,    // hand the descriptor on, release the bank
        P_DROP       // shorter than a header: release the bank, answer nothing
    } pr_fsm_t;
    pr_fsm_t pr_state;

    logic [ 7:0] opcode, slot, status;
    logic [15:0] req_id;
    logic [11:0] rx_len;          // bytes of request received
    logic        crypto_cmd;      // this packet took the engine
    logic [31:0] cnt_rx, cnt_drop;

    // Request bytes 8..39, byte 8 in [7:0], filled a word at a time. The
    // header word arrives through the same register and is read out of
    // the top of it, then leaves as the four argument words shift in, so
    // it needs no register of its own. Only bytes 0..6 are aliased: byte
    // 7 is the status field, which a request does not carry.
    //
    // It belongs to PROC alone and is rewritten for every packet, which
    // is precisely why nothing DRAIN decides on may read it.
    logic [255:0] args;
    logic [ 55:0] hdr;
    always_comb hdr = args[247:192];

    logic [511:0] blk;            // block being assembled for the engine
    logic [ 11:0] feed_addr;      // byte offset of the block being read
    logic [  2:0] feed_shift;     // feed_addr within its word
    logic [ 63:0] feed_prev;      // the word before the one on rx_rd_data
    logic [ 15:0] sec_left;       // bytes left in the section being fed
    logic         sec_aad;        // that section is the AAD
    logic [  6:0] blk_len;
    logic         blk_last;

    // Sequential reader over the receive buffer, owned by PROC and shared
    // between the parse and the block feed, which never run at once. The
    // buffer's read port is registered, so a word lands two edges after
    // its address is driven; issuing one address per cycle and consuming
    // one word per cycle behind a two-deep valid pipeline keeps that at
    // one word per cycle instead of the three a per-word handshake would
    // cost. Between blocks PROC waits in P_PRESENT with rd_left at zero:
    // the address holds, the buffer keeps reading that word every edge,
    // and the two-stage valid pipeline discards every one of them.
    logic [8:0] rd_ptr;
    logic [3:0] rd_left, rd_got;
    logic       rd_v, rd_v_d;

    // Request fields, sliced out of the shift register above.
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
    always_comb len_bad = rx_rd_full || (want_len > 18'(BYTES))
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

    // ------------------------------------------------------------------
    // The descriptor: everything about a packet that outlives PROC.
    //
    // pd_tag carries the received tag of an open, or the two counters
    // PROC owns for a stats — they cannot both be present, so one
    // register serves. Nothing DRAIN decides on is read from a register
    // PROC will reuse, which is the whole point of the copy.
    // ------------------------------------------------------------------
    logic [  7:0] pd_opcode, pd_slot, pd_status;
    logic [ 15:0] pd_req_id;
    logic         pd_crypto;      // an engine result is still to come
    logic         pd_engine;      // this packet holds the engine token
    logic [127:0] pd_tag;

    // ------------------------------------------------------------------
    // DRAIN: the transmit buffer's write port, the tag check, and the one
    // place a result becomes a response
    // ------------------------------------------------------------------
    typedef enum logic [2:0] {
        D_IDLE,      // take ciphertext blocks, take descriptors, wait
        D_WRITE,     // write one block into the transmit buffer
        D_FIN,       // size the response
        D_CHECK,     // compare the tag (open only)
        D_PUBLISH,   // hand the response to TX, in arrival order
        D_CLEAR      // zero the bank the next packet will write
    } dr_fsm_t;
    dr_fsm_t dr_state;

    logic [511:0] outbuf;         // block coming back, drained a word at a time
    logic [  6:0] out_left;
    logic [127:0] resp_tag;       // the tag the engine computed
    logic         eng_done_seen;
    logic [ 31:0] cnt_done, cnt_auth_fail;

    logic [  7:0] dcur_opcode, dcur_slot, dcur_status;
    logic [ 15:0] dcur_req_id;
    logic         dcur_crypto, dcur_engine, dcur_valid;
    logic [127:0] dcur_tag;
    logic [ 11:0] dcur_body_len;

    always_comb drain_empty = !pd_valid && !dcur_valid;

    // Draining a block into the transmit buffer. Only the last word of
    // the last block of the message can be partial, which is the
    // invariant oca_pktbuf requires of its writer.
    logic [3:0] out_bytes;
    logic [6:0] nx_out_left;
    always_comb out_bytes   = (out_left >= 7'd8) ? 4'd8 : {1'b0, out_left[2:0]};
    always_comb nx_out_left = (out_left >= 7'd8) ? (out_left - 7'd8) : 7'd0;

    // The security property of the whole design. One 128-bit equality
    // between the tag the engine computed and the sixteen bytes the host
    // sent, both of them registers belonging to the packet being judged:
    // fixed-width combinational logic, so it is constant-time by
    // construction, and both outcomes below cost the same single cycle.
    // Never a byte loop, and never a comparison delegated to the host
    // (docs/design/2026-08-03-host-protocol.md section 5).
    logic tag_match;
    always_comb tag_match = (resp_tag == dcur_tag);

    // ------------------------------------------------------------------
    // TX: the transmit buffer's read port and the output stream
    // ------------------------------------------------------------------
    typedef enum logic [0:0] { T_IDLE, T_STREAM } ts_fsm_t;
    ts_fsm_t ts_state;

    // The published response. Written once by DRAIN while TX is idle,
    // read by TX for the whole of a response: the header a packet ships
    // is the header its own request was judged into, not whatever the
    // stage behind it happens to hold.
    logic [  7:0] rsp_opcode, rsp_slot, rsp_status;
    logic [ 15:0] rsp_req_id;
    logic [127:0] rsp_extra;
    logic [ 11:0] rsp_body_len;
    logic         rsp_bank;

    // Response pipeline, three stages deep: stage 1 walks the word index
    // and says where that word comes from, stage 2 is the transmit
    // buffer's registered read with the metadata that travels beside it,
    // stage 3 is the output register.
    logic [11:0] resp_left;
    logic [ 8:0] resp_widx, body_start_w;
    logic [ 1:0] resp_sel;
    logic        resp_v;
    logic [ 3:0] beat_bytes;
    logic [ 1:0] beat_sel;
    logic        beat_last, beat_v;

    // Response word source: the header, the two words of the extra field
    // where there is one, then the transmit buffer. Selected by a
    // registered two-bit code rather than by comparing the word index
    // here — the same multiplexer written as an if / else chain over
    // comparators costs 771 LUT4 at 64 bits against 129 for this,
    // measured 2026-08-04. The extra field is indexed rather than shifted
    // out because a shift register would have to be advanced by the same
    // enable as the stage reading it, and a second thing to freeze is
    // what the pipeline below exists to avoid.
    logic [63:0] resp_hdr, resp_word;
    always_comb resp_hdr = {rsp_status, rsp_slot, rsp_req_id, rsp_opcode,
                            VERSION, MAGIC};

    // A response carries the extra field when it is a successful seal
    // (the tag) or a successful stats (the counters). Every other
    // response goes straight from the header to the buffer body, if it
    // has one at all.
    logic has_extra;
    always_comb has_extra = (rsp_status == ST_OK)
                            && ((rsp_opcode == OP_SEAL)
                                || (rsp_opcode == OP_STATS));
    always_comb begin
        case (beat_sel)
            2'd0:    resp_word = resp_hdr;
            2'd1:    resp_word = rsp_extra[ 63: 0];
            2'd2:    resp_word = rsp_extra[127:64];
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
    // header is one word and the extra field two — so the transmit buffer
    // is read straight, with no funnel on this side. Below body_start_w
    // the word index is its own source code: word 0 is the header, words
    // 1 and 2 are the extra field where there is one.
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

    // The word address the transmit buffer is shown this cycle, inside
    // the responding packet's own bank. Its read is unconditional — one
    // word lands on tx_rd_data every edge, whatever the pipeline is doing
    // — so freezing the stages is not enough on its own: with the address
    // left at the word stage 1 is fetching, the edge ending a stalled
    // cycle would overwrite the word stage 2 still owes the output
    // register. Showing it the stage-2 word instead makes that edge
    // re-read what is already there, and is the one place go has to reach
    // combinationally. resp_widx counts stage 1, so stage 2 is the word
    // below it. The subtraction underflows for the words above the body,
    // where oca_pktbuf clamps it to word zero of this same bank — never
    // of the neighbour's.
    always_comb tx_rd_bank = rsp_bank;
    always_comb tx_rd_addr = (go ? resp_widx : (resp_widx - 9'd1))
                             - body_start_w;


    // ==================================================================
    // RX
    // ==================================================================
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            rx_state    <= R_CLEAR;
            rx_wr_bank  <= 1'b0;
            rx_wr_en    <= 1'b0;
            rx_wr_data  <= 64'd0;
            rx_wr_bytes <= 4'd0;
            rx_wr_clear <= 1'b0;
            fill_par    <= 2'd0;
            keep_bad    <= 2'd0;
        end else begin
            rx_wr_en    <= 1'b0;
            rx_wr_clear <= 1'b0;

            case (rx_state)
                // The clear pulsed on the way in is active during this
                // cycle, and s_tready is low here. Asserting it in the
                // first accepting cycle instead would meet a source that
                // already has a beat waiting, and oca_pktbuf gives the
                // clear priority: the packet would silently lose its
                // first eight bytes.
                R_CLEAR: begin
                    keep_bad[rx_wr_bank] <= 1'b0;
                    rx_state             <= R_ACCEPT;
                end

                R_ACCEPT: begin
                    if (s_tvalid && s_tready) begin
                        rx_wr_en    <= 1'b1;
                        rx_wr_data  <= s_tdata;
                        rx_wr_bytes <= keep_bytes;
                        if (!s_tlast && (s_tkeep != 8'hFF))
                            keep_bad[rx_wr_bank] <= 1'b1;
                        if (s_tlast) rx_state <= R_LAST;
                    end
                end

                // The write side is registered, so the beat accepted in
                // R_ACCEPT is still landing during this cycle. Flipping
                // the bank on the accepting edge instead would put every
                // packet's final beat into the bank the *next* packet is
                // about to use, and the packet would be answered eight
                // bytes short of what arrived — with every length check
                // still agreeing, because the count moved with it.
                // Handing the bank to PROC waits here for the same
                // reason: the byte count is not final until this edge.
                R_LAST: begin
                    fill_par[rx_wr_bank] <= ~fill_par[rx_wr_bank];
                    rx_wr_bank           <= ~rx_wr_bank;
                    rx_state             <= R_WAIT;
                end

                R_WAIT: begin
                    if (!bank_full[rx_wr_bank]) begin
                        // only ever the bank RX owns: a clear reaching
                        // the other one truncates the packet PROC is
                        // reading, and produces a short response under a
                        // tag that verifies
                        rx_wr_clear <= 1'b1;
                        rx_state    <= R_CLEAR;
                    end
                end

                default: rx_state <= R_CLEAR;
            endcase
        end
    end


    // ==================================================================
    // PROC
    // ==================================================================
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            pr_state     <= P_IDLE;
            rx_rd_bank   <= 1'b0;
            rx_rd_addr   <= 9'd0;
            free_par     <= 2'd0;
            pd_put_par   <= 1'b0;
            eng_take_par <= 1'b0;
            ks_wr_en     <= 1'b0;
            ks_wr_slot   <= 8'd0;
            ks_wr_key    <= 256'd0;
            ks_rd_slot   <= 8'd0;
            eng_start    <= 1'b0;
            eng_dec      <= 1'b0;
            eng_key      <= 256'd0;
            eng_nonce    <= 96'd0;
            eng_in_valid <= 1'b0;
            eng_in_aad   <= 1'b0;
            eng_in_last  <= 1'b0;
            eng_in_len   <= 7'd0;
            eng_in_data  <= 512'd0;
            opcode       <= 8'd0;
            slot         <= 8'd0;
            status       <= ST_OK;
            req_id       <= 16'd0;
            rx_len       <= 12'd0;
            crypto_cmd   <= 1'b0;
            cnt_rx       <= 32'd0;
            cnt_drop     <= 32'd0;
            args         <= 256'd0;
            blk          <= 512'd0;
            feed_addr    <= 12'd0;
            feed_shift   <= 3'd0;
            feed_prev    <= 64'd0;
            sec_left     <= 16'd0;
            sec_aad      <= 1'b0;
            blk_len      <= 7'd0;
            blk_last     <= 1'b0;
            pd_opcode    <= 8'd0;
            pd_slot      <= 8'd0;
            pd_status    <= ST_OK;
            pd_req_id    <= 16'd0;
            pd_crypto    <= 1'b0;
            pd_engine    <= 1'b0;
            pd_tag       <= 128'd0;
            rd_ptr       <= 9'd0;
            rd_left      <= 4'd0;
            rd_got       <= 4'd0;
            rd_v         <= 1'b0;
            rd_v_d       <= 1'b0;
        end else begin
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

            case (pr_state)
                P_IDLE: begin
                    if (bank_full[rx_rd_bank]) begin
                        rd_ptr     <= 9'd0;
                        rd_left    <= 4'd1;
                        rd_got     <= 4'd0;
                        crypto_cmd <= 1'b0;
                        pr_state   <= P_PARSE;
                    end
                end

                P_PARSE: begin
                    if (rd_v_d) args <= {rx_rd_data, args[255:64]};
                    if (rd_got == 4'd1) pr_state <= P_DISPATCH;
                end

                P_DISPATCH: begin
                    opcode     <= hdr[31:24];
                    req_id     <= hdr[47:32];
                    slot       <= hdr[55:48];
                    ks_rd_slot <= hdr[55:48];
                    rx_len     <= rx_rd_count;
                    // Counted here rather than at tlast: PROC takes banks
                    // in arrival order, so a statistics snapshot is a
                    // prefix of the command sequence instead of including
                    // whatever RX happened to have received by then.
                    cnt_rx     <= cnt_rx + 32'd1;
                    if (rx_rd_count < 12'(HDR_LEN)) begin
                        // not even a header: the only packet that gets no
                        // answer, because there is nothing to answer to.
                        // It never becomes a descriptor, so it cannot
                        // disturb the order of its neighbours either.
                        cnt_drop <= cnt_drop + 32'd1;
                        pr_state <= P_DROP;
                    end else if (keep_bad[rx_rd_bank]) begin
                        // the write pointer is not where the byte count
                        // says it is, so the magic below would be read
                        // out of a word the packet never filled
                        status   <= ST_BAD_LENGTH;
                        pr_state <= P_ENDREQ;
                    end else if (hdr[15:0] != MAGIC) begin
                        status   <= ST_BAD_MAGIC;
                        cnt_drop <= cnt_drop + 32'd1;
                        pr_state <= P_ENDREQ;
                    end else if (hdr[23:16] != VERSION) begin
                        status   <= ST_BAD_VERSION;
                        cnt_drop <= cnt_drop + 32'd1;
                        pr_state <= P_ENDREQ;
                    end else if (hdr[31:24] != OP_LOAD_KEY
                              && hdr[31:24] != OP_SEAL
                              && hdr[31:24] != OP_OPEN
                              && hdr[31:24] != OP_STATS) begin
                        status   <= ST_BAD_OPCODE;
                        cnt_drop <= cnt_drop + 32'd1;
                        pr_state <= P_ENDREQ;
                    end else begin
                        rd_ptr   <= 9'(HDR_LEN / 8);
                        rd_left  <= 4'd4;
                        rd_got   <= 4'd0;
                        pr_state <= P_ARGS;
                    end
                end

                P_ARGS: begin
                    if (rd_v_d) args <= {rx_rd_data, args[255:64]};
                    if (rd_got == 4'd4) begin
                        // Latched out of `args` here, for the packet that
                        // owns it, and never read from `args` again: the
                        // parse of the packet behind this one rewrites
                        // that register long before the tag is compared.
                        pd_tag <= args[255:128];   // bytes 24..39
                        if (opcode == OP_LOAD_KEY) begin
                            if (slot >= 8'(NUM_SLOTS)) begin
                                status   <= ST_BAD_SLOT;
                                pr_state <= P_ENDREQ;
                            end else if (rx_len != 12'(HDR_LEN + 32)) begin
                                status   <= ST_BAD_LENGTH;
                                pr_state <= P_ENDREQ;
                            end else begin
                                pr_state <= P_LOADKEY;
                            end
                        end else if (opcode == OP_STATS) begin
                            // PROC's two counters, exact for every packet
                            // ahead of this one because PROC is in order.
                            // DRAIN fills in its own two at publication.
                            pd_tag[63:0] <= {cnt_drop, cnt_rx};
                            status       <= ST_OK;
                            pr_state     <= P_ENDREQ;
                        end else if (!ks_rd_valid) begin
                            status   <= ST_BAD_SLOT;
                            pr_state <= P_ENDREQ;
                        end else if (len_bad) begin
                            status   <= ST_BAD_LENGTH;
                            pr_state <= P_ENDREQ;
                        end else begin
                            eng_dec    <= (opcode == OP_OPEN);
                            eng_key    <= ks_rd_key;
                            eng_nonce  <= nonce;
                            sec_aad    <= first_aad;
                            sec_left   <= first_left;
                            blk_len    <= first_len;
                            blk_last   <= first_last;
                            feed_addr  <= data_off;
                            feed_shift <= data_off[2:0];
                            status     <= ST_OK;
                            pr_state   <= P_START;
                        end
                    end
                end

                P_LOADKEY: begin
                    ks_wr_en   <= 1'b1;
                    ks_wr_slot <= slot;
                    ks_wr_key  <= args;
                    status     <= ST_OK;
                    pr_state   <= P_ENDREQ;
                end

                // The engine is taken here and not in P_ARGS, and it is
                // taken as a token rather than by looking at `busy` alone.
                // A start honoured while the previous packet's result had
                // not yet been read out would let that packet's `done`,
                // `tag` and `err` be consumed by the wrong owner; a start
                // arriving while the engine is genuinely busy would be
                // discarded in silence, and the whole message fed into
                // whatever the engine was actually running.
                //
                // drain_empty is the second half of the same statement,
                // and it is the one that is easy to miss: it makes the
                // start wait for every earlier packet to have been
                // published, including the ones that never touched the
                // engine, so that the drain is always free when the first
                // ciphertext block appears. It costs nothing at steady
                // state, because publication is already paced by the
                // response before it leaving.
                P_START: begin
                    if (!eng_busy && !eng_owned && drain_empty) begin
                        eng_start    <= 1'b1;
                        eng_take_par <= ~eng_take_par;
                        crypto_cmd   <= 1'b1;
                        rd_ptr       <= feed_addr[11:3];
                        rd_left      <= 4'd9;
                        rd_got       <= 4'd0;
                        pr_state     <= P_FEED;
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
                P_FEED: begin
                    if (rd_v_d) begin
                        feed_prev <= rx_rd_data;
                        if (rd_got != 4'd0) blk <= {feed_word, blk[511:64]};
                    end
                    if (rd_got == 4'd9) pr_state <= P_PRESENT;
                end

                // in_valid is asserted the cycle after in_ready was seen
                // high. That is still a handshake, not a speculative
                // present: the engine lowers in_ready only in response to
                // in_valid, so it cannot have fallen in between. A block
                // offered while it is low would be discarded in silence.
                //
                // No second block register is needed for the prefetch:
                // the engine samples in_data into its own c_data_in on
                // the handshake cycle, so `blk` is free from the cycle
                // after this one, two before P_FEED writes it again.
                P_PRESENT: begin
                    if (eng_err) begin
                        pr_state <= P_ENDREQ;
                    end else if (eng_in_ready) begin
                        eng_in_valid <= 1'b1;
                        eng_in_aad   <= sec_aad;
                        eng_in_last  <= blk_last;
                        eng_in_len   <= blk_len;
                        eng_in_data  <= blk;
                        pr_state     <= P_NEXTBLK;
                    end
                end

                P_NEXTBLK: begin
                    if (blk_last) begin
                        pr_state <= P_ENDREQ;
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
                        pr_state   <= P_FEED;
                    end
                end

                // The hand-off. Everything DRAIN will judge this packet on
                // is copied here, before PROC touches `args`, `opcode` or
                // `status` for the packet behind it.
                //
                // An engine error is folded in here so that a packet
                // whose message was abandoned is never left waiting for a
                // `done` that cannot come. It is qualified by crypto_cmd
                // because `err` is sticky until the next start, so an
                // error raised by an earlier packet would otherwise fail
                // a stats command that never touched the engine.
                P_ENDREQ: begin
                    if (!pd_valid) begin
                        pd_opcode  <= opcode;
                        pd_req_id  <= req_id;
                        pd_slot    <= slot;
                        pd_status  <= (crypto_cmd && eng_err) ? ST_ENGINE_ERR
                                                              : status;
                        pd_crypto  <= crypto_cmd && !eng_err;
                        // Two bits, not one. A packet whose message the
                        // engine abandoned still holds the token — the
                        // release has to be paired with the take, or the
                        // next packet waits on a token nobody owns.
                        pd_engine  <= crypto_cmd;
                        pd_put_par <= ~pd_put_par;

                        free_par[rx_rd_bank] <= ~free_par[rx_rd_bank];
                        rx_rd_bank           <= ~rx_rd_bank;
                        pr_state             <= P_IDLE;
                    end
                end

                P_DROP: begin
                    free_par[rx_rd_bank] <= ~free_par[rx_rd_bank];
                    rx_rd_bank           <= ~rx_rd_bank;
                    pr_state             <= P_IDLE;
                end

                default: pr_state <= P_IDLE;
            endcase
        end
    end


    // ==================================================================
    // DRAIN
    // ==================================================================
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            dr_state      <= D_IDLE;
            tx_wr_bank    <= 1'b0;
            tx_wr_en      <= 1'b0;
            tx_wr_data    <= 64'd0;
            tx_wr_bytes   <= 4'd0;
            tx_wr_clear   <= 1'b0;
            outbuf        <= 512'd0;
            out_left      <= 7'd0;
            resp_tag      <= 128'd0;
            eng_done_seen <= 1'b0;
            cnt_done      <= 32'd0;
            cnt_auth_fail <= 32'd0;
            dcur_opcode   <= 8'd0;
            dcur_slot     <= 8'd0;
            dcur_status   <= ST_OK;
            dcur_req_id   <= 16'd0;
            dcur_crypto   <= 1'b0;
            dcur_engine   <= 1'b0;
            dcur_valid    <= 1'b0;
            dcur_tag      <= 128'd0;
            dcur_body_len <= 12'd0;
            pd_get_par    <= 1'b0;
            eng_rel_par   <= 1'b0;
            rsp_opcode    <= 8'd0;
            rsp_slot      <= 8'd0;
            rsp_status    <= ST_OK;
            rsp_req_id    <= 16'd0;
            rsp_extra     <= 128'd0;
            rsp_body_len  <= 12'd0;
            rsp_bank      <= 1'b0;
            rsp_pub_par   <= 1'b0;
        end else begin
            tx_wr_en    <= 1'b0;
            tx_wr_clear <= 1'b0;

            // `done` is one cycle wide and the MAC of the last block
            // finishes long after its ciphertext has been drained, so it
            // is caught here rather than in a state. It can only belong
            // to the packet holding the engine token, and that packet's
            // result is read out before the token is released, so the
            // marker is never stale when the next packet arrives.
            if (eng_done) begin
                eng_done_seen <= 1'b1;
                resp_tag      <= eng_tag;
            end

            case (dr_state)
                // Ciphertext first. out_valid has no back-pressure, and
                // the token held until publication means the block can
                // only belong to the packet this stage is already
                // holding, so there is never a descriptor to take or a
                // result to finish in the same cycle as a block to catch.
                D_IDLE: begin
                    if (eng_out_valid) begin
                        outbuf   <= eng_out_data;
                        out_left <= eng_out_len;
                        dr_state <= D_WRITE;
                    end else if (pd_valid && !dcur_valid) begin
                        dcur_opcode <= pd_opcode;
                        dcur_req_id <= pd_req_id;
                        dcur_slot   <= pd_slot;
                        dcur_status <= pd_status;
                        dcur_crypto <= pd_crypto;
                        dcur_engine <= pd_engine;
                        dcur_tag    <= pd_tag;
                        dcur_valid  <= 1'b1;
                        pd_get_par  <= ~pd_get_par;
                    end else if (dcur_valid
                                 && (!dcur_crypto || eng_done_seen)) begin
                        eng_done_seen <= 1'b0;
                        dr_state      <= D_FIN;
                    end
                end

                D_WRITE: begin
                    if (out_left != 7'd0) begin
                        tx_wr_en    <= 1'b1;
                        tx_wr_data  <= outbuf[63:0];
                        tx_wr_bytes <= out_bytes;
                        outbuf      <= {64'd0, outbuf[511:64]};
                        out_left    <= nx_out_left;
                    end else begin
                        dr_state <= D_IDLE;
                    end
                end

                // The byte count comes from this packet's own bank of the
                // transmit buffer, which no other packet writes: sizing a
                // response from a counter a neighbour can move is how the
                // plaintext of one packet leaves inside another's body.
                D_FIN: begin
                    if (dcur_crypto && (dcur_opcode == OP_OPEN)) begin
                        dr_state <= D_CHECK;
                    end else begin
                        dcur_body_len <= (dcur_crypto
                                          && (dcur_status == ST_OK))
                                         ? tx_wr_count : 12'd0;
                        dr_state      <= D_PUBLISH;
                    end
                end

                // Store and forward is what makes this possible: the
                // plaintext is already whole in this packet's bank, so a
                // failed tag simply never addresses it. One cycle and one
                // equality either way — do not fold this into D_PUBLISH
                // behind a condition that lengthens one branch.
                D_CHECK: begin
                    if (tag_match) begin
                        dcur_body_len <= tx_wr_count;
                    end else begin
                        dcur_status   <= ST_AUTH_FAIL;
                        dcur_body_len <= 12'd0;
                        tx_wr_clear   <= 1'b1;
                        cnt_auth_fail <= cnt_auth_fail + 32'd1;
                    end
                    dr_state <= D_PUBLISH;
                end

                // The single point where a result becomes a response, for
                // every command without exception, which is what makes
                // arrival order structural. Waiting on the previous
                // response having left is also what makes flipping the
                // transmit bank safe.
                D_PUBLISH: begin
                    if (!rsp_pending) begin
                        rsp_opcode   <= dcur_opcode;
                        rsp_req_id   <= dcur_req_id;
                        rsp_slot     <= dcur_slot;
                        rsp_status   <= dcur_status;
                        rsp_body_len <= dcur_body_len;
                        rsp_bank     <= tx_wr_bank;
                        // DRAIN's two counters are read on the cycle that
                        // increments cnt_done, so the value published is
                        // the one before this command counts itself.
                        rsp_extra    <= (dcur_opcode == OP_STATS)
                                        ? {cnt_auth_fail, cnt_done,
                                           dcur_tag[63:0]}
                                        : resp_tag;
                        rsp_pub_par  <= ~rsp_pub_par;
                        if (dcur_status == ST_OK) cnt_done <= cnt_done + 32'd1;

                        dcur_valid  <= 1'b0;
                        if (dcur_engine) eng_rel_par <= ~eng_rel_par;
                        tx_wr_bank  <= ~tx_wr_bank;
                        dr_state    <= D_CLEAR;
                    end
                end

                // The clear lands one cycle after the flip so that it
                // reaches the bank now being written and not the one just
                // published. That bank held the response before last,
                // which the interlock above has already seen leave.
                D_CLEAR: begin
                    tx_wr_clear <= 1'b1;
                    dr_state    <= D_IDLE;
                end

                default: dr_state <= D_IDLE;
            endcase
        end
    end


    // ==================================================================
    // TX
    // ==================================================================
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            ts_state     <= T_IDLE;
            m_tdata      <= 64'd0;
            m_tkeep      <= 8'd0;
            m_tvalid     <= 1'b0;
            m_tlast      <= 1'b0;
            resp_left    <= 12'd0;
            resp_widx    <= 9'd0;
            resp_sel     <= 2'd0;
            resp_v       <= 1'b0;
            body_start_w <= 9'd0;
            beat_bytes   <= 4'd0;
            beat_sel     <= 2'd0;
            beat_last    <= 1'b0;
            beat_v       <= 1'b0;
            rsp_done_par <= 1'b0;
        end else begin
            case (ts_state)
                // The response is sized from the registers it will ship,
                // so its shape and its label cannot disagree.
                T_IDLE: begin
                    if (rsp_pending) begin
                        resp_widx <= 9'd0;
                        resp_sel  <= 2'd0;
                        resp_v    <= 1'b1;
                        beat_v    <= 1'b0;
                        if (has_extra) begin
                            body_start_w <= 9'((HDR_LEN + 16) / 8);
                            resp_left    <= 12'(HDR_LEN + 16) + rsp_body_len;
                        end else begin
                            body_start_w <= 9'(HDR_LEN / 8);
                            resp_left    <= 12'(HDR_LEN) + rsp_body_len;
                        end
                        ts_state <= T_STREAM;
                    end
                end

                // One beat per cycle: the three stages step together
                // under go, so the two cycles the buffer's registered
                // read costs are paid once at the head of the response
                // and never again. resp_widx keeps counting after the
                // last word has been issued because the stalled address
                // above is derived from it.
                T_STREAM: begin
                    if (go) begin
                        if (m_tvalid && m_tlast) begin
                            m_tvalid     <= 1'b0;
                            m_tlast      <= 1'b0;
                            rsp_done_par <= ~rsp_done_par;
                            ts_state     <= T_IDLE;
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

                default: ts_state <= T_IDLE;
            endcase
        end
    end

endmodule
