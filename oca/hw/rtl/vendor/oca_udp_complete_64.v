// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * Parameter-fixing wrapper around verilog-ethernet's udp_complete_64, the
 * 64-bit UDP/IP/ARP stack.
 *
 * Why a wrapper exists at all: see oca_eth_mac_1g_fifo_64.v. In short, a
 * module that reaches yosys's read_slang frontend by way of read_verilog
 * arrives already elaborated, so a parameter override written in
 * SystemVerilog fails. This file fixes the parameters in plain Verilog-2005
 * on the read_verilog side of that boundary. Nothing under oca/hw/vendor/ is
 * edited.
 *
 * udp_complete_64's own port widths are literals already -- it has no width
 * parameter -- so this wrapper changes no port. It exists only for the seven
 * parameters below, four of which are wrong for this design at their
 * defaults.
 *
 * RESET POLARITY IS THE VENDOR'S, NOT OURS. `rst` is active HIGH and
 * synchronous throughout this subtree. The project's own RTL is active-low
 * asynchronous (always_ff @(posedge clk or negedge rst_n)). The top level
 * must invert.
 *
 * Every status and error output is a port. None is tied off or aggregated:
 * ip_eth_rx_64 discards a frame whose IPv4 header checksum fails by jumping
 * to STATE_WAIT_LAST, and the only trace it leaves is
 * ip_rx_error_invalid_checksum. The same holds for every other error wire
 * here -- a drop the operator cannot count reads as success.
 *
 * ONE PORT PAIR THAT MUST NOT BE TIED LOW, even though this design has no
 * use for it. m_ip_hdr_ready and m_ip_payload_axis_tready are the raw IP
 * receive path: every received IPv4 frame whose protocol is not 0x11 leaves
 * by it -- an ICMP echo request, a stray TCP segment, anything. In
 * udp_complete_64 lines 361-365,
 *
 *   ip_rx_ip_hdr_ready = (s_select_udp && udp_rx_ip_hdr_ready) ||
 *                        (s_select_ip  && m_ip_hdr_ready);
 *   ip_rx_ip_payload_axis_tready = (s_select_udp_reg && udp_...tready) ||
 *                                  (s_select_ip_reg  && m_ip_payload_axis_tready);
 *
 * so with both tied to 0 a single non-UDP frame is never consumed,
 * ip_eth_rx_64 holds it forever, back pressure reaches the MAC receive FIFO
 * and every subsequent frame is dropped on overflow. Tie both to 1'b1 at the
 * top and let the frame be discarded, or route them somewhere that always
 * accepts. Leaving them unconnected is the same failure with a warning.
 */

`resetall
`timescale 1ns / 1ps
`default_nettype none

module oca_udp_complete_64 (
    input  wire        clk,
    input  wire        rst,

    // Ethernet frame input, from the MAC by way of eth_axis_rx
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

    // Ethernet frame output, toward the MAC by way of eth_axis_tx
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

    // Raw IP input (non-UDP transmit path)
    input  wire        s_ip_hdr_valid,
    output wire        s_ip_hdr_ready,
    input  wire [5:0]  s_ip_dscp,
    input  wire [1:0]  s_ip_ecn,
    input  wire [15:0] s_ip_length,
    input  wire [7:0]  s_ip_ttl,
    input  wire [7:0]  s_ip_protocol,
    input  wire [31:0] s_ip_source_ip,
    input  wire [31:0] s_ip_dest_ip,
    input  wire [63:0] s_ip_payload_axis_tdata,
    input  wire [7:0]  s_ip_payload_axis_tkeep,
    input  wire        s_ip_payload_axis_tvalid,
    output wire        s_ip_payload_axis_tready,
    input  wire        s_ip_payload_axis_tlast,
    input  wire        s_ip_payload_axis_tuser,

    // Raw IP output (every received IPv4 frame whose protocol is not 0x11)
    output wire        m_ip_hdr_valid,
    input  wire        m_ip_hdr_ready,
    output wire [47:0] m_ip_eth_dest_mac,
    output wire [47:0] m_ip_eth_src_mac,
    output wire [15:0] m_ip_eth_type,
    output wire [3:0]  m_ip_version,
    output wire [3:0]  m_ip_ihl,
    output wire [5:0]  m_ip_dscp,
    output wire [1:0]  m_ip_ecn,
    output wire [15:0] m_ip_length,
    output wire [15:0] m_ip_identification,
    output wire [2:0]  m_ip_flags,
    output wire [12:0] m_ip_fragment_offset,
    output wire [7:0]  m_ip_ttl,
    output wire [7:0]  m_ip_protocol,
    output wire [15:0] m_ip_header_checksum,
    output wire [31:0] m_ip_source_ip,
    output wire [31:0] m_ip_dest_ip,
    output wire [63:0] m_ip_payload_axis_tdata,
    output wire [7:0]  m_ip_payload_axis_tkeep,
    output wire        m_ip_payload_axis_tvalid,
    input  wire        m_ip_payload_axis_tready,
    output wire        m_ip_payload_axis_tlast,
    output wire        m_ip_payload_axis_tuser,

    // UDP input, our transmit path
    input  wire        s_udp_hdr_valid,
    output wire        s_udp_hdr_ready,
    input  wire [5:0]  s_udp_ip_dscp,
    input  wire [1:0]  s_udp_ip_ecn,
    input  wire [7:0]  s_udp_ip_ttl,
    input  wire [31:0] s_udp_ip_source_ip,
    input  wire [31:0] s_udp_ip_dest_ip,
    input  wire [15:0] s_udp_source_port,
    input  wire [15:0] s_udp_dest_port,
    input  wire [15:0] s_udp_length,
    input  wire [15:0] s_udp_checksum,
    input  wire [63:0] s_udp_payload_axis_tdata,
    input  wire [7:0]  s_udp_payload_axis_tkeep,
    input  wire        s_udp_payload_axis_tvalid,
    output wire        s_udp_payload_axis_tready,
    input  wire        s_udp_payload_axis_tlast,
    input  wire        s_udp_payload_axis_tuser,

    // UDP output, our receive path
    output wire        m_udp_hdr_valid,
    input  wire        m_udp_hdr_ready,
    output wire [47:0] m_udp_eth_dest_mac,
    output wire [47:0] m_udp_eth_src_mac,
    output wire [15:0] m_udp_eth_type,
    output wire [3:0]  m_udp_ip_version,
    output wire [3:0]  m_udp_ip_ihl,
    output wire [5:0]  m_udp_ip_dscp,
    output wire [1:0]  m_udp_ip_ecn,
    output wire [15:0] m_udp_ip_length,
    output wire [15:0] m_udp_ip_identification,
    output wire [2:0]  m_udp_ip_flags,
    output wire [12:0] m_udp_ip_fragment_offset,
    output wire [7:0]  m_udp_ip_ttl,
    output wire [7:0]  m_udp_ip_protocol,
    output wire [15:0] m_udp_ip_header_checksum,
    output wire [31:0] m_udp_ip_source_ip,
    output wire [31:0] m_udp_ip_dest_ip,
    output wire [15:0] m_udp_source_port,
    output wire [15:0] m_udp_dest_port,
    output wire [15:0] m_udp_length,
    output wire [15:0] m_udp_checksum,
    output wire [63:0] m_udp_payload_axis_tdata,
    output wire [7:0]  m_udp_payload_axis_tkeep,
    output wire        m_udp_payload_axis_tvalid,
    input  wire        m_udp_payload_axis_tready,
    output wire        m_udp_payload_axis_tlast,
    output wire        m_udp_payload_axis_tuser,

    // Status, one wire per event
    output wire        ip_rx_busy,
    output wire        ip_tx_busy,
    output wire        udp_rx_busy,
    output wire        udp_tx_busy,
    output wire        ip_rx_error_header_early_termination,
    output wire        ip_rx_error_payload_early_termination,
    output wire        ip_rx_error_invalid_header,
    output wire        ip_rx_error_invalid_checksum,
    output wire        ip_tx_error_payload_early_termination,
    output wire        ip_tx_error_arp_failed,
    output wire        udp_rx_error_header_early_termination,
    output wire        udp_rx_error_payload_early_termination,
    output wire        udp_tx_error_payload_early_termination,

    // Configuration
    input  wire [47:0] local_mac,
    input  wire [31:0] local_ip,
    input  wire [31:0] gateway_ip,
    input  wire [31:0] subnet_mask,
    input  wire        clear_arp_cache
);

// The ARP timers below are cycle counts, so they are only as right as the
// frequency they were computed for. The 48000000 in them is clk_sys, which
// docs/design/2026-08-05-ethernet-integration.md section 4 puts at ~48 MHz
// and which no PLL configuration has fixed exactly yet. If the PLL lands
// somewhere else, those two constants are what to revisit -- an error of a
// few per cent turns a 2 s retry into a 2.1 s retry and is immaterial; the
// 2.6x error of leaving the 125 MHz defaults in place is not.

udp_complete_64 #(
    // 64 entries, direct-mapped on a CRC32 hash of the peer IP, against the
    // vendor's 512. Sized down deliberately, and for LUTs rather than for
    // block RAM: arp_cache.v reads valid_mem and ip_addr_mem from an
    // always @* block (line 166), so those two memories are asynchronous
    // reads and cannot be a DP16KD -- they land in LUT fabric, and their
    // cost is linear in 2**CACHE_ADDR_WIDTH. Only mac_addr_mem is read
    // synchronously.
    //
    // 64 is not "as small as possible": the cache is direct-mapped, so two
    // peers that collide evict each other and every packet to them costs an
    // ARP round trip. 64 slots keeps that improbable for a subnet's worth of
    // peers, while this device's real peer set is one host and perhaps a
    // gateway.
    .ARP_CACHE_ADDR_WIDTH(6),

    // Vendor default. Four attempts before ip_tx_error_arp_failed.
    .ARP_REQUEST_RETRY_COUNT(4),

    // Rescaled from 125 MHz to clk_sys. Left alone, every ARP timeout would
    // be 2.6x longer than intended.
    //
    // And the vendor's own 30 s default is not 30 s: 125000000*30 is
    // 3_750_000_000, which does not fit a signed 32-bit Verilog integer
    // constant, so it wraps to -544_967_296 and sign-extends into arp.v's
    // 36-bit arp_request_timer_reg as 68_174_509_440 -- about 545 s at
    // 125 MHz, 18x the intended value. Measured on tools/yosys, not
    // reasoned about. Our values stay well inside 32 bits: 1_440_000_000 is
    // the largest, against 2_147_483_647.
    .ARP_REQUEST_RETRY_INTERVAL(48000000 * 2),
    .ARP_REQUEST_TIMEOUT(48000000 * 30),

    // Kept on. The measured saving is smaller than it looks: turning it off
    // gives 6419 LUTs against 7147 and three fewer block RAMs, but brings
    // 288 TRELLIS_RAMW -- LUT fabric spent as distributed RAM -- and drops
    // Fmax from 81 to 72.9 MHz (docs/design/2026-08-05-ethernet-integration.md
    // section 2). What it buys is larger than the difference: with it on,
    // udp_64 computes s_udp_length and s_udp_checksum for us, so our
    // transmit side may present a UDP header before it knows the response
    // length. With it off, both become our arithmetic on a path where a
    // hand-rolled checksum is exactly the kind of code this project does not
    // write.
    //
    // The price is store-and-forward through the payload FIFO below. It does
    // not cost throughput here: filling and draining 1472 bytes at 64 bits
    // per cycle is 368 cycles even with no overlap, 1.5 Gbps at 48 MHz,
    // above the port and far above one oca_core's 0.561 Gbps.
    .UDP_CHECKSUM_GEN_ENABLE(1),

    // Bytes, like every other DEPTH in this vendor tree: axis_fifo derives
    // ADDR_WIDTH = $clog2(DEPTH/KEEP_WIDTH) at KEEP_ENABLE, so 2048 is 256
    // cycles of a 64-bit word. It must hold one whole UDP payload, and the
    // largest that fits an MTU-1500 frame is 1500 - 20 - 8 = 1472. Already
    // minimal: the next power of two down is 1024 cycles' worth... 1024
    // bytes, which is under 1472, and any value between 1473 and 2048
    // rounds back up to the same 256 cycles. So this is both the smallest
    // legal value and the vendor default.
    .UDP_CHECKSUM_PAYLOAD_FIFO_DEPTH(2048),

    // Eight headers in flight behind the checksum. $clog2(8) = 3 address
    // bits over a handful of registers; no block RAM either way.
    .UDP_CHECKSUM_HEADER_FIFO_DEPTH(8)
)
udp_complete_64_inst (
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

    .s_ip_hdr_valid(s_ip_hdr_valid),
    .s_ip_hdr_ready(s_ip_hdr_ready),
    .s_ip_dscp(s_ip_dscp),
    .s_ip_ecn(s_ip_ecn),
    .s_ip_length(s_ip_length),
    .s_ip_ttl(s_ip_ttl),
    .s_ip_protocol(s_ip_protocol),
    .s_ip_source_ip(s_ip_source_ip),
    .s_ip_dest_ip(s_ip_dest_ip),
    .s_ip_payload_axis_tdata(s_ip_payload_axis_tdata),
    .s_ip_payload_axis_tkeep(s_ip_payload_axis_tkeep),
    .s_ip_payload_axis_tvalid(s_ip_payload_axis_tvalid),
    .s_ip_payload_axis_tready(s_ip_payload_axis_tready),
    .s_ip_payload_axis_tlast(s_ip_payload_axis_tlast),
    .s_ip_payload_axis_tuser(s_ip_payload_axis_tuser),

    .m_ip_hdr_valid(m_ip_hdr_valid),
    .m_ip_hdr_ready(m_ip_hdr_ready),
    .m_ip_eth_dest_mac(m_ip_eth_dest_mac),
    .m_ip_eth_src_mac(m_ip_eth_src_mac),
    .m_ip_eth_type(m_ip_eth_type),
    .m_ip_version(m_ip_version),
    .m_ip_ihl(m_ip_ihl),
    .m_ip_dscp(m_ip_dscp),
    .m_ip_ecn(m_ip_ecn),
    .m_ip_length(m_ip_length),
    .m_ip_identification(m_ip_identification),
    .m_ip_flags(m_ip_flags),
    .m_ip_fragment_offset(m_ip_fragment_offset),
    .m_ip_ttl(m_ip_ttl),
    .m_ip_protocol(m_ip_protocol),
    .m_ip_header_checksum(m_ip_header_checksum),
    .m_ip_source_ip(m_ip_source_ip),
    .m_ip_dest_ip(m_ip_dest_ip),
    .m_ip_payload_axis_tdata(m_ip_payload_axis_tdata),
    .m_ip_payload_axis_tkeep(m_ip_payload_axis_tkeep),
    .m_ip_payload_axis_tvalid(m_ip_payload_axis_tvalid),
    .m_ip_payload_axis_tready(m_ip_payload_axis_tready),
    .m_ip_payload_axis_tlast(m_ip_payload_axis_tlast),
    .m_ip_payload_axis_tuser(m_ip_payload_axis_tuser),

    .s_udp_hdr_valid(s_udp_hdr_valid),
    .s_udp_hdr_ready(s_udp_hdr_ready),
    .s_udp_ip_dscp(s_udp_ip_dscp),
    .s_udp_ip_ecn(s_udp_ip_ecn),
    .s_udp_ip_ttl(s_udp_ip_ttl),
    .s_udp_ip_source_ip(s_udp_ip_source_ip),
    .s_udp_ip_dest_ip(s_udp_ip_dest_ip),
    .s_udp_source_port(s_udp_source_port),
    .s_udp_dest_port(s_udp_dest_port),
    .s_udp_length(s_udp_length),
    .s_udp_checksum(s_udp_checksum),
    .s_udp_payload_axis_tdata(s_udp_payload_axis_tdata),
    .s_udp_payload_axis_tkeep(s_udp_payload_axis_tkeep),
    .s_udp_payload_axis_tvalid(s_udp_payload_axis_tvalid),
    .s_udp_payload_axis_tready(s_udp_payload_axis_tready),
    .s_udp_payload_axis_tlast(s_udp_payload_axis_tlast),
    .s_udp_payload_axis_tuser(s_udp_payload_axis_tuser),

    .m_udp_hdr_valid(m_udp_hdr_valid),
    .m_udp_hdr_ready(m_udp_hdr_ready),
    .m_udp_eth_dest_mac(m_udp_eth_dest_mac),
    .m_udp_eth_src_mac(m_udp_eth_src_mac),
    .m_udp_eth_type(m_udp_eth_type),
    .m_udp_ip_version(m_udp_ip_version),
    .m_udp_ip_ihl(m_udp_ip_ihl),
    .m_udp_ip_dscp(m_udp_ip_dscp),
    .m_udp_ip_ecn(m_udp_ip_ecn),
    .m_udp_ip_length(m_udp_ip_length),
    .m_udp_ip_identification(m_udp_ip_identification),
    .m_udp_ip_flags(m_udp_ip_flags),
    .m_udp_ip_fragment_offset(m_udp_ip_fragment_offset),
    .m_udp_ip_ttl(m_udp_ip_ttl),
    .m_udp_ip_protocol(m_udp_ip_protocol),
    .m_udp_ip_header_checksum(m_udp_ip_header_checksum),
    .m_udp_ip_source_ip(m_udp_ip_source_ip),
    .m_udp_ip_dest_ip(m_udp_ip_dest_ip),
    .m_udp_source_port(m_udp_source_port),
    .m_udp_dest_port(m_udp_dest_port),
    .m_udp_length(m_udp_length),
    .m_udp_checksum(m_udp_checksum),
    .m_udp_payload_axis_tdata(m_udp_payload_axis_tdata),
    .m_udp_payload_axis_tkeep(m_udp_payload_axis_tkeep),
    .m_udp_payload_axis_tvalid(m_udp_payload_axis_tvalid),
    .m_udp_payload_axis_tready(m_udp_payload_axis_tready),
    .m_udp_payload_axis_tlast(m_udp_payload_axis_tlast),
    .m_udp_payload_axis_tuser(m_udp_payload_axis_tuser),

    .ip_rx_busy(ip_rx_busy),
    .ip_tx_busy(ip_tx_busy),
    .udp_rx_busy(udp_rx_busy),
    .udp_tx_busy(udp_tx_busy),
    .ip_rx_error_header_early_termination(ip_rx_error_header_early_termination),
    .ip_rx_error_payload_early_termination(ip_rx_error_payload_early_termination),
    .ip_rx_error_invalid_header(ip_rx_error_invalid_header),
    .ip_rx_error_invalid_checksum(ip_rx_error_invalid_checksum),
    .ip_tx_error_payload_early_termination(ip_tx_error_payload_early_termination),
    .ip_tx_error_arp_failed(ip_tx_error_arp_failed),
    .udp_rx_error_header_early_termination(udp_rx_error_header_early_termination),
    .udp_rx_error_payload_early_termination(udp_rx_error_payload_early_termination),
    .udp_tx_error_payload_early_termination(udp_tx_error_payload_early_termination),

    .local_mac(local_mac),
    .local_ip(local_ip),
    .gateway_ip(gateway_ip),
    .subnet_mask(subnet_mask),
    .clear_arp_cache(clear_arp_cache)
);

endmodule

`resetall
