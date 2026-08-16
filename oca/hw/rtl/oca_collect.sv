// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * Response collector: two oca_core response streams in, the one stream
 * to oca_slip_tx out.
 *
 * The other half is oca_dispatch. Its push0/push1 pulses arrive here in
 * dispatch order — one pulse per frame per core it went to, both
 * together for a broadcast — and queue in one expectation FIFO per
 * core. Each core answers its requests in order (oca_proto's DRAIN
 * publishes in arrival order, structurally), so the head of a core's
 * FIFO always describes the response at the head of that core's stream:
 * one bit, broadcast or not, and that bit is everything this module
 * needs to know about a frame.
 *
 * PASS-THROUGH, NOT ONE-FRAME-BUFFERED. A buffered collector would need
 * a response-sized bank of its own — 2048 bytes — to hold a frame
 * before forwarding it, and the staging would buy nothing: the sink is
 * a UART at 115200 baud, so the frame leaves at line pace whether or
 * not it was copied first, and the host matches responses by req_id, so
 * completion order needs no repair. Forwarding is therefore a lock: the
 * arbiter picks a source and holds it until tlast, which is what makes
 * a forwarded frame whole and interleaving impossible by construction.
 * What the lock costs is that the other core's completed response waits
 * — in that core's own transmit bank, storage that already exists.
 *
 * Two cores completing in the same cycle is the arbitration case, not a
 * hazard: the one not served last wins, the loser's response stays
 * whole in its bank, and no beat of either is lost. The preference is
 * why a core that completes continuously cannot starve its neighbour —
 * a fixed priority could, in the same way it would idle the second
 * engine on the dispatch side.
 *
 * A BROADCAST IS ANSWERED ONCE. Both cores respond to it; the host sent
 * one frame and gets one back. This module waits until both broadcast
 * responses stand at the head of their streams, compares their status
 * bytes, and forwards core 0's frame with the status byte carrying the
 * ERROR status of the two — s0 if s0 is nonzero, else s1 — while core
 * 1's frame is drained and discarded. Fail closed: a key that one core
 * refused is not in both keystores, so reporting the success would
 * report a device state that does not exist. Identical RTL on one clock
 * and one reset, fed byte-identical beats, cannot produce differing
 * statuses, so a mismatch is a fault — a stuck bit, an SEU, a bug — and
 * it latches `trouble` sticky for the heartbeat, oca_uart_crypto's
 * reason: a fault that flashes once and clears is a fault nobody
 * catches.
 *
 * WHY THERE IS NO TIMEOUT on that wait. The partner's response arrives
 * structurally, not hopefully: oca_slip_rx refuses every frame shorter
 * than MIN_BYTES = 8 = oca_proto's HDR_LEN, so every dispatched frame
 * carries a readable header, and oca_proto answers every such frame
 * with exactly one response — its one no-answer path, P_DROP, is
 * reachable only below HDR_LEN. A broadcast delivers the same beats to
 * both cores, so each owes exactly one response; per-core responses
 * leave in request order, so the k-th broadcast entry in one FIFO pairs
 * with the k-th in the other and the two heads this wait converges on
 * are the same broadcast. While one head waits, the partner's earlier
 * non-broadcast responses are still forwardable and forwarded, and the
 * drained side is never backpressured, so the wait is bounded by
 * pipeline occupancy. A timeout would trade that argument for a guess
 * about how long a core may take, and its expiry would forward a
 * response whose partner is still owed — desynchronising every
 * expectation entry behind it. The fault it would handle cannot occur;
 * the fault it would introduce can.
 *
 * The expectation FIFOs cannot overflow — oca_dispatch does not
 * dispatch while tag_full is up — but their overflow flags are wired
 * into `trouble` anyway, oca_uart_crypto's rule: a signal that cannot
 * assert costs one input on an OR gate to prove and is otherwise an
 * assumption, and here it is an assumption about another module's
 * discipline.
 */
`default_nettype none

module oca_collect (
    input  var logic        clk,
    input  var logic        rst_n,

    // Response streams, one per core.
    input  var logic [63:0] s0_tdata,
    input  var logic [ 7:0] s0_tkeep,
    input  var logic        s0_tvalid,
    output var logic        s0_tready,
    input  var logic        s0_tlast,

    input  var logic [63:0] s1_tdata,
    input  var logic [ 7:0] s1_tkeep,
    input  var logic        s1_tvalid,
    output var logic        s1_tready,
    input  var logic        s1_tlast,

    // The one response stream out: oca_slip_tx's input.
    output var logic [63:0] m_tdata,
    output var logic [ 7:0] m_tkeep,
    output var logic        m_tvalid,
    input  var logic        m_tready,
    output var logic        m_tlast,

    // Dispatch records from oca_dispatch.
    input  var logic        push0,
    input  var logic        push1,
    output var logic        tag_full0,
    output var logic        tag_full1,

    // Sticky: the cores diverged on a broadcast, or an expectation FIFO
    // overflowed (which a correct dispatcher cannot cause).
    output var logic        trouble
);

    // Deeper than the core's four-stage pipeline can ever fill: a core
    // stops presenting tready long before eight requests are in flight,
    // and the dispatcher stalls on tag_full besides.
    localparam int TAG_DEPTH = 8;

    logic tag0_bcast, tag1_bcast, tag0_empty, tag1_empty;
    logic tag0_pop, tag1_pop, tag0_ovf, tag1_ovf;
    logic [$clog2(TAG_DEPTH+1)-1:0] tag0_level, tag1_level;

    // Both pulses in one cycle is the definition of a broadcast, so the
    // stored bit is their AND rather than a third port.
    logic bcast_bit;
    always_comb bcast_bit = push0 && push1;

    logic unused_ok;
    always_comb unused_ok = (|tag0_level) | (|tag1_level);

    oca_fifo #(.WIDTH (1), .DEPTH (TAG_DEPTH)) u_tag0 (
        .clk      (clk),
        .rst_n    (rst_n),
        .wr_data  (bcast_bit),
        .push     (push0),
        .full     (tag_full0),
        .overflow (tag0_ovf),
        .rd_data  (tag0_bcast),
        .pop      (tag0_pop),
        .empty    (tag0_empty),
        .level    (tag0_level)
    );

    oca_fifo #(.WIDTH (1), .DEPTH (TAG_DEPTH)) u_tag1 (
        .clk      (clk),
        .rst_n    (rst_n),
        .wr_data  (bcast_bit),
        .push     (push1),
        .full     (tag_full1),
        .overflow (tag1_ovf),
        .rd_data  (tag1_bcast),
        .pop      (tag1_pop),
        .empty    (tag1_empty),
        .level    (tag1_level)
    );

    typedef enum logic [1:0] {
        C_IDLE,    // arbitrating; nothing is forwarded
        C_PASS,    // forwarding `src` until tlast
        C_BCAST    // forwarding core 0, draining core 1, until both tlast
    } state_e;

    state_e     state;
    logic       src;             // C_PASS source
    logic       last_served;     // most recent C_PASS source
    logic       fwd_done;        // C_BCAST: core 0's frame fully forwarded
    logic       drn_done;        // C_BCAST: core 1's frame fully drained
    logic       fwd_first;       // C_BCAST: the status byte goes here
    logic [7:0] merged_status;

    // Byte 7 of the header beat, which is the first beat of every
    // response: the status.
    logic [7:0] status0, status1;
    always_comb status0 = s0_tdata[63:56];
    always_comb status1 = s1_tdata[63:56];

    // A candidate is a response beat with its expectation at the head.
    // The empty qualifier cannot bite — the push precedes the frame,
    // which precedes its response — but reading a FIFO head that is not
    // there would be reading stale state as a decision.
    logic cand0, cand1, sel0, sel1, both_bcast;
    always_comb cand0      = s0_tvalid && !tag0_empty;
    always_comb cand1      = s1_tvalid && !tag1_empty;
    always_comb sel0       = cand0 && !tag0_bcast;
    always_comb sel1       = cand1 && !tag1_bcast;
    always_comb both_bcast = cand0 && cand1 && tag0_bcast && tag1_bcast;

    always_comb begin
        m_tdata   = s0_tdata;
        m_tkeep   = s0_tkeep;
        m_tlast   = s0_tlast;
        m_tvalid  = 1'b0;
        s0_tready = 1'b0;
        s1_tready = 1'b0;
        unique case (state)
            C_PASS: begin
                if (src) begin
                    m_tdata   = s1_tdata;
                    m_tkeep   = s1_tkeep;
                    m_tlast   = s1_tlast;
                    m_tvalid  = s1_tvalid;
                    s1_tready = m_tready;
                end else begin
                    m_tvalid  = s0_tvalid;
                    s0_tready = m_tready;
                end
            end
            C_BCAST: begin
                if (fwd_first) m_tdata = {merged_status, s0_tdata[55:0]};
                m_tvalid  = s0_tvalid && !fwd_done;
                s0_tready = m_tready && !fwd_done;
                s1_tready = !drn_done;
            end
            default: ;   // C_IDLE
        endcase
    end

    logic fire0, fire1;
    always_comb fire0 = s0_tvalid && s0_tready;
    always_comb fire1 = s1_tvalid && s1_tready;

    // Done-now forms, so two halves finishing in the same cycle leave
    // together instead of costing a lap through the state.
    logic bcast_done;
    always_comb bcast_done = (state == C_BCAST)
                             && (fwd_done || (fire0 && s0_tlast))
                             && (drn_done || (fire1 && s1_tlast));

    always_comb tag0_pop = ((state == C_PASS) && !src && fire0 && s0_tlast)
                           || bcast_done;
    always_comb tag1_pop = ((state == C_PASS) && src && fire1 && s1_tlast)
                           || bcast_done;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state         <= C_IDLE;
            src           <= 1'b0;
            // so the first arbitration prefers core 0
            last_served   <= 1'b1;
            fwd_done      <= 1'b0;
            drn_done      <= 1'b0;
            fwd_first     <= 1'b0;
            merged_status <= '0;
        end else begin
            unique case (state)
                C_IDLE: begin
                    if (both_bcast) begin
                        state         <= C_BCAST;
                        fwd_done      <= 1'b0;
                        drn_done      <= 1'b0;
                        fwd_first     <= 1'b1;
                        merged_status <= (status0 != 8'h00) ? status0
                                                            : status1;
                    end else if (sel0 && (last_served || !sel1)) begin
                        state <= C_PASS;
                        src   <= 1'b0;
                    end else if (sel1) begin
                        state <= C_PASS;
                        src   <= 1'b1;
                    end
                end

                C_PASS: if (m_tvalid && m_tready && m_tlast) begin
                    last_served <= src;
                    state       <= C_IDLE;
                end

                default: begin   // C_BCAST
                    if (fire0) begin
                        fwd_first <= 1'b0;
                        if (s0_tlast) fwd_done <= 1'b1;
                    end
                    if (fire1 && s1_tlast) drn_done <= 1'b1;
                    if (bcast_done) state <= C_IDLE;
                end
            endcase
        end
    end

    // The compare happens in the one cycle C_IDLE sees both broadcast
    // heads — the same cycle the merge is decided — and the latch never
    // clears short of a reset.
    logic diverged;
    always_comb diverged = (state == C_IDLE) && both_bcast
                           && (status0 != status1);

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            trouble <= 1'b0;
        end else if (diverged || tag0_ovf || tag1_ovf) begin
            trouble <= 1'b1;
        end
    end

endmodule

`default_nettype wire
