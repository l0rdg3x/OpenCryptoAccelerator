// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * Request dispatcher: one decoded frame stream in, two oca_core behind it.
 *
 * The other half is oca_collect, which owns the responses; the two meet
 * only in the push/tag_full pair below. Together they are what lets two
 * engines sit behind the one serial line while the wire format above
 * SLIP stays exactly the contract SPEC.md PHASE 3 makes drivers depend
 * on: the host cannot tell this fabric from one core, except by
 * throughput.
 *
 * ONE FRAME, ONE CORE, WHOLE. The route is decided once per frame, on
 * its first beat, and locked until tlast: no frame is ever split across
 * cores, and the payload bus is a single pass-through — one m_tdata into
 * both cores, only the handshake per core — so bytes cannot reorder
 * within a frame because there is exactly one path they can take.
 *
 * THE FIRST BEAT IS THE HEADER, which is what makes a one-beat decision
 * possible. The wire header is 8 bytes (docs/design/2026-08-03-
 * host-protocol.md) and oca_slip_rx refuses every frame shorter than
 * MIN_BYTES, so with MIN_BYTES at 8 every frame offered here opens with
 * a full header beat and the opcode is s_tdata[31:24] of that beat.
 * This module is therefore one more reader of the MIN_BYTES = HDR_LEN
 * equality that oca_slip_rx records as an unenforced residual: a
 * MIN_BYTES below 8 would put the decision on a beat that need not
 * carry an opcode at all.
 *
 * LOAD_KEY BROADCASTS TO BOTH CORES. Dispatch is free — a seal can land
 * on either engine — so a key that existed in only one keystore would
 * make half the subsequent traffic fail by scheduling accident. There is
 * no shared keystore and no cross-core key path, deliberately (oca_dual
 * records why: no arbiter is cheaper than that is dangerous), so the
 * key must arrive in both private keystores, which means both cores
 * must accept every beat of the frame. The fork below does that with
 * one taken-flag per core: a beat is offered to each core that has not
 * taken it yet, and the upstream sees tready only on the cycle the
 * second core takes it — backpressure until both have. The cores'
 * tready timing may differ (their bank occupancy diverges the moment a
 * routed frame lands on one of them), and the sent flags are exactly
 * the slack between them.
 *
 * The broadcast is decided on the opcode byte alone, before magic and
 * version are judged — those are oca_proto's checks, not this module's.
 * A frame whose opcode byte reads 01 under a bad magic broadcasts, both
 * cores refuse it with the identical status, and oca_collect forwards
 * one refusal: harmless, and cheaper than a second header parser here.
 *
 * EVERYTHING ELSE GOES TO ONE CORE, the one not used last when both can
 * take it. A fixed priority would be simpler and wrong for the purpose:
 * at serial pace a core is nearly always ready again before the next
 * frame finishes arriving, so "core 0 if free" routes every frame to
 * core 0 and the second engine never runs. Alternation is what puts
 * consecutive frames on different engines so their compute overlaps.
 * When neither core can take a frame the beat simply waits (s_tready
 * low): backpressure runs upstream through oca_slip_rx's buffer, the
 * FIFO behind it, and ultimately the line.
 *
 * THE DECISION COSTS ONE DEAD CYCLE PER FRAME, registered rather than
 * combinational, so no path runs from s_tdata through the opcode
 * compare into s_tready. Against the 4173 cycles a byte occupies on the
 * line at 48.0769 MHz it is not measurable.
 *
 * ONE PUSH PER DISPATCHED FRAME tells oca_collect what to expect:
 * push0/push1 pulse together for a broadcast and alone for a routed
 * frame, in dispatch order, which is each core's request order because
 * frames on the one upstream are strictly sequential. The pulse fires
 * on the decision edge, before the frame has fully crossed — safe
 * because oca_slip_rx delivers an accepted frame whole (its drain
 * cannot abort) and oca_core answers nothing before tlast. A frame is
 * not dispatched while its record cannot be queued (tag_full), which is
 * what makes the expectation FIFOs structurally unable to overflow.
 */
`default_nettype none

module oca_dispatch (
    input  var logic        clk,
    input  var logic        rst_n,

    // One request frame per tlast: oca_slip_rx's output.
    input  var logic [63:0] s_tdata,
    input  var logic [ 7:0] s_tkeep,
    input  var logic        s_tvalid,
    output var logic        s_tready,
    input  var logic        s_tlast,

    // One payload bus into both cores; only the handshake is per core.
    output var logic [63:0] m_tdata,
    output var logic [ 7:0] m_tkeep,
    output var logic        m_tlast,
    output var logic        m0_tvalid,
    input  var logic        m0_tready,
    output var logic        m1_tvalid,
    input  var logic        m1_tready,

    // Dispatch records for oca_collect: a pulse per frame per core it
    // went to, both together meaning broadcast.
    output var logic        push0,
    output var logic        push1,
    input  var logic        tag_full0,
    input  var logic        tag_full1
);

    // oca_proto's OP_LOAD_KEY, the one broadcast opcode.
    localparam logic [7:0] OP_LOAD_KEY = 8'h01;

    typedef enum logic [1:0] {
        D_IDLE,    // the next beat is a header: decide, do not accept
        D_FWD,     // locked to `target` until tlast
        D_BCAST    // forked to both until tlast
    } state_e;

    state_e     state;
    logic       target;      // D_FWD destination
    logic       last_used;   // most recent routed destination
    logic [1:0] sent;        // D_BCAST: core N has taken the current beat

    logic is_bcast;
    always_comb is_bcast = (s_tdata[31:24] == OP_LOAD_KEY);

    logic can0, can1;
    always_comb can0 = m0_tready && !tag_full0;
    always_comb can1 = m1_tready && !tag_full1;

    always_comb m_tdata = s_tdata;
    always_comb m_tkeep = s_tkeep;
    always_comb m_tlast = s_tlast;

    always_comb begin
        m0_tvalid = 1'b0;
        m1_tvalid = 1'b0;
        s_tready  = 1'b0;
        unique case (state)
            D_FWD: begin
                m0_tvalid = s_tvalid && !target;
                m1_tvalid = s_tvalid && target;
                s_tready  = target ? m1_tready : m0_tready;
            end
            D_BCAST: begin
                m0_tvalid = s_tvalid && !sent[0];
                m1_tvalid = s_tvalid && !sent[1];
                s_tready  = (sent[0] || (m0_tvalid && m0_tready))
                         && (sent[1] || (m1_tvalid && m1_tready));
            end
            default: ;   // D_IDLE: the beat waits on the decision
        endcase
    end

    logic fire0, fire1;
    always_comb fire0 = m0_tvalid && m0_tready;
    always_comb fire1 = m1_tvalid && m1_tready;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state     <= D_IDLE;
            target    <= 1'b0;
            // so the first routed frame prefers core 0
            last_used <= 1'b1;
            sent      <= 2'b00;
            push0     <= 1'b0;
            push1     <= 1'b0;
        end else begin
            push0 <= 1'b0;
            push1 <= 1'b0;
            unique case (state)
                D_IDLE: if (s_tvalid) begin
                    if (is_bcast) begin
                        if (!tag_full0 && !tag_full1) begin
                            sent  <= 2'b00;
                            push0 <= 1'b1;
                            push1 <= 1'b1;
                            state <= D_BCAST;
                        end
                    end else if (can0 && (last_used || !can1)) begin
                        target <= 1'b0;
                        push0  <= 1'b1;
                        state  <= D_FWD;
                    end else if (can1) begin
                        target <= 1'b1;
                        push1  <= 1'b1;
                        state  <= D_FWD;
                    end
                end

                D_FWD: if (s_tvalid && s_tready && s_tlast) begin
                    last_used <= target;
                    state     <= D_IDLE;
                end

                default: begin   // D_BCAST
                    if (s_tvalid && s_tready) begin
                        sent <= 2'b00;
                        if (s_tlast) state <= D_IDLE;
                    end else begin
                        sent <= sent | {fire1, fire0};
                    end
                end
            endcase
        end
    end

endmodule

`default_nettype wire
