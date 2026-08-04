// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * Packet buffer for the OCA host protocol.
 *
 * 64 bits wide, written sequentially from the packet stream and read at
 * random word offsets. A 64-byte block costs 8 reads plus the read
 * pipeline, against the AEAD engine's 40 cycles to process one, so the
 * buffer no longer sets the pace: at one byte per cycle it needed 66
 * cycles to assemble a block the engine consumed in 40, which is why the
 * datapath moved to 64 bits (amendment of 2026-08-04 in
 * docs/design/2026-08-03-host-protocol.md).
 *
 * wr_count stays a byte count because rx_len, want_len and resp_len all
 * compare against it, and that comparison is what keeps a field from
 * being read out of bytes the packet never wrote. wr_bytes carries 1..8
 * valid bytes, so only the final write of a packet may be partial: a
 * partial write mid-stream leaves wr_count off a word boundary and the
 * next write lands back on the word just written. oca_proto fails such a
 * packet closed rather than issue one.
 *
 * Writes past BYTES are dropped and wr_full is raised: a truncated
 * packet becomes a length error at the protocol layer rather than a
 * silent wrap that corrupts what is already stored.
 *
 * Reads are registered, one word per cycle, and there is exactly one
 * write port and one read port, which is what lets yosys map the memory
 * through $__PDPW16KD_ — pseudo dual-port, 36 bits wide — at 2 DP16KD
 * per buffer. A second read port, an asynchronous read, or a
 * multiplexer between the memory and its output register would take it
 * to 4 DP16KD or into LUT RAM (SPEC.md portability rule: inferred,
 * never instantiated).
 *
 * Not obvious, and someone will want it: that 36-bit mode is 512 x 36
 * and only 256 words are used, so BYTES could go from 2048 to 4096 at
 * zero block-RAM cost.
 */
module oca_pktbuf #(
    parameter int BYTES = 2048
) (
    input  logic        clk,
    input  logic        rst_n,
    input  logic        wr_en,
    input  logic [63:0] wr_data,
    input  logic [ 3:0] wr_bytes,
    input  logic        wr_clear,
    output logic [11:0] wr_count,
    output logic        wr_full,
    input  logic [ 8:0] rd_addr,
    output logic [63:0] rd_data
);

    // Offsets are carried as a 12-bit byte quantity at the protocol
    // layer; the memory is indexed by the word address derived from it,
    // which is only ever used once the wide one has been range-checked.
    localparam int WORDS  = BYTES / 8;
    localparam int ADDR_W = $clog2(WORDS);

    logic [63:0] mem [WORDS];

    logic [ADDR_W-1:0] rd_index;

    always_comb wr_full = (wr_count >= 12'(BYTES));

    // The range check sits on the address, not on the read data: a
    // multiplexer between the memory and its output register would stop
    // the read port being registered, and with it the block RAM.
    always_comb rd_index = (rd_addr < 9'(WORDS)) ? rd_addr[ADDR_W-1:0]
                                                 : '0;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            wr_count <= '0;
            rd_data  <= '0;
        end else begin
            if (wr_clear) begin
                wr_count <= '0;
            end else if (wr_en && !wr_full) begin
                mem[wr_count[ADDR_W+2:3]] <= wr_data;
                wr_count                  <= wr_count + 12'(wr_bytes);
            end
            rd_data <= mem[rd_index];
        end
    end

endmodule
