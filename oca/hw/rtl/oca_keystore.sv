// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * Key slots for the OCA host protocol.
 *
 * NUM_SLOTS one-time keys, written by the load-key command and read by
 * index. This is the only place key material lives.
 *
 * A slot carries a loaded bit: reading a slot that was never written
 * reports rd_valid = 0 rather than handing back a key of zeros, so a
 * host mistake becomes a protocol error instead of a message encrypted
 * under a key an attacker can guess.
 *
 * Reset clears both the keys and the loaded bits (Security.md).
 */
module oca_keystore #(
    parameter int NUM_SLOTS = 8
) (
    input  logic         clk,
    input  logic         rst_n,
    input  logic         wr_en,
    input  logic [  7:0] wr_slot,
    input  logic [255:0] wr_key,
    input  logic [  7:0] rd_slot,
    output logic [255:0] rd_key,
    output logic         rd_valid
);

    // Slot numbers are a protocol byte; the arrays are indexed by the
    // narrow form, which is only ever used once the full byte has been
    // range-checked.
    localparam int SLOT_W = $clog2(NUM_SLOTS);

    logic [255:0] keys   [NUM_SLOTS];
    logic         loaded [NUM_SLOTS];

    logic in_range;
    always_comb in_range = (rd_slot < 8'(NUM_SLOTS));

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            for (int i = 0; i < NUM_SLOTS; i++) begin
                keys[i]   <= '0;
                loaded[i] <= 1'b0;
            end
            rd_key   <= '0;
            rd_valid <= 1'b0;
        end else begin
            if (wr_en && wr_slot < 8'(NUM_SLOTS)) begin
                keys[wr_slot[SLOT_W-1:0]]   <= wr_key;
                loaded[wr_slot[SLOT_W-1:0]] <= 1'b1;
            end
            rd_key   <= in_range ? keys[rd_slot[SLOT_W-1:0]]   : '0;
            rd_valid <= in_range ? loaded[rd_slot[SLOT_W-1:0]] : 1'b0;
        end
    end

endmodule
