// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * Poly1305 one-time authenticator (RFC 8439 section 2.5).
 *
 * Usage: pulse `start` with the 256-bit one-time key (r || s,
 * key[127:0] = r, key[255:128] = s, little-endian words). Then feed the
 * message in 16-byte blocks: while `blk_ready` is high, pulse `blk`
 * with data_in/data_len (1..16 valid bytes, byte 0 = bits [7:0]) and
 * `last` on the final block. `done` pulses with `tag` valid.
 *
 * WARNING: r and s must be single-use (RFC 8439 2.5). The caller is
 * responsible for key generation (e.g. chacha20 block with counter 0).
 *
 * Latency: 3 cycles per block + 1 for the tag.
 */
module poly1305 (
    input  logic         clk,
    input  logic         rst_n,
    input  logic         start,
    input  logic         blk,
    input  logic         last,
    output logic         busy,
    output logic         blk_ready,
    output logic         done,
    input  logic [255:0] key,
    input  logic [127:0] data_in,
    input  logic [  4:0] data_len,
    output logic [127:0] tag
);

    // p = 2^130 - 5
    localparam logic [130:0] P = 131'h3_FFFF_FFFF_FFFF_FFFF_FFFF_FFFF_FFFF_FFFB;
    localparam logic [127:0] CLAMP = 128'h0fff_fffc_0fff_fffc_0fff_fffc_0fff_ffff;

    typedef enum logic [2:0] {S_IDLE, S_WAIT, S_MUL, S_RED, S_TAG} fsm_t;
    fsm_t state;

    logic [127:0] r, s;
    logic [129:0] a;        // accumulator < p
    logic [128:0] n;        // current block as number (registered)
    logic [128:0] n_comb;
    logic [260:0] prod;
    logic         last_r;

    // block bytes as a number, with the 0x01 byte appended (RFC 8439 2.5):
    // n = (data_in masked to data_len bytes) | 1 << (8*data_len)
    logic [7:0]   nbits;
    logic [127:0] masked;
    always_comb begin
        nbits  = {3'b000, data_len} << 3;
        masked = data_in & ({128{1'b1}} >> (8'd128 - nbits));
        n_comb = {1'b0, masked} | (129'd1 << nbits);
    end

    // reduction mod 2^130 - 5: two folds of the high part (x*5), then a
    // single conditional subtract (after two folds the value is < 2p)
    function automatic logic [129:0] reduce(input logic [260:0] x);
        logic [134:0] t1;
        logic [130:0] t2;
        begin
            t1 = {5'b0, x[129:0]} + {1'b0, x[260:130]} * 135'd5;
            t2 = {1'b0, t1[129:0]} + {126'b0, t1[134:130]} * 131'd5;
            if (t2 >= P)
                return t2[129:0] - P[129:0];
            return t2[129:0];
        end
    endfunction

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state     <= S_IDLE;
            busy      <= 1'b0;
            blk_ready <= 1'b0;
            done      <= 1'b0;
        end else begin
            done <= 1'b0;
            case (state)
                S_IDLE: begin
                    if (start) begin
                        r         <= key[127:0] & CLAMP;
                        s         <= key[255:128];
                        a         <= 130'd0;
                        busy      <= 1'b1;
                        blk_ready <= 1'b1;
                        state     <= S_WAIT;
                    end
                end
                S_WAIT: begin
                    if (blk) begin
                        n         <= n_comb;
                        last_r    <= last;
                        blk_ready <= 1'b0;
                        state     <= S_MUL;
                    end
                end
                S_MUL: begin
                    // a + n needs 131 bits: size the sum before widening
                    prod  <= 261'({1'b0, a} + {2'b00, n}) * 261'(r);
                    state <= S_RED;
                end
                S_RED: begin
                    a <= reduce(prod);
                    if (last_r)
                        state <= S_TAG;
                    else begin
                        blk_ready <= 1'b1;
                        state     <= S_WAIT;
                    end
                end
                S_TAG: begin
                    tag  <= a[127:0] + s;
                    busy <= 1'b0;
                    done <= 1'b1;
                    state <= S_IDLE;
                end
                default: state <= S_IDLE;
            endcase
        end
    end

endmodule
