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
 * Datapath: the accumulator and r are five 26-bit digits (5*26 = 130,
 * the width of the modulus). Products landing above bit 130 re-enter the
 * low digits multiplied by 5, because 2^130 = 5 (mod 2^130-5): the
 * reduction is part of the accumulation, not a stage of its own.
 * Digits are kept lazily reduced (under 2^28) between blocks; only the
 * final tag is fully reduced.
 *
 * Cost per block: 1 (add) + ceil(5/ROWS_PER_CYCLE) (multiply rows)
 * + 1 (pipeline drain) + 2 (carry normalisation) cycles, so 9 cycles at
 * the default ROWS_PER_CYCLE = 1, with 5 multipliers.
 *
 * Latency does not depend on the data: no stage iterates on the value of
 * the accumulator, and the final conditional subtract is a fixed-duration
 * combinational choice. This constant-time property is required by
 * SPEC.md and must survive any change.
 *
 * Portability: no vendor primitives are instantiated and multiplier
 * inputs and outputs are registered, so ECP5 MULT18X18D, 7-series
 * DSP48E1 and UltraScale+ DSP48E2 blocks can absorb them.
 *
 * Reset clears r, s, the accumulator, the tag and every intermediate
 * derived from them, not only the control state (Security.md).
 */
module poly1305 #(
    // Rows of r consumed per cycle. 1 costs 5 multipliers and 5 cycles;
    // raise it on devices with DSP to spare (see SPEC.md, OCA-10/50).
    parameter int ROWS_PER_CYCLE = 1
) (
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

    localparam int NL   = 5;                 // digits
    localparam int LW   = 26;                // digit width
    localparam int MCYC = (NL + ROWS_PER_CYCLE - 1) / ROWS_PER_CYCLE;
    // The accumulator vector rotates by ROWS_PER_CYCLE on each of the
    // MCYC-1 accumulation cycles that follow the first, so absolute digit
    // d ends up at local index (d + CSH) % NL. S_C1 reads through CSH to
    // undo it: wiring, not logic.
    localparam int CSH = (NL - (ROWS_PER_CYCLE * (MCYC - 1)) % NL) % NL;
    localparam logic [127:0] CLAMP = 128'h0fff_fffc_0fff_fffc_0fff_fffc_0fff_ffff;

    // p = 2^130 - 5
    localparam logic [130:0] P = 131'h3_FFFF_FFFF_FFFF_FFFF_FFFF_FFFF_FFFF_FFFB;

    logic [LW-1:0] r_d  [NL];    // clamped r, by digit
    logic [LW+2:0] r5_d [NL];    // 5*r_d, < 2^29
    logic [127:0]  s;
    logic [LW+1:0] a_d  [NL];    // accumulator, lazily reduced (< 2^28)
    logic [LW+1:0] sum_d[NL];    // a + n, no carry propagation
    logic [63:0]   t    [NL];    // row accumulators
    logic [63:0]   c1   [NL];    // half-normalised digits, S_C1 -> S_C2
    logic [LW-1:0] f    [NL];    // fully propagated digits, S_FIN -> S_FIN2
    logic [63:0]   fold;         // bit-130 overflow, times 5
    logic [127:0]  a_flat;       // canonical accumulator (< p), low 128 bits
    logic [2:0]    row;          // current multiply cycle, 0..MCYC-1
    logic          last_r;

    // block bytes as a number, with the 0x01 byte appended (RFC 8439 2.5):
    // n = (data_in masked to data_len bytes) | 1 << (8*data_len)
    logic [7:0]   nbits;
    logic [127:0] masked;
    logic [128:0] n_full;
    logic [LW+1:0] n_d [NL];

    always_comb begin
        nbits  = {3'b000, data_len} << 3;
        masked = data_in & ({128{1'b1}} >> (8'd128 - nbits));
        n_full = {1'b0, masked} | (129'd1 << nbits);
        for (int i = 0; i < NL; i++)
            n_d[i] = (LW+2)'((n_full >> (i * LW)) & ((129'd1 << LW) - 129'd1));
    end

    // One row of products per slot, registered on both sides so the DSP
    // blocks can absorb them. The A operand is `sum_d`, a register, and
    // reaches the multiplier with nothing in front of it. The B operand
    // is a five-way choice over r and 5r, and it used to be made in the
    // cycle that multiplied: the ECP5 build put that mux, the haul to
    // the MULT18X18D column and the accumulate that follows it in one
    // 20.86 ns path, which became the critical path of the two-engine
    // top once the protocol engine's own cone was cut. The row index is
    // deterministic, so the choice is made one cycle early instead --
    // for the row that will be multiplied next, not the one being
    // multiplied now. No cycle is added: the operand for row 0 is
    // prepared while the accumulator waits in S_WAIT, and every later
    // row while its predecessor multiplies.
    logic [LW+1:0] mul_a  [ROWS_PER_CYCLE][NL];
    logic [LW+2:0] mul_b  [ROWS_PER_CYCLE][NL];   // registered
    logic [LW+2:0] mul_b_c[ROWS_PER_CYCLE][NL];
    logic [56:0]   prod   [ROWS_PER_CYCLE][NL];

    logic [63:0] t_next [NL];

    always_comb begin
        for (int k = 0; k < NL; k++)
            t_next[k] = t[(k + ROWS_PER_CYCLE) % NL];
        for (int sl = 0; sl < ROWS_PER_CYCLE; sl++)
            for (int i = 0; i < NL; i++)
                t_next[(i + sl) % NL] = t_next[(i + sl) % NL] + 64'(prod[sl][i]);
    end

    typedef enum logic [3:0] {
        S_IDLE, S_WAIT, S_MUL, S_DRAIN, S_C1, S_C2, S_FIN, S_FIN2, S_TAG
    } fsm_t;
    fsm_t state;

    // The row whose operands are being prepared: the next one while the
    // multiply walks, row 0 in every other state, which is what makes the
    // wait before a block double as the first row's preparation. Out of
    // range at the end of the walk, where the padding row takes over.
    logic [2:0] row_nx;
    always_comb row_nx = (state == S_MUL) ? row + 3'd1 : 3'd0;

    always_comb begin
        for (int sl = 0; sl < ROWS_PER_CYCLE; sl++) begin
            automatic int unsigned j = row_nx * ROWS_PER_CYCLE + sl;
            for (int i = 0; i < NL; i++) begin
                mul_a[sl][i] = sum_d[i];
                if (j >= NL)
                    mul_b_c[sl][i] = '0;               // padding row
                else
                    mul_b_c[sl][i] = (i + j >= NL) ? r5_d[j] : {3'b000, r_d[j]};
            end
        end
    end

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state     <= S_IDLE;
            busy      <= 1'b0;
            blk_ready <= 1'b0;
            done      <= 1'b0;
            s         <= '0;
            fold      <= '0;
            a_flat    <= '0;
            tag       <= '0;
            for (int i = 0; i < NL; i++) begin
                r_d[i]   <= '0;
                r5_d[i]  <= '0;
                a_d[i]   <= '0;
                sum_d[i] <= '0;
                t[i]     <= '0;
                c1[i]    <= '0;
                f[i]     <= '0;
            end
            for (int sl = 0; sl < ROWS_PER_CYCLE; sl++)
                for (int i = 0; i < NL; i++) begin
                    mul_b[sl][i] <= '0;
                    prod[sl][i]  <= '0;
                end
        end else begin
            done <= 1'b0;
            // registered operands and products, every cycle: the DSP
            // blocks absorb these registers
            for (int sl = 0; sl < ROWS_PER_CYCLE; sl++)
                for (int i = 0; i < NL; i++) begin
                    mul_b[sl][i] <= mul_b_c[sl][i];
                    prod[sl][i]  <= mul_a[sl][i] * mul_b[sl][i];
                end

            case (state)
                S_IDLE: if (start) begin
                    for (int i = 0; i < NL; i++) begin
                        automatic logic [LW-1:0] rd =
                            LW'(((key[127:0] & CLAMP) >> (i * LW)));
                        r_d[i]  <= rd;
                        r5_d[i] <= (LW+3)'(rd) + ((LW+3)'(rd) << 2);  // *5
                        a_d[i]  <= '0;
                    end
                    s         <= key[255:128];
                    busy      <= 1'b1;
                    blk_ready <= 1'b1;
                    state     <= S_WAIT;
                end
                S_WAIT: if (blk) begin
                    for (int i = 0; i < NL; i++) begin
                        sum_d[i] <= a_d[i] + n_d[i];
                        t[i]     <= '0;
                    end
                    last_r    <= last;
                    blk_ready <= 1'b0;
                    row       <= '0;
                    state     <= S_MUL;
                end
                S_MUL: begin
                    // the first cycle has no products in flight yet
                    if (row != '0)
                        for (int k = 0; k < NL; k++) t[k] <= t_next[k];
                    if (row == 3'(MCYC - 1))
                        state <= S_DRAIN;
                    row <= row + 3'd1;
                end
                S_DRAIN: begin
                    for (int k = 0; k < NL; k++) t[k] <= t_next[k];
                    state <= S_C1;
                end
                S_C1: begin
                    automatic logic [63:0] carry = '0;
                    for (int k = 0; k < 3; k++) begin
                        automatic logic [63:0] v = t[(k + CSH) % NL] + carry;
                        c1[k] <= {38'd0, v[LW-1:0]};
                        carry  = v >> LW;
                    end
                    c1[3] <= t[(3 + CSH) % NL] + carry;
                    c1[4] <= t[(4 + CSH) % NL];
                    state <= S_C2;
                end
                S_C2: begin
                    automatic logic [63:0] v3 = c1[3];
                    automatic logic [63:0] v4 = c1[4] + (v3 >> LW);
                    // carry out of digit 4 is the bit-130 overflow: it
                    // re-enters digit 0 multiplied by 5
                    automatic logic [63:0] v0 =
                        c1[0] + (((v4 >> LW) << 2) + (v4 >> LW));
                    a_d[0] <= (LW+2)'(v0[LW-1:0]);
                    a_d[1] <= (LW+2)'(c1[1] + (v0 >> LW));
                    a_d[2] <= (LW+2)'(c1[2]);
                    a_d[3] <= (LW+2)'(v3[LW-1:0]);
                    a_d[4] <= (LW+2)'(v4[LW-1:0]);
                    if (last_r) begin
                        state <= S_FIN;
                    end else begin
                        blk_ready <= 1'b1;
                        state     <= S_WAIT;
                    end
                end
                S_FIN: begin
                    // no laziness left: propagate every digit
                    automatic logic [63:0] carry = '0;
                    for (int k = 0; k < NL; k++) begin
                        automatic logic [63:0] v = {36'd0, a_d[k]} + carry;
                        f[k]  <= v[LW-1:0];
                        carry  = v >> LW;
                    end
                    fold  <= (carry << 2) + carry;   // *5
                    state <= S_FIN2;
                end
                S_FIN2: begin
                    automatic logic [130:0] flat = 131'(fold);
                    for (int k = 0; k < NL; k++)
                        flat = flat + (131'(f[k]) << (k * LW));
                    // flat < 2p here, so one conditional subtract is enough
                    a_flat <= 128'((flat >= P) ? (flat - P) : flat);
                    state  <= S_TAG;
                end
                S_TAG: begin
                    tag   <= a_flat[127:0] + s;
                    busy  <= 1'b0;
                    done  <= 1'b1;
                    state <= S_IDLE;
                end
                default: state <= S_IDLE;
            endcase
        end
    end

endmodule
