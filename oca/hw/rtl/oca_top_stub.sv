// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * The smallest design that occupies every pin colorlight_i9.lpf names and
 * every clock domain the real top will have. It exists to answer one
 * question before oca_top.sv is written: which clocks does nextpnr
 * actually constrain from the .lpf, and what does it achieve on them?
 *
 * That question is not rhetorical. run_synth.py passes --timing-allow-fail
 * and defaults --freq to 100 MHz, which nextpnr applies to any clock a
 * constraint did not reach, so a FREQUENCY line that names a net wrongly
 * produces a build that reports numbers and means nothing. Finding that
 * out here costs one short build; finding it out on the full top costs a
 * long one, and finding it out on the bench costs a day.
 *
 * It is a measurement instrument, not a step toward the product: there is
 * no MAC, no UDP stack, no core. The GMII path is looped back through one
 * register per direction, which is the least logic that keeps both the
 * 125 MHz transmit domain and the recovered receive domain alive and
 * unoptimisable. Nothing here should be reused in oca_top.sv beyond the
 * port list, which the .lpf fixes anyway.
 *
 * SIMULATION is false: this instantiates the real IDDRX1F/ODDRX1F/DELAYF,
 * which is the point, since it is the pinned build that decides whether
 * they place at all.
 */
`default_nettype none

module oca_top_stub (
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

    // The board has no reset pin: the .lpf constrains 17 pads and none of
    // them is one. So the only reset root is power-on, and it is built
    // here rather than tying oca_clkrst's ext_rst_n to a constant --
    // which is not merely inelegant, it changes what the linter can
    // prove: with a constant there, verilator reports SYNCASYNCNET on
    // every reset synchroniser inside oca_clkrst, a module that is clean
    // on its own.
    //
    // ECP5 flip-flops come out of configuration cleared, so this counter
    // starts at zero on its own and holds por_n low for 16 cycles of the
    // 25 MHz input before releasing it. No initialiser is written here:
    // the reset value is the device's, and stating it as well would be a
    // second source for it.
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
        // rst_n_rx, not rst_n_sys. In the synthesised branch the only
        // thing inside oca_rgmii that this reset reaches is the in-band
        // status register, which is clocked by rgmii_rx_clk
        // (oca_rgmii.sv:281). Releasing it from the sys domain would
        // release it unsynchronised to its own clock, which is a
        // recovery/removal violation on link_up and friends -- the kind
        // that works on the bench until the day it does not.
        .rst_n            (rst_n_rx),
        .link_up          (link_up),
        .link_speed       (link_speed),
        .link_full_duplex (link_full_duplex)
    );

    // One register per direction. Not a loopback that would work on the
    // wire -- the two domains are unrelated -- but enough that neither
    // domain is empty and neither can be optimised away, which is all a
    // clock-constraint measurement needs.
    logic [7:0] rx_hold;
    logic       rx_dv_hold;

    always_ff @(posedge gmii_rx_clk or negedge rst_n_rx) begin
        if (!rst_n_rx) begin
            rx_hold    <= 8'd0;
            rx_dv_hold <= 1'b0;
        end else begin
            rx_hold    <= gmii_rxd;
            rx_dv_hold <= gmii_rx_dv ^ gmii_rx_er;
        end
    end

    always_ff @(posedge clk_tx or negedge rst_n_tx) begin
        if (!rst_n_tx) begin
            gmii_txd   <= 8'd0;
            gmii_tx_en <= 1'b0;
            gmii_tx_er <= 1'b0;
        end else begin
            gmii_txd   <= rx_hold;
            gmii_tx_en <= rx_dv_hold;
            gmii_tx_er <= 1'b0;
        end
    end

    // THIS LED IS NOT A BRING-UP INDICATOR, and the sentence that used
    // to stand here said it was: "a lit-and-steady LED means the PLL
    // never locked and a dark one means no bitstream at all".
    //
    // The defect is not which way round it reads. It is that ONE READING
    // COVERS TWO STATES the operator needs to tell apart, so the whole
    // sentence is stated below in terms of led_n, the level this module
    // drives, and never in terms of lit or dark -- the polarity is
    // litex's user_led_n and has never been seen on silicon either.
    //
    // A PLL that never locks drives led_n HIGH AND STEADY. oca_clkrst
    // gates arst_n on pll_locked (oca_clkrst.sv:378), so rst_n_sys stays
    // low, beat stays at zero, and the seven-term AND below is zero.
    //
    // A healthy board with a link can drive led_n high and steady too,
    // and nothing here can rule it out. The AND includes dly_cflag_seen,
    // a sticky bit set by any high LEVEL on dly_cflag, not by a pulse
    // (the always_ff below). dly_cflag is the OR of the receive DELAYFs'
    // CFLAG (oca_rgmii.sv:154), and this stub ties dly_move low and
    // dly_loadn high at the instance above, so no move is ever
    // commanded. Whether CFLAG nonetheless RESTS high is characterised
    // nowhere we can read: yosys carries DELAYF as a bare blackbox
    // (techlibs/lattice/cells_bb_ecp5.v), prjtrellis ships no simulation
    // model for it, and nextpnr only moves the port to the IOLOGIC bel
    // (ecp5/pack.cc:2116). So the term is unknown, in a design that
    // needs it true.
    //
    // What that leaves is a signal that says the same thing when the
    // clocking failed and when everything worked, which is the one thing
    // a bring-up indicator may not do. Whether an unconfigured device
    // gives a third reading is not claimed here: this file has no
    // PULLMODE and no source for the pad state during configuration.
    //
    // Bring-up step 2 is oca_blink.sv, which is two pins and no PLL for
    // this reason. Left as it is rather than repaired: this build is a
    // measurement instrument for clock constraints, and the AND exists
    // to keep the status outputs from being optimised away, which it
    // still does.
    logic [24:0] beat;

    always_ff @(posedge clk_sys or negedge rst_n_sys) begin
        if (!rst_n_sys) begin
            beat <= 25'd0;
        end else begin
            beat <= beat + 25'd1;
        end
    end

    // Declared before use: verilator accepts the other order and slang,
    // which is what run_synth.py reads this with, does not.
    logic dly_cflag_seen;

    always_ff @(posedge clk_sys or negedge rst_n_sys) begin
        if (!rst_n_sys) begin
            dly_cflag_seen <= 1'b0;
        end else if (dly_cflag) begin
            dly_cflag_seen <= 1'b1;
        end
    end

    // Fold the status bits into the LED rather than leaving them
    // dangling: an unused output is an output yosys deletes, and with it
    // the logic behind it that this build exists to place.
    always_comb led_n = ~(beat[24] & pll_locked & phy_ready
                          & link_up & link_full_duplex & (link_speed != 2'd3)
                          & dly_cflag_seen);

    // MDIO is not driven by this stub: the pins exist because the .lpf
    // constrains them, and a pin nextpnr cannot place is the failure this
    // build is looking for. phy_mdc idles low, phy_mdio idles high, which
    // is what the bus does when nobody is talking.
    always_comb phy_mdc  = 1'b0;
    always_comb phy_mdio = 1'b1;

    // rst_sys, rst_tx and rst_rx are the active-high forms the vendor
    // subtree needs. This stub has no vendor logic, so they are unused
    // here by design.
    logic unused;
    always_comb unused = &{1'b0, rst_sys, rst_tx, rst_rx, link_speed[0]};

endmodule

`default_nettype wire
