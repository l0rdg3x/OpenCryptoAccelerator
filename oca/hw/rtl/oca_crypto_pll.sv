// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * The crypto console on the PLL: oca_clkrst plus oca_uart_crypto, on the
 * same four pins the raw-oscillator build uses.
 *
 * oca_uart_crypto ran on the 25 MHz pin directly and closed 49.85 MHz
 * there -- seed 1, measured 2026-08-14, docs/RECORD.md -- so the clock
 * it was given was half of what that build could take. This top gives it
 * oca_clkrst's clk_sys instead: 48.0769 MHz, a 625 MHz VCO over
 * CLKOS_DIV 13.
 *
 * WHAT STEP 3 OF THE LADDER ESTABLISHED IS NOT THIS RUNG. oca_pll
 * proved on 2026-08-11 that this PLL locks off the 25 MHz pin and that
 * CLKOP runs, by counting clk_tx onto D2 at 1 Hz. clk_sys is CLKOS, a
 * different output over a different divider, and in that bitstream it
 * drove nothing: only two flip-flops of oca_clkrst survived, on the
 * transmit side (oca_pll.sv). So CLKOS has never reached a pad on this
 * board, and this design is the first to depend on it -- which is also
 * why the heartbeat below cannot be allowed to depend on it. What the
 * ladder gives us is the VCO and the lock, not the rung.
 *
 * THIS NETLIST CLOSES 48.0769, measured 2026-08-16 over four placer
 * seeds on yosys f77ddfb87 (commit 4f879ee, the bubble-and-bench
 * change), pinned against colorlight_i9_crypto.lpf:
 * 49.19 / 51.21 / 52.99 / 51.55 MHz on clk_sys, mean 51.23, spread 7.7%
 * on (max-min)/min, at 13381 TRELLIS_COMB and 12589 TRELLIS_FF. All
 * four seeds clear the constraint. The netlist before that change read
 * 50.38 / 51.65 / 52.60 / 49.21, mean 50.96, spread 6.9%, at
 * 13062 / 12529 -- 32 LUTs and 11 flip-flops more than the pre-PLL
 * top. docs/RECORD.md carries both entries and their limits.
 *
 * WHAT THAT MARGIN IS NOT is the one the raw-oscillator build had. The
 * 49.85 belongs to a design with no PLL, its heartbeat in the only
 * domain there was and its reset made internally, and it was asked for
 * 25 MHz -- a 99% margin that could not fail. This one moves all three
 * and asks for 48.0769, and the worst of the four seeds clears it by
 * 2.31%. Re-synthesise any RTL change here before believing it,
 * including one that looks harmless: equal area would not make it the
 * same placement, which is the project's own lesson about reading an
 * Fmax across two netlists.
 *
 * THREE SEED SPREADS EXIST FOR THIS DESIGN'S TOPS AND NONE ORDERS
 * ANOTHER. A four-seed sweep taken before the reset and heartbeat
 * corrections described below spread 8.1%; the 2026-08-15 netlist
 * spread 6.9%; this one spreads 7.7%. Each belongs to a different
 * netlist, and four-seed draws cannot rank netlists --
 * .claude/skills/synth-sweep says so in as many words -- so all three
 * are recorded and the comparison is refused.
 *
 * AND NOTHING HERE HAS RUN ON SILICON. No bitstream containing this
 * design has been loaded onto the board: 48.0769 MHz is a
 * place-and-route result about a netlist, not a board that answered.
 *
 * THE PLL IS HERE AND NOT IN THE CORE, and that is a testability
 * decision rather than a layering one. EHXPLLL is a body-less blackbox:
 * put it inside oca_uart_crypto and hw/sim/run_uart_crypto.py -- the
 * only suite that drives real UART bit timing through the AEAD
 * datapath, oca_uart_console and oca_uart_echo doing the same for
 * theirs -- would have to fabricate the clock it exists to test. So the core
 * keeps a clock port, takes its frequency as CLK_HZ, and stays the
 * simulation DUT; this file is the part no simulation can cover.
 *
 * TWO CONNECTIONS OF oca_clkrst ARE NOT WHAT AN ETHERNET TOP WOULD
 * DRIVE, and oca_pll settled both (oca_pll.sv:8-19 and :58-64, wired at
 * :79-81). ext_rst_n is tied high for the reason oca_clkrst's own port
 * comment gives: with no reset pin the PLL lock is the only reset root,
 * which brings the design up and leaves no way to restart it short of
 * reconfiguring. clk_rx is tied to clk25 rather than left dangling --
 * there is no recovered receive clock in a design with no PHY, and this
 * project's rule is that an undriven input is not an input reading zero
 * but whichever value deletes the most logic. Nothing here consumes
 * rst_n_rx, so the synchroniser behind that port goes with it and the
 * tie costs nothing at all: run_synth.py records oca_clkrst at 2
 * flip-flops in oca_pll, which ties the same port the same way, and
 * names the receive chain among the ones optimised away
 * (hw/syn/run_synth.py, NETLIST_FF_FLOOR).
 *
 * ----------------------------------------------------------------------
 * D2, AND WHY IT IS COUNTED ON clk25
 * ----------------------------------------------------------------------
 *
 * The heartbeat moved out of oca_uart_crypto with this file, and it did
 * not move domains by accident. Driven from clk_sys, a PLL that never
 * locks freezes D2 -- and static is the one reading this scheme reserves
 * for "no bitstream or no clock". Counted on the 25 MHz reference the
 * LED beats whether or not the PLL locks, LED_BITS stays 25, the rates
 * below stay exactly the rates the bench was taught, and a failed lock
 * becomes a seventh cause of the fast rate instead of a reading that
 * means nothing. "Reset never released" is not among the static cases
 * any more either: the beat below carries no reset at all. What that
 * costs is a domain the LED cannot see into, and paying it is the
 * subject of THE EIGHTH CAUSE below.
 *
 * oca_uart_console toggles D2 per byte received, which on this design is
 * the one reading that fails. A console command is a byte a person
 * typed; a request here is hundreds of bytes at line rate, and 115200
 * 8N1 delivers 11520 of them a second -- 5760 complete blinks -- so
 * during a frame the LED is a lamp at half brightness and between frames
 * it is static, which is what a board with no bitstream also looks like.
 * oca_blink's lesson is that a steady LED and a dead board must not read
 * the same, so D2 is a free-running heartbeat with the rate carrying the
 * one bit of state nothing else can report:
 *
 *   static              no bitstream, or no clk25.
 *   0.75 Hz, symmetric  alive, PLL locked, the datapath out of reset,
 *                       and nothing has been refused or lost since
 *                       power-on.
 *   6 Hz, symmetric     alive, and one of eight things: the PLL is not
 *                       locked, the datapath has not left reset, or at
 *                       least one frame was refused, one byte lost, or
 *                       one UART frame malformed. The six datapath
 *                       causes are sticky: they never go back to the
 *                       slow rate, because a fault that flashes once and
 *                       clears is a fault nobody catches.
 *
 * Eight to one, so the two live readings are told apart at a glance and
 * not by counting.
 *
 * THE LOCK TERM IS THE SEVENTH CAUSE and it is not sticky, which is a
 * deliberate asymmetry: pll_locked is low from configuration until the
 * loop closes, so a latch on it would fire on every power-on and pin the
 * LED to the fast rate for good. Fast before lock, slow after it. What
 * that leaves is a residual, recorded rather than fixed: a PLL that
 * locks, drops lock and locks again resets the core through arst_n and
 * clears the sticky bit with it, so the LED returns to the slow rate and
 * the dropout leaves no trace. It costs a latch and a "lock has been
 * seen" flop to close, and nothing on this board has yet produced one.
 *
 * THE EIGHTH CAUSE IS THE DATAPATH'S OWN RESET, and without it this LED
 * would be the failure it exists to prevent. clk25 is not the datapath's
 * clock, and neither of the other two terms can assert without one:
 * `trouble` is a register in the clk_sys domain, so a datapath with no
 * clock cannot raise it, and LOCK closes through CLKFB from CLKOP
 * (oca_clkrst.sv:60-62) and says nothing whatever about CLKOS -- while
 * ENCLKOS is left unconnected under a waiver whose own text records that
 * nothing in prjtrellis or nextpnr documents what that port gates. A
 * build in which CLKOS never runs therefore
 * locks: the power-on counter never advances, the datapath never leaves
 * reset, no byte is ever received, `trouble` stays 0 -- and D2, counted
 * on a clock that is still running, would blink 0.75 Hz for "alive and
 * clean" at a board that answers nobody.
 *
 * rst_n_core closes that for two flops. It is a register in the clk_sys
 * domain (oca_uart_crypto.sv), it comes out of configuration cleared,
 * and the only thing that sets it is oca_clkrst's release followed by
 * sixteen edges of clk_sys. No clk_sys, no release, fast rate.
 *
 * WHAT D2 STILL CANNOT SAY, recorded rather than fixed:
 *
 *   WHY the datapath has not started. "Held in reset" and "no clock at
 *   all" are different faults and this rate is the same for both of
 *   them, as it is for the six datapath causes and for a lock that
 *   never arrived. The fast rate says something is wrong and never what.
 *
 *   That clk_sys STOPPED after the datapath left reset. Nothing
 *   re-asserts rst_n_core, so a frozen datapath keeps the slow rate.
 *   Closing it needs a liveness bit -- a flop toggled by clk_sys,
 *   watched from clk25 against a timeout -- which is a counter and a
 *   window rather than two flops, and what it would cover is a PLL that
 *   drops CLKOS while LOCK stays high. Losing LOCK is not that case:
 *   arst_n clears, rst_n_sys asserts asynchronously, and both terms go
 *   fast with no timeout to wait for.
 *
 *   Whether clk_sys is at 48.0769 MHz. LOCK reports that the loop
 *   closed, not what it closed on -- the rule this project has already
 *   paid for -- and the dividers are checked against the netlist by
 *   hw/syn/run_synth.py, never by this LED.
 *
 * READ THE FAST RATE BEFORE THE HOST OPENS THE PORT, because otherwise
 * it covers several states and that is the trap this whole scheme exists
 * to avoid. `rx_frame_error` is one of the six datapath sources of
 * `trouble`, and oca_uart_rx raises it whenever a stop bit is not high
 * -- which a line left undriven, a break, or the edge a host puts on the
 * line when it opens /dev/ttyACM0 will all produce. One of those latches
 * the bit for the rest of the session, and 6 Hz then means "a byte on
 * the line was malformed at some point", which is true and is not the
 * same claim as "the datapath lost something". Fast already before any
 * host touches the port is line noise or a PLL, not a fault; fast only
 * after traffic is the reading this rate is for. Nothing here can clear
 * it short of reconfiguring, which is deliberate.
 *
 * LED_BITS is what makes the rates simulable: at the default 25 the slow
 * half-period is 0.671 s and no testbench can afford to watch one, so a
 * bench elaborates the module small. Nothing in a simulation can hold
 * the default at 25 -- that is a netlist census's job in run_synth.py,
 * and the census has to count this file now that the counter has left
 * oca_uart_crypto.
 *
 * WHAT A SIMULATION OF THIS FILE CAN SHOW IS ONE OF THE TWO LIVE
 * READINGS. EHXPLLL has no body, so in Verilator pll_locked never rises
 * and clk_sys never runs, so a bench that only drives clk25 sees the
 * fast rate and nothing else -- which is the right reading for a PLL
 * that has not locked, and is the whole of what such a bench can say.
 * hw/sim/test_crypto_pll.py goes further by writing the blackbox's own
 * pins, u_clkrst.u_pll.LOCK and .CLKOS, the way test_clkrst.py does:
 * that reaches the slow rate and the transition back to fast, so the
 * mapping from `trouble` to a rate is covered. THE SLOW RATE NEEDS BOTH
 * OF THOSE PINS. Writing LOCK alone leaves rst_n_core low, which is the
 * eighth term above and the fast rate, so a testbench reaches the clean
 * reading only by clocking CLKOS through oca_clkrst's release and the
 * datapath's power-on counter -- which is the point of the term, and is
 * why a bench that once read slow on LOCK alone now reads fast. What no
 * simulation covers is the PLL itself deciding to lock; that is the
 * bench's.
 *
 * THE BEAT CARRIES NO RESET, for oca_pll's reason (oca_pll.sv:53-56):
 * ECP5 flip-flops come out of configuration cleared, which is the same
 * start a power-on reset would give it, and it must not depend on
 * rst_n_sys -- that is gated on pll_locked, so a reset-driven beat would
 * be dead in exactly the case it exists to report.
 *
 * `trouble` and rst_n_core are bits crossing from clk_sys into clk25 and
 * they are synchronised rather than sampled raw. So is pll_locked, which
 * belongs to no clock domain at all. Each gets its own two-flop chain and
 * the three are ORed afterwards: ORing first would put a combinational
 * glitch between unrelated domains in front of a synchroniser, which is a
 * fast rate for two clocks with nothing wrong.
 */
`default_nettype none

module oca_crypto_pll #(
    // Heartbeat counter width, on clk25. 25 is the board: bit 24 toggles
    // every 0.671 s. A simulation elaborates it small so that both rates
    // fit in a run. The floor of 5 is the fast tap: at 4 it would be bit
    // 0, which toggles every cycle and is a rate nobody can read off a
    // pad or count in a testbench.
    parameter int LED_BITS = 25,
    // The PLL divider set, forwarded to oca_clkrst, and the frequency
    // those dividers put on clk_sys, forwarded to oca_uart_crypto as
    // CLK_HZ. The defaults are the shipping configuration: a 625 MHz
    // VCO over CLKOS_DIV 13, 48.0769 MHz, and the default elaboration
    // is the measured netlist unchanged. A variant top overrides the
    // five together (oca_crypto_pll_50.sv); the guard below refuses a
    // CLK_SYS_HZ that is not what the dividers make, so the pair cannot
    // be overridden apart.
    parameter int CLK_SYS_HZ = 48_076_923,
    parameter int CLKI_DIV   = 1,
    parameter int CLKFB_DIV  = 5,
    parameter int CLKOP_DIV  = 5,
    parameter int CLKOS_DIV  = 13
) (
    input  var logic clk25,
    output var logic led_n,
    output var logic uart_tx,
    input  var logic uart_rx
);

    localparam int SLOW = LED_BITS - 1;
    localparam int FAST = LED_BITS - 4;

    if (LED_BITS < 5) begin : gen_illegal_led_bits
        $fatal(1, "oca_crypto_pll: LED_BITS must be at least 5 (got %0d)",
               LED_BITS);
    end

    // The board's only oscillator, the same 25 MHz oca_clkrst holds as
    // its own CLKI_HZ; clk25 is wired straight to it.
    localparam int CLKI_HZ = 25_000_000;

    // What the divider set makes of it — oca_clkrst.sv's own arithmetic,
    // int division throughout. oca_clkrst exports no parameter to read
    // CLK_SYS_HZ from, so it arrives here as a parameter beside the
    // dividers, and this guard is what keeps that pair together: a
    // CLK_SYS_HZ that is not what the dividers produce is a UART divisor
    // computed for a frequency the board does not run — a mute serial
    // line, not a build failure. hw/syn/run_synth.py's
    // check_clk_sys_const makes the same comparison against the built
    // netlist's dividers; this one fails the lint instead of the
    // synthesis.
    localparam int DERIVED_CLK_SYS_HZ =
        ((CLKI_HZ / CLKI_DIV) * CLKFB_DIV * CLKOP_DIV) / CLKOS_DIV;

    if (CLK_SYS_HZ != DERIVED_CLK_SYS_HZ) begin : gen_clk_sys_mismatch
        $fatal(1, "oca_crypto_pll: CLK_SYS_HZ %0d Hz but the dividers make %0d Hz",
               CLK_SYS_HZ, DERIVED_CLK_SYS_HZ);
    end

    logic clk_sys, pll_locked, rst_n_sys;

    /*
     * Eight of oca_clkrst's outputs belong to a route this design does
     * not have: the transmit clock, the transmit and receive resets in
     * both polarities, and the PHY's reset pair. They are named so that
     * a forgotten pin still fails the build, and waived rather than
     * folded into an `unused_ok` OR the way oca_uart_crypto handles its
     * unread FIFO flags -- clk_tx is a global clock net, and putting one
     * into a data OR to satisfy a lint is a netlist change made for a
     * message. The waiver covers these four declarations and nothing
     * else.
     */
    /* verilator lint_off UNUSEDSIGNAL */
    logic clk_tx;
    logic rst_n_tx, rst_n_rx;
    logic rst_sys, rst_tx, rst_rx;
    logic phy_rst_n, phy_ready;
    /* verilator lint_on UNUSEDSIGNAL */

    oca_clkrst #(
        .CLKI_DIV  (CLKI_DIV),
        .CLKFB_DIV (CLKFB_DIV),
        .CLKOP_DIV (CLKOP_DIV),
        .CLKOS_DIV (CLKOS_DIV)
    ) u_clkrst (
        .clk_in     (clk25),
        .ext_rst_n  (1'b1),
        .clk_rx     (clk25),
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

    logic trouble, rst_n_core;

    oca_uart_crypto #(.CLK_HZ (CLK_SYS_HZ)) u_crypto (
        .clk        (clk_sys),
        .rst_n      (rst_n_sys),
        .uart_tx    (uart_tx),
        .uart_rx    (uart_rx),
        .rst_n_core (rst_n_core),
        .trouble    (trouble)
    );

    // ------------------------------------------------------------------
    // D2
    // ------------------------------------------------------------------
    logic [LED_BITS-1:0] beat;
    logic [1:0]          trouble_sync, locked_sync, started_sync;

    // verilator lint_off SYNCASYNCNET
    //
    // pll_locked is oca_clkrst's asynchronous reset root and rst_n_core
    // is oca_uart_crypto's, and here both are read synchronously. The
    // warning cannot tell that apart from the mistake it exists for: a
    // reset crossing domains with nothing to resolve it. oca_clkrst
    // waives it at the pattern that makes pll_locked one of these; this
    // is the synchronous end of both nets, two flops deep, driving
    // nothing but an LED.
    always_ff @(posedge clk25) begin
        beat         <= beat + LED_BITS'(1);
        trouble_sync <= {trouble_sync[0], trouble};
        locked_sync  <= {locked_sync[0], pll_locked};
        started_sync <= {started_sync[0], rst_n_core};
        // Active low, settled on the board 2026-08-11 by oca_blink's
        // asymmetric duty cycle.
        led_n <= ~((trouble_sync[1] || !locked_sync[1] || !started_sync[1])
                   ? beat[FAST] : beat[SLOW]);
    end
    // verilator lint_on SYNCASYNCNET

endmodule

`default_nettype wire
