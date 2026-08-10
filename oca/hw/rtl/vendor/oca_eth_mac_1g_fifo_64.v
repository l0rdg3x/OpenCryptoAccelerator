// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * Parameter-fixing wrapper around verilog-ethernet's eth_mac_1g_fifo at
 * AXIS_DATA_WIDTH = 64.
 *
 * Why a wrapper exists at all. A module that reaches yosys's read_slang
 * frontend by way of read_verilog arrives already elaborated, with its
 * parameters resolved, so an override written in SystemVerilog fails with
 * "parameter 'X' does not exist in 'Y'". The way out is this file: plain
 * Verilog-2005, read by read_verilog alongside the vendor tree, fixing every
 * parameter and presenting a port list whose widths are literals. Our
 * SystemVerilog top instantiates it with no parameters at all.
 *
 * Nothing under oca/hw/vendor/ is edited to make this work, which is the
 * whole point of the layer.
 *
 * eth_mac_1g_fifo and not eth_mac_1g_rgmii_fifo: the latter embeds
 * rgmii_phy_if, whose TARGET parameter has no ECP5 value and whose oddr.v
 * synth_ecp5 rejects outright. That layer is oca/hw/rtl/oca_rgmii.sv. This
 * module takes GMII plus rx_clk and tx_clk and carries the two
 * axis_async_fifo_adapter instances that do the 8-to-64 width conversion and
 * both clock domain crossings.
 *
 * Where each conversion happens, because it is the reason for 64 bits. The
 * receive adapter upsizes 8 -> 64 *before* its FIFO, in the rx_clk domain, so
 * the asynchronous FIFO and everything after it is already 64 bits wide; the
 * transmit adapter downsizes 64 -> 8 *after* its FIFO, in the tx_clk domain.
 * An 8-bit stream in our ~48 MHz domain would carry 384 Mbps, under the port
 * it feeds.
 *
 * RESET POLARITY IS THE VENDOR'S, NOT OURS. Every *_rst input here is active
 * HIGH, and asynchronously asserted: "posedge rst" appears in the
 * sensitivity list of the status toggle synchronisers (eth_mac_1g_fifo.v
 * 142-191) and of the FIFOs' own reset logic (axis_async_fifo.v 356-375)
 * alike. The project's own RTL is active-low asynchronous
 * (always_ff @(posedge clk or negedge rst_n)), so the top must invert.
 *
 * Release each of the three resets synchronously to its own clock.
 * axis_async_fifo builds cross-domain reset synchronisers for itself;
 * eth_mac_1g_fifo's status path does not, and rx_error_bad_frame,
 * rx_error_bad_fcs and tx_error_underflow are edge detectors of the form
 * sync_reg_3 ^ sync_reg_4 over registers that all reset on posedge
 * logic_rst. Two of them leaving reset on different edges is a counted error
 * that never happened.
 *
 * Every status and error output is a port. None is tied off, aggregated or
 * left dangling: the receive path drops oversized frames, bad-FCS frames and
 * frames arriving while full, and it has no back pressure toward the wire, so
 * a drop the operator cannot count reads as success.
 *
 * Two things the vendor does not let us expose, recorded so nobody looks for
 * them. eth_mac_1g_fifo leaves both adapters' s_status_depth,
 * s_status_depth_commit, m_status_depth and m_status_depth_commit
 * unconnected (lines 272-277 and 323-328), so FIFO occupancy -- the only
 * early warning before an overflow -- is not reachable without instantiating
 * the adapters ourselves. And eth_mac_1g's tx_start_packet/rx_start_packet
 * are PTP strobes, not status.
 */

`resetall
`timescale 1ns / 1ps
`default_nettype none

module oca_eth_mac_1g_fifo_64 (
    input  wire        rx_clk,
    input  wire        rx_rst,
    input  wire        tx_clk,
    input  wire        tx_rst,
    input  wire        logic_clk,
    input  wire        logic_rst,

    // AXI-Stream transmit, in the logic_clk domain
    input  wire [63:0] tx_axis_tdata,
    input  wire [7:0]  tx_axis_tkeep,
    input  wire        tx_axis_tvalid,
    output wire        tx_axis_tready,
    input  wire        tx_axis_tlast,
    input  wire        tx_axis_tuser,

    // AXI-Stream receive, in the logic_clk domain
    output wire [63:0] rx_axis_tdata,
    output wire [7:0]  rx_axis_tkeep,
    output wire        rx_axis_tvalid,
    input  wire        rx_axis_tready,
    output wire        rx_axis_tlast,
    output wire        rx_axis_tuser,

    // GMII, to oca_rgmii
    input  wire [7:0]  gmii_rxd,
    input  wire        gmii_rx_dv,
    input  wire        gmii_rx_er,
    output wire [7:0]  gmii_txd,
    output wire        gmii_tx_en,
    output wire        gmii_tx_er,

    // Control
    input  wire        rx_clk_enable,
    input  wire        tx_clk_enable,
    input  wire        rx_mii_select,
    input  wire        tx_mii_select,

    // Status, one wire per event, all pulses in the logic_clk domain
    output wire        tx_error_underflow,
    output wire        tx_fifo_overflow,
    output wire        tx_fifo_bad_frame,
    output wire        tx_fifo_good_frame,
    output wire        rx_error_bad_frame,
    output wire        rx_error_bad_fcs,
    output wire        rx_fifo_overflow,
    output wire        rx_fifo_bad_frame,
    output wire        rx_fifo_good_frame,

    // Configuration
    input  wire [7:0]  cfg_ifg,
    input  wire        cfg_tx_enable,
    input  wire        cfg_rx_enable
);

eth_mac_1g_fifo #(
    // 64 bits is the whole reason this wrapper exists; KEEP_ENABLE and
    // KEEP_WIDTH would be derived from it, and are written out anyway so the
    // port widths above have a stated origin rather than an inferred one.
    .AXIS_DATA_WIDTH(64),
    .AXIS_KEEP_ENABLE(1),
    .AXIS_KEEP_WIDTH(8),

    // 802.3 has a 64-byte minimum frame including FCS. A shorter frame is a
    // runt that switches discard, and our responses can be short, so the MAC
    // pads rather than leaving the length to the layer that builds them.
    .ENABLE_PADDING(1),
    .MIN_FRAME_LENGTH(64),

    // DEPTH is in BYTES, not entries: axis_async_fifo derives
    // ADDR_WIDTH = $clog2(DEPTH/KEEP_WIDTH) when KEEP_ENABLE is set, so 4096
    // here is 512 cycles of a 64-bit word. 4096 holds two maximum-length
    // frames (1518 on the wire, 1514 into the FIFO once the MAC has stripped
    // the FCS), which is what a store-and-forward frame FIFO needs to accept
    // one frame while the previous one drains.
    //
    // Deliberately not larger. Depth cannot fix a sustained rate mismatch:
    // one oca_core sinks ~0.560 Gbps at MTU against a 1 Gbps port, so
    // back-to-back line-rate traffic overflows any depth we could afford.
    // What depth buys is tolerance of bursts, and what makes the shortfall
    // visible is rx_fifo_overflow, not a bigger memory.
    .TX_FIFO_DEPTH(4096),
    .RX_FIFO_DEPTH(4096),

    // One pipeline register on the memory read, which is the ECP5 block
    // RAM's own output register (DP16KD REGMODE=OUTREG) rather than fabric.
    // Costs a cycle of latency on a path that has none to spare only if we
    // were chasing latency, which we are not.
    .TX_FIFO_RAM_PIPELINE(1),
    .RX_FIFO_RAM_PIPELINE(1),

    // Frame FIFO on both sides: the write pointer is only committed at
    // tlast, so a reader never starts a frame the writer has not finished.
    // On transmit that is what stops a stall in oca_proto from becoming
    // tx_error_underflow on the wire; on receive it is what makes it
    // possible to discard a frame after its FCS has failed.
    .TX_FRAME_FIFO(1),
    .RX_FRAME_FIFO(1),

    // DROP_OVERSIZE_FRAME is not optional with FRAME_FIFO set: a frame
    // larger than the FIFO can never be committed, so without the drop it
    // would wedge the FIFO. The depths above make it unreachable for any
    // legal frame; if it ever fires, the drop is counted by
    // tx_fifo_bad_frame / rx_fifo_bad_frame.
    .TX_DROP_OVERSIZE_FRAME(1),
    .RX_DROP_OVERSIZE_FRAME(1),

    // Drop frames marked bad by tuser at tlast. On receive that is a bad FCS
    // or a PHY receive error, and handing a corrupt frame to the UDP stack
    // is not recoverable once it has begun emitting it. On transmit it is an
    // abort from our side, which must not reach the wire. Both are counted.
    .TX_DROP_BAD_FRAME(1),
    .RX_DROP_BAD_FRAME(1),

    // The asymmetry that matters, and the one place the two directions must
    // not be set alike.
    //
    // Transmit has back pressure: tx_axis_tready falls and our side waits.
    // DROP_WHEN_FULL there would silently discard a response that the host
    // is waiting for, so it stays 0 and a full transmit FIFO stalls us
    // instead.
    //
    // Receive has none. eth_mac_1g's rx_axis has no tready at all --
    // eth_mac_1g_fifo leaves the receive FIFO's s_axis_tready unconnected --
    // so when the FIFO is full the bytes are lost either way. The only
    // choice is whether they are lost as a cleanly discarded frame with
    // rx_fifo_overflow asserted, or as a truncated one. 1, and count it.
    .TX_DROP_WHEN_FULL(0),
    .RX_DROP_WHEN_FULL(1)
)
eth_mac_1g_fifo_inst (
    .rx_clk(rx_clk),
    .rx_rst(rx_rst),
    .tx_clk(tx_clk),
    .tx_rst(tx_rst),
    .logic_clk(logic_clk),
    .logic_rst(logic_rst),

    .tx_axis_tdata(tx_axis_tdata),
    .tx_axis_tkeep(tx_axis_tkeep),
    .tx_axis_tvalid(tx_axis_tvalid),
    .tx_axis_tready(tx_axis_tready),
    .tx_axis_tlast(tx_axis_tlast),
    .tx_axis_tuser(tx_axis_tuser),

    .rx_axis_tdata(rx_axis_tdata),
    .rx_axis_tkeep(rx_axis_tkeep),
    .rx_axis_tvalid(rx_axis_tvalid),
    .rx_axis_tready(rx_axis_tready),
    .rx_axis_tlast(rx_axis_tlast),
    .rx_axis_tuser(rx_axis_tuser),

    .gmii_rxd(gmii_rxd),
    .gmii_rx_dv(gmii_rx_dv),
    .gmii_rx_er(gmii_rx_er),
    .gmii_txd(gmii_txd),
    .gmii_tx_en(gmii_tx_en),
    .gmii_tx_er(gmii_tx_er),

    .rx_clk_enable(rx_clk_enable),
    .tx_clk_enable(tx_clk_enable),
    .rx_mii_select(rx_mii_select),
    .tx_mii_select(tx_mii_select),

    .tx_error_underflow(tx_error_underflow),
    .tx_fifo_overflow(tx_fifo_overflow),
    .tx_fifo_bad_frame(tx_fifo_bad_frame),
    .tx_fifo_good_frame(tx_fifo_good_frame),
    .rx_error_bad_frame(rx_error_bad_frame),
    .rx_error_bad_fcs(rx_error_bad_fcs),
    .rx_fifo_overflow(rx_fifo_overflow),
    .rx_fifo_bad_frame(rx_fifo_bad_frame),
    .rx_fifo_good_frame(rx_fifo_good_frame),

    .cfg_ifg(cfg_ifg),
    .cfg_tx_enable(cfg_tx_enable),
    .cfg_rx_enable(cfg_rx_enable)
);

endmodule

`resetall
