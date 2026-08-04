// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * OCA host-protocol core: one UDP payload in, one UDP payload out.
 *
 * Wiring, and one gate. The two packet buffers, the key store, the
 * protocol FSM and the AEAD engine each keep their own responsibility,
 * so that the boundary the Ethernet integration attaches to is a plain
 * AXI-Stream pair (docs/design/2026-08-03-host-protocol.md).
 *
 * That pair is 64 bits wide with a byte-enable, and the width conversion
 * to the 8 bits verilog-ethernet hands over belongs outside this module
 * (amendment of 2026-08-04 in the same document): inside it, 8 bits
 * could not feed one engine.
 *
 * The gate is the request stream, held off while either packet buffer
 * zeroes itself out of reset. It sits here rather than in oca_proto
 * because it is a property of the buffers, and because oca_proto's four
 * interacting stages have no state that means "not yet": the receive
 * stage would have to learn one, and every other stage would have to
 * agree with it.
 *
 * Both halves of the handshake are masked, not just the tready leaving
 * the core. AXI-Stream forbids a master from waiting for tready before
 * it asserts tvalid, so a request can be sitting on the bus from the
 * cycle reset ends; oca_proto's receive stage reaches its accepting
 * state one cycle after reset and would consume beats out of it that
 * the master never saw taken — dropped by the buffer's clear, and
 * offered again by the master, which is a packet corrupted in both
 * directions at once. With s_tvalid masked as well, nothing of the
 * request exists for oca_proto until the buffers are usable.
 */
module oca_core #(
    parameter int NUM_SLOTS = 8,
    parameter int BYTES     = 2048
) (
    input  logic        clk,
    input  logic        rst_n,
    // request payload in
    input  logic [63:0] s_axis_tdata,
    input  logic [ 7:0] s_axis_tkeep,
    input  logic        s_axis_tvalid,
    output logic        s_axis_tready,
    input  logic        s_axis_tlast,
    // response payload out
    output logic [63:0] m_axis_tdata,
    output logic [ 7:0] m_axis_tkeep,
    output logic        m_axis_tvalid,
    input  logic        m_axis_tready,
    output logic        m_axis_tlast
);

    logic        rx_clr_busy, tx_clr_busy, buf_ready;
    logic        proto_s_tready, proto_s_tvalid;

    always_comb buf_ready      = !rx_clr_busy && !tx_clr_busy;
    always_comb s_axis_tready  = proto_s_tready && buf_ready;
    always_comb proto_s_tvalid = s_axis_tvalid && buf_ready;

    logic        rx_wr_bank, rx_wr_en, rx_wr_clear, rx_rd_bank, rx_rd_full;
    logic [63:0] rx_wr_data, rx_rd_data;
    logic [ 3:0] rx_wr_bytes;
    logic [11:0] rx_wr_count, rx_rd_count;
    logic [ 8:0] rx_rd_addr;

    logic        tx_wr_bank, tx_wr_en, tx_wr_clear, tx_rd_bank, tx_rd_full;
    logic [63:0] tx_wr_data, tx_rd_data;
    logic [ 3:0] tx_wr_bytes;
    logic [11:0] tx_wr_count, tx_rd_count;
    logic [ 8:0] tx_rd_addr;

    logic         ks_wr_en, ks_rd_valid;
    logic [  7:0] ks_wr_slot, ks_rd_slot;
    logic [255:0] ks_wr_key, ks_rd_key;

    logic         eng_start, eng_dec, eng_busy;
    logic [255:0] eng_key;
    logic [ 95:0] eng_nonce;
    logic         eng_in_valid, eng_in_aad, eng_in_last, eng_in_ready;
    logic [  6:0] eng_in_len, eng_out_len;
    logic [511:0] eng_in_data, eng_out_data;
    logic         eng_out_valid, eng_done, eng_err;
    logic [127:0] eng_tag;

    // The receive buffer's writer needs no count (oca_pktbuf drops past
    // capacity on its own) and nothing reads the transmit buffer's byte
    // count or full flag on the read side.
    logic unused_ok;
    assign unused_ok = (|rx_wr_count) | (|tx_rd_count) | tx_rd_full;

    oca_pktbuf #(.BYTES(BYTES)) u_rxbuf (
        .clk,
        .rst_n,
        .wr_bank (rx_wr_bank),
        .wr_en   (rx_wr_en),
        .wr_data (rx_wr_data),
        .wr_bytes(rx_wr_bytes),
        .wr_clear(rx_wr_clear),
        .wr_count(rx_wr_count),
        .rd_bank (rx_rd_bank),
        .rd_addr (rx_rd_addr),
        .rd_data (rx_rd_data),
        .rd_count(rx_rd_count),
        .rd_full (rx_rd_full),
        .clr_busy(rx_clr_busy)
    );

    oca_pktbuf #(.BYTES(BYTES)) u_txbuf (
        .clk,
        .rst_n,
        .wr_bank (tx_wr_bank),
        .wr_en   (tx_wr_en),
        .wr_data (tx_wr_data),
        .wr_bytes(tx_wr_bytes),
        .wr_clear(tx_wr_clear),
        .wr_count(tx_wr_count),
        .rd_bank (tx_rd_bank),
        .rd_addr (tx_rd_addr),
        .rd_data (tx_rd_data),
        .rd_count(tx_rd_count),
        .rd_full (tx_rd_full),
        .clr_busy(tx_clr_busy)
    );

    oca_keystore #(.NUM_SLOTS(NUM_SLOTS)) u_keystore (
        .clk,
        .rst_n,
        .wr_en   (ks_wr_en),
        .wr_slot (ks_wr_slot),
        .wr_key  (ks_wr_key),
        .rd_slot (ks_rd_slot),
        .rd_key  (ks_rd_key),
        .rd_valid(ks_rd_valid)
    );

    chacha20_poly1305 u_aead (
        .clk,
        .rst_n,
        .start    (eng_start),
        .dec      (eng_dec),
        .busy     (eng_busy),
        .key      (eng_key),
        .nonce    (eng_nonce),
        .in_valid (eng_in_valid),
        .in_aad   (eng_in_aad),
        .in_last  (eng_in_last),
        .in_len   (eng_in_len),
        .in_data  (eng_in_data),
        .in_ready (eng_in_ready),
        .out_valid(eng_out_valid),
        .out_data (eng_out_data),
        .out_len  (eng_out_len),
        .done     (eng_done),
        .tag      (eng_tag),
        .err      (eng_err)
    );

    oca_proto #(.NUM_SLOTS(NUM_SLOTS), .BYTES(BYTES)) u_proto (
        .clk,
        .rst_n,
        .s_tdata     (s_axis_tdata),
        .s_tkeep     (s_axis_tkeep),
        .s_tvalid    (proto_s_tvalid),
        .s_tready    (proto_s_tready),
        .s_tlast     (s_axis_tlast),
        .m_tdata     (m_axis_tdata),
        .m_tkeep     (m_axis_tkeep),
        .m_tvalid    (m_axis_tvalid),
        .m_tready    (m_axis_tready),
        .m_tlast     (m_axis_tlast),
        .rx_wr_bank  (rx_wr_bank),
        .rx_wr_en    (rx_wr_en),
        .rx_wr_data  (rx_wr_data),
        .rx_wr_bytes (rx_wr_bytes),
        .rx_wr_clear (rx_wr_clear),
        .rx_rd_bank  (rx_rd_bank),
        .rx_rd_addr  (rx_rd_addr),
        .rx_rd_data  (rx_rd_data),
        .rx_rd_count (rx_rd_count),
        .rx_rd_full  (rx_rd_full),
        .tx_wr_bank  (tx_wr_bank),
        .tx_wr_en    (tx_wr_en),
        .tx_wr_data  (tx_wr_data),
        .tx_wr_bytes (tx_wr_bytes),
        .tx_wr_clear (tx_wr_clear),
        .tx_wr_count (tx_wr_count),
        .tx_rd_bank  (tx_rd_bank),
        .tx_rd_addr  (tx_rd_addr),
        .tx_rd_data  (tx_rd_data),
        .ks_wr_en    (ks_wr_en),
        .ks_wr_slot  (ks_wr_slot),
        .ks_wr_key   (ks_wr_key),
        .ks_rd_slot  (ks_rd_slot),
        .ks_rd_key   (ks_rd_key),
        .ks_rd_valid (ks_rd_valid),
        .eng_start   (eng_start),
        .eng_dec     (eng_dec),
        .eng_busy    (eng_busy),
        .eng_key     (eng_key),
        .eng_nonce   (eng_nonce),
        .eng_in_valid(eng_in_valid),
        .eng_in_aad  (eng_in_aad),
        .eng_in_last (eng_in_last),
        .eng_in_len  (eng_in_len),
        .eng_in_data (eng_in_data),
        .eng_in_ready(eng_in_ready),
        .eng_out_valid(eng_out_valid),
        .eng_out_data(eng_out_data),
        .eng_out_len (eng_out_len),
        .eng_done    (eng_done),
        .eng_tag     (eng_tag),
        .eng_err     (eng_err)
    );

endmodule
