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
 * Decryption (dec=1): feed the ciphertext instead of the plaintext;
 * the output stream is the recovered plaintext and the MAC is computed
 * over the input blocks (the ciphertext), as RFC 8439 requires. The
 * caller must compare `tag` with the received tag and discard the
 * plaintext on mismatch.
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
    output logic [127:0] tag
);

    typedef enum logic [3:0] {
        S_IDLE, S_KEY, S_KEYP, S_RUN, S_ENC,
        S_MAC_W, S_MAC_P, S_LEN, S_LEN_P, S_TAG
    } fsm_t;
    fsm_t state;

    logic [255:0] key_r;
    logic [ 95:0] nonce_r;
    logic         dec_r;
    logic [ 31:0] ctr;
    logic [ 63:0] aad_len, ct_len;
    logic [511:0] src;          // bytes to MAC for the current block
    logic [  6:0] cur_len;
    logic         cur_last;
    logic [  1:0] sub_idx;

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

    poly1305 u_poly (
        .clk,
        .rst_n,
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

    // zero all bytes at index >= len
    function automatic logic [511:0] mask_bytes(
        input logic [511:0] d, input logic [6:0] len
    );
        logic [511:0] m;
        begin
            if (len >= 7'd64)
                m = {512{1'b1}};
            else
                m = (512'd1 << (len * 8)) - 512'd1;
            return d & m;
        end
    endfunction

    // number of 16-byte poly1305 sub-blocks for cur_len bytes
    logic [2:0] sub_cnt;
    always_comb sub_cnt = 3'((cur_len + 7'd15) >> 4);

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state     <= S_IDLE;
            busy      <= 1'b0;
            in_ready  <= 1'b0;
            out_valid <= 1'b0;
            done      <= 1'b0;
            c_start   <= 1'b0;
            p_start   <= 1'b0;
            p_blk     <= 1'b0;
            p_last    <= 1'b0;
        end else begin
            out_valid <= 1'b0;
            done      <= 1'b0;
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
                    state    <= S_RUN;
                end
                S_RUN: begin
                    if (in_valid) begin
                        in_ready <= 1'b0;
                        cur_len  <= in_len;
                        cur_last <= in_last;
                        sub_idx  <= 2'd0;
                        if (in_aad) begin
                            aad_len <= aad_len + 64'(in_len);
                            src     <= mask_bytes(in_data, in_len);
                            if (in_len == 7'd0) begin
                                in_ready <= !in_last;
                                state    <= in_last ? S_LEN : S_RUN;
                            end else begin
                                state <= S_MAC_W;
                            end
                        end else if (in_len == 7'd0) begin
                            state <= S_LEN;  // empty-message marker
                        end else begin
                            ct_len    <= ct_len + 64'(in_len);
                            c_counter <= ctr;
                            ctr       <= ctr + 32'd1;
                            c_data_in <= mask_bytes(in_data, in_len);
                            c_start   <= 1'b1;
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
                        // MAC the ciphertext: our output on encrypt,
                        // the input block on decrypt (c_data_in is
                        // already masked to cur_len bytes)
                        src       <= dec_r ? c_data_in
                                           : mask_bytes(c_data_out, cur_len);
                        state     <= S_MAC_W;
                    end
                end
                S_MAC_W: begin
                    if (p_blk_ready) begin
                        p_blk     <= 1'b1;
                        p_data_in <= src[sub_idx * 128 +: 128];
                        p_last    <= 1'b0;
                        state     <= S_MAC_P;
                    end
                end
                S_MAC_P: begin
                    p_blk <= 1'b0;
                    if ({1'b0, sub_idx} == sub_cnt - 3'd1) begin
                        if (cur_last) begin
                            state <= S_LEN;
                        end else begin
                            in_ready <= 1'b1;
                            state    <= S_RUN;
                        end
                    end else begin
                        sub_idx <= sub_idx + 2'd1;
                        state   <= S_MAC_W;
                    end
                end
                S_LEN: begin
                    if (p_blk_ready) begin
                        p_blk     <= 1'b1;
                        p_last    <= 1'b1;
                        p_data_in <= {ct_len, aad_len};
                        state     <= S_LEN_P;
                    end
                end
                S_LEN_P: begin
                    p_blk  <= 1'b0;
                    p_last <= 1'b0;
                    state  <= S_TAG;
                end
                S_TAG: begin
                    if (p_done) begin
                        done  <= 1'b1;
                        busy  <= 1'b0;
                        state <= S_IDLE;
                    end
                end
                default: state <= S_IDLE;
            endcase
        end
    end

endmodule
