// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * Packet buffer for the OCA host protocol.
 *
 * One byte wide, written sequentially from the packet stream and read
 * at random offsets. Deliberately the simplest thing that works: at one
 * byte per cycle a 64-byte block takes 64 cycles to read out against the
 * AEAD engine's 40 to process it, so the buffer, not the engine, sets
 * the pace. Measured end to end, a 64-byte block costs 415 cycles:
 * 64 in, 159 through buffer and engine, and 192 out, because the
 * response stream re-fetches through the registered read port and
 * spends three cycles a byte. That is about 0.062 Gbps against the
 * engine's own 0.68, a fifteenth of it, and 6% of the GbE link.
 *
 * This is the largest deliberate simplification in the design, not an
 * oversight. Note where the cost actually sits: the response handshake
 * dominates, not this memory's width, so the first optimisation is that
 * state machine (measured at 287 cycles, +45%) rather than a wider
 * buffer (docs/design/2026-08-03-host-protocol.md).
 *
 * Writes past BYTES are dropped and wr_full is raised: a truncated
 * packet becomes a length error at the protocol layer rather than a
 * silent wrap that corrupts what is already stored.
 *
 * Reads are registered, one byte per cycle, so the memory infers block
 * RAM rather than LUT RAM (SPEC.md portability rule: inferred, never
 * instantiated).
 */
module oca_pktbuf #(
    parameter int BYTES = 2048
) (
    input  logic        clk,
    input  logic        rst_n,
    input  logic        wr_en,
    input  logic [ 7:0] wr_data,
    input  logic        wr_clear,
    output logic [11:0] wr_count,
    output logic        wr_full,
    input  logic [11:0] rd_addr,
    output logic [ 7:0] rd_data
);

    // Offsets are carried as a 12-bit protocol quantity; the memory is
    // indexed by the narrow form, which is only ever used once the wide
    // one has been range-checked.
    localparam int ADDR_W = $clog2(BYTES);

    logic [7:0] mem [BYTES];

    logic [ADDR_W-1:0] rd_index;

    always_comb wr_full = (wr_count >= 12'(BYTES));

    // The range check sits on the address, not on the read data: a
    // multiplexer between the memory and its output register would stop
    // the read port being registered, and with it the block RAM.
    always_comb rd_index = (rd_addr < 12'(BYTES)) ? rd_addr[ADDR_W-1:0]
                                                  : '0;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            wr_count <= '0;
            rd_data  <= '0;
        end else begin
            if (wr_clear) begin
                wr_count <= '0;
            end else if (wr_en && !wr_full) begin
                mem[wr_count[ADDR_W-1:0]] <= wr_data;
                wr_count                  <= wr_count + 12'd1;
            end
            rd_data <= mem[rd_index];
        end
    end

endmodule
