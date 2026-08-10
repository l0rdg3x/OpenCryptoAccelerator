// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * The clocking, the RGMII front end and the MAC, and nothing else.
 *
 * It exists to answer one question that the full build poses and cannot
 * answer: oca_top misses its 125 MHz receive constraint at 102.59 MHz,
 * with the critical path entirely inside the MAC's receive CRC
 * (axis_gmii_rx's crc_state -> crc_next -> error_bad_frame_next, every
 * hop attributed to verilog-ethernet's lfsr.v) and 73% of those 9.75 ns
 * spent on routing rather than logic.
 *
 * Routing-dominated has two very different causes. Either that CRC is
 * simply too deep for 8 ns on an LFE5U-45F speed 6 -- nine LUT4 levels,
 * and abc9 is already on by default in synth_lattice, so the depth is
 * what it is -- or the path is being stretched by the rest of oca_top
 * competing for the same fabric, in which case placement, not the
 * module, is the problem.
 *
 * This build removes the competition. Same device, same package, same
 * speed grade, same .lpf, same 125 MHz constraint, one tenth of the
 * design. If the receive clock closes here, the shortfall in oca_top is
 * congestion and a placement lever can reach it. If it misses here too,
 * the MAC does not run at gigabit on this part and no seed will change
 * that -- which is a conclusion worth having early, because it invalidates
 * the port cost, the MVP target and the phase plan all at once.
 *
 * The transmit side is looped back into the receive side's checker only
 * enough to keep both alive; this is a timing instrument, not a design.
 */
`default_nettype none

module oca_top_mac (
    input  var  logic       clk25,

    output var  logic       led_n,

    output var  logic       phy_rst_n,
    output var  logic       phy_mdio,
    output var  logic       phy_mdc,

    input  var  logic       rgmii_rx_clk,
    input  var  logic [3:0] rgmii_rxd,
    input  var  logic       rgmii_rx_ctl,
    output var  logic       rgmii_tx_clk,
    output var  logic [3:0] rgmii_txd,
    output var  logic       rgmii_tx_ctl
);

    logic [3:0] por_cnt;
    logic       por_n;

    always_ff @(posedge clk25) begin
        if (!por_n) begin
            por_cnt <= por_cnt + 4'd1;
        end
    end

    always_comb por_n = (por_cnt == 4'd15);

    logic clk_sys, clk_tx, pll_locked;
    logic rst_n_sys, rst_n_tx, rst_n_rx;
    logic rst_sys, rst_tx, rst_rx;
    logic phy_ready;

    logic       gmii_rx_clk;
    logic [7:0] gmii_rxd;
    logic       gmii_rx_dv, gmii_rx_er;
    logic [7:0] gmii_txd;
    logic       gmii_tx_en, gmii_tx_er;
    logic       link_up, link_full_duplex;
    logic [1:0] link_speed;
    logic       dly_cflag;

    oca_clkrst u_clkrst (
        .clk_in     (clk25),
        .ext_rst_n  (por_n),
        .clk_rx     (gmii_rx_clk),
        .clk_sys    (clk_sys),
        .clk_tx     (clk_tx),
        .pll_locked (pll_locked),
        .rst_n_sys  (rst_n_sys),
        .rst_n_tx   (rst_n_tx),
        .rst_n_rx   (rst_n_rx),
        .rst_sys    (rst_sys),
        .rst_tx     (rst_tx),
        .rst_rx     (rst_rx),
        .phy_rst_n  (phy_rst_n),
        .phy_ready  (phy_ready)
    );

    oca_rgmii #(
        .SIMULATION (1'b0)
    ) u_rgmii (
        .rgmii_rx_clk     (rgmii_rx_clk),
        .rgmii_rxd        (rgmii_rxd),
        .rgmii_rx_ctl     (rgmii_rx_ctl),
        .rgmii_tx_clk     (rgmii_tx_clk),
        .rgmii_txd        (rgmii_txd),
        .rgmii_tx_ctl     (rgmii_tx_ctl),
        .gmii_rx_clk      (gmii_rx_clk),
        .gmii_rxd         (gmii_rxd),
        .gmii_rx_dv       (gmii_rx_dv),
        .gmii_rx_er       (gmii_rx_er),
        .gmii_tx_clk      (clk_tx),
        .gmii_txd         (gmii_txd),
        .gmii_tx_en       (gmii_tx_en),
        .gmii_tx_er       (gmii_tx_er),
        .dly_loadn        (1'b1),
        .dly_move         (1'b0),
        .dly_direction    (1'b0),
        .dly_cflag        (dly_cflag),
        .rst_n            (rst_n_rx),
        .link_up          (link_up),
        .link_speed       (link_speed),
        .link_full_duplex (link_full_duplex)
    );

    logic [63:0] rx_tdata, tx_tdata;
    logic [7:0]  rx_tkeep, tx_tkeep;
    logic        rx_tvalid, rx_tready, rx_tlast, rx_tuser;
    logic        tx_tvalid, tx_tready, tx_tlast, tx_tuser;

    logic tx_error_underflow, tx_fifo_overflow, tx_fifo_bad_frame;
    logic tx_fifo_good_frame, rx_error_bad_frame, rx_error_bad_fcs;
    logic rx_fifo_overflow, rx_fifo_bad_frame, rx_fifo_good_frame;

    oca_eth_mac_1g_fifo_64 u_mac (
        .rx_clk         (gmii_rx_clk),
        .rx_rst         (rst_rx),
        .tx_clk         (clk_tx),
        .tx_rst         (rst_tx),
        .logic_clk      (clk_sys),
        .logic_rst      (rst_sys),

        .tx_axis_tdata  (tx_tdata),
        .tx_axis_tkeep  (tx_tkeep),
        .tx_axis_tvalid (tx_tvalid),
        .tx_axis_tready (tx_tready),
        .tx_axis_tlast  (tx_tlast),
        .tx_axis_tuser  (tx_tuser),

        .rx_axis_tdata  (rx_tdata),
        .rx_axis_tkeep  (rx_tkeep),
        .rx_axis_tvalid (rx_tvalid),
        .rx_axis_tready (rx_tready),
        .rx_axis_tlast  (rx_tlast),
        .rx_axis_tuser  (rx_tuser),

        .gmii_rxd       (gmii_rxd),
        .gmii_rx_dv     (gmii_rx_dv),
        .gmii_rx_er     (gmii_rx_er),
        .gmii_txd       (gmii_txd),
        .gmii_tx_en     (gmii_tx_en),
        .gmii_tx_er     (gmii_tx_er),

        .rx_clk_enable  (1'b1),
        .tx_clk_enable  (1'b1),
        .rx_mii_select  (1'b0),
        .tx_mii_select  (1'b0),

        .tx_error_underflow (tx_error_underflow),
        .tx_fifo_overflow   (tx_fifo_overflow),
        .tx_fifo_bad_frame  (tx_fifo_bad_frame),
        .tx_fifo_good_frame (tx_fifo_good_frame),
        .rx_error_bad_frame (rx_error_bad_frame),
        .rx_error_bad_fcs   (rx_error_bad_fcs),
        .rx_fifo_overflow   (rx_fifo_overflow),
        .rx_fifo_bad_frame  (rx_fifo_bad_frame),
        .rx_fifo_good_frame (rx_fifo_good_frame),

        .cfg_ifg        (8'd12),
        .cfg_tx_enable  (1'b1),
        .cfg_rx_enable  (1'b1)
    );

    // Straight loopback in the logic domain: every received frame is
    // offered back for transmission. Enough to keep both directions from
    // being optimised away, and nothing more.
    always_comb tx_tdata  = rx_tdata;
    always_comb tx_tkeep  = rx_tkeep;
    always_comb tx_tvalid = rx_tvalid;
    always_comb rx_tready = tx_tready;
    always_comb tx_tlast  = rx_tlast;
    always_comb tx_tuser  = rx_tuser;

    logic any_error, error_seen;

    always_comb any_error =
        tx_error_underflow || tx_fifo_overflow || tx_fifo_bad_frame ||
        rx_error_bad_frame || rx_error_bad_fcs || rx_fifo_overflow ||
        rx_fifo_bad_frame;

    always_ff @(posedge clk_sys or negedge rst_n_sys) begin
        if (!rst_n_sys) begin
            error_seen <= 1'b0;
        end else if (any_error) begin
            error_seen <= 1'b1;
        end
    end

    logic [25:0] beat;

    always_ff @(posedge clk_sys or negedge rst_n_sys) begin
        if (!rst_n_sys) begin
            beat <= 26'd0;
        end else begin
            beat <= beat + 26'd1;
        end
    end

    logic activity;

    always_comb activity = |{tx_fifo_good_frame, rx_fifo_good_frame,
                             phy_ready, dly_cflag, link_full_duplex,
                             link_speed, pll_locked, rst_n_tx};

    logic lit;

    always_comb begin
        if (error_seen)    lit = 1'b1;
        else if (!link_up) lit = beat[25];
        else               lit = beat[23];
    end

    always_comb led_n = ~(lit | (activity & 1'b0));

    always_comb phy_mdc  = 1'b0;
    always_comb phy_mdio = 1'b1;

endmodule

`default_nettype wire
