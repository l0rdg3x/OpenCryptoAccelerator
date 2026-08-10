# SPDX-License-Identifier: MIT
"""Run the whole-path cocotb tests under the project-local Verilator.

The device under test is everything oca_top.sv puts between the GMII pins
and oca_core: the MAC, the Ethernet header parser and builder, the
ARP/IP/UDP stack, the seam and the core, wired exactly as oca_top wires
them. The testbench drives gmii_rxd/gmii_rx_dv/gmii_rx_er and reads
gmii_txd/gmii_tx_en/gmii_tx_er, so a frame goes in on the wire and a frame
comes back out on the wire.

Why not oca_top itself. oca_clkrst takes clk_sys and clk_tx from an
EHXPLLL declared `(* blackbox *)` with an empty body (ecp5_prims.sv:94-156),
so under a simulator neither clock toggles and pll_locked reads 0 -- which
holds every reset synchroniser asserted forever (oca_clkrst.sv:373). The
same is true of oca_rgmii, which oca_top elaborates with SIMULATION(1'b0)
and therefore out of IDDRX1F/ODDRX1F blackboxes (ecp5_prims.sv:62-80).
oca_clkrst.sv:186-198 states this outright. The two links left out are
exactly those two, and the testbench drives the three clocks and the three
resets in their place.

The harness is generated rather than committed for the reason
run_udp_seam.py gives: cocotb takes one toplevel, six modules have to
elaborate together, and generating it keeps the parameters the tests assume
and the parameters the design elaborates with from drifting apart.

THE HARNESS MIRRORS oca_top, INCLUDING WHAT IT LEAVES OPEN. The pins
oca_top does not connect on its oca_udp_complete_64 instance are not
connected here either, and the lint gate below pins that set exactly: it
fails both when the harness drops a pin oca_top connects and when it
connects one oca_top does not. A testbench free to differ from the board
proves nothing about the board.

That set shrank by three on 2026-08-10. m_ip_hdr_ready and
m_ip_payload_axis_tready were open, which
test_a_non_udp_frame_does_not_wedge_the_receive_path showed wedges the
whole receive path on the first ICMP echo request, exactly as
oca_udp_complete_64.v:29-44 predicted; both are now tied high in oca_top
and here. clear_arp_cache went with them, tied low -- no behaviour change,
since an undriven input already reads 0 in both flows, but an intent
stated rather than inherited from the toolchain.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
VERILATOR_BIN = ROOT / "tools" / "verilator" / "bin"
RTL = ROOT / "oca" / "hw" / "rtl"
sys.path.insert(0, str(ROOT / "oca" / "hw" / "vendor"))
import vendor_patches  # noqa: E402

# The patched vendor tree, never the pinned submodule: unpatched, the MAC
# delivers tkeep = 0 on every receive beat and eth_axis_rx finds no valid
# byte in any frame, so the whole path this suite exists to test is dead.
#
# The override points it at a copy, which is how a mutation is proved
# without writing to the submodule:
#   OCA_PATH_VENDOR=/path/to/a/copy .venv/bin/python hw/sim/run_oca_path.py
_override = os.environ.get("OCA_PATH_VENDOR")
VENDOR = Path(_override) if _override else vendor_patches.PATCHED
vendor_patches.require(VENDOR)

os.environ["PATH"] = str(VERILATOR_BIN) + os.pathsep + os.environ["PATH"]

try:
    from cocotb.runner import get_runner  # cocotb 1.x  # noqa: E402
except ModuleNotFoundError:
    from cocotb_tools.runner import get_runner  # cocotb 2.x  # noqa: E402
from cocotb_tools.check_results import get_results  # noqa: E402

SIM_DIR = Path(__file__).resolve().parent
BUILD = SIM_DIR / "sim_build_oca_path"
TOPLEVEL = "tb_oca_path"

# Imported by test_oca_path.py rather than repeated there, so that a change
# to the elaboration cannot leave the expectations behind. Every one of them
# is oca_top.sv:53-57.
LOCAL_MAC = 0x02005E000001
LOCAL_IP = 0xC0A80164     # 192.168.1.100
GATEWAY_IP = 0xC0A80101   # 192.168.1.1
SUBNET_MASK = 0xFFFFFF00  # 255.255.255.0
LOCAL_PORT = 5000

# oca_udp_seam's defaults, not overridden by oca_top and not overridden
# here. HDR_Q_DEPTH only reaches the harness as the width of the
# hdr_q_watermark port; a seam whose default moved would elaborate against a
# port one bit wide and the lint gate's WIDTH findings on our own file would
# fail the run. REPLY_TTL reaches it not at all, and is here because the
# tests read it off every reply.
HDR_Q_DEPTH = 8
REPLY_TTL = 64

# The two clear windows a first frame has to survive, both in clk_sys cycles.
#
# arp_cache.v:211 sets clear_cache_reg on reset and :197-201 then walks
# wr_ptr one entry per cycle until it wraps, which at ARP_CACHE_ADDR_WIDTH=6
# (oca_udp_complete_64.v:218) is 64 of them. Inside that window
# write_request_ready is held low (arp_cache.v:181) -- and arp.v:245 leaves
# write_request_ready unconnected, so the cache write is fire and forget and
# an entry that lands inside the window is silently discarded.
ARP_CACHE_CLEAR_CYCLES = 64

# oca_pktbuf zeroes 2 * (BYTES/8) words at BYTES = 2048 (test_oca_core.py:40)
# and oca_core holds s_axis_tready low until it is done (oca_core.sv:55-57).
PKTBUF_CLEAR_CYCLES = 512

# 1 ns / 1 ps, stated rather than inherited. clk_sys is 20.8 ns --
# oca_clkrst's 48.0769 MHz -- and a build that fell back to 1 ns precision
# would round it to 21 and drift it away from the two 125 MHz domains,
# silently.
TIMESCALE = "1ns/1ps"

HARNESS = """// SPDX-License-Identifier: MIT
// GENERATED by run_oca_path.py -- edit that file, not this one.
//
// Structure only: the chain oca_top.sv builds from the GMII pins inward,
// with the same instances, the same connections and the same parameters.
// oca_clkrst and oca_rgmii are the two links left out, because both are
// built from empty ECP5 blackboxes; the testbench drives the three clocks
// and the three resets in their place.
//
// rst_n_sys is the inverse of logic_rst and nothing else. oca_clkrst
// derives both polarities from one flop (oca_clkrst.sv:414-419) precisely
// so they cannot disagree by more than an inverter, and a harness that let
// the testbench drive them separately would be testing a board that does
// not exist.
//
// Every status, error and counter wire oca_top spends on one LED is a port
// here instead. At this boundary "no reply" and "silently dropped" look the
// same on the wire, and the difference is only in these.
`default_nettype none

module tb_oca_path (
    input  var logic        rx_clk,
    input  var logic        rx_rst,
    input  var logic        tx_clk,
    input  var logic        tx_rst,
    input  var logic        logic_clk,
    input  var logic        logic_rst,

    input  var logic [7:0]  gmii_rxd,
    input  var logic        gmii_rx_dv,
    input  var logic        gmii_rx_er,
    output var logic [7:0]  gmii_txd,
    output var logic        gmii_tx_en,
    output var logic        gmii_tx_er,

    // The address the seam publishes and the stack answers ARP for. On the
    // board these are one wire; here they are one wire the test can see.
    output var logic [31:0] stack_local_ip,

    output var logic        tx_error_underflow,
    output var logic        tx_fifo_overflow,
    output var logic        tx_fifo_bad_frame,
    output var logic        tx_fifo_good_frame,
    output var logic        rx_error_bad_frame,
    output var logic        rx_error_bad_fcs,
    output var logic        rx_fifo_overflow,
    output var logic        rx_fifo_bad_frame,
    output var logic        rx_fifo_good_frame,

    output var logic        eth_rx_busy,
    output var logic        eth_rx_early_term,
    output var logic        eth_tx_busy,

    output var logic        ip_rx_busy,
    output var logic        ip_tx_busy,
    output var logic        udp_rx_busy,
    output var logic        udp_tx_busy,
    output var logic        ip_rx_err_hdr_early,
    output var logic        ip_rx_err_payload_early,
    output var logic        ip_rx_err_invalid_hdr,
    output var logic        ip_rx_err_invalid_csum,
    output var logic        ip_tx_err_payload_early,
    output var logic        ip_tx_err_arp_failed,
    output var logic        udp_rx_err_hdr_early,
    output var logic        udp_rx_err_payload_early,
    output var logic        udp_tx_err_payload_early,

    output var logic [31:0] cnt_accepted,
    output var logic [31:0] cnt_drop_short,
    output var logic [31:0] cnt_drop_port,
    output var logic [31:0] cnt_drop_full,
    output var logic [31:0] cnt_drop_nohdr,
    output var logic [31:0] cnt_tuser,
    output var logic [31:0] cnt_resp_orphan,
    output var logic [%(wm_hi)d:0] hdr_q_watermark
);

    logic rst_n_sys;
    always_comb rst_n_sys = ~logic_rst;

    logic [63:0] mac_rx_tdata, mac_tx_tdata;
    logic [7:0]  mac_rx_tkeep, mac_tx_tkeep;
    logic        mac_rx_tvalid, mac_rx_tready, mac_rx_tlast, mac_rx_tuser;
    logic        mac_tx_tvalid, mac_tx_tready, mac_tx_tlast, mac_tx_tuser;

    oca_eth_mac_1g_fifo_64 u_mac (
        .rx_clk         (rx_clk),
        .rx_rst         (rx_rst),
        .tx_clk         (tx_clk),
        .tx_rst         (tx_rst),
        .logic_clk      (logic_clk),
        .logic_rst      (logic_rst),

        .tx_axis_tdata  (mac_tx_tdata),
        .tx_axis_tkeep  (mac_tx_tkeep),
        .tx_axis_tvalid (mac_tx_tvalid),
        .tx_axis_tready (mac_tx_tready),
        .tx_axis_tlast  (mac_tx_tlast),
        .tx_axis_tuser  (mac_tx_tuser),

        .rx_axis_tdata  (mac_rx_tdata),
        .rx_axis_tkeep  (mac_rx_tkeep),
        .rx_axis_tvalid (mac_rx_tvalid),
        .rx_axis_tready (mac_rx_tready),
        .rx_axis_tlast  (mac_rx_tlast),
        .rx_axis_tuser  (mac_rx_tuser),

        .gmii_rxd,
        .gmii_rx_dv,
        .gmii_rx_er,
        .gmii_txd,
        .gmii_tx_en,
        .gmii_tx_er,

        .rx_clk_enable  (1'b1),
        .tx_clk_enable  (1'b1),
        .rx_mii_select  (1'b0),
        .tx_mii_select  (1'b0),

        .tx_error_underflow,
        .tx_fifo_overflow,
        .tx_fifo_bad_frame,
        .tx_fifo_good_frame,
        .rx_error_bad_frame,
        .rx_error_bad_fcs,
        .rx_fifo_overflow,
        .rx_fifo_bad_frame,
        .rx_fifo_good_frame,

        .cfg_ifg        (8'd12),
        .cfg_tx_enable  (1'b1),
        .cfg_rx_enable  (1'b1)
    );

    logic        rx_eth_hdr_valid, rx_eth_hdr_ready;
    logic [47:0] rx_eth_dest_mac, rx_eth_src_mac;
    logic [15:0] rx_eth_type;
    logic [63:0] rx_eth_payload_tdata;
    logic [7:0]  rx_eth_payload_tkeep;
    logic        rx_eth_payload_tvalid, rx_eth_payload_tready;
    logic        rx_eth_payload_tlast, rx_eth_payload_tuser;

    oca_eth_axis_rx_64 u_eth_rx (
        .clk                            (logic_clk),
        .rst                            (logic_rst),
        .s_axis_tdata                   (mac_rx_tdata),
        .s_axis_tkeep                   (mac_rx_tkeep),
        .s_axis_tvalid                  (mac_rx_tvalid),
        .s_axis_tready                  (mac_rx_tready),
        .s_axis_tlast                   (mac_rx_tlast),
        .s_axis_tuser                   (mac_rx_tuser),
        .m_eth_hdr_valid                (rx_eth_hdr_valid),
        .m_eth_hdr_ready                (rx_eth_hdr_ready),
        .m_eth_dest_mac                 (rx_eth_dest_mac),
        .m_eth_src_mac                  (rx_eth_src_mac),
        .m_eth_type                     (rx_eth_type),
        .m_eth_payload_axis_tdata       (rx_eth_payload_tdata),
        .m_eth_payload_axis_tkeep       (rx_eth_payload_tkeep),
        .m_eth_payload_axis_tvalid      (rx_eth_payload_tvalid),
        .m_eth_payload_axis_tready      (rx_eth_payload_tready),
        .m_eth_payload_axis_tlast       (rx_eth_payload_tlast),
        .m_eth_payload_axis_tuser       (rx_eth_payload_tuser),
        .busy                           (eth_rx_busy),
        .error_header_early_termination (eth_rx_early_term)
    );

    logic        tx_eth_hdr_valid, tx_eth_hdr_ready;
    logic [47:0] tx_eth_dest_mac, tx_eth_src_mac;
    logic [15:0] tx_eth_type;
    logic [63:0] tx_eth_payload_tdata;
    logic [7:0]  tx_eth_payload_tkeep;
    logic        tx_eth_payload_tvalid, tx_eth_payload_tready;
    logic        tx_eth_payload_tlast, tx_eth_payload_tuser;

    oca_eth_axis_tx_64 u_eth_tx (
        .clk                       (logic_clk),
        .rst                       (logic_rst),
        .s_eth_hdr_valid           (tx_eth_hdr_valid),
        .s_eth_hdr_ready           (tx_eth_hdr_ready),
        .s_eth_dest_mac            (tx_eth_dest_mac),
        .s_eth_src_mac             (tx_eth_src_mac),
        .s_eth_type                (tx_eth_type),
        .s_eth_payload_axis_tdata  (tx_eth_payload_tdata),
        .s_eth_payload_axis_tkeep  (tx_eth_payload_tkeep),
        .s_eth_payload_axis_tvalid (tx_eth_payload_tvalid),
        .s_eth_payload_axis_tready (tx_eth_payload_tready),
        .s_eth_payload_axis_tlast  (tx_eth_payload_tlast),
        .s_eth_payload_axis_tuser  (tx_eth_payload_tuser),
        .m_axis_tdata              (mac_tx_tdata),
        .m_axis_tkeep              (mac_tx_tkeep),
        .m_axis_tvalid             (mac_tx_tvalid),
        .m_axis_tready             (mac_tx_tready),
        .m_axis_tlast              (mac_tx_tlast),
        .m_axis_tuser              (mac_tx_tuser),
        .busy                      (eth_tx_busy)
    );

    logic        udp_rx_hdr_valid, udp_rx_hdr_ready;
    logic [31:0] udp_rx_source_ip;
    logic [15:0] udp_rx_source_port, udp_rx_dest_port;
    logic [63:0] udp_rx_payload_tdata;
    logic [7:0]  udp_rx_payload_tkeep;
    logic        udp_rx_payload_tvalid, udp_rx_payload_tready;
    logic        udp_rx_payload_tlast, udp_rx_payload_tuser;

    logic        udp_tx_hdr_valid, udp_tx_hdr_ready;
    logic [5:0]  udp_tx_ip_dscp;
    logic [1:0]  udp_tx_ip_ecn;
    logic [7:0]  udp_tx_ip_ttl;
    logic [31:0] udp_tx_ip_source_ip, udp_tx_ip_dest_ip;
    logic [15:0] udp_tx_source_port, udp_tx_dest_port;
    logic [15:0] udp_tx_length, udp_tx_checksum;
    logic [63:0] udp_tx_payload_tdata;
    logic [7:0]  udp_tx_payload_tkeep;
    logic        udp_tx_payload_tvalid, udp_tx_payload_tready;
    logic        udp_tx_payload_tlast, udp_tx_payload_tuser;

    // The raw IP transmit port is the one thing oca_top leaves open on this
    // instance, and it is left open here too: nothing in this design
    // originates a non-UDP datagram, so s_ip_hdr_valid reading 0 is the
    // whole of its behaviour. The receive half of that port is a different
    // matter and is tied high below, in both files.
    oca_udp_complete_64 u_udp (
        .clk (logic_clk),
        .rst (logic_rst),

        .s_eth_hdr_valid           (rx_eth_hdr_valid),
        .s_eth_hdr_ready           (rx_eth_hdr_ready),
        .s_eth_dest_mac            (rx_eth_dest_mac),
        .s_eth_src_mac             (rx_eth_src_mac),
        .s_eth_type                (rx_eth_type),
        .s_eth_payload_axis_tdata  (rx_eth_payload_tdata),
        .s_eth_payload_axis_tkeep  (rx_eth_payload_tkeep),
        .s_eth_payload_axis_tvalid (rx_eth_payload_tvalid),
        .s_eth_payload_axis_tready (rx_eth_payload_tready),
        .s_eth_payload_axis_tlast  (rx_eth_payload_tlast),
        .s_eth_payload_axis_tuser  (rx_eth_payload_tuser),

        .m_eth_hdr_valid           (tx_eth_hdr_valid),
        .m_eth_hdr_ready           (tx_eth_hdr_ready),
        .m_eth_dest_mac            (tx_eth_dest_mac),
        .m_eth_src_mac             (tx_eth_src_mac),
        .m_eth_type                (tx_eth_type),
        .m_eth_payload_axis_tdata  (tx_eth_payload_tdata),
        .m_eth_payload_axis_tkeep  (tx_eth_payload_tkeep),
        .m_eth_payload_axis_tvalid (tx_eth_payload_tvalid),
        .m_eth_payload_axis_tready (tx_eth_payload_tready),
        .m_eth_payload_axis_tlast  (tx_eth_payload_tlast),
        .m_eth_payload_axis_tuser  (tx_eth_payload_tuser),

        .m_ip_hdr_ready            (1'b1),
        .m_ip_payload_axis_tready  (1'b1),

        .s_udp_hdr_valid           (udp_tx_hdr_valid),
        .s_udp_hdr_ready           (udp_tx_hdr_ready),
        .s_udp_ip_dscp             (udp_tx_ip_dscp),
        .s_udp_ip_ecn              (udp_tx_ip_ecn),
        .s_udp_ip_ttl              (udp_tx_ip_ttl),
        .s_udp_ip_source_ip        (udp_tx_ip_source_ip),
        .s_udp_ip_dest_ip          (udp_tx_ip_dest_ip),
        .s_udp_source_port         (udp_tx_source_port),
        .s_udp_dest_port           (udp_tx_dest_port),
        .s_udp_length              (udp_tx_length),
        .s_udp_checksum            (udp_tx_checksum),
        .s_udp_payload_axis_tdata  (udp_tx_payload_tdata),
        .s_udp_payload_axis_tkeep  (udp_tx_payload_tkeep),
        .s_udp_payload_axis_tvalid (udp_tx_payload_tvalid),
        .s_udp_payload_axis_tready (udp_tx_payload_tready),
        .s_udp_payload_axis_tlast  (udp_tx_payload_tlast),
        .s_udp_payload_axis_tuser  (udp_tx_payload_tuser),

        .m_udp_hdr_valid           (udp_rx_hdr_valid),
        .m_udp_hdr_ready           (udp_rx_hdr_ready),

        // verilator lint_off PINCONNECTEMPTY
        .m_udp_eth_dest_mac        (),
        .m_udp_eth_src_mac         (),
        .m_udp_eth_type            (),
        .m_udp_ip_version          (),
        .m_udp_ip_ihl              (),
        .m_udp_ip_dscp             (),
        .m_udp_ip_ecn              (),
        .m_udp_ip_length           (),
        .m_udp_ip_identification   (),
        .m_udp_ip_flags            (),
        .m_udp_ip_fragment_offset  (),
        .m_udp_ip_ttl              (),
        .m_udp_ip_protocol         (),
        .m_udp_ip_header_checksum  (),
        .m_udp_ip_source_ip        (udp_rx_source_ip),
        .m_udp_ip_dest_ip          (),
        .m_udp_source_port         (udp_rx_source_port),
        .m_udp_dest_port           (udp_rx_dest_port),
        .m_udp_length              (),
        .m_udp_checksum            (),
        // verilator lint_on PINCONNECTEMPTY
        .m_udp_payload_axis_tdata  (udp_rx_payload_tdata),
        .m_udp_payload_axis_tkeep  (udp_rx_payload_tkeep),
        .m_udp_payload_axis_tvalid (udp_rx_payload_tvalid),
        .m_udp_payload_axis_tready (udp_rx_payload_tready),
        .m_udp_payload_axis_tlast  (udp_rx_payload_tlast),
        .m_udp_payload_axis_tuser  (udp_rx_payload_tuser),

        .ip_rx_busy                            (ip_rx_busy),
        .ip_tx_busy                            (ip_tx_busy),
        .udp_rx_busy                           (udp_rx_busy),
        .udp_tx_busy                           (udp_tx_busy),
        .ip_rx_error_header_early_termination  (ip_rx_err_hdr_early),
        .ip_rx_error_payload_early_termination (ip_rx_err_payload_early),
        .ip_rx_error_invalid_header            (ip_rx_err_invalid_hdr),
        .ip_rx_error_invalid_checksum          (ip_rx_err_invalid_csum),
        .ip_tx_error_payload_early_termination (ip_tx_err_payload_early),
        .ip_tx_error_arp_failed                (ip_tx_err_arp_failed),
        .udp_rx_error_header_early_termination  (udp_rx_err_hdr_early),
        .udp_rx_error_payload_early_termination (udp_rx_err_payload_early),
        .udp_tx_error_payload_early_termination (udp_tx_err_payload_early),

        .local_mac   (48'h%(local_mac)012X),
        .local_ip    (stack_local_ip),
        .gateway_ip  (32'h%(gateway_ip)08X),
        .subnet_mask (32'h%(subnet_mask)08X),
        .clear_arp_cache (1'b0)
    );

    logic [63:0] core_s_tdata, core_m_tdata;
    logic [7:0]  core_s_tkeep, core_m_tkeep;
    logic        core_s_tvalid, core_s_tready, core_s_tlast;
    logic        core_m_tvalid, core_m_tready, core_m_tlast;

    oca_udp_seam #(
        .LOCAL_IP   (32'h%(local_ip)08X),
        .LOCAL_PORT (16'd%(local_port)d)
    ) u_seam (
        .clk               (logic_clk),
        .rst_n             (rst_n_sys),
        .stack_local_ip,

        .rx_hdr_valid      (udp_rx_hdr_valid),
        .rx_hdr_ready      (udp_rx_hdr_ready),
        .rx_ip_source_ip   (udp_rx_source_ip),
        .rx_source_port    (udp_rx_source_port),
        .rx_dest_port      (udp_rx_dest_port),
        .rx_payload_tdata  (udp_rx_payload_tdata),
        .rx_payload_tkeep  (udp_rx_payload_tkeep),
        .rx_payload_tvalid (udp_rx_payload_tvalid),
        .rx_payload_tready (udp_rx_payload_tready),
        .rx_payload_tlast  (udp_rx_payload_tlast),
        .rx_payload_tuser  (udp_rx_payload_tuser),

        .tx_hdr_valid      (udp_tx_hdr_valid),
        .tx_hdr_ready      (udp_tx_hdr_ready),
        .tx_ip_dscp        (udp_tx_ip_dscp),
        .tx_ip_ecn         (udp_tx_ip_ecn),
        .tx_ip_ttl         (udp_tx_ip_ttl),
        .tx_ip_source_ip   (udp_tx_ip_source_ip),
        .tx_ip_dest_ip     (udp_tx_ip_dest_ip),
        .tx_source_port    (udp_tx_source_port),
        .tx_dest_port      (udp_tx_dest_port),
        .tx_length         (udp_tx_length),
        .tx_checksum       (udp_tx_checksum),
        .tx_payload_tdata  (udp_tx_payload_tdata),
        .tx_payload_tkeep  (udp_tx_payload_tkeep),
        .tx_payload_tvalid (udp_tx_payload_tvalid),
        .tx_payload_tready (udp_tx_payload_tready),
        .tx_payload_tlast  (udp_tx_payload_tlast),
        .tx_payload_tuser  (udp_tx_payload_tuser),

        .core_s_tdata      (core_s_tdata),
        .core_s_tkeep      (core_s_tkeep),
        .core_s_tvalid     (core_s_tvalid),
        .core_s_tready     (core_s_tready),
        .core_s_tlast      (core_s_tlast),
        .core_m_tdata      (core_m_tdata),
        .core_m_tkeep      (core_m_tkeep),
        .core_m_tvalid     (core_m_tvalid),
        .core_m_tready     (core_m_tready),
        .core_m_tlast      (core_m_tlast),

        .cnt_accepted,
        .cnt_drop_short,
        .cnt_drop_port,
        .cnt_drop_full,
        .cnt_drop_nohdr,
        .cnt_tuser,
        .cnt_resp_orphan,
        .hdr_q_watermark
    );

    oca_core u_core (
        .clk           (logic_clk),
        .rst_n         (rst_n_sys),
        .s_axis_tdata  (core_s_tdata),
        .s_axis_tkeep  (core_s_tkeep),
        .s_axis_tvalid (core_s_tvalid),
        .s_axis_tready (core_s_tready),
        .s_axis_tlast  (core_s_tlast),
        .m_axis_tdata  (core_m_tdata),
        .m_axis_tkeep  (core_m_tkeep),
        .m_axis_tvalid (core_m_tvalid),
        .m_axis_tready (core_m_tready),
        .m_axis_tlast  (core_m_tlast)
    );

endmodule

`resetall
"""

# The same list run_synth.py builds the oca_top target from
# (run_synth.py:145-192), in the same order, minus the three files whose
# ECP5 primitives are empty blackboxes -- ecp5_prims.sv, oca_clkrst.sv,
# oca_rgmii.sv -- and minus oca_top.sv, which the generated harness stands
# in for. A file the board needs cannot be present for synthesis and
# missing here.
SOURCES = [
    VENDOR / "lib" / "axis" / "rtl" / "arbiter.v",
    VENDOR / "lib" / "axis" / "rtl" / "priority_encoder.v",
    VENDOR / "lib" / "axis" / "rtl" / "axis_fifo.v",
    VENDOR / "lib" / "axis" / "rtl" / "axis_adapter.v",
    VENDOR / "lib" / "axis" / "rtl" / "axis_async_fifo.v",
    VENDOR / "lib" / "axis" / "rtl" / "axis_async_fifo_adapter.v",
    VENDOR / "rtl" / "lfsr.v",
    VENDOR / "rtl" / "axis_gmii_rx.v",
    VENDOR / "rtl" / "axis_gmii_tx.v",
    VENDOR / "rtl" / "eth_mac_1g.v",
    VENDOR / "rtl" / "eth_mac_1g_fifo.v",
    VENDOR / "rtl" / "eth_axis_rx.v",
    VENDOR / "rtl" / "eth_axis_tx.v",
    VENDOR / "rtl" / "arp_cache.v",
    VENDOR / "rtl" / "arp_eth_rx.v",
    VENDOR / "rtl" / "arp_eth_tx.v",
    VENDOR / "rtl" / "arp.v",
    VENDOR / "rtl" / "eth_arb_mux.v",
    VENDOR / "rtl" / "ip_eth_rx_64.v",
    VENDOR / "rtl" / "ip_eth_tx_64.v",
    VENDOR / "rtl" / "ip_arb_mux.v",
    VENDOR / "rtl" / "ip_64.v",
    VENDOR / "rtl" / "ip_complete_64.v",
    VENDOR / "rtl" / "udp_checksum_gen_64.v",
    VENDOR / "rtl" / "udp_ip_rx_64.v",
    VENDOR / "rtl" / "udp_ip_tx_64.v",
    VENDOR / "rtl" / "udp_64.v",
    VENDOR / "rtl" / "udp_complete_64.v",
    RTL / "vendor" / "oca_eth_mac_1g_fifo_64.v",
    RTL / "vendor" / "oca_eth_axis_64.v",
    RTL / "vendor" / "oca_udp_complete_64.v",
    RTL / "chacha20.sv",
    RTL / "poly1305.sv",
    RTL / "chacha20_poly1305.sv",
    RTL / "oca_keystore.sv",
    RTL / "oca_pktbuf.sv",
    RTL / "oca_proto.sv",
    RTL / "oca_core.sv",
    RTL / "oca_udp_seam.sv",
]

# %Warning-NAME: path:line:col: text, or %Error / %Error-NAME the same way.
# The path is optional: "%Error: Exiting due to N warning(s)" has none.
DIAGNOSTIC = re.compile(
    r"^%(?P<kind>Warning|Error)(?:-(?P<code>[A-Z0-9_]+))?: "
    r"(?:(?P<file>\S+?):\d+:\d+: )?")

PIN = re.compile(r"Instance has missing pin: '(?P<pin>\w+)'")

# The two findings against our own files that no edit here can answer,
# named rather than folded into the vendor count.
#
# SYNCASYNCNET on rx_rst, tx_rst and logic_rst: each reset is used
# asynchronously by eth_mac_1g_fifo's status synchronisers and synchronously
# inside axis_gmii_rx. Both usages are vendor code; the only thing our files
# contribute is the port the net is declared on, which is where Verilator
# hangs the report. run_eth_mac.py waives it for the same reason.
#
# DECLFILENAME on oca_eth_axis_64.v: it holds oca_eth_axis_rx_64 and
# oca_eth_axis_tx_64, one parse and one build of the same header, and the
# file is named for the pair. Answering the warning means splitting a file
# the board's synthesis script lists by that name.
WAIVED = {"SYNCASYNCNET", "DECLFILENAME"}

# Exactly the pins oca_top leaves unconnected on its oca_udp_complete_64
# instance, mirrored by the harness so that the thing under test is the
# board and not an improved version of it. Verilator reports every one of
# them as PINMISSING against the generated file; the gate below requires
# this set and no other, in both directions, so neither a pin dropped by
# accident nor a pin quietly connected can pass as a count.
#
# 37 pins since 2026-08-10, down from 40. m_ip_hdr_ready and
# m_ip_payload_axis_tready left it because they were load-bearing:
# udp_complete_64.v:361-365 makes ip_rx_ip_hdr_ready depend on them for any
# IPv4 frame whose protocol is not 0x11, so with both reading 0 one ICMP
# echo request was never consumed and the whole receive path wedged behind
# it with no error wire raised. clear_arp_cache left it with them, tied low.
# What remains open is the raw IP transmit port, which nothing drives, and
# the raw IP receive port's outputs, which nothing reads.
MIRRORED_OMISSIONS = {
    # The raw IP transmit port, which this design never drives. Only
    # s_ip_hdr_valid and s_ip_payload_axis_tvalid decide anything, and
    # reading 0 is what keeps the port idle.
    "s_ip_hdr_valid", "s_ip_hdr_ready", "s_ip_dscp", "s_ip_ecn",
    "s_ip_length", "s_ip_ttl", "s_ip_protocol", "s_ip_source_ip",
    "s_ip_dest_ip", "s_ip_payload_axis_tdata", "s_ip_payload_axis_tkeep",
    "s_ip_payload_axis_tvalid", "s_ip_payload_axis_tready",
    "s_ip_payload_axis_tlast", "s_ip_payload_axis_tuser",
    # The raw IP receive port's outputs. The frame is accepted and dropped,
    # so its header fields and payload go nowhere by design; the two ready
    # pins that make that happen are no longer in this set.
    "m_ip_hdr_valid", "m_ip_eth_dest_mac", "m_ip_eth_src_mac",
    "m_ip_eth_type", "m_ip_version", "m_ip_ihl", "m_ip_dscp", "m_ip_ecn",
    "m_ip_length", "m_ip_identification", "m_ip_flags",
    "m_ip_fragment_offset", "m_ip_ttl", "m_ip_protocol",
    "m_ip_header_checksum", "m_ip_source_ip", "m_ip_dest_ip",
    "m_ip_payload_axis_tdata", "m_ip_payload_axis_tkeep",
    "m_ip_payload_axis_tvalid", "m_ip_payload_axis_tlast",
    "m_ip_payload_axis_tuser",
}


def failed_tests() -> int:
    """Red tests in the run that just finished.

    runner.test() only inspects results.xml under pytest, and Verilator exits
    0 on $finish even with failing tests: without this check the process exits
    0 however the suite went, and anything driving these runners by exit code
    would call a red suite green.
    """
    num_tests, num_failed = get_results(SIM_DIR / "results.xml")
    if num_tests == 0:
        raise RuntimeError("results.xml records no tests")
    return num_failed


def lint(harness: Path) -> int:
    """-Wall over the whole elaborated path. Only our own files are fatal."""
    proc = subprocess.run(
        [str(VERILATOR_BIN / "verilator"), "--lint-only", "-Wall", "-Wno-fatal",
         "--timescale-override", TIMESCALE,
         *[str(src) for src in SOURCES], str(harness),
         "--top-module", TOPLEVEL],
        capture_output=True, text=True)

    ours, theirs, waived, mirrored = [], 0, 0, set()
    for line in (proc.stdout + proc.stderr).splitlines():
        match = DIAGNOSTIC.match(line)
        if not match:
            continue
        path = match.group("file")
        pin = PIN.search(line)
        if path is None:
            # Only the "Exiting due to N warning(s)" tail has no file, and
            # -Wno-fatal means it cannot appear. Anything else without one is
            # ours by default: an unattributable error is not a pass.
            if "Exiting due to" not in line:
                ours.append(line)
        elif "verilog-ethernet" in path:
            theirs += 1
        elif match.group("code") == "PINMISSING" and path == str(harness) and pin:
            mirrored.add(pin.group("pin"))
        elif match.group("kind") == "Warning" and match.group("code") in WAIVED:
            waived += 1
        else:
            ours.append(line)

    if proc.returncode != 0 and not ours:
        # The design did not elaborate and no diagnostic carried the blame.
        print(proc.stdout + proc.stderr, flush=True)
        print(f"lint: verilator exited {proc.returncode}: FAILED", flush=True)
        return 1
    if mirrored != MIRRORED_OMISSIONS:
        for pin in sorted(mirrored - MIRRORED_OMISSIONS):
            print(f"harness leaves '{pin}' unconnected and oca_top does not",
                  flush=True)
        for pin in sorted(MIRRORED_OMISSIONS - mirrored):
            print(f"harness connects '{pin}' and oca_top does not: the "
                  "harness is no longer the design that ships", flush=True)
        print("lint: the harness and oca_top.sv:324 disagree: FAILED",
              flush=True)
        return 1
    if ours:
        print("\n".join(ours), flush=True)
        print(f"lint: {len(ours)} finding(s) outside the vendor tree: FAILED",
              flush=True)
        return 1
    print(f"lint: ok — {theirs} finding(s) in oca/hw/vendor/verilog-ethernet, "
          f"{waived} waived {'/'.join(sorted(WAIVED))} in our wrappers, and "
          f"{len(mirrored)} pin(s) the harness leaves unconnected because "
          "oca_top.sv:324 does, nothing else in ours", flush=True)
    return 0


def write_harness() -> Path:
    BUILD.mkdir(parents=True, exist_ok=True)
    path = BUILD / "tb_oca_path.sv"
    path.write_text(HARNESS % {
        "local_mac": LOCAL_MAC, "local_ip": LOCAL_IP,
        "gateway_ip": GATEWAY_IP, "subnet_mask": SUBNET_MASK,
        "local_port": LOCAL_PORT,
        "wm_hi": (HDR_Q_DEPTH - 1).bit_length(),
    })
    return path


def main() -> int:
    harness = write_harness()
    rc = lint(harness)
    if rc != 0:
        return rc

    runner = get_runner("verilator")
    runner.build(
        sources=SOURCES + [harness],
        hdl_toplevel=TOPLEVEL,
        build_dir=BUILD,
        # The vendor tree's findings are the lint gate's business, and it has
        # already had it; repeating them on every build would bury the one
        # line that matters.
        build_args=["-Wno-lint", "-Wno-style", "-Wno-fatal",
                    "--timescale-override", TIMESCALE],
        always=True,
    )
    (SIM_DIR / "results.xml").unlink(missing_ok=True)  # never grade a stale file
    runner.test(
        hdl_toplevel=TOPLEVEL,
        test_module="test_oca_path",
        test_dir=SIM_DIR,
        build_dir=BUILD,
    )
    return 1 if failed_tests() else 0


if __name__ == "__main__":
    sys.exit(main())
