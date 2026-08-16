// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * Clocks and resets for the Colorlight i9 v7.2: one PLL, three clock
 * domains, both reset polarities in each of them, and the PHY's
 * power-on reset.
 *
 * The board's only oscillator is 25 MHz on P3, which prjtrellis's IO
 * database calls LLC_GPLL0T_IN — one of the twelve dedicated PLL
 * reference balls on CABGA381 — so EHXPLLL.CLKI reaches it without
 * general routing. Everything else this design runs on is made here.
 *
 * THE PLL PARAMETERS ARE ecppll's, NOT ANYONE'S RECOLLECTION. Run, and
 * the output pasted rather than paraphrased:
 *
 *   tools/trellis/bin/ecppll -n oca_pll -i 25 -o 125 --clkout1 48 \
 *       --clkin_name clk25 --clkout0_name clk_tx --clkout1_name clk_sys \
 *       --reset
 *
 *   sdiv 13
 *   Pll parameters:
 *   Refclk divisor: 1
 *   Feedback divisor: 5
 *   clkout0 divisor: 5
 *   clkout0 frequency: 125 MHz
 *   clkout1 divisor: 13
 *   clkout1 frequency: 48.0769 MHz
 *   clkout1 phase shift: 0 degrees
 *   VCO frequency: 625
 *
 * CPHASE=2 on both outputs is ecppll's own arithmetic and not a phase
 * shift we asked for: primary_cphase is half an output period expressed
 * in VCO cycles, 0.5 * (1/125 MHz) * 625 MHz = 2.5, truncated to 2
 * (libtrellis/tools/ecppll.cpp:301), and a secondary output with zero
 * requested phase inherits it. The design document's earlier run asked
 * for a 90-degree copy of 125 MHz and got CLKOS_CPHASE=3, FPHASE=2;
 * that copy is not built any more, because oca_rgmii.sv makes the
 * transmit clock out of an ODDRX1F fed from a constant edge pair rather
 * than from a second PLL output.
 *
 * EHXPLLL IS DECLARED IN ecp5_prims.sv, for the reason that file gives:
 * a module that reaches read_slang by way of read_verilog arrives
 * already elaborated, so overriding CLKOS_DIV from here against yosys's
 * own cells_bb.v would fail. Declaring it ourselves puts all 36
 * parameters in front of the frontend that elaborates this file. It has
 * a second effect worth knowing: the blackbox's declared defaults are
 * emitted into the netlist even where this instance does not override
 * them, so CLKOS2_ENABLE and CLKOS3_ENABLE arrive as DISABLED. A bare
 * ecppll module omits them, and nextpnr's own default for an absent
 * CLKOS2_ENABLE is ENABLED (ecp5/bitstream.cc:1236) — two unused output
 * dividers switching for nothing.
 *
 * THE FOUR ANALOGUE SETTINGS ARE ATTRIBUTES, NOT PARAMETERS, and that
 * is not a stylistic choice. nextpnr reads ICP_CURRENT, LPF_RESISTOR,
 * MFG_ENABLE_FILTEROPAMP and MFG_GMCREF_SEL from the cell's attribute
 * map (ecp5/bitstream.cc:1278-1300) and defaults every one of them to
 * zero. Written as parameters they would be ignored and the bitstream
 * would carry a charge pump with no current. The values are ecppll's
 * for this VCO.
 *
 * FEEDBK_PATH is CLKOP, which is external feedback: CLKFB is wired to
 * the CLKOP output net, so the loop closes through the clock tree the
 * transmit side actually uses.
 *
 * WHAT THE ELABORATION GUARDS BELOW ARE FOR. They look like arithmetic
 * anyone could do by eye, and that is the point — nothing downstream
 * does it. A VCO outside the legal 400-800 MHz band is reported by
 * nextpnr with log_info and nothing stronger (ecp5/pack.cc:3006), so it
 * scrolls past in a build that otherwise succeeds and produces a PLL
 * that never locks. ecppll will happily compute a phase detector rate
 * of 3.125 MHz where the datasheet and LiteX both say 10 MHz. And a
 * transmit clock that is not exactly 125 MHz is a gigabit link that
 * does not come up, with nothing in the FPGA to say why. Each guard
 * fires at elaboration, in Verilator and in slang, so an edited divider
 * fails the lint rather than the board.
 *
 * IF 48.08 MHz DOES NOT CLOSE TIMING. clk_tx is an integer division of
 * the same VCO, so the VCO must be a multiple of 125 MHz, and the
 * 400-800 MHz band the guard below enforces leaves exactly 500, 625 and
 * 750. From those, clk_sys near this range can be 45.45, 46.88, 48.08,
 * 50.00 or 52.08: the ladder is coarse, and a design that misses 48.08
 * drops to 46.88. (This comment said the VCO was pinned at 625 MHz by
 * the transmit clock until 2026-08-10. It is 625 because ecppll breaks a
 * tie among equal-error candidates by taking the VCO nearest 600 MHz,
 * libtrellis/tools/ecppll.cpp:293 — a preference, not a constraint.)
 *
 * The measured Fmax behind this choice — oca_core around 48-52 MHz,
 * oca_dual 50.4 — was taken out of context, with no pins and no
 * Ethernet stack sharing the fabric, so it is an upper bound on what
 * the pinned design will reach and not a promise. oca_top bore that out
 * and then some: as of 2026-08-11 it closes nothing. clk_sys itself is
 * not the problem -- it clears 48.08 on 20 of the 32 seeds measured and
 * reaches 50.44 at best -- but rgmii_rx_clk clears 125 MHz on none of
 * them, and the seed that comes closest overall (10) has clk_sys at
 * 47.40. So moving clk_sys would not help until the receive clock
 * closes. 50.00 has been asked for once, at one seed -- CLKOP_DIV 4,
 * CLKOS_DIV 10, VCO 500 -- and reached 48.22; that is one placement,
 * not a sweep, so the rung above is untested rather than ruled out.
 *
 * ----------------------------------------------------------------------
 * RESETS
 * ----------------------------------------------------------------------
 *
 * Two polarities, because this design has two conventions in it and
 * neither side is going to change. Our RTL takes an active-low
 * asynchronous rst_n and assigns every register in the reset branch;
 * verilog-ethernet's datapath takes an active-HIGH SYNCHRONOUS reset —
 * eth_mac_1g_fifo's rx_rst, tx_rst and logic_rst, and udp_complete_64's
 * rst. Both polarities of a domain come off the same flip-flop and are
 * inverted combinationally, so they can never disagree by more than an
 * inverter: derived from two separate synchronisers they could have
 * differed for a whole cycle, with half the design out of reset and
 * half of it still in.
 *
 * Each domain gets its own synchroniser and no domain is reset from
 * another's clock. Assertion is asynchronous and needs no clock at all,
 * which is the point: a domain whose clock has stopped still enters
 * reset immediately. Release is synchronous to that domain's own clock,
 * so a domain whose clock has never started never leaves reset.
 *
 * WHY EACH FLOP IN THE SYNCHRONISER IS THERE. This is the failure that
 * shows up once every few hours on a bench and never in simulation, so
 * it is written out rather than assumed.
 *
 *   Stage 0 is the flop that is allowed to go metastable. Its D input
 *   is a hard 1 while its asynchronous clear is being released, so when
 *   arst_n rises inside its recovery/removal window the flop is
 *   genuinely deciding between 0 and 1 and may settle late.
 *
 *   Stage 1 resolves it. It gets a full period of its own clock, less
 *   stage 0's clock-to-Q and the routing between them, for that
 *   decision to finish. Its own removal window is violated at the same
 *   instant — both flops share one asynchronous clear — and that cannot
 *   hurt: at the release edge stage 0 still reads 0, which is exactly
 *   stage 1's reset value, so both outcomes of a failed removal agree
 *   and there is nothing left to be metastable about.
 *
 * That asymmetry is why two stages is the minimum and why one is not a
 * cheaper version of the same thing. RST_SYNC_STAGES below can be
 * raised; a value of 1 is refused, because it would build a
 * synchroniser that passes every simulation and synchronises nothing.
 *
 * clk_rx IS NOT THERE UNTIL THE LINK IS. It is the PHY's recovered
 * receive clock, so before the link comes up there are no edges,
 * rst_n_rx never releases, and everything held by it stays in reset —
 * which is correct, and is also why nothing in that domain may be read
 * from clk_sys without a crossing that tolerates a stopped source.
 * Three consequences worth having in front of you at the bench: the
 * receive domain silently does nothing when the link is down and that
 * looks identical to a broken receive path; the clock reappears at 25
 * MHz at 100BASE-TX and 2.5 MHz at 10BASE-T, so a link that negotiates
 * down leaves the domain running at a fifth or a fiftieth of its
 * constraint; and the reset asserts asynchronously when the link drops
 * even though it can only release synchronously when the link returns.
 *
 * The reset root is the PLL's LOCK gated with ext_rst_n. Holding the
 * receive domain in reset until the system PLL locks is deliberate even
 * though the two are independent: everything the receive path feeds
 * lives in clk_sys, so releasing it earlier would only fill a FIFO
 * nobody is draining.
 *
 * ----------------------------------------------------------------------
 * THE PHY RESET
 * ----------------------------------------------------------------------
 *
 * From the B50612D datasheet (B50612D-DS100-R, Table 86 "Reset Timing",
 * page 148), which is a scanned PDF the project has since put through
 * OCR:
 *
 *   RESET_PU    power up to RESET deassertion            min 10 ms
 *   RESET_WAIT  RESET deassertion to normal operation    min 20 us
 *   RESET_LEN   RESET pulse length                       min  2 us
 *
 * and, in the body text, "The B50612D requires a hardware RESET prior
 * to normal operation" with "MII register read/write access and normal
 * PHY operation can start at the end of the RESET_WAIT time."
 *
 * RESET_PU is measured from power-up, and this counter starts from PLL
 * lock, which is after the oscillator has started, after the FPGA has
 * finished configuring, after power-up. So counting the full 10 ms from
 * here satisfies a requirement that only asked for 10 ms from a strictly
 * earlier instant, with the whole configuration time as unearned
 * margin. It satisfies RESET_LEN by four orders of magnitude.
 *
 * phy_ready is the RESET_WAIT that follows, and it exists because that
 * 20 us is a datasheet requirement no other module is placed to
 * enforce: MDIO reads issued before it are not promised to answer. It
 * may be left unconnected if nothing here talks MDIO yet.
 *
 * Both PHYs on this board share one reset line and one MDIO bus, so
 * this reset is not addressed to port 0 — it resets both.
 *
 * ----------------------------------------------------------------------
 * WHAT A SIMULATION OF THIS MODULE CAN AND CANNOT SHOW
 * ----------------------------------------------------------------------
 *
 * There is no SIMULATION branch here, unlike oca_rgmii.sv, because
 * there is nothing honest to put in one: a PLL multiplies a clock, and
 * synthesisable RTL cannot. A testbench therefore drives clk_sys,
 * clk_tx and clk_rx itself and gets no pll_locked from the blackbox. It
 * can exercise the reset sequencing, the counter and the release order
 * at the real cycle counts; it cannot show the PLL locking, and it
 * cannot show the metastability the synchroniser exists for, which no
 * simulation ever shows.
 *
 * ----------------------------------------------------------------------
 * WIRING
 * ----------------------------------------------------------------------
 *
 *   clk_sys, rst_n_sys  -> oca_core
 *   clk_sys, rst_sys    -> udp_complete_64, eth_mac_1g_fifo logic side
 *   clk_tx,  rst_tx     -> eth_mac_1g_fifo tx side, oca_rgmii gmii_tx_clk
 *   clk_rx,  rst_rx     -> eth_mac_1g_fifo rx side
 *   clk_rx,  rst_n_rx   -> oca_rgmii rst_n
 *   phy_rst_n           -> P4
 */
module oca_clkrst #(
    // Synchroniser depth. Two is the minimum that synchronises anything
    // (see the header); raising it costs one flop per domain per stage
    // and buys mean-time-between-failure.
    parameter int RST_SYNC_STAGES = 2,
    // B50612D Table 86: RESET_PU >= 10 ms, RESET_WAIT >= 20 us.
    parameter int PHY_RST_MS      = 10,
    parameter int PHY_WAIT_US     = 20,
    // The PLL divider set. The defaults are ecppll's answer for 25 MHz
    // in, 125 MHz and 48 MHz out, transcribed from the run quoted in
    // the header — the configuration bring-up step 3 measured on
    // silicon, and the default elaboration is that design unchanged.
    // Parameters rather than localparams so a variant top can ask for
    // another rung of the clk_sys ladder (the header's IF 48.08 MHz
    // DOES NOT CLOSE TIMING); every guard below recomputes from them,
    // so a set the PLL cannot make still fails the lint rather than
    // the board.
    parameter int CLKI_DIV        = 1,
    parameter int CLKFB_DIV       = 5,
    parameter int CLKOP_DIV       = 5,
    parameter int CLKOS_DIV       = 13
) (
    // 25 MHz board oscillator, P3 (LLC_GPLL0T_IN).
    input  logic clk_in,
    // Asynchronous, active low, from wherever the top wants one. Tie to
    // 1'b1 if the design has no reset pin; the PLL lock below is then
    // the only reset root, which is enough to bring the design up but
    // leaves no way to restart it short of reconfiguring.
    input  logic ext_rst_n,
    // The PHY's recovered receive clock, straight off the pad at H2.
    // Absent until the link is up. See the header.
    input  logic clk_rx,

    output logic clk_sys,
    output logic clk_tx,
    output logic pll_locked,

    // Active low, asynchronous assert, synchronous release, one per
    // domain. For our RTL.
    output logic rst_n_sys,
    output logic rst_n_tx,
    output logic rst_n_rx,
    // The same three inverted, for verilog-ethernet.
    output logic rst_sys,
    output logic rst_tx,
    output logic rst_rx,

    // To the PHY reset pin, P4. Shared by both PHYs on this board.
    output logic phy_rst_n,
    // High once RESET_WAIT has elapsed: MDIO may be used from here.
    output logic phy_ready
);

    // The board's oscillator. Not a parameter: every clock this module
    // makes is made from the 25 MHz on P3.
    localparam int CLKI_HZ    = 25_000_000;

    localparam int PFD_HZ     = CLKI_HZ / CLKI_DIV;
    localparam int VCO_HZ     = PFD_HZ * CLKFB_DIV * CLKOP_DIV;
    localparam int CLK_TX_HZ  = VCO_HZ / CLKOP_DIV;
    localparam int CLK_SYS_HZ = VCO_HZ / CLKOS_DIV;

    // ecppll's own arithmetic for the output phase, recomputed rather
    // than transcribed so a divider override keeps ecppll's phase too:
    // half an output period in VCO cycles, truncated (the header, and
    // libtrellis/tools/ecppll.cpp:301). CLKOP_DIV/2 because one CLKOP
    // period is exactly CLKOP_DIV VCO cycles; 2 for both the 625 and
    // the 500 MHz VCO. The secondary output inherits it, per the header.
    localparam int CLKOP_CPH  = CLKOP_DIV / 2;
    localparam int CLKOS_CPH  = CLKOP_CPH;

    // Each of these is the only check of its kind anywhere in the flow;
    // the header says which tool declines to make it.
    if (VCO_HZ < 400_000_000 || VCO_HZ > 800_000_000) begin : gen_bad_vco
        $fatal(1, "oca_clkrst: VCO %0d Hz outside the legal 400-800 MHz range",
               VCO_HZ);
    end
    if (PFD_HZ < 10_000_000) begin : gen_bad_pfd
        $fatal(1, "oca_clkrst: phase detector %0d Hz below the 10 MHz minimum",
               PFD_HZ);
    end
    if (CLK_TX_HZ != 125_000_000) begin : gen_bad_tx_clock
        $fatal(1, "oca_clkrst: clk_tx is %0d Hz; RGMII gigabit needs exactly 125 MHz",
               CLK_TX_HZ);
    end
    if (RST_SYNC_STAGES < 2) begin : gen_bad_sync_stages
        $fatal(1, "oca_clkrst: RST_SYNC_STAGES must be >= 2 (got %0d)",
               RST_SYNC_STAGES);
    end
    // The lower bounds are the datasheet's. The upper bounds keep the
    // cycle counts below inside a 32-bit int: past them the localparam
    // wraps negative, the counter target becomes a small number, and the
    // PHY gets a reset far shorter than the one that was asked for, with
    // nothing to show for it.
    if (PHY_RST_MS < 10 || PHY_RST_MS > 1000) begin : gen_bad_phy_rst_ms
        $fatal(1, "oca_clkrst: PHY_RST_MS must be 10..1000 (got %0d)",
               PHY_RST_MS);
    end
    if (PHY_WAIT_US < 20 || PHY_WAIT_US > 100_000) begin : gen_bad_phy_wait_us
        $fatal(1, "oca_clkrst: PHY_WAIT_US must be 20..100000 (got %0d)",
               PHY_WAIT_US);
    end

    // Rounded up, both of them: 48_076_923 Hz is not a whole number of
    // cycles per millisecond, and truncating would land the reset a few
    // tens of nanoseconds under a datasheet minimum for no reason.
    localparam int PHY_RST_CYCLES  = ((CLK_SYS_HZ + 999) / 1000) * PHY_RST_MS;
    localparam int PHY_WAIT_CYCLES = ((CLK_SYS_HZ + 999_999) / 1_000_000)
                                     * PHY_WAIT_US;
    localparam int PHY_CNT_MAX     = PHY_RST_CYCLES + PHY_WAIT_CYCLES;
    localparam int PHY_CNT_W       = $clog2(PHY_CNT_MAX + 1);

    // ------------------------------------------------------------------
    // The PLL
    // ------------------------------------------------------------------
    /*
     * The three unused output enables and the five unused outputs are
     * left unconnected, which is what ecppll emits, and -Wall wants
     * either every pin named (PINMISSING) or every unused one named
     * with an empty connection (PINCONNECTEMPTY) — the two warnings
     * cover each other, so one of them has to be waived whatever this
     * instance does. PINMISSING is the one waived, over this
     * instantiation and nothing else, because the alternative changes
     * the netlist: driving ENCLKOS low is not something ecppll does,
     * nothing in prjtrellis or nextpnr documents what that port gates
     * when CLKOS_ENABLE is "ENABLED", and clk_sys is not the signal to
     * find out on. A forgotten pin anywhere else in this file still
     * stops the build.
     */
    /* verilator lint_off PINMISSING */
    (* ICP_CURRENT = "12" *) (* LPF_RESISTOR = "8" *)
    (* MFG_ENABLE_FILTEROPAMP = "1" *) (* MFG_GMCREF_SEL = "2" *)
    EHXPLLL #(
        .CLKI_DIV      (CLKI_DIV),
        .CLKFB_DIV     (CLKFB_DIV),
        .CLKOP_DIV     (CLKOP_DIV),
        .CLKOS_DIV     (CLKOS_DIV),
        .CLKOP_ENABLE  ("ENABLED"),
        .CLKOS_ENABLE  ("ENABLED"),
        .CLKOP_CPHASE  (CLKOP_CPH),
        .CLKOS_CPHASE  (CLKOS_CPH),
        .CLKOP_FPHASE  (0),
        .CLKOS_FPHASE  (0),
        .FEEDBK_PATH   ("CLKOP"),
        .PLLRST_ENA    ("ENABLED")
    ) u_pll (
        // Exactly the connections ecppll writes, tie values included.
        // The dynamic-phase inputs are inert with DPHASE_SOURCE
        // "DISABLED" and PHASESTEP applies on a falling edge, so the
        // tool holds the three of them high; the unused output enables
        // and the five unused outputs it leaves unconnected, and so do
        // we. Deviating from a field-proven PLL instantiation to tidy it
        // is not a trade this module is here to make.
        .CLKI         (clk_in),
        .CLKFB        (clk_tx),
        .RST          (~ext_rst_n),
        .STDBY        (1'b0),
        .PHASESEL0    (1'b0),
        .PHASESEL1    (1'b0),
        .PHASEDIR     (1'b1),
        .PHASESTEP    (1'b1),
        .PHASELOADREG (1'b1),
        .PLLWAKESYNC  (1'b0),
        .ENCLKOP      (1'b0),
        .CLKOP        (clk_tx),
        .CLKOS        (clk_sys),
        .LOCK         (pll_locked)
    );
    /* verilator lint_on PINMISSING */

    // ------------------------------------------------------------------
    // Reset root and the three synchronisers
    // ------------------------------------------------------------------
    //
    // The one asynchronous edge in the module. Everything below turns it
    // into a per-domain release; nothing below ever delays the assert.
    logic arst_n;
    always_comb arst_n = ext_rst_n && pll_locked;

    // Three explicit chains rather than one indexed over a vector of
    // clocks. The domains differ in ways an index hides — one of these
    // clocks stops when a cable is unplugged — and this is the module
    // where "each domain is released by its own clock and no other" has
    // to be readable rather than inferred.
    logic [RST_SYNC_STAGES-1:0] sync_sys, sync_tx, sync_rx;

    // verilator lint_off SYNCASYNCNET
    //
    // SYNCASYNCNET fires on exactly the thing this module is for: these
    // registers are clocked with an asynchronous clear and their last
    // stage then serves as an asynchronous reset elsewhere. That is what
    // a reset synchroniser is, and verilator cannot tell it apart from
    // the mistake it usually indicates -- a reset crossing domains with
    // nothing to resolve it.
    //
    // Linting this module alone stays silent, because rst_n_* leave as
    // outputs and nothing here consumes them; the warning appears the
    // moment a top instantiates it and uses them as resets, which is
    // every real use. Waived here, at the pattern, rather than at each
    // call site.
    always_ff @(posedge clk_sys or negedge arst_n) begin
        if (!arst_n) sync_sys <= '0;
        else         sync_sys <= {sync_sys[RST_SYNC_STAGES-2:0], 1'b1};
    end

    always_ff @(posedge clk_tx or negedge arst_n) begin
        if (!arst_n) sync_tx <= '0;
        else         sync_tx <= {sync_tx[RST_SYNC_STAGES-2:0], 1'b1};
    end

    always_ff @(posedge clk_rx or negedge arst_n) begin
        if (!arst_n) sync_rx <= '0;
        else         sync_rx <= {sync_rx[RST_SYNC_STAGES-2:0], 1'b1};
    end

    // verilator lint_on SYNCASYNCNET

    // Both polarities off the same flop, per the header.
    always_comb rst_n_sys = sync_sys[RST_SYNC_STAGES-1];
    always_comb rst_n_tx  = sync_tx[RST_SYNC_STAGES-1];
    always_comb rst_n_rx  = sync_rx[RST_SYNC_STAGES-1];
    always_comb rst_sys   = ~sync_sys[RST_SYNC_STAGES-1];
    always_comb rst_tx    = ~sync_tx[RST_SYNC_STAGES-1];
    always_comb rst_rx    = ~sync_rx[RST_SYNC_STAGES-1];

    // ------------------------------------------------------------------
    // The PHY reset
    // ------------------------------------------------------------------
    //
    // One counter for both intervals: it stops at PHY_CNT_MAX and the
    // two outputs are thresholds on it. Both are registered, so what
    // reaches P4 is a flip-flop output and not a comparator's glitch.
    logic [PHY_CNT_W-1:0] phy_cnt;
    logic                 phy_cnt_done;

    always_comb phy_cnt_done = (phy_cnt == PHY_CNT_W'(PHY_CNT_MAX));

    always_ff @(posedge clk_sys or negedge rst_n_sys) begin
        if (!rst_n_sys) begin
            phy_cnt   <= '0;
            phy_rst_n <= 1'b0;
            phy_ready <= 1'b0;
        end else begin
            if (!phy_cnt_done) phy_cnt <= phy_cnt + PHY_CNT_W'(1);
            phy_rst_n <= (phy_cnt >= PHY_CNT_W'(PHY_RST_CYCLES));
            phy_ready <= phy_cnt_done;
        end
    end

endmodule
