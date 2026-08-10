// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * Parameter-fixing wrappers around verilog-ethernet's eth_axis_rx and
 * eth_axis_tx at DATA_WIDTH = 64.
 *
 * These are the piece between the MAC and the UDP stack, and the project
 * did not know it needed them until 2026-08-09.
 * oca_eth_mac_1g_fifo_64 hands out a raw AXI-Stream with the Ethernet
 * header still in the data; udp_complete_64 wants the header already
 * parsed, on s_eth_hdr_valid / s_eth_dest_mac / s_eth_src_mac /
 * s_eth_type plus a payload stream, and it does not contain the parser:
 * it instantiates ip_complete_64 and udp_64 and nothing else. Upstream's
 * own examples read these two files alongside the stack for exactly this
 * reason.
 *
 * Why wrappers rather than direct instantiation: the same boundary that
 * forced the other two. A module reaching yosys's read_slang frontend by
 * way of read_verilog arrives already elaborated, with its parameters
 * resolved, so an override written in SystemVerilog fails with
 * "parameter 'X' does not exist in 'Y'". These fix DATA_WIDTH at 64 in
 * plain Verilog-2005 on the read_verilog side and present port lists
 * whose widths are literals. Nothing under oca/hw/vendor/ is edited.
 *
 * KEEP_ENABLE and KEEP_WIDTH are left to their defaults deliberately:
 * upstream derives them as (DATA_WIDTH>8) and (DATA_WIDTH/8), which at
 * 64 bits is 1 and 8. Restating them here would be a second source for
 * a value the vendor already computes, and the two could drift.
 *
 * RESET POLARITY IS THE VENDOR'S: rst is active HIGH, as everywhere else
 * in that tree. Our RTL is active low, so the top inverts. Both of these
 * live in a single clock domain each -- the receive parser in the MAC's
 * logic domain and the transmit builder in the same -- because
 * oca_eth_mac_1g_fifo_64 has already crossed both streams into it.
 *
 * error_header_early_termination is brought out rather than left
 * dangling. It fires when a frame ends inside the 14-byte Ethernet
 * header, which on a real link means a runt or a truncated capture, and
 * a counter the operator cannot read is a drop that reads as success.
 */

`resetall
`timescale 1ns / 1ps
`default_nettype none

module oca_eth_axis_rx_64 (
    input  wire        clk,
    input  wire        rst,

    // From the MAC's receive side, header included.
    input  wire [63:0] s_axis_tdata,
    input  wire [7:0]  s_axis_tkeep,
    input  wire        s_axis_tvalid,
    output wire        s_axis_tready,
    input  wire        s_axis_tlast,
    input  wire        s_axis_tuser,

    // To udp_complete_64's s_eth_* side, header parsed off.
    output wire        m_eth_hdr_valid,
    input  wire        m_eth_hdr_ready,
    output wire [47:0] m_eth_dest_mac,
    output wire [47:0] m_eth_src_mac,
    output wire [15:0] m_eth_type,
    output wire [63:0] m_eth_payload_axis_tdata,
    output wire [7:0]  m_eth_payload_axis_tkeep,
    output wire        m_eth_payload_axis_tvalid,
    input  wire        m_eth_payload_axis_tready,
    output wire        m_eth_payload_axis_tlast,
    output wire        m_eth_payload_axis_tuser,

    output wire        busy,
    output wire        error_header_early_termination
);

    eth_axis_rx #(
        .DATA_WIDTH(64)
    ) u_eth_axis_rx (
        .clk(clk),
        .rst(rst),

        .s_axis_tdata(s_axis_tdata),
        .s_axis_tkeep(s_axis_tkeep),
        .s_axis_tvalid(s_axis_tvalid),
        .s_axis_tready(s_axis_tready),
        .s_axis_tlast(s_axis_tlast),
        .s_axis_tuser(s_axis_tuser),

        .m_eth_hdr_valid(m_eth_hdr_valid),
        .m_eth_hdr_ready(m_eth_hdr_ready),
        .m_eth_dest_mac(m_eth_dest_mac),
        .m_eth_src_mac(m_eth_src_mac),
        .m_eth_type(m_eth_type),
        .m_eth_payload_axis_tdata(m_eth_payload_axis_tdata),
        .m_eth_payload_axis_tkeep(m_eth_payload_axis_tkeep),
        .m_eth_payload_axis_tvalid(m_eth_payload_axis_tvalid),
        .m_eth_payload_axis_tready(m_eth_payload_axis_tready),
        .m_eth_payload_axis_tlast(m_eth_payload_axis_tlast),
        .m_eth_payload_axis_tuser(m_eth_payload_axis_tuser),

        .busy(busy),
        .error_header_early_termination(error_header_early_termination)
    );

endmodule

module oca_eth_axis_tx_64 (
    input  wire        clk,
    input  wire        rst,

    // From udp_complete_64's m_eth_* side.
    input  wire        s_eth_hdr_valid,
    output wire        s_eth_hdr_ready,
    input  wire [47:0] s_eth_dest_mac,
    input  wire [47:0] s_eth_src_mac,
    input  wire [15:0] s_eth_type,
    input  wire [63:0] s_eth_payload_axis_tdata,
    input  wire [7:0]  s_eth_payload_axis_tkeep,
    input  wire        s_eth_payload_axis_tvalid,
    output wire        s_eth_payload_axis_tready,
    input  wire        s_eth_payload_axis_tlast,
    input  wire        s_eth_payload_axis_tuser,

    // To the MAC's transmit side, header prepended.
    output wire [63:0] m_axis_tdata,
    output wire [7:0]  m_axis_tkeep,
    output wire        m_axis_tvalid,
    input  wire        m_axis_tready,
    output wire        m_axis_tlast,
    output wire        m_axis_tuser,

    output wire        busy
);

    eth_axis_tx #(
        .DATA_WIDTH(64)
    ) u_eth_axis_tx (
        .clk(clk),
        .rst(rst),

        .s_eth_hdr_valid(s_eth_hdr_valid),
        .s_eth_hdr_ready(s_eth_hdr_ready),
        .s_eth_dest_mac(s_eth_dest_mac),
        .s_eth_src_mac(s_eth_src_mac),
        .s_eth_type(s_eth_type),
        .s_eth_payload_axis_tdata(s_eth_payload_axis_tdata),
        .s_eth_payload_axis_tkeep(s_eth_payload_axis_tkeep),
        .s_eth_payload_axis_tvalid(s_eth_payload_axis_tvalid),
        .s_eth_payload_axis_tready(s_eth_payload_axis_tready),
        .s_eth_payload_axis_tlast(s_eth_payload_axis_tlast),
        .s_eth_payload_axis_tuser(s_eth_payload_axis_tuser),

        .m_axis_tdata(m_axis_tdata),
        .m_axis_tkeep(m_axis_tkeep),
        .m_axis_tvalid(m_axis_tvalid),
        .m_axis_tready(m_axis_tready),
        .m_axis_tlast(m_axis_tlast),
        .m_axis_tuser(m_axis_tuser),

        .busy(busy)
    );

endmodule

`resetall
