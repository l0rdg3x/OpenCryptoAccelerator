// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * The AEAD core on the board's serial line: the first design here that
 * puts crypto in a bitstream.
 *
 * oca_uart_rx -> oca_fifo -> oca_slip_rx -> oca_core -> oca_slip_tx ->
 * oca_fifo -> oca_uart_tx8. Same four pins as oca_uart_console, same
 * power-on reset, and oca_console replaced by the protocol it was a
 * stand-in for. The wire format is unchanged: SLIP supplies the frame
 * boundary UDP used to supply and nothing else (oca_slip_rx.sv).
 *
 * THIS IS THE DATAPATH, NOT THE BOARD TOP. oca_crypto_pll is the top:
 * it holds oca_clkrst, hands this module a clock and a reset, and owns
 * the LED. The PLL is outside on purpose. EHXPLLL is a body-less
 * blackbox, so a PLL inside here would leave hw/sim/run_uart_crypto.py
 * -- the only suite that drives real UART bit timing through this
 * datapath; oca_uart_console and oca_uart_echo drive it through theirs --
 * fabricating the very clock it exists to test. What arrives instead is
 * CLK_HZ, and every figure below that moves with the clock is derived
 * from it rather than written down.
 *
 * BYTES IS ONE LOCALPARAM, PASSED TO BOTH. oca_slip_rx's buffer and
 * oca_core's packet buffers have to be the same size -- a decoder that
 * accepts more than the core can hold answers about a prefix -- and the
 * only way to make that provable rather than reviewed is to have one
 * number. MIN_BYTES is a different matter: it is oca_proto's HDR_LEN
 * written down a second time, and nothing across the module boundary
 * enforces that they agree. That residual is oca_slip_rx's, recorded
 * there, and it is not fixed here.
 *
 * FIFO DEPTHS, from the arithmetic rather than from the console's 16/32.
 *
 * A byte on the line occupies ten bit times: 86.8 us, which is 2170
 * cycles at the 25 MHz board oscillator and 4173 at the 48.0769 MHz
 * oca_crypto_pll supplies. Every other cycle count below belongs to the
 * design rather than to the clock and does not move with it, so 25 MHz
 * is the tighter of the two builds and the comparisons are made there.
 * The decoder empties the input queue at a byte a cycle whenever it is
 * in S_RECV, and it is not in S_RECV during three windows:
 *
 *   S_CLEAR      BYTES/8 = 256 cycles, once, out of reset.
 *   S_PRIME+DRAIN one cycle plus one beat per m_axis_tready, at most
 *                256 beats for a full frame.
 *   a stall      the drain waits while oca_core holds tready low.
 *
 * The first two are 257 cycles against 2170, an eighth of a byte time,
 * so they cannot cost a byte. The third is the one that matters, and it
 * splits by what the host does. A host that sends a request and reads
 * the whole response before the next one never stalls the decoder for
 * longer than oca_core takes to accept a frame into a free bank --
 * for the longest CRYPTO command that is the 1222 cycles of a
 * 2048-byte seal (36 cycles per 64-byte block plus 70 per packet,
 * docs/RECORD.md), still under one byte time, so in that regime ONE
 * entry would do and every depth here is margin. The one command that
 * bound does NOT cover is a bench (opcode 05): it holds its bank for
 * its whole run, up to ~2.4M cycles at N = 65535, so a host that
 * pipelines a request behind a long bench and keeps typing can
 * overflow the input FIFO. Not silent -- the short frame answers with
 * a status error and `trouble` latches -- and a request/response host
 * never sees it, but the depths here are sized for crypto traffic,
 * not for pipelining behind a bench.
 *
 * A host that keeps writing while a response is still coming back is a
 * different case and NO DEPTH FIXES IT. oca_proto is store and forward
 * on two transmit banks, and a response leaves at a byte per 86.8 us:
 * 2048 bytes take 178 ms, during which the same line delivers 2048 more
 * bytes inbound. The queue that would absorb that is the size of the
 * traffic, not a constant. So the depth is chosen for the bounded case
 * and the unbounded one is counted instead, which is the only honest
 * thing left to do with it.
 *
 * 16 and 16. Sixteen rather than two because an oca_fifo's bytes are not
 * in flip-flops -- yosys puts them in distributed RAM, which is why both
 * of the console's instances together floor at 23 registers -- so eight
 * byte times of margin over the one the arithmetic requires costs a bit
 * of pointer and nothing else. The output side is sixteen rather than
 * the console's thirty-two because the console's number bought something
 * this design cannot have: a whole response in the queue, so that a line
 * never stalls mid-way. A response here is up to 2048 bytes and no depth
 * holds one, so the mechanism is backpressure from end to end and the
 * only thing depth buys is a shorter gap in s_axis_tready, which nothing
 * measures.
 *
 * WHAT HAPPENS WHEN THEY FILL. The input one refuses the byte and pulses
 * overflow; oca_fifo does not overwrite (oca_fifo.sv:15-20). The frame
 * that byte belonged to is then one byte short, so it reaches oca_proto
 * with a header whose aad_len and msg_len no longer match the bytes
 * present and comes back as status 05, or it fails its tag -- the loss
 * is not silent at the protocol level, but neither of those statuses
 * says "the line lost a byte", which is what `trouble` below is for.
 * The output one cannot fill in a way that loses anything: oca_slip_tx
 * raises tx_push only with tx_ready high and tx_ready is that FIFO's own
 * full flag inverted, so its overflow cannot assert. It is wired into
 * `trouble` anyway, because a signal that cannot assert costs one input
 * on an OR gate to prove and is otherwise an assumption.
 *
 * TROUBLE IS ONE STICKY BIT AND IT LEAVES AS AN OUTPUT. Six things set
 * it, and this module never clears it short of a reset:
 *
 *   rx_frame_error                 a stop bit was not high.
 *   rx_fifo_overflow               a byte off the line met a full queue.
 *   tx_fifo_overflow               cannot assert; wired in anyway, per
 *                                  WHAT HAPPENS WHEN THEY FILL above.
 *   cnt_short, cnt_long, cnt_esc   oca_slip_rx refused a frame.
 *
 * Sticky because a fault that flashes once and clears is a fault nobody
 * catches. What the bit is FOR is oca_crypto_pll's heartbeat LED, and
 * the trap in reading it -- rx_frame_error is also what a host produces
 * merely by opening the port -- is documented there with the rates.
 *
 * WHAT THE HOST CANNOT ASK FOR, recorded as a blind spot rather than
 * fixed. Opcode 04 is the in-band diagnostic and it answers four
 * counters -- packets received, packets dropped, commands completed,
 * authentication failures -- all of them oca_proto's. oca_slip_rx's
 * cnt_short, cnt_long and cnt_esc are not among them and cannot be
 * reached through that opcode: a refused frame never arrives at
 * oca_core, so every counter the protocol has is blind to it by
 * construction. A second command channel to read them was rejected --
 * it is a second parser on the only channel available for finding bugs,
 * and the wire format above SLIP is the contract SPEC.md PHASE 3 makes
 * later drivers depend on. What is done instead is `trouble`, and it is
 * deliberately coarse: the operator learns that the link refused or lost
 * something, never which of the four reasons and never how many.
 *
 * AND THE COUNTS ARE NOT IN THE BITSTREAM AT ALL, which is the honest
 * end of that decision rather than a surprise waiting at the bench. The
 * three counters are read only by the OR below, so nothing observes
 * their value; measured on this toolchain, yosys keeps the disjunction
 * and deletes all forty-eight bits, and the netlist holds no cnt_* net
 * at all against thirty-three in a build of oca_slip_rx on its own.
 * Reading the counts back needs somewhere to put them, which is a
 * change to the wire format and not to this file.
 *
 * That deletion does NOT show up in the flip-flop floors, and this
 * paragraph said it did until 2026-08-12: the per-file census reads 160
 * for oca_slip_rx.sv in both builds, the counters having never been in
 * that bucket. run_synth.py's table says so at length, because the
 * mistake there was to read a per-file census against a whole-netlist
 * total.
 *
 * RESET, AND WHY THERE ARE TWO OF THEM. This module is built both ways.
 * Standalone on the board oscillator there is no reset pin and no PLL to
 * lock, and the only root is oca_uart_console's power-on counter: ECP5
 * flip-flops come out of configuration cleared, the counter starts at
 * zero and releases reset once. Under oca_crypto_pll the clock is a PLL
 * output and rst_n carries oca_clkrst's synchronised release, which is
 * gated on LOCK. Keeping both is what makes the two builds the same
 * module: dropping the counter would make the standalone build depend on
 * a pin that is not there, and ignoring rst_n would start the datapath
 * on the first edges of a clock whose PLL has not locked.
 *
 * THE TWO ARE NOT ANDed, which is what the shape below is for. rst_n_core
 * is a flip-flop: rst_n is its asynchronous clear and the counter
 * reaching fifteen is the only thing that sets it, so what drives the
 * asynchronous reset of everything else here is a register output and
 * never a decode. That matters because `por_count == 4'd15` is a
 * four-input AND, its LUT carries a static-0 hazard on the counter's
 * 7->8, 11->12 and 13->14, and a glitch on an asynchronous reset net is
 * a spurious RELEASE rather than a spurious reset. (Until 2026-08-15
 * this decode WAS the reset: named rst_n, with no input to combine it
 * with, driving those same asynchronous resets straight out of a LUT.
 * It survived because a design whose only reset is its own power-on
 * counter releases once and never again, so a hazard on the way to
 * fifteen had one chance to fire and nothing downstream to confuse.
 * Under oca_crypto_pll that stops being true: rst_n is oca_clkrst's
 * rst_n_sys, whose stated purpose is to assert asynchronously when the
 * PLL loses lock, so the count is walked again on every dropout.)
 *
 * Assertion asynchronous, release synchronous, and rst_n restarts the
 * count instead of passing through it: a lock that drops and returns
 * gives the datapath the same sixteen cycles it gets at power-on. The
 * signal leaves as an output because oca_crypto_pll's heartbeat reads
 * it -- that LED is counted on clk25 and is otherwise blind to whether
 * `clk` ever ran at all, which is documented there.
 *
 * Everything below that clears itself does so afterwards and gates its
 * own inputs while it does -- the decoder's buffer walk, both packet
 * buffers' walks -- so there is no ordering to arrange here.
 */
`default_nettype none

module oca_uart_crypto #(
    // The frequency of `clk`, in hertz, and the only thing that tells
    // this module what a bit time is. 25_000_000 is the board oscillator
    // on P3, which is what a standalone build of this module runs on;
    // oca_crypto_pll passes oca_clkrst's clk_sys instead, a 625 MHz VCO
    // over CLKOS_DIV 13 (oca_clkrst.sv).
    parameter int CLK_HZ = 25_000_000
) (
    input  var logic clk,
    // Asynchronous, active low: the clear of the power-on register
    // below, and through it of everything in the datapath. Tie high
    // where there is nothing to gate on. See the header.
    input  var logic rst_n,
    output var logic uart_tx,
    input  var logic uart_rx,
    // Low while the datapath is held: rst_n asserted, or the power-on
    // counter not finished. A register in this clock domain, so it
    // cannot rise without edges on `clk`, which is what oca_crypto_pll
    // reads it for.
    output var logic rst_n_core,
    // Sticky: something was refused or lost. Six sources, per the header.
    output var logic trouble
);

    // 115200 8N1, which is what the DAPLink bridge and hw/host/ speak.
    localparam int BAUD_HZ = 115_200;
    localparam int DIV     = CLK_HZ / BAUD_HZ;

    /*
     * WHERE THE STOP BIT LANDS, for the CLK_HZ this module is given. A
     * wrong divisor is a mute board and an off-by-one is invisible: 416,
     * 417 and 418 all carry a byte over this link and nothing in hw/sim/
     * separates them.
     *
     * The error is the receiver's, accumulated the way oca_uart_rx
     * accumulates it (oca_uart_rx.sv:45-114) and not a comparison of
     * baud rates. Counted from the falling edge on the pin, the stop bit
     * -- the tenth sample -- is taken between
     *
     *     DIV/2 + 9*DIV + 3   and   DIV/2 + 9*DIV + 4   cycles,
     *
     * where 9.5 bit times would put it. The first two terms are the
     * counter: it waits DIV/2 to reach the middle of the start bit and
     * then samples every DIV. Both divisions truncate, and truncating
     * DIV/2 throws away up to half a cycle that CLK_HZ/DIV against
     * 115200 never sees at all. The three cycles after them are the path
     * from the pad to that counter -- the second synchroniser flop, IDLE
     * reading it, the cycle spent entering DATA -- and the fourth is the
     * first synchroniser flop's aperture, a pin edge landing anywhere
     * inside a clock period. Every one of them is LATE, which is why the
     * figure is an interval one cycle wide and not a midpoint.
     *
     * MEASURED, NOT DERIVED, 2026-08-15 under Verilator: oca_uart_rx
     * elaborated at DIV 10, 100, 217 and 417, a start edge driven at
     * three phases of the clock in each, and the edge at which `valid`
     * rises read back. In all twelve runs it is 4 + DIV/2 + 9*DIV edges
     * after the last clock edge before the pin fell, which is the
     * interval above with the phase folded into it. This module modelled
     * DIV/2 + 9*DIV and nothing after it until that measurement, so the
     * figures it published were short by those three to four cycles:
     * -8810 ppm at DIV 417, where the sample is between 1621 early and
     * 774 late, and -2912 at DIV 217, where it is between 10912 and
     * 15520 LATE.
     *
     * The budget is half of the ~5% the receiver tolerates end to end.
     * The other half belongs to the host's oscillator, which this design
     * does not choose and cannot measure.
     *
     * WHAT THIS DOES NOT CHECK, and it is the likelier mistake: that
     * CLK_HZ is the frequency `clk` actually carries. The interval is
     * one cycle wide however fast the clock is, so measured in ppm of a
     * bit time it shrinks as the clock rises, and 32_025_599 Hz is the
     * highest CLK_HZ that lands outside the budget at all -- the top of
     * the DIV 277 band, found by walking both ends of every divisor
     * band, which is exhaustive because the figure is monotone inside
     * one. Above it this guard accepts every frequency there is, so at
     * the board's 48.0769 MHz it bounds the sampler and can fire at
     * nothing.
     */
    localparam longint MAX_SAMPLE_ERR_PPM = 25_000;

    // 64-bit throughout, and not for tidiness: 19 * CLK_HZ passes 2^31
    // above 113_025_455 Hz and the scaling to ppm passes it at every
    // frequency there is. An overflowed guard is a guard that passes.
    localparam longint STOP_CYCLES_EARLY =
        longint'(DIV) / 2 + 9 * longint'(DIV) + 3;
    localparam longint STOP_CYCLES_LATE = STOP_CYCLES_EARLY + 1;

    // ppm of a bit time, positive meaning late: STOP*BAUD_HZ/CLK_HZ - 9.5
    // scaled by a million, doubled to clear the half.
    localparam longint SAMPLE_ERR_EARLY_PPM =
        ((2 * STOP_CYCLES_EARLY * BAUD_HZ - 19 * longint'(CLK_HZ)) * 1_000_000)
        / (2 * longint'(CLK_HZ));
    localparam longint SAMPLE_ERR_LATE_PPM =
        ((2 * STOP_CYCLES_LATE * BAUD_HZ - 19 * longint'(CLK_HZ)) * 1_000_000)
        / (2 * longint'(CLK_HZ));

    // 48_076_923 / 115200 = 417.334, so DIV is 417 and the interval is
    // -1621 to +774 ppm. 25_000_000 gives DIV 217, where all of it is
    // late -- +10912 to +15520, five eighths of the budget, and nearly
    // all of that the four cycles between the pad and the sample. The
    // 0.006% oca_uart_rx.sv quotes is none of it: that figure compares
    // baud rates, which is the comparison this one exists to replace.
    // Each end is checked against the budget it can reach.
    if (SAMPLE_ERR_LATE_PPM > MAX_SAMPLE_ERR_PPM
        || SAMPLE_ERR_EARLY_PPM < -MAX_SAMPLE_ERR_PPM) begin : gen_bad_clk_hz
        // One string literal, not a concatenation of two: verilator
        // renders a concatenated format argument as one enormous decimal
        // and the message is lost exactly where it is needed.
        $fatal(1,
            "oca_uart_crypto: CLK_HZ %0d, DIV %0d, stop bit sampled %0d to %0d ppm off 9.5 bit times (max %0d)",
            CLK_HZ, DIV, SAMPLE_ERR_EARLY_PPM, SAMPLE_ERR_LATE_PPM,
            MAX_SAMPLE_ERR_PPM);
    end

    // One number for both, so they cannot drift apart.
    localparam int BYTES = 2048;
    // oca_proto's HDR_LEN. See the header: this is a copy, not a link.
    localparam int MIN_BYTES = 8;

    localparam int RX_DEPTH = 16;
    localparam int TX_DEPTH = 16;

    logic [3:0] por_count;

    // rst_n is the asynchronous clear and the count is the only release:
    // no combinational term reaches the reset net. See the header for
    // why ANDing the two would not do.
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            por_count  <= 4'd0;
            rst_n_core <= 1'b0;
        end else begin
            if (por_count != 4'd15) begin
                por_count <= por_count + 4'd1;
            end
            rst_n_core <= (por_count == 4'd15);
        end
    end

    // ------------------------------------------------------------------
    // Line in: receiver, queue, decoder
    // ------------------------------------------------------------------
    logic [7:0] rx_byte;
    logic       rx_valid, rx_frame_error;

    oca_uart_rx #(.DIV (DIV)) u_rx (
        .clk         (clk),
        .rx          (uart_rx),
        .data        (rx_byte),
        .valid       (rx_valid),
        .frame_error (rx_frame_error)
    );

    logic [7:0] slip_rx_data;
    logic       slip_rx_pop, rx_fifo_empty, rx_fifo_overflow;

    // Three FIFO flags this design does not read, named rather than left
    // dangling so that -Wall stays clean without a waiver. The input
    // FIFO's full flag says nothing its overflow does not -- the
    // receiver pushes whatever the line delivers either way -- and
    // neither level is published anywhere.
    logic       rx_fifo_full;
    logic [4:0] rx_fifo_level, tx_fifo_level;
    logic       unused_ok;
    always_comb unused_ok = rx_fifo_full | (|rx_fifo_level) | (|tx_fifo_level);

    oca_fifo #(.WIDTH (8), .DEPTH (RX_DEPTH)) u_rx_fifo (
        .clk      (clk),
        .rst_n    (rst_n_core),
        .wr_data  (rx_byte),
        .push     (rx_valid),
        .full     (rx_fifo_full),
        .overflow (rx_fifo_overflow),
        .rd_data  (slip_rx_data),
        .pop      (slip_rx_pop),
        .empty    (rx_fifo_empty),
        .level    (rx_fifo_level)
    );

    logic [63:0] req_tdata;
    logic [ 7:0] req_tkeep;
    logic        req_tvalid, req_tready, req_tlast;
    logic [15:0] cnt_short, cnt_long, cnt_esc;

    oca_slip_rx #(.BYTES (BYTES), .MIN_BYTES (MIN_BYTES)) u_slip_rx (
        .clk           (clk),
        .rst_n         (rst_n_core),
        .rx_data       (slip_rx_data),
        .rx_valid      (!rx_fifo_empty),
        .rx_pop        (slip_rx_pop),
        .m_axis_tdata  (req_tdata),
        .m_axis_tkeep  (req_tkeep),
        .m_axis_tvalid (req_tvalid),
        .m_axis_tready (req_tready),
        .m_axis_tlast  (req_tlast),
        .cnt_short     (cnt_short),
        .cnt_long      (cnt_long),
        .cnt_esc       (cnt_esc)
    );

    // ------------------------------------------------------------------
    // The engine
    // ------------------------------------------------------------------
    logic [63:0] rsp_tdata;
    logic [ 7:0] rsp_tkeep;
    logic        rsp_tvalid, rsp_tready, rsp_tlast;

    oca_core #(.BYTES (BYTES)) u_core (
        .clk           (clk),
        .rst_n         (rst_n_core),
        .s_axis_tdata  (req_tdata),
        .s_axis_tkeep  (req_tkeep),
        .s_axis_tvalid (req_tvalid),
        .s_axis_tready (req_tready),
        .s_axis_tlast  (req_tlast),
        .m_axis_tdata  (rsp_tdata),
        .m_axis_tkeep  (rsp_tkeep),
        .m_axis_tvalid (rsp_tvalid),
        .m_axis_tready (rsp_tready),
        .m_axis_tlast  (rsp_tlast)
    );

    // ------------------------------------------------------------------
    // Line out: encoder, queue, transmitter
    // ------------------------------------------------------------------
    logic [7:0] tx_byte;
    logic       tx_push, tx_fifo_full, tx_fifo_empty, tx_fifo_overflow;
    logic [7:0] tx_fifo_data;
    logic       tx_busy, tx_fifo_pop;

    oca_slip_tx u_slip_tx (
        .clk           (clk),
        .rst_n         (rst_n_core),
        .s_axis_tdata  (rsp_tdata),
        .s_axis_tkeep  (rsp_tkeep),
        .s_axis_tvalid (rsp_tvalid),
        .s_axis_tready (rsp_tready),
        .s_axis_tlast  (rsp_tlast),
        .tx_data       (tx_byte),
        .tx_push       (tx_push),
        .tx_ready      (!tx_fifo_full)
    );

    oca_fifo #(.WIDTH (8), .DEPTH (TX_DEPTH)) u_tx_fifo (
        .clk      (clk),
        .rst_n    (rst_n_core),
        .wr_data  (tx_byte),
        .push     (tx_push),
        .full     (tx_fifo_full),
        .overflow (tx_fifo_overflow),
        .rd_data  (tx_fifo_data),
        .pop      (tx_fifo_pop),
        .empty    (tx_fifo_empty),
        .level    (tx_fifo_level)
    );

    always_comb tx_fifo_pop = !tx_fifo_empty && !tx_busy;

    oca_uart_tx8 #(.DIV (DIV)) u_tx (
        .clk  (clk),
        .data (tx_fifo_data),
        .send (tx_fifo_pop),
        .busy (tx_busy),
        .tx   (uart_tx)
    );

    // ------------------------------------------------------------------
    // The sticky trouble latch
    // ------------------------------------------------------------------
    always_ff @(posedge clk or negedge rst_n_core) begin
        if (!rst_n_core) begin
            trouble <= 1'b0;
        end else if (rx_frame_error || rx_fifo_overflow || tx_fifo_overflow
                     || (|cnt_short) || (|cnt_long) || (|cnt_esc)) begin
            trouble <= 1'b1;
        end
    end

endmodule

`default_nettype wire
