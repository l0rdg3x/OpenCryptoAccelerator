// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * ChaCha20 stream cipher core (RFC 8439 section 2.3/2.4).
 *
 * One 64-byte block per operation: pulse `start` with key/nonce/counter
 * and data_in stable; `done` pulses 12 clock cycles later with data_out
 * valid. The caller increments `counter` between blocks (RFC 8439 2.4).
 *
 * Bus layout (little-endian, matching the RFC's word encoding):
 *   key[i*32 +: 32]      = key word i   (key byte 4i is bits [7:0])
 *   nonce[i*32 +: 32]    = nonce word i
 *   data_in/out[i*32+:32]= data word i  (byte 4i is bits [7:0])
 * In other words: drive/read these buses with int.from_bytes(x, "little").
 *
 * Latency: 1 (load) + 10 (20 rounds, 2 rounds/cycle) + 1 (serialize) cycles.
 */
module chacha20 (
    input  logic         clk,
    input  logic         rst_n,
    input  logic         start,
    output logic         busy,
    output logic         done,
    input  logic [255:0] key,
    input  logic [ 95:0] nonce,
    input  logic [ 31:0] counter,
    input  logic [511:0] data_in,
    output logic [511:0] data_out
);

    // Quarter round (RFC 8439 2.1). Returns {a', b', c', d'}.
    function automatic logic [127:0] qr(
        input logic [31:0] a, input logic [31:0] b,
        input logic [31:0] c, input logic [31:0] d
    );
        logic [31:0] a1, b1, c1, d1;
        begin
            a1 = a + b;
            d1 = d ^ a1; d1 = {d1[15:0], d1[31:16]}; // rotl 16
            c1 = c + d1;
            b1 = b ^ c1; b1 = {b1[19:0], b1[31:20]}; // rotl 12
            a1 = a1 + b1;
            d1 = d1 ^ a1; d1 = {d1[23:0], d1[31:24]}; // rotl 8
            c1 = c1 + d1;
            b1 = b1 ^ c1; b1 = {b1[24:0], b1[31:25]}; // rotl 7
            return {a1, b1, c1, d1};
        end
    endfunction

    // Column round + diagonal round (2 of the 20 rounds).
    function automatic logic [511:0] double_round(input logic [511:0] s);
        logic [511:0] c, o;
        logic [127:0] q;
        begin
            // column round
            q = qr(s[ 0*32 +: 32], s[ 4*32 +: 32], s[ 8*32 +: 32], s[12*32 +: 32]);
            c[ 0*32 +: 32] = q[127:96]; c[ 4*32 +: 32] = q[95:64];
            c[ 8*32 +: 32] = q[ 63:32]; c[12*32 +: 32] = q[31: 0];
            q = qr(s[ 1*32 +: 32], s[ 5*32 +: 32], s[ 9*32 +: 32], s[13*32 +: 32]);
            c[ 1*32 +: 32] = q[127:96]; c[ 5*32 +: 32] = q[95:64];
            c[ 9*32 +: 32] = q[ 63:32]; c[13*32 +: 32] = q[31: 0];
            q = qr(s[ 2*32 +: 32], s[ 6*32 +: 32], s[10*32 +: 32], s[14*32 +: 32]);
            c[ 2*32 +: 32] = q[127:96]; c[ 6*32 +: 32] = q[95:64];
            c[10*32 +: 32] = q[ 63:32]; c[14*32 +: 32] = q[31: 0];
            q = qr(s[ 3*32 +: 32], s[ 7*32 +: 32], s[11*32 +: 32], s[15*32 +: 32]);
            c[ 3*32 +: 32] = q[127:96]; c[ 7*32 +: 32] = q[95:64];
            c[11*32 +: 32] = q[ 63:32]; c[15*32 +: 32] = q[31: 0];
            // diagonal round
            q = qr(c[ 0*32 +: 32], c[ 5*32 +: 32], c[10*32 +: 32], c[15*32 +: 32]);
            o[ 0*32 +: 32] = q[127:96]; o[ 5*32 +: 32] = q[95:64];
            o[10*32 +: 32] = q[ 63:32]; o[15*32 +: 32] = q[31: 0];
            q = qr(c[ 1*32 +: 32], c[ 6*32 +: 32], c[11*32 +: 32], c[12*32 +: 32]);
            o[ 1*32 +: 32] = q[127:96]; o[ 6*32 +: 32] = q[95:64];
            o[11*32 +: 32] = q[ 63:32]; o[12*32 +: 32] = q[31: 0];
            q = qr(c[ 2*32 +: 32], c[ 7*32 +: 32], c[ 8*32 +: 32], c[13*32 +: 32]);
            o[ 2*32 +: 32] = q[127:96]; o[ 7*32 +: 32] = q[95:64];
            o[ 8*32 +: 32] = q[ 63:32]; o[13*32 +: 32] = q[31: 0];
            q = qr(c[ 3*32 +: 32], c[ 4*32 +: 32], c[ 9*32 +: 32], c[14*32 +: 32]);
            o[ 3*32 +: 32] = q[127:96]; o[ 4*32 +: 32] = q[95:64];
            o[ 9*32 +: 32] = q[ 63:32]; o[14*32 +: 32] = q[31: 0];
            return o;
        end
    endfunction

    typedef enum logic [1:0] {S_IDLE, S_RUN, S_FINISH} fsm_t;
    fsm_t       state;
    logic [511:0] st;       // working state, 16 words
    logic [511:0] st_init;  // snapshot for the final addition
    logic [3:0]   round_cnt;

    // "expand 32-byte k" || key || counter || nonce, word-wise
    function automatic logic [511:0] init_state(
        input logic [255:0] k, input logic [95:0] n, input logic [31:0] ctr
    );
        logic [511:0] s;
        begin
            s[ 0*32 +: 32] = 32'h6170_7865;
            s[ 1*32 +: 32] = 32'h3320_646e;
            s[ 2*32 +: 32] = 32'h7962_2d32;
            s[ 3*32 +: 32] = 32'h6b20_6574;
            for (int i = 0; i < 8; i++)
                s[(4 + i)*32 +: 32] = k[i*32 +: 32];
            s[12*32 +: 32] = ctr;
            s[13*32 +: 32] = n[ 0*32 +: 32];
            s[14*32 +: 32] = n[ 1*32 +: 32];
            s[15*32 +: 32] = n[ 2*32 +: 32];
            return s;
        end
    endfunction

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state     <= S_IDLE;
            busy      <= 1'b0;
            done      <= 1'b0;
            round_cnt <= 4'd0;
        end else begin
            done <= 1'b0;
            case (state)
                S_IDLE: begin
                    if (start) begin
                        st        <= init_state(key, nonce, counter);
                        st_init   <= init_state(key, nonce, counter);
                        busy      <= 1'b1;
                        round_cnt <= 4'd0;
                        state     <= S_RUN;
                    end
                end
                S_RUN: begin
                    st        <= double_round(st);
                    round_cnt <= round_cnt + 4'd1;
                    if (round_cnt == 4'd9)
                        state <= S_FINISH;
                end
                S_FINISH: begin
                    for (int i = 0; i < 16; i++)
                        data_out[i*32 +: 32] <=
                            (st[i*32 +: 32] + st_init[i*32 +: 32]) ^ data_in[i*32 +: 32];
                    busy  <= 1'b0;
                    done  <= 1'b1;
                    state <= S_IDLE;
                end
                default: state <= S_IDLE;
            endcase
        end
    end

endmodule
