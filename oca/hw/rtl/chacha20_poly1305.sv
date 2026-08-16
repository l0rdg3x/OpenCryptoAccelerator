// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * AEAD_CHACHA20_POLY1305 encryption engine (RFC 8439 section 2.8).
 *
 * Combines chacha20.sv and poly1305.sv: the Poly1305 one-time key is
 * derived internally (ChaCha20 block with counter 0); the message is
 * encrypted with counter 1 and up. The MAC is computed over
 * aad || pad16 || ciphertext || pad16 || le64(|aad|) || le64(|ct|).
 *
 * Usage:
 *   1. Pulse `start` with key/nonce stable.
 *   2. When `in_ready` is high, feed AAD in 64-byte blocks
 *      (in_valid pulse with in_aad=1), then plaintext blocks
 *      (in_aad=0). Only the last block of each section may be partial
 *      (in_len < 64). Mark the final block overall with in_last=1.
 *   3. Ciphertext blocks come out with out_valid (out_len valid bytes,
 *      byte 0 = bits [7:0]).
 *   4. `done` pulses with `tag` valid.
 *
 * An empty message is a single block with in_len=0 and in_last=1.
 *
 * `in_len` above 64 is illegal: it does not fit the 64-byte datapath and
 * the MAC FSM cannot terminate such a block (it counts 16-byte
 * sub-blocks in two bits, and mac_len + 15 wraps in seven). Presenting
 * one raises `err` instead: the message is abandoned there and then —
 * `busy` drops, `done` never pulses, no tag and no further ciphertext
 * are produced — and `err` stays high until the next `start`, which
 * clears it and begins a fresh message. A caller that ignores `err`
 * loses the message, never the engine.
 *
 * `len_bad` is evaluated in S_ACCEPT only, so a message aborted at block
 * N has already emitted the ciphertext of blocks 1..N-1 on `out_valid`.
 * A retry must therefore use a fresh nonce: the same (key, nonce) with
 * any change to the plaintext puts two plaintexts under one keystream.
 * This header said "no ciphertext or tag is produced" until 2026-08-09.
 *
 * Decryption (dec=1): feed the ciphertext instead of the plaintext;
 * the output stream is the recovered plaintext and the MAC is computed
 * over the input blocks (the ciphertext), as RFC 8439 requires. The
 * caller must compare `tag` with the received tag and discard the
 * plaintext on mismatch.
 *
 * Structure: two FSMs joined by a one-block buffer, so the two cores
 * run at the same time instead of taking turns. The input FSM accepts a
 * block, runs ChaCha20 over it and emits the ciphertext; the MAC FSM
 * drains the buffer into Poly1305 sixteen bytes at a time. Block N is
 * authenticated while block N+1 is encrypted, and a block costs the
 * slower of the two phases instead of their sum.
 *
 * The buffer is one block deep on purpose. `mac_valid` is set by the
 * input FSM and cleared by `mac_take` from the MAC FSM, and a block is
 * written only while `buf_free` holds, so a block can never be
 * overwritten before it has been authenticated. Poly1305 sees the AAD
 * blocks, then the ciphertext blocks, then the length block, in that
 * order, because the input FSM fills the buffer in the order it accepts
 * blocks and the MAC FSM drains it in the order it was filled.
 * Deepening the buffer would break that argument.
 *
 * Reset clears the key, the derived one-time key and the buffered
 * plaintext and ciphertext, not only the control state (Security.md).
 */
module chacha20_poly1305 (
    input  logic         clk,
    input  logic         rst_n,
    input  logic         start,
    input  logic         dec,       // 0 = encrypt, 1 = decrypt
    output logic         busy,
    input  logic [255:0] key,
    input  logic [ 95:0] nonce,
    // input stream: AAD blocks, then plaintext blocks
    input  logic         in_valid,
    input  logic         in_aad,
    input  logic         in_last,
    input  logic [  6:0] in_len,    // valid bytes in in_data, 0..64
    input  logic [511:0] in_data,
    output logic         in_ready,
    // ciphertext output stream
    output logic         out_valid,
    output logic [511:0] out_data,
    output logic [  6:0] out_len,
    // result
    output logic         done,
    output logic [127:0] tag,
    // sticky: an illegal in_len aborted the message (see header)
    output logic         err
);

    typedef enum logic [2:0] {
        S_IDLE, S_KEY, S_KEYP, S_ACCEPT, S_ENC, S_WAITBUF, S_FIN
    } in_fsm_t;
    in_fsm_t state;

    typedef enum logic [1:0] {
        S_M_IDLE, S_M_FEED, S_M_LEN, S_M_TAG
    } mac_fsm_t;
    mac_fsm_t m_state;

    logic [255:0] key_r;
    logic [ 95:0] nonce_r;
    logic         dec_r;
    logic [ 31:0] ctr;
    logic [ 63:0] aad_len, ct_len;
    logic [  6:0] cur_len;      // block held by the input FSM
    logic         cur_last;
    logic         cur_aad;      // block needs no ChaCha20 run
    logic [  1:0] sub_idx;

    // one-block buffer between the two FSMs
    logic [511:0] mac_buf;      // bytes to MAC (AAD, or ciphertext)
    logic [  6:0] mac_len;      // valid bytes, 0..64
    logic         mac_last;     // final block of the whole message
    logic         mac_valid;    // buffer holds a block to authenticate
    logic         mac_take;     // MAC FSM has consumed it
    logic         buf_free;

    // chacha20 instance
    logic         c_start, c_busy, c_done;
    logic [ 31:0] c_counter;
    logic [511:0] c_data_in, c_data_out;
    // poly1305 instance
    logic         p_start, p_blk, p_last, p_busy, p_blk_ready, p_done;
    logic [255:0] p_key;
    logic [127:0] p_data_in, p_tag;

    chacha20 u_chacha (
        .clk,
        .rst_n,
        .start   (c_start),
        .busy    (c_busy),
        .done    (c_done),
        .key     (key_r),
        .nonce   (nonce_r),
        .counter (c_counter),
        .data_in (c_data_in),
        .data_out(c_data_out)
    );

    // An aborted message leaves poly1305 part-way through a message, and
    // poly1305 only honours `start` from its idle state: without this it
    // would authenticate the next message with the abandoned one's r and
    // s. Held in reset from the abort until the next `start` clears
    // `err`, it returns to idle and its reset erases that r, s and
    // accumulator on the way -- the next message cannot reuse them and
    // they are no longer in the flip-flops either.
    logic p_rst_n;
    assign p_rst_n = rst_n && !err;

    poly1305 u_poly (
        .clk,
        .rst_n     (p_rst_n),
        .start     (p_start),
        .blk       (p_blk),
        .last      (p_last),
        .busy      (p_busy),
        .blk_ready (p_blk_ready),
        .done      (p_done),
        .key       (p_key),
        .data_in   (p_data_in),
        .data_len  (5'd16),     // AEAD pads every MAC block to 16 bytes
        .tag       (p_tag)
    );

    logic unused_ok;
    assign unused_ok = c_busy & p_busy;
    assign tag = p_tag;

    // Zero all bytes at index >= len of one 16-byte poly1305 sub-block.
    // The padding only ever has to be zero where poly1305 consumes it, so
    // the mask sits on this 128-bit slice rather than on the 512-bit
    // buses feeding it: one masking stage a quarter of the width instead
    // of two full-width ones. Built per byte on purpose: a
    // (1 << len*8) - 1 mask synthesises into one full-width carry chain,
    // 16 independent 5-bit compares into none.
    function automatic logic [127:0] mask_sub(
        input logic [127:0] d, input logic [4:0] len
    );
        logic [127:0] m;
        begin
            for (int i = 0; i < 16; i++)
                m[i*8 +: 8] = (5'(i) < len) ? 8'hff : 8'h00;
            return d & m;
        end
    endfunction

    // number of 16-byte poly1305 sub-blocks for the buffered block
    logic [2:0] sub_cnt;
    logic       sub_done;
    always_comb sub_cnt  = 3'((mac_len + 7'd15) >> 4);
    always_comb sub_done = ({1'b0, sub_idx} == sub_cnt - 3'd1);

    // Valid bytes of the sub-block sub_idx addresses: a full 16, except
    // in the last sub-block of a block whose length is not a multiple of
    // 16. mac_len[3:0] is that remainder, and it is zero exactly when the
    // last sub-block is full, which is why the zero case selects 16.
    logic [4:0] sub_len;
    always_comb sub_len = (sub_done && mac_len[3:0] != 4'd0)
                          ? {1'b0, mac_len[3:0]} : 5'd16;

    // An illegal block, caught on the cycle it is offered: both FSMs act
    // on it, so neither is left holding state from the dead message.
    logic len_bad;
    always_comb len_bad = (state == S_ACCEPT) && in_valid && (in_len > 7'd64);

    // The buffer is released in the cycle poly1305 consumes its last
    // sub-block: p_data_in is combinational from mac_buf, but poly1305
    // registers the sum on that same edge, so a write landing on the
    // release edge is never seen. A zero-length section is released
    // straight away, it contributes no MAC block.
    always_comb mac_take = (m_state == S_M_FEED && p_blk_ready && sub_done)
                        || (m_state == S_M_IDLE && mac_valid
                            && mac_len == 7'd0);
    always_comb buf_free = !mac_valid || mac_take;

    // Input FSM: accepts blocks, runs ChaCha20, fills the buffer.
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state     <= S_IDLE;
            busy      <= 1'b0;
            in_ready  <= 1'b0;
            out_valid <= 1'b0;
            c_start   <= 1'b0;
            p_start   <= 1'b0;
            mac_valid <= 1'b0;
            err       <= 1'b0;
            key_r     <= '0;
            nonce_r   <= '0;
            p_key     <= '0;
            c_data_in <= '0;
            mac_buf   <= '0;
            out_data  <= '0;
        end else begin
            out_valid <= 1'b0;
            // a write in the same cycle wins: the buffer stays occupied
            if (mac_take) mac_valid <= 1'b0;
            case (state)
                S_IDLE: begin
                    if (start) begin
                        key_r     <= key;
                        nonce_r   <= nonce;
                        dec_r     <= dec;
                        ctr       <= 32'd1;
                        aad_len   <= 64'd0;
                        ct_len    <= 64'd0;
                        c_counter <= 32'd0;
                        c_data_in <= 512'd0;
                        c_start   <= 1'b1;
                        busy      <= 1'b1;
                        err       <= 1'b0;
                        state     <= S_KEY;
                    end
                end
                S_KEY: begin
                    c_start <= 1'b0;
                    if (c_done) begin
                        p_key   <= c_data_out[255:0];  // first 32 bytes
                        p_start <= 1'b1;
                        state   <= S_KEYP;
                    end
                end
                S_KEYP: begin
                    p_start  <= 1'b0;
                    in_ready <= 1'b1;
                    state    <= S_ACCEPT;
                end
                S_ACCEPT: begin
                    if (len_bad) begin
                        err       <= 1'b1;
                        busy      <= 1'b0;
                        in_ready  <= 1'b0;
                        mac_valid <= 1'b0;
                        state     <= S_IDLE;
                    end else if (in_valid) begin
                        in_ready  <= 1'b0;
                        cur_len   <= in_len;
                        cur_last  <= in_last;
                        c_data_in <= in_data;
                        if (in_aad) begin
                            aad_len <= aad_len + 64'(in_len);
                            cur_aad <= 1'b1;
                            state   <= S_WAITBUF;
                        end else if (in_len == 7'd0) begin
                            // empty-message marker: nothing to encrypt
                            // and nothing more to accept
                            cur_aad  <= 1'b1;
                            cur_last <= 1'b1;
                            state    <= S_WAITBUF;
                        end else begin
                            ct_len    <= ct_len + 64'(in_len);
                            c_counter <= ctr;
                            ctr       <= ctr + 32'd1;
                            c_start   <= 1'b1;
                            cur_aad   <= 1'b0;
                            state     <= S_ENC;
                        end
                    end
                end
                S_ENC: begin
                    c_start <= 1'b0;
                    if (c_done) begin
                        out_valid <= 1'b1;
                        out_data  <= c_data_out;
                        out_len   <= cur_len;
                        state     <= S_WAITBUF;
                    end
                end
                S_WAITBUF: begin
                    if (buf_free) begin
                        // MAC the ciphertext: our output on encrypt, the
                        // input block on decrypt, as RFC 8439 requires.
                        // AAD is MACed as it arrived, so it takes the
                        // same path as decrypt. Either way the bytes past
                        // cur_len are whatever the caller drove, and are
                        // zeroed on the way into poly1305, not here.
                        mac_buf   <= (cur_aad || dec_r)
                                     ? c_data_in : c_data_out;
                        mac_len   <= cur_len;
                        mac_last  <= cur_last;
                        mac_valid <= 1'b1;
                        if (cur_last) begin
                            state <= S_FIN;
                        end else begin
                            in_ready <= 1'b1;
                            state    <= S_ACCEPT;
                        end
                    end
                end
                S_FIN: begin
                    // p_done, not done: the engine must be back in
                    // S_IDLE on the cycle `done` is visible outside
                    if (p_done) begin
                        busy  <= 1'b0;
                        state <= S_IDLE;
                    end
                end
                default: state <= S_IDLE;
            endcase
        end
    end

    // The wrapper's side of the poly1305 handshake is combinational on
    // blk_ready: the sub-block is consumed on the edge after poly1305
    // re-enters S_WAIT, one cycle there instead of two, 36 cycles per
    // 64-byte block instead of 40. Only this side may be unregistered.
    // An early blk_ready in poly1305.sv is the same one-cycle repair
    // from the other end, and the two combined silently corrupt the tag
    // (hw/syn/README.md, "How this pass was found").
    always_comb begin
        p_blk     = 1'b0;
        p_last    = 1'b0;
        p_data_in = '0;
        case (m_state)
            S_M_FEED: begin
                p_blk     = p_blk_ready;
                p_data_in = mask_sub(mac_buf[sub_idx * 128 +: 128], sub_len);
            end
            S_M_LEN: begin
                p_blk     = p_blk_ready;
                p_last    = 1'b1;
                p_data_in = {ct_len, aad_len};
            end
            default: ;
        endcase
    end

    // MAC FSM: drains the buffer into poly1305, then the length block.
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            m_state <= S_M_IDLE;
            done    <= 1'b0;
        end else if (len_bad) begin
            // poly1305 goes into reset on this same edge, so a half-fed
            // block here would wait on a blk_ready that never returns
            m_state <= S_M_IDLE;
            done    <= 1'b0;
        end else begin
            done <= 1'b0;
            case (m_state)
                S_M_IDLE: begin
                    sub_idx <= 2'd0;
                    if (mac_valid) begin
                        if (mac_len == 7'd0)
                            m_state <= mac_last ? S_M_LEN : S_M_IDLE;
                        else
                            m_state <= S_M_FEED;
                    end
                end
                S_M_FEED: begin
                    if (p_blk_ready) begin
                        if (sub_done)
                            m_state <= mac_last ? S_M_LEN : S_M_IDLE;
                        else
                            sub_idx <= sub_idx + 2'd1;
                    end
                end
                S_M_LEN: begin
                    if (p_blk_ready)
                        m_state <= S_M_TAG;
                end
                S_M_TAG: begin
                    if (p_done) begin
                        done    <= 1'b1;
                        m_state <= S_M_IDLE;
                    end
                end
                default: m_state <= S_M_IDLE;
            endcase
        end
    end

endmodule
