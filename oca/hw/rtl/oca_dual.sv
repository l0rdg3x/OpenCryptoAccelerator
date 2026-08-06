// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * Two oca_core on one device: what a pair costs in area and clock.
 *
 * The Colorlight i9 v7.2 carries two gigabit PHYs and one core cannot
 * saturate one of them, so a pair is the configuration worth measuring —
 * and measuring it is all this module is for. What the MVP is actually
 * wired as depends on what fits beside the cores, and with one Ethernet
 * port's area measured, two cores and two ports do not fit this device:
 * the MVP that fits is one core on one port (AGENTS.md;
 * docs/design/2026-08-05-ethernet-integration.md). Three cores do not
 * route on this device — 76.4% of the LUTs and 83.3% of the multipliers
 * fit, and nextpnr still leaves ~50000 arcs unrouted at any constraint
 * from 35 to 100 MHz, which is congestion rather than timing (README.md,
 * "The occupancy study"). Two is therefore the ceiling, not a step
 * toward more.
 *
 * Nothing is shared. Each core owns its key store, its packet buffers
 * and its engine, and the two never address each other: a shared key
 * store would put one port's keys within reach of the other port's
 * traffic, and no arbiter can be cheaper than that is dangerous. The
 * cost of that choice is the duplicated area this module exists to
 * measure.
 *
 * This is a synthesis target, not a board top level: it has no PLL, no
 * reset synchroniser and no pins. It exists so the two-core figures the
 * MVP is judged against come from one documented command rather than
 * from a wrapper rebuilt by hand each time — the earlier two-core
 * reading was taken on a netlist yosys had silently emptied of its key
 * stores, and nothing in the flow could see that it had.
 */
module oca_dual #(
    parameter int NUM_SLOTS = 8,
    parameter int BYTES     = 2048
) (
    input  logic        clk,
    input  logic        rst_n,

    input  logic [63:0] s0_axis_tdata,
    input  logic [ 7:0] s0_axis_tkeep,
    input  logic        s0_axis_tvalid,
    output logic        s0_axis_tready,
    input  logic        s0_axis_tlast,
    output logic [63:0] m0_axis_tdata,
    output logic [ 7:0] m0_axis_tkeep,
    output logic        m0_axis_tvalid,
    input  logic        m0_axis_tready,
    output logic        m0_axis_tlast,

    input  logic [63:0] s1_axis_tdata,
    input  logic [ 7:0] s1_axis_tkeep,
    input  logic        s1_axis_tvalid,
    output logic        s1_axis_tready,
    input  logic        s1_axis_tlast,
    output logic [63:0] m1_axis_tdata,
    output logic [ 7:0] m1_axis_tkeep,
    output logic        m1_axis_tvalid,
    input  logic        m1_axis_tready,
    output logic        m1_axis_tlast
);

    oca_core #(.NUM_SLOTS(NUM_SLOTS), .BYTES(BYTES)) core0 (
        .clk(clk), .rst_n(rst_n),
        .s_axis_tdata(s0_axis_tdata), .s_axis_tkeep(s0_axis_tkeep),
        .s_axis_tvalid(s0_axis_tvalid), .s_axis_tready(s0_axis_tready),
        .s_axis_tlast(s0_axis_tlast),
        .m_axis_tdata(m0_axis_tdata), .m_axis_tkeep(m0_axis_tkeep),
        .m_axis_tvalid(m0_axis_tvalid), .m_axis_tready(m0_axis_tready),
        .m_axis_tlast(m0_axis_tlast)
    );

    oca_core #(.NUM_SLOTS(NUM_SLOTS), .BYTES(BYTES)) core1 (
        .clk(clk), .rst_n(rst_n),
        .s_axis_tdata(s1_axis_tdata), .s_axis_tkeep(s1_axis_tkeep),
        .s_axis_tvalid(s1_axis_tvalid), .s_axis_tready(s1_axis_tready),
        .s_axis_tlast(s1_axis_tlast),
        .m_axis_tdata(m1_axis_tdata), .m_axis_tkeep(m1_axis_tkeep),
        .m_axis_tvalid(m1_axis_tvalid), .m_axis_tready(m1_axis_tready),
        .m_axis_tlast(m1_axis_tlast)
    );

endmodule
