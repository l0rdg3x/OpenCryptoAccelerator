// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * ChaCha20 stream cipher core (RFC 8439 section 2.3/2.4).
 *
 * One 64-byte block per operation: pulse `start` with key/nonce/counter
 * and data_in stable; `done` pulses 22 clock cycles later with data_out
 * valid. The caller increments `counter` between blocks (RFC 8439 2.4).
 *
 * Bus layout (little-endian, matching the RFC's word encoding):
 *   key[i*32 +: 32]      = key word i   (key byte 4i is bits [7:0])
 *   nonce[i*32 +: 32]    = nonce word i
 *   data_in/out[i*32+:32]= data word i  (byte 4i is bits [7:0])
 * In other words: drive/read these buses with int.from_bytes(x, "little").
 *
 * Datapath: the 20 rounds alternate a column round and a diagonal round
 * (RFC 8439 2.3.1), one state register shared by both. ROUNDS_PER_CYCLE
 * chooses how many rounds a single cycle covers: it trades the length of
 * the combinational path, and so the achievable clock, against the
 * number of cycles a block costs. Only 1 and 2 are implemented; any
 * other value is rejected at elaboration (see the guard below).
 *
 * Latency: 1 (load) + 20/ROUNDS_PER_CYCLE (rounds) + 1 (serialize)
 * cycles, so 22 cycles at the default ROUNDS_PER_CYCLE = 1.
 */
module chacha20 #(
    // Rounds computed per cycle. 1 halves the combinational path (22
    // cycles per block); 2 is the original behaviour (12 cycles) and
    // suits devices that can clock it. No other value is supported.
    parameter int ROUNDS_PER_CYCLE = 1
) (
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

    // Column round: quarter rounds on the four columns (RFC 8439 2.3.1).
    function automatic logic [511:0] column_round(input logic [511:0] s);
        logic [511:0] c;
        logic [127:0] q;
        begin
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
            return c;
        end
    endfunction

    // Diagonal round: quarter rounds on the four diagonals.
    function automatic logic [511:0] diagonal_round(input logic [511:0] c);
        logic [511:0] o;
        logic [127:0] q;
        begin
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

    // NCYCLE divides for any divisor of 20, but the FSM below only ever
    // composes two rounds in a cycle for the literal 2 and one round
    // otherwise: any other value would run NCYCLE single rounds and emit
    // wrong keystream. Stop the build rather than the crypto.
    if (ROUNDS_PER_CYCLE != 1 && ROUNDS_PER_CYCLE != 2) begin : gen_bad_rounds
        $fatal(1, "chacha20: ROUNDS_PER_CYCLE must be 1 or 2");
    end

    localparam int NROUND = 20;
    localparam int NCYCLE = NROUND / ROUNDS_PER_CYCLE;

    typedef enum logic [1:0] {S_IDLE, S_RUN, S_FINISH} fsm_t;
    fsm_t       state;
    logic [511:0] st;       // working state, 16 words
    logic [511:0] st_init;  // snapshot for the final addition
    logic [4:0]   round_cnt;

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
            round_cnt <= 5'd0;
        end else begin
            done <= 1'b0;
            case (state)
                S_IDLE: begin
                    if (start) begin
                        st        <= init_state(key, nonce, counter);
                        st_init   <= init_state(key, nonce, counter);
                        busy      <= 1'b1;
                        round_cnt <= 5'd0;
                        state     <= S_RUN;
                    end
                end
                S_RUN: begin
                    if (ROUNDS_PER_CYCLE == 2)
                        st <= diagonal_round(column_round(st));
                    else
                        st <= round_cnt[0] ? diagonal_round(st)
                                           : column_round(st);
                    round_cnt <= round_cnt + 5'd1;
                    if (round_cnt == 5'(NCYCLE - 1))
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
