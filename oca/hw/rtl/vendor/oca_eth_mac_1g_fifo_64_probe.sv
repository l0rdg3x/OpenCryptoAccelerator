// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * Synthesis probe for oca_eth_mac_1g_fifo_64.
 *
 * Not part of any build. It exists so the wrapper can be pushed through the
 * exact frontend combination the real build uses -- vendor Verilog and the
 * wrapper via read_verilog, our SystemVerilog via read_slang, instantiating
 * the wrapper with no parameter overrides -- because that combination is
 * what the wrapper exists to survive, and a wrapper that only passes
 * read_verilog on its own has not been tested.
 *
 * Every port is carried to the probe's own boundary so nothing is optimised
 * away and the area figures characterise the wrapper rather than what
 * survived constant propagation.
 *
 * From the repository root, with tools/yosys built by
 * scripts/build-toolchain.sh:
 *
 *   timeout 900 tools/yosys/bin/yosys -p "read_verilog \
 *     -Ioca/hw/vendor/verilog-ethernet/rtl \
 *     -Ioca/hw/vendor/verilog-ethernet/lib/axis/rtl \
 *     oca/hw/vendor/verilog-ethernet/rtl/eth_mac_1g_fifo.v \
 *     oca/hw/vendor/verilog-ethernet/rtl/eth_mac_1g.v \
 *     oca/hw/vendor/verilog-ethernet/rtl/axis_gmii_rx.v \
 *     oca/hw/vendor/verilog-ethernet/rtl/axis_gmii_tx.v \
 *     oca/hw/vendor/verilog-ethernet/rtl/lfsr.v \
 *     oca/hw/vendor/verilog-ethernet/lib/axis/rtl/axis_async_fifo_adapter.v \
 *     oca/hw/vendor/verilog-ethernet/lib/axis/rtl/axis_async_fifo.v \
 *     oca/hw/vendor/verilog-ethernet/lib/axis/rtl/axis_adapter.v \
 *     oca/hw/rtl/vendor/oca_eth_mac_1g_fifo_64.v; \
 *     read_slang --top oca_eth_mac_1g_fifo_64_probe \
 *     oca/hw/rtl/vendor/oca_eth_mac_1g_fifo_64_probe.sv; \
 *     hierarchy -check -top oca_eth_mac_1g_fifo_64_probe; \
 *     synth_ecp5 -top oca_eth_mac_1g_fifo_64_probe; stat"
 */
module oca_eth_mac_1g_fifo_64_probe (
    input  logic        rx_clk,
    input  logic        rx_rst,
    input  logic        tx_clk,
    input  logic        tx_rst,
    input  logic        logic_clk,
    input  logic        logic_rst,

    input  logic [63:0] tx_axis_tdata,
    input  logic [7:0]  tx_axis_tkeep,
    input  logic        tx_axis_tvalid,
    output logic        tx_axis_tready,
    input  logic        tx_axis_tlast,
    input  logic        tx_axis_tuser,

    output logic [63:0] rx_axis_tdata,
    output logic [7:0]  rx_axis_tkeep,
    output logic        rx_axis_tvalid,
    input  logic        rx_axis_tready,
    output logic        rx_axis_tlast,
    output logic        rx_axis_tuser,

    input  logic [7:0]  gmii_rxd,
    input  logic        gmii_rx_dv,
    input  logic        gmii_rx_er,
    output logic [7:0]  gmii_txd,
    output logic        gmii_tx_en,
    output logic        gmii_tx_er,

    input  logic        rx_clk_enable,
    input  logic        tx_clk_enable,
    input  logic        rx_mii_select,
    input  logic        tx_mii_select,

    output logic        tx_error_underflow,
    output logic        tx_fifo_overflow,
    output logic        tx_fifo_bad_frame,
    output logic        tx_fifo_good_frame,
    output logic        rx_error_bad_frame,
    output logic        rx_error_bad_fcs,
    output logic        rx_fifo_overflow,
    output logic        rx_fifo_bad_frame,
    output logic        rx_fifo_good_frame,

    input  logic [7:0]  cfg_ifg,
    input  logic        cfg_tx_enable,
    input  logic        cfg_rx_enable
);

    oca_eth_mac_1g_fifo_64 u_mac (
        .rx_clk,
        .rx_rst,
        .tx_clk,
        .tx_rst,
        .logic_clk,
        .logic_rst,

        .tx_axis_tdata,
        .tx_axis_tkeep,
        .tx_axis_tvalid,
        .tx_axis_tready,
        .tx_axis_tlast,
        .tx_axis_tuser,

        .rx_axis_tdata,
        .rx_axis_tkeep,
        .rx_axis_tvalid,
        .rx_axis_tready,
        .rx_axis_tlast,
        .rx_axis_tuser,

        .gmii_rxd,
        .gmii_rx_dv,
        .gmii_rx_er,
        .gmii_txd,
        .gmii_tx_en,
        .gmii_tx_er,

        .rx_clk_enable,
        .tx_clk_enable,
        .rx_mii_select,
        .tx_mii_select,

        .tx_error_underflow,
        .tx_fifo_overflow,
        .tx_fifo_bad_frame,
        .tx_fifo_good_frame,
        .rx_error_bad_frame,
        .rx_error_bad_fcs,
        .rx_fifo_overflow,
        .rx_fifo_bad_frame,
        .rx_fifo_good_frame,

        .cfg_ifg,
        .cfg_tx_enable,
        .cfg_rx_enable
    );

endmodule
