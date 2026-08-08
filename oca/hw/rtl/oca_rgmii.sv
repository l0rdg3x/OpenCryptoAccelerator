// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * RGMII front end for the ECP5: pads on one side, GMII on the other.
 *
 * verilog-ethernet ships rgmii_phy_if.v, but its TARGET parameter accepts
 * only SIM, GENERIC, XILINX and ALTERA and falls through to GENERIC without
 * a warning, and its oddr.v drives one register from two always blocks on
 * opposite edges, which synth_ecp5 reports as conflicting drivers on every
 * bit. So this layer is ours. The structure is a transliteration of
 * LiteEth's liteeth/phy/ecp5rgmii.py (BSD-2-Clause, Florent Kermarrec),
 * which is the only arrangement with a working precedent on this board
 * family.
 *
 * Nibble order, traced through two source files rather than assumed:
 * litex's DDROutput(i1, i2) becomes ODDRX1F(D0=i1, D1=i2) and DDRInput(o1,
 * o2) becomes IDDRX1F(Q0=o1, Q1=o2); LiteEth passes the low nibble as i1/o1
 * and the high nibble as i2/o2. So D0/Q0 carry GMII bits 3:0 on the rising
 * edge and D1/Q1 carry bits 7:4 on the falling edge, and a recovered byte is
 * {Q1, Q0}. Getting this backwards produces a link that comes up and passes
 * garbage, which is why it is written down.
 *
 * The receive delay sits on the five data lines and not on the clock. Both
 * are geometrically equivalent — the RX unit interval is 4 ns, so delaying
 * data by +2 ns is congruent to delaying the clock by 2 ns — but a DELAY
 * cell on RXC moves the clock net off the PIO's own output onto the
 * IOLOGIC's INDD, and nextpnr then re-runs its dedicated-routing test from
 * there. If that test fails the 125 MHz clock falls back to general fabric
 * routing with skew this flow does not characterise.
 *
 * DELAYF rather than DELAYG, which is where this departs from LiteEth. It is
 * the same delay element plus LOADN/MOVE/DIRECTION/CFLAG, and nextpnr moves
 * all four onto the IOLOGIC (ecp5/pack.cc:2104-2118) and sets
 * IOLOGIC.LOADNMUX=LOADN when LOADN is connected (ecp5/bitstream.cc:1318).
 * That makes the tap count movable while the design runs. The alternative is
 * one bitstream per candidate value, and the value is empirical: prjtrellis
 * carries no timing characterisation of DELAYG or DELAYF at all, so static
 * analysis says nothing about this path in either direction.
 *
 * What no simulation here can check: the tap count, the primitive mapping,
 * and whether the PHY adds a delay of its own. litex-boards drives this
 * board family with tx_delay = 0 and rx_delay = 2 ns and reports it working,
 * which is only possible if the B50612D delays its own receive side and not
 * its transmit side. That is a prediction, not a datasheet reading: if the
 * transmit direction is dead at the bench while receive is fine, it is
 * wrong and TX_DEL_VALUE is what to move.
 *
 * The transmit delays are DELAYG, so moving TX_DEL_VALUE costs a rebuild.
 * That asymmetry is deliberate: the receive taps are the ones the field
 * sweeps and the ones with no defensible starting value, while the
 * transmit side has a known-working setting to start from and one
 * falsifiable question to answer. If the bench says otherwise, the receive
 * side already shows what a dynamic version of this looks like.
 */
module oca_rgmii #(
    // Tap counts, not nanoseconds. LiteEth's 25 ps per tap is an empirical
    // constant of theirs (rx_delay/25e-12 in ecp5rgmii.py), not a Lattice
    // figure, and nothing in prjtrellis can confirm it — so the tap count
    // that corresponds to 2 ns may not be 80. Sweep, do not trust.
    parameter int RX_DEL_VALUE = 80,
    parameter int TX_DEL_VALUE = 0,
    // Behavioural DDR instead of ECP5 primitives. yosys declares IDDRX1F and
    // ODDRX1F only in cells_bb.v, as blackboxes with no behaviour, so a
    // testbench built on the primitives elaborates and captures nothing.
    parameter bit SIMULATION   = 1'b0
) (
    // RGMII pads
    input  logic       rgmii_rx_clk,
    input  logic [3:0] rgmii_rxd,
    input  logic       rgmii_rx_ctl,
    output logic       rgmii_tx_clk,
    output logic [3:0] rgmii_txd,
    output logic       rgmii_tx_ctl,

    // GMII receive, in the rgmii_rx_clk domain
    output logic       gmii_rx_clk,
    output logic [7:0] gmii_rxd,
    output logic       gmii_rx_dv,
    output logic       gmii_rx_er,

    // GMII transmit, in the gmii_tx_clk domain
    input  logic       gmii_tx_clk,
    input  logic [7:0] gmii_txd,
    input  logic       gmii_tx_en,
    input  logic       gmii_tx_er,

    // Runtime delay control, all five receive lines moved together so the
    // residual skew stays the element-to-element mismatch. Tie loadn high
    // and move low to leave the delay at RX_DEL_VALUE.
    input  logic       dly_loadn,
    input  logic       dly_move,
    input  logic       dly_direction,
    output logic       dly_cflag,

    // RGMII in-band status, sampled in the inter-frame gap. Free of MDIO,
    // and the first thing worth looking at when a link will not come up.
    input  logic       rst_n,
    output logic       link_up,
    output logic [1:0] link_speed,
    output logic       link_full_duplex
);

    // DEL_VALUE is a 7-bit field: nextpnr writes it with
    // int_to_bitvector(value, 7) (ecp5/bitstream.cc:1313), so 128 wraps to 0
    // and the design comes up with no delay at all and no diagnostic.
    if (RX_DEL_VALUE < 0 || RX_DEL_VALUE > 127) begin : gen_bad_rx_delay
        $fatal(1, "oca_rgmii: RX_DEL_VALUE must be 0..127 (got %0d)", RX_DEL_VALUE);
    end
    if (TX_DEL_VALUE < 0 || TX_DEL_VALUE > 127) begin : gen_bad_tx_delay
        $fatal(1, "oca_rgmii: TX_DEL_VALUE must be 0..127 (got %0d)", TX_DEL_VALUE);
    end

    // The recovered clock goes straight from the pad to the fabric. nextpnr
    // promotes it to a global itself: any net with IOLOGIC.CLK or
    // TRELLIS_FF.CLK users is a clock port to its is_clock_port test, and
    // there is no BUFG on this device to instantiate — the ECP5 global
    // buffer is DCCA and the placer inserts it.
    always_comb gmii_rx_clk = rgmii_rx_clk;

    // RGMII control: the rising half is RX_DV, the falling half is
    // RX_DV xor RX_ER. Same encoding on the way out.
    logic [4:0] rx_lo, rx_hi;
    logic [4:0] cflag;

    logic [4:0] tx_lo, tx_hi;
    logic [4:0] tx_ddr;

    always_comb tx_lo = {gmii_tx_en, gmii_txd[3:0]};
    always_comb tx_hi = {gmii_tx_en ^ gmii_tx_er, gmii_txd[7:4]};

    always_comb gmii_rxd   = {rx_hi[3:0], rx_lo[3:0]};
    always_comb gmii_rx_dv = rx_lo[4];
    always_comb gmii_rx_er = rx_lo[4] ^ rx_hi[4];
    always_comb dly_cflag  = |cflag;

    generate
        if (SIMULATION) begin : gen_sim
            // Behavioural DDR. Not a model of the primitives' timing — it
            // captures on the two edges and nothing more — so it exercises
            // the nibble order and the control encoding, which is all a
            // simulation of this layer can honestly claim.
            logic [4:0] rx_rise, rx_fall;
            logic [4:0] tx_rise, tx_fall;

            always_ff @(posedge rgmii_rx_clk or negedge rst_n) begin
                if (!rst_n) rx_rise <= '0;
                else        rx_rise <= {rgmii_rx_ctl, rgmii_rxd};
            end
            always_ff @(negedge rgmii_rx_clk or negedge rst_n) begin
                if (!rst_n) rx_fall <= '0;
                else        rx_fall <= {rgmii_rx_ctl, rgmii_rxd};
            end
            always_comb rx_lo = rx_rise;
            always_comb rx_hi = rx_fall;

            always_ff @(posedge gmii_tx_clk or negedge rst_n) begin
                if (!rst_n) tx_rise <= '0;
                else        tx_rise <= tx_lo;
            end
            always_ff @(posedge gmii_tx_clk or negedge rst_n) begin
                if (!rst_n) tx_fall <= '0;
                else        tx_fall <= tx_hi;
            end
            always_comb tx_ddr = gmii_tx_clk ? tx_rise : tx_fall;

            always_comb rgmii_tx_clk = gmii_tx_clk;
            always_comb rgmii_txd    = tx_ddr[3:0];
            always_comb rgmii_tx_ctl = tx_ddr[4];
            always_comb cflag        = '0;

            // There is no delay element to move in this branch, so the
            // three control inputs have no reader. Absorbing them keeps
            // -Wall clean without hiding a real unused signal elsewhere.
            logic unused_ok;
            always_comb unused_ok = dly_loadn | dly_move | dly_direction;
        end else begin : gen_ecp5
            // Receive: one DELAYF per line, then the DDR register. The
            // delay cell has to touch the top-level port directly and the
            // PIO has to be pin-constrained, or nextpnr will not absorb it
            // into the IOLOGIC.
            logic [4:0] rx_pad;
            logic [4:0] rx_delayed;
            always_comb rx_pad = {rgmii_rx_ctl, rgmii_rxd};

            for (genvar i = 0; i < 5; i++) begin : gen_rx
                // DEL_MODE is a nextpnr lookup that an explicit DEL_VALUE
                // overrides (ecp5/pack.cc:2098-2103). It is inert in
                // silicon: the tile carries only DEL_VALUE, OUTDEL and
                // WAIT_FOR_EDGE fuses, and no calibration of any kind, so
                // the delay is open loop and drifts with voltage and
                // temperature. Kept to match LiteEth.
                DELAYF #(
                    .DEL_MODE ("SCLK_ALIGNED"),
                    .DEL_VALUE(RX_DEL_VALUE)
                ) u_rx_delay (
                    .A        (rx_pad[i]),
                    .LOADN    (dly_loadn),
                    .MOVE     (dly_move),
                    .DIRECTION(dly_direction),
                    .Z        (rx_delayed[i]),
                    .CFLAG    (cflag[i])
                );
                IDDRX1F u_rx_ddr (
                    .D   (rx_delayed[i]),
                    .SCLK(rgmii_rx_clk),
                    .RST (1'b0),
                    .Q0  (rx_lo[i]),
                    .Q1  (rx_hi[i])
                );
            end

            // Transmit: DDR register then delay, which nextpnr turns into
            // DELAY.OUTDEL on the same IOLOGIC.
            logic [4:0] tx_pad;
            for (genvar i = 0; i < 5; i++) begin : gen_tx
                ODDRX1F u_tx_ddr (
                    .D0  (tx_lo[i]),
                    .D1  (tx_hi[i]),
                    .SCLK(gmii_tx_clk),
                    .RST (1'b0),
                    .Q   (tx_ddr[i])
                );
                DELAYG #(
                    .DEL_MODE ("SCLK_ALIGNED"),
                    .DEL_VALUE(0)
                ) u_tx_delay (
                    .A(tx_ddr[i]),
                    .Z(tx_pad[i])
                );
            end
            always_comb rgmii_txd    = tx_pad[3:0];
            always_comb rgmii_tx_ctl = tx_pad[4];

            // The transmit clock is a copy of gmii_tx_clk emitted through a
            // DDR register, edge aligned with the data. LiteEth builds it
            // the same way, from rising = 1 and falling = 0, rather than
            // from a 90-degree PLL output as verilog-ethernet does: one PLL
            // output instead of two, and no clk-to-clk90 handoff that
            // nextpnr never analysed.
            logic tx_clk_ddr;
            ODDRX1F u_tx_clk_ddr (
                .D0  (1'b1),
                .D1  (1'b0),
                .SCLK(gmii_tx_clk),
                .RST (1'b0),
                .Q   (tx_clk_ddr)
            );
            DELAYG #(
                .DEL_MODE ("SCLK_ALIGNED"),
                .DEL_VALUE(TX_DEL_VALUE)
            ) u_tx_clk_delay (
                .A(tx_clk_ddr),
                .Z(rgmii_tx_clk)
            );
        end
    endgenerate

    // In-band status rides the data lines while the control lines are both
    // low, which is the inter-frame gap. Nothing else decodes it, so a
    // bring-up can read link state without bringing up MDIO first.
    always_ff @(posedge rgmii_rx_clk or negedge rst_n) begin
        if (!rst_n) begin
            link_up          <= 1'b0;
            link_speed       <= 2'b00;
            link_full_duplex <= 1'b0;
        end else if (!rx_lo[4] && !rx_hi[4]) begin
            link_up          <= rx_lo[0];
            link_speed       <= rx_lo[2:1];
            link_full_duplex <= rx_lo[3];
        end
    end

endmodule
