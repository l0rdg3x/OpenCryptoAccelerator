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
 * A byte on the line occupies ten bit times: 86.8 us, 2170 cycles of
 * clk25. The decoder empties the input queue at a byte a cycle whenever
 * it is in S_RECV, and it is not in S_RECV during three windows:
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
 * longer than oca_core takes to accept a frame into a free bank, which
 * for the longest command is the 1351 cycles of a 2048-byte seal
 * (40 cycles per 64-byte block plus 71 per packet, AGENTS.md) -- still
 * under one byte time. So in that regime ONE entry would do and every
 * depth here is margin.
 *
 * A host that keeps writing while a response is still coming back is a
 * different case and NO DEPTH FIXES IT. oca_proto is store and forward
 * on two transmit banks, and a response leaves at 2170 cycles a byte:
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
 * THE LED, AND WHY IT IS NOT THE CONSOLE'S. oca_uart_console toggles D2
 * per byte received, which on this design is the one reading that fails.
 * A console command is a byte a person typed; a request here is hundreds
 * of bytes at line rate, and 115200 8N1 delivers 11520 of them a second
 * -- 5760 complete blinks -- so during a frame the LED is a lamp at half
 * brightness and between frames it is static, which is what a board with
 * no bitstream also looks like. oca_blink's lesson is that a steady LED and
 * a dead board must not read the same, so D2 is a free-running heartbeat
 * with the rate carrying the one bit of state nothing else can report:
 *
 *   static              no bitstream, no clock, or reset never released.
 *   0.75 Hz, symmetric  alive, and nothing has been refused or lost
 *                       since power-on.
 *   6 Hz, symmetric     alive, and at least one frame was refused, one
 *                       byte lost, or one UART frame malformed. Sticky:
 *                       it never goes back to the slow rate, because a
 *                       fault that flashes once and clears is a fault
 *                       nobody catches.
 *
 * Eight to one, so the two live readings are told apart at a glance and
 * not by counting.
 *
 * READ THE FAST RATE BEFORE THE HOST OPENS THE PORT, because otherwise
 * it covers two states and that is the trap this whole scheme exists to
 * avoid. `rx_frame_error` is one of the six sources of `trouble`, and
 * oca_uart_rx raises it whenever a stop bit is not high -- which a line
 * left undriven, a break, or the edge a host puts on the line when it
 * opens /dev/ttyACM0 will all produce. One of those latches the bit for
 * the rest of the session, and 6 Hz then means "a byte on the line was
 * malformed at some point", which is true and is not the same claim as
 * "the datapath lost something". The order is the disambiguation: watch
 * D2 for a few seconds after configuration and before any host touches
 * the port. Fast already at that point is line noise, not a fault; fast
 * only after traffic is the reading this rate is for. Nothing here can
 * clear it short of reconfiguring, which is deliberate.
 *
 * LED_BITS is what makes the rates simulable: at the
 * default 25 the slow half-period is 0.671 s and no testbench can afford
 * to watch one, so the suite elaborates the module small and the netlist
 * census in run_synth.py is what holds the default at all 25 bits.
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
 * later drivers depend on. What is done instead is the LED, and it is
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
 * RESET. No reset pin and no PLL to lock, so rst_n is oca_uart_console's
 * power-on counter: ECP5 flip-flops come out of configuration cleared,
 * the counter starts at zero and releases reset once. Everything below
 * that clears itself does so afterwards and gates its own inputs while
 * it does -- the decoder's buffer walk, both packet buffers' walks --
 * so there is no ordering to arrange here.
 */
`default_nettype none

module oca_uart_crypto #(
    // Heartbeat counter width. 25 is the board: bit 24 toggles every
    // 0.671 s. A simulation elaborates it small so that both rates fit
    // in a run. The floor of 5 is the fast tap: at 4 it would be bit 0,
    // which toggles every cycle and is a rate nobody can read off a pad
    // or count in a testbench.
    parameter int LED_BITS = 25
) (
    input  var logic clk25,
    output var logic led_n,
    output var logic uart_tx,
    input  var logic uart_rx
);

    // 25e6 / 115200 = 217.01. The 0.006% error is nothing against the
    // ~5% a mid-bit sampler tolerates (oca_uart_rx.sv).
    localparam int DIV = 217;

    // One number for both, so they cannot drift apart.
    localparam int BYTES = 2048;
    // oca_proto's HDR_LEN. See the header: this is a copy, not a link.
    localparam int MIN_BYTES = 8;

    localparam int RX_DEPTH = 16;
    localparam int TX_DEPTH = 16;

    localparam int SLOW = LED_BITS - 1;
    localparam int FAST = LED_BITS - 4;

    if (LED_BITS < 5) begin : gen_illegal_led_bits
        $fatal(1, "oca_uart_crypto: LED_BITS must be at least 5 (got %0d)",
               LED_BITS);
    end

    logic [3:0] por_count;
    logic       rst_n;

    always_ff @(posedge clk25) begin
        if (por_count != 4'd15) begin
            por_count <= por_count + 4'd1;
        end
    end

    always_comb rst_n = (por_count == 4'd15);

    // ------------------------------------------------------------------
    // Line in: receiver, queue, decoder
    // ------------------------------------------------------------------
    logic [7:0] rx_byte;
    logic       rx_valid, rx_frame_error;

    oca_uart_rx #(.DIV (DIV)) u_rx (
        .clk         (clk25),
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
        .clk      (clk25),
        .rst_n    (rst_n),
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
        .clk           (clk25),
        .rst_n         (rst_n),
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
        .clk           (clk25),
        .rst_n         (rst_n),
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
        .clk           (clk25),
        .rst_n         (rst_n),
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
        .clk      (clk25),
        .rst_n    (rst_n),
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
        .clk  (clk25),
        .data (tx_fifo_data),
        .send (tx_fifo_pop),
        .busy (tx_busy),
        .tx   (uart_tx)
    );

    // ------------------------------------------------------------------
    // D2
    // ------------------------------------------------------------------
    logic                trouble;
    logic [LED_BITS-1:0] beat;

    always_ff @(posedge clk25 or negedge rst_n) begin
        if (!rst_n) begin
            trouble <= 1'b0;
            beat    <= '0;
            led_n   <= 1'b1;
        end else begin
            beat <= beat + LED_BITS'(1);
            if (rx_frame_error || rx_fifo_overflow || tx_fifo_overflow
                || (|cnt_short) || (|cnt_long) || (|cnt_esc)) begin
                trouble <= 1'b1;
            end
            // Active low, settled on the board 2026-08-11 by oca_blink's
            // asymmetric duty cycle.
            led_n <= ~(trouble ? beat[FAST] : beat[SLOW]);
        end
    end

endmodule

`default_nettype wire
