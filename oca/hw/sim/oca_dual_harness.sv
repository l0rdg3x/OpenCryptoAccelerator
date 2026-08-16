// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * Simulation harness for the dual-core fabric: oca_dispatch, two
 * oca_core, oca_collect — the middle of oca_uart_crypto_dual with the
 * serial front end cut away, so the suite drives the fabric at the
 * 64-bit stream the decoder drives it at in the assembled design.
 *
 * It lives in hw/sim/ because nothing builds it into a bitstream: the
 * instance names and the wiring are oca_uart_crypto_dual's, member for
 * member, so a hierarchical peek written against this harness reads
 * the same path in the real top, and a change to the top's fabric
 * wiring that this file does not mirror is a harness testing a design
 * nobody loads. The front end it omits is proved byte by byte in
 * test_uart_crypto.py and once more, through this very top, by the
 * smoke test in test_uart_crypto_dual.py.
 */
`default_nettype none

module oca_dual_harness #(
    parameter int BYTES = 2048
) (
    input  var logic        clk,
    input  var logic        rst_n,

    // One request frame per tlast: what oca_slip_rx emits.
    input  var logic [63:0] s_tdata,
    input  var logic [ 7:0] s_tkeep,
    input  var logic        s_tvalid,
    output var logic        s_tready,
    input  var logic        s_tlast,

    // The one response stream: what oca_slip_tx consumes.
    output var logic [63:0] m_tdata,
    output var logic [ 7:0] m_tkeep,
    output var logic        m_tvalid,
    input  var logic        m_tready,
    output var logic        m_tlast,

    // oca_collect's sticky fault latch, unwrapped.
    output var logic        trouble
);

    logic [63:0] d_tdata;
    logic [ 7:0] d_tkeep;
    logic        d_tlast;
    logic        d0_tvalid, d0_tready, d1_tvalid, d1_tready;
    logic        push0, push1, tag_full0, tag_full1;

    oca_dispatch u_dispatch (
        .clk       (clk),
        .rst_n     (rst_n),
        .s_tdata   (s_tdata),
        .s_tkeep   (s_tkeep),
        .s_tvalid  (s_tvalid),
        .s_tready  (s_tready),
        .s_tlast   (s_tlast),
        .m_tdata   (d_tdata),
        .m_tkeep   (d_tkeep),
        .m_tlast   (d_tlast),
        .m0_tvalid (d0_tvalid),
        .m0_tready (d0_tready),
        .m1_tvalid (d1_tvalid),
        .m1_tready (d1_tready),
        .push0     (push0),
        .push1     (push1),
        .tag_full0 (tag_full0),
        .tag_full1 (tag_full1)
    );

    logic [63:0] r0_tdata, r1_tdata;
    logic [ 7:0] r0_tkeep, r1_tkeep;
    logic        r0_tvalid, r0_tready, r0_tlast;
    logic        r1_tvalid, r1_tready, r1_tlast;

    oca_core #(.BYTES (BYTES)) u_core0 (
        .clk           (clk),
        .rst_n         (rst_n),
        .s_axis_tdata  (d_tdata),
        .s_axis_tkeep  (d_tkeep),
        .s_axis_tvalid (d0_tvalid),
        .s_axis_tready (d0_tready),
        .s_axis_tlast  (d_tlast),
        .m_axis_tdata  (r0_tdata),
        .m_axis_tkeep  (r0_tkeep),
        .m_axis_tvalid (r0_tvalid),
        .m_axis_tready (r0_tready),
        .m_axis_tlast  (r0_tlast)
    );

    oca_core #(.BYTES (BYTES)) u_core1 (
        .clk           (clk),
        .rst_n         (rst_n),
        .s_axis_tdata  (d_tdata),
        .s_axis_tkeep  (d_tkeep),
        .s_axis_tvalid (d1_tvalid),
        .s_axis_tready (d1_tready),
        .s_axis_tlast  (d_tlast),
        .m_axis_tdata  (r1_tdata),
        .m_axis_tkeep  (r1_tkeep),
        .m_axis_tvalid (r1_tvalid),
        .m_axis_tready (r1_tready),
        .m_axis_tlast  (r1_tlast)
    );

    oca_collect u_collect (
        .clk       (clk),
        .rst_n     (rst_n),
        .s0_tdata  (r0_tdata),
        .s0_tkeep  (r0_tkeep),
        .s0_tvalid (r0_tvalid),
        .s0_tready (r0_tready),
        .s0_tlast  (r0_tlast),
        .s1_tdata  (r1_tdata),
        .s1_tkeep  (r1_tkeep),
        .s1_tvalid (r1_tvalid),
        .s1_tready (r1_tready),
        .s1_tlast  (r1_tlast),
        .m_tdata   (m_tdata),
        .m_tkeep   (m_tkeep),
        .m_tvalid  (m_tvalid),
        .m_tready  (m_tready),
        .m_tlast   (m_tlast),
        .push0     (push0),
        .push1     (push1),
        .tag_full0 (tag_full0),
        .tag_full1 (tag_full1),
        .trouble   (trouble)
    );

endmodule

`default_nettype wire
