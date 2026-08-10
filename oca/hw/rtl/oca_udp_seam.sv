// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * The seam between verilog-ethernet's UDP application interface and
 * oca_core's bare AXI-Stream pair.
 *
 * oca_core answers a payload with a payload and knows nothing about who
 * asked. udp_complete_64 hands the request's addressing over on a 22-field
 * sideband that is valid once, before the payload, and wants a header of its
 * own back for the reply. This module is the memory in between: it holds a
 * request's peer address while the core works and presents it again when the
 * core answers. Every reply must carry its own requester's address, so the
 * queue below has to stay in step with the core's responses under every input
 * the wire can present -- including the ones that produce no response at all.
 *
 * Wiring at the top level (the crossing is the easy thing to get wrong):
 *
 *     seam.rx_*   <->  udp_complete_64.m_udp_*     request from the wire
 *     seam.tx_*   <->  udp_complete_64.s_udp_*     reply to the wire
 *     seam.core_s_*  <->  oca_core.s_axis_*        request into the core
 *     seam.core_m_*  <->  oca_core.m_axis_*        response out of the core
 *
 * Reset is ours: active low, asynchronous assert. The vendor subtree is
 * active high and synchronous, and the top level inverts (see the header of
 * vendor/oca_udp_complete_64.v).
 *
 * ------------------------------------------------------------------
 * THE FOUR PROPERTIES THIS MODULE EXISTS FOR
 * ------------------------------------------------------------------
 *
 * 1. HEADER AND PAYLOAD ARE ONE ADMISSION DECISION.
 *
 * m_udp_hdr_ready appears nowhere in udp_ip_rx_64's STATE_READ_PAYLOAD,
 * STATE_READ_PAYLOAD_LAST or STATE_WAIT_LAST (udp_ip_rx_64.v:325-409). A
 * whole packet, tlast included, can cross while its header sits unaccepted.
 * Refusing a header while letting its payload through would put the queue one
 * entry behind for the rest of time, and every later reply would go to the
 * previous peer. So the verdict is taken once, in R_DECIDE, and it governs
 * the header handshake and the payload together.
 *
 * A refused header is still *taken*, never left standing. udp_ip_rx_64.v:273
 * holds m_udp_hdr_valid until ready, and :287 refuses the next frame's IP
 * header while it stands; leaving it unaccepted therefore back-pressures
 * ip_64, then ip_eth_rx_64, then the MAC receive FIFO, which is
 * RX_DROP_WHEN_FULL (vendor/oca_eth_mac_1g_fifo_64.v:192) and answers by
 * discarding unrelated whole frames. A packet this module rejects is accepted
 * off the bus and thrown away, and counted. It is never stalled.
 *
 * 2. A PACKET THE CORE WILL NOT ANSWER MUST NEVER REACH IT.
 *
 * oca_proto has exactly one silent path: rx_rd_count < HDR_LEN goes to P_DROP
 * and answers nothing (oca_proto.sv:779-785, and its comment says so -- every
 * other malformed request is answered with a status). One such request would
 * leave its header in the queue for good.
 *
 * This is made unreachable rather than predicted. The first payload beat is
 * held one cycle; if it is tlast and carries fewer than MIN_REQ_BYTES bytes,
 * the packet is sunk and its header is never enqueued. Exact because
 * udp_ip_rx_64 only ever partials the final beat (:342 masks tkeep solely
 * where word_count_reg <= 8), so a datagram short enough to worry about is
 * necessarily one beat, and MIN_REQ_BYTES is guarded at 8 or less below --
 * one beat's worth -- which is what makes one beat enough to decide.
 *
 * The residual coupling is honest and worth stating: MIN_REQ_BYTES is
 * oca_proto's HDR_LEN written down a second time, and nothing across the
 * module boundary enforces that the two agree. What the guard buys is that
 * the seam cannot be *silently* wrong about a value it can check -- not that
 * it tracks a change in oca_proto.
 *
 * The tkeep == 8'h00 beat is the same rule: a datagram whose UDP length field
 * is 8 leaves word_count_reg at zero, and :342 masks the beat down to no
 * bytes at all while :248 keeps it a legal transfer.
 *
 * 3. THE REPLY HEADER GOES OUT ONLY ONCE THE RESPONSE IS FLOWING.
 *
 * udp_checksum_gen_64 leaves IDLE on the header handshake and then waits for
 * a payload tlast that it has no timeout for and no error output about
 * (:465, :467-476, :489-491, :541-546). Presenting a header speculatively
 * therefore risks wedging the transmit path with no diagnostic. T_HDR is
 * entered only with core_m_tvalid high, and oca_proto holds a response beat
 * until it is taken (oca_proto.sv:587, :1222), so the payload that header
 * promises is already committed before the header is offered.
 *
 * 4. THE REPLY'S SOURCE ADDRESS IS OURS TO DRIVE.
 *
 * ip_64's local_ip input is declared (ip_64.v:143) and read nowhere, and
 * udp_64.v:347 copies s_udp_ip_source_ip verbatim into both the header and
 * the checksum. So what a peer sees as our address is LOCAL_IP from here,
 * not the stack's configuration input. The reply is the request with its
 * addresses swapped, except that the source address is LOCAL_IP rather
 * than the address the request was sent to: a request that arrived by
 * broadcast must not be answered from a broadcast source.
 *
 * That does NOT make the stack's local_ip irrelevant, and an earlier
 * version of this comment said it did. It reaches arp.v:197 as the sender
 * protocol address of every ARP packet we send, and arp.v:305 tests it to
 * decide which ARP requests we answer at all. Two uncoupled sources of
 * truth for one address is a board that answers ARP for one address and
 * replies from another: the requests arrive and are processed, every reply
 * is discarded by the peer's UDP layer for not matching a socket, and no
 * counter here moves, because from this module's point of view nothing
 * went wrong. So LOCAL_IP is published on stack_local_ip and the top level
 * wires the stack from that, rather than being trusted to repeat it.
 *
 * s_udp_length and s_udp_checksum are ignored by the stack -- with
 * UDP_CHECKSUM_GEN_ENABLE both are recomputed downstream -- so no length is
 * computed here in advance.
 *
 * ------------------------------------------------------------------
 * QUEUE DEPTH
 * ------------------------------------------------------------------
 *
 * An entry is held from the cycle the request is admitted to the cycle its
 * response begins, so the depth must cover every request oca_core can be
 * holding at once. Tracing oca_core: one packet still streaming in, one
 * complete in the other receive bank (oca_pktbuf is two-banked), one in
 * PROC/engine, one in the descriptor handoff (oca_proto.sv:245-247, a single
 * parity-flag slot), and two published responses in the transmit banks -- six
 * -- and the transmit side does stall for a long time, because ip_64 blocks
 * in STATE_ARP_QUERY until an ARP reply and the timeout for that is about
 * 30 s.
 *
 * Six is a trace, not a proof, so the default is 8 and the depth is GUARDED
 * rather than asserted to be sufficient: hq_full stops admission, and a
 * request refused for it is dropped, not misaddressed, and lands on
 * cnt_drop_full. An entry is 48 bits, so the choice costs almost nothing.
 * hdr_q_watermark reports the deepest the queue has ever been, which is what
 * turns the trace above into a measurement once there is a board.
 */

`default_nettype none

module oca_udp_seam #(
    parameter logic [31:0] LOCAL_IP      = 32'hC0A8_0164,
    parameter logic [15:0] LOCAL_PORT    = 16'd5000,
    parameter logic [ 7:0] REPLY_TTL     = 8'd64,
    parameter int          HDR_Q_DEPTH   = 8,
    parameter int          MIN_REQ_BYTES = 8,
    parameter int          CNT_W         = 32
) (
    input  var logic        clk,
    input  var logic        rst_n,

    // The stack's local_ip, driven from here so the board has one address
    // and not two. See the LOCAL_IP note in the header: the stack's input
    // is not dead, it decides which ARP requests are answered, and an
    // address that disagrees with the one in our replies produces a board
    // that receives requests and whose answers every peer discards.
    output var logic [31:0] stack_local_ip,

    // Request from the wire: udp_complete_64's m_udp_* application interface.
    input  var logic        rx_hdr_valid,
    output var logic        rx_hdr_ready,
    input  var logic [31:0] rx_ip_source_ip,
    input  var logic [15:0] rx_source_port,
    input  var logic [15:0] rx_dest_port,
    input  var logic [63:0] rx_payload_tdata,
    input  var logic [ 7:0] rx_payload_tkeep,
    input  var logic        rx_payload_tvalid,
    output var logic        rx_payload_tready,
    input  var logic        rx_payload_tlast,
    input  var logic        rx_payload_tuser,

    // Reply to the wire: udp_complete_64's s_udp_* application interface.
    output var logic        tx_hdr_valid,
    input  var logic        tx_hdr_ready,
    output var logic [ 5:0] tx_ip_dscp,
    output var logic [ 1:0] tx_ip_ecn,
    output var logic [ 7:0] tx_ip_ttl,
    output var logic [31:0] tx_ip_source_ip,
    output var logic [31:0] tx_ip_dest_ip,
    output var logic [15:0] tx_source_port,
    output var logic [15:0] tx_dest_port,
    output var logic [15:0] tx_length,
    output var logic [15:0] tx_checksum,
    output var logic [63:0] tx_payload_tdata,
    output var logic [ 7:0] tx_payload_tkeep,
    output var logic        tx_payload_tvalid,
    input  var logic        tx_payload_tready,
    output var logic        tx_payload_tlast,
    output var logic        tx_payload_tuser,

    // Request into oca_core.
    output var logic [63:0] core_s_tdata,
    output var logic [ 7:0] core_s_tkeep,
    output var logic        core_s_tvalid,
    input  var logic        core_s_tready,
    output var logic        core_s_tlast,

    // Response out of oca_core.
    input  var logic [63:0] core_m_tdata,
    input  var logic [ 7:0] core_m_tkeep,
    input  var logic        core_m_tvalid,
    output var logic        core_m_tready,
    input  var logic        core_m_tlast,

    // Every packet leaves by exactly one of these, and a drop nobody can
    // count reads as success.
    output var logic [CNT_W-1:0] cnt_accepted,
    output var logic [CNT_W-1:0] cnt_drop_short,
    output var logic [CNT_W-1:0] cnt_drop_port,
    output var logic [CNT_W-1:0] cnt_drop_full,
    output var logic [CNT_W-1:0] cnt_drop_nohdr,
    output var logic [CNT_W-1:0] cnt_tuser,
    output var logic [CNT_W-1:0] cnt_resp_orphan,
    output var logic [$clog2(HDR_Q_DEPTH):0] hdr_q_watermark
);

    localparam int PTR_W = $clog2(HDR_Q_DEPTH);
    localparam int CNT_Q = PTR_W + 1;

    if ((HDR_Q_DEPTH < 2) || (2 ** PTR_W != HDR_Q_DEPTH)) begin : gen_bad_depth
        $fatal(1, "oca_udp_seam: HDR_Q_DEPTH must be a power of two >= 2 (got %0d)",
               HDR_Q_DEPTH);
    end

    // Over 8 and a single beat could no longer settle the question, which is
    // the whole reason the first beat is held.
    if ((MIN_REQ_BYTES < 1) || (MIN_REQ_BYTES > 8)) begin : gen_bad_min
        $fatal(1, "oca_udp_seam: MIN_REQ_BYTES must be 1..8, one beat (got %0d)",
               MIN_REQ_BYTES);
    end

    if ((CNT_W < 8) || (CNT_W > 32)) begin : gen_bad_cnt
        $fatal(1, "oca_udp_seam: CNT_W must be 8..32 (got %0d)", CNT_W);
    end

    // ==================================================================
    // Header queue
    // ==================================================================
    logic [47:0]      hq_mem [HDR_Q_DEPTH];
    logic [PTR_W-1:0] hq_wr, hq_rd;
    logic [CNT_Q-1:0] hq_cnt;
    logic             hq_full, hq_empty, hq_push, hq_pop;
    logic [47:0]      hq_din;

    always_comb stack_local_ip = LOCAL_IP;

    always_comb hq_full  = (hq_cnt == CNT_Q'(HDR_Q_DEPTH));
    always_comb hq_empty = (hq_cnt == CNT_Q'(0));

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            hq_wr           <= PTR_W'(0);
            hq_rd           <= PTR_W'(0);
            hq_cnt          <= CNT_Q'(0);
            hdr_q_watermark <= CNT_Q'(0);
        end else begin
            if (hq_push) begin
                hq_mem[hq_wr] <= hq_din;
                hq_wr         <= hq_wr + PTR_W'(1);
            end
            if (hq_pop) hq_rd <= hq_rd + PTR_W'(1);
            case ({hq_push, hq_pop})
                2'b10:    hq_cnt <= hq_cnt + CNT_Q'(1);
                2'b01:    hq_cnt <= hq_cnt - CNT_Q'(1);
                default: hq_cnt <= hq_cnt;
            endcase
            if (hq_push && !hq_pop && ((hq_cnt + CNT_Q'(1)) > hdr_q_watermark))
                hdr_q_watermark <= hq_cnt + CNT_Q'(1);
        end
    end

    // ==================================================================
    // Receive: one verdict per packet
    // ==================================================================
    typedef enum logic [2:0] {
        R_IDLE,    // between packets; always ready, so nothing is ever stalled here
        R_DECIDE,  // the held first beat, one cycle, header handshake and verdict
        R_FWD0,    // hand the held beat to the core
        R_FWD,     // the rest of an admitted packet, straight through
        R_SINK     // a rejected packet, taken off the bus and discarded
    } rx_state_e;

    rx_state_e   r_state;
    logic [63:0] b0_data;
    logic [ 7:0] b0_keep;
    logic        b0_last, b0_user;

    logic v_hdr, v_port, v_long, v_room, v_accept;

    always_comb begin
        v_hdr    = rx_hdr_valid;
        v_port   = (rx_dest_port == LOCAL_PORT);
        v_long   = !(b0_last && ($countones(b0_keep) < MIN_REQ_BYTES));
        v_room   = !hq_full;
        v_accept = v_hdr && v_port && v_long && v_room;
    end

    always_comb hq_din  = {rx_ip_source_ip, rx_source_port};
    always_comb hq_push = (r_state == R_DECIDE) && v_accept;

    always_comb begin
        rx_payload_tready = 1'b0;
        case (r_state)
            R_IDLE:  rx_payload_tready = 1'b1;
            R_FWD:   rx_payload_tready = core_s_tready;
            R_SINK:  rx_payload_tready = 1'b1;
            default: rx_payload_tready = 1'b0;
        endcase
    end

    // Taken whatever the verdict: an unaccepted header stops the receive
    // path dead (udp_ip_rx_64.v:287), and R_DECIDE lasts exactly one cycle,
    // so this cannot handshake twice for one header.
    always_comb rx_hdr_ready = (r_state == R_DECIDE) && rx_hdr_valid;

    always_comb begin
        core_s_tdata  = rx_payload_tdata;
        core_s_tkeep  = rx_payload_tkeep;
        core_s_tlast  = rx_payload_tlast;
        core_s_tvalid = 1'b0;
        if (r_state == R_FWD0) begin
            core_s_tdata  = b0_data;
            core_s_tkeep  = b0_keep;
            core_s_tlast  = b0_last;
            core_s_tvalid = 1'b1;
        end else if (r_state == R_FWD) begin
            core_s_tvalid = rx_payload_tvalid;
        end
    end

    logic tuser_seen;
    always_comb begin
        tuser_seen = 1'b0;
        if (r_state == R_DECIDE)
            tuser_seen = b0_user;
        else if (((r_state == R_FWD) || (r_state == R_SINK))
                 && rx_payload_tvalid && rx_payload_tready)
            tuser_seen = rx_payload_tuser;
    end

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            r_state        <= R_IDLE;
            b0_data        <= 64'd0;
            b0_keep        <= 8'd0;
            b0_last        <= 1'b0;
            b0_user        <= 1'b0;
            cnt_accepted   <= CNT_W'(0);
            cnt_drop_short <= CNT_W'(0);
            cnt_drop_port  <= CNT_W'(0);
            cnt_drop_full  <= CNT_W'(0);
            cnt_drop_nohdr <= CNT_W'(0);
            cnt_tuser      <= CNT_W'(0);
        end else begin
            if (tuser_seen) cnt_tuser <= cnt_tuser + CNT_W'(1);

            case (r_state)
                R_IDLE: begin
                    if (rx_payload_tvalid) begin
                        b0_data <= rx_payload_tdata;
                        b0_keep <= rx_payload_tkeep;
                        b0_last <= rx_payload_tlast;
                        b0_user <= rx_payload_tuser;
                        r_state <= R_DECIDE;
                    end
                end

                // Reported by the most specific reason that applies: a short
                // packet is refused whatever the queue is doing, so calling
                // it a queue overflow would point the operator at the wrong
                // thing.
                R_DECIDE: begin
                    if (v_accept) begin
                        cnt_accepted <= cnt_accepted + CNT_W'(1);
                        r_state      <= R_FWD0;
                    end else begin
                        if (!v_hdr)       cnt_drop_nohdr <= cnt_drop_nohdr + CNT_W'(1);
                        else if (!v_port) cnt_drop_port  <= cnt_drop_port  + CNT_W'(1);
                        else if (!v_long) cnt_drop_short <= cnt_drop_short + CNT_W'(1);
                        else              cnt_drop_full  <= cnt_drop_full  + CNT_W'(1);
                        r_state <= b0_last ? R_IDLE : R_SINK;
                    end
                end

                R_FWD0: begin
                    if (core_s_tready) r_state <= b0_last ? R_IDLE : R_FWD;
                end

                R_FWD: begin
                    if (rx_payload_tvalid && core_s_tready && rx_payload_tlast)
                        r_state <= R_IDLE;
                end

                R_SINK: begin
                    if (rx_payload_tvalid && rx_payload_tlast) r_state <= R_IDLE;
                end

                default: r_state <= R_IDLE;
            endcase
        end
    end

    // ==================================================================
    // Transmit: the reply is the request, turned around
    // ==================================================================
    typedef enum logic [1:0] {
        T_IDLE,
        T_HDR,   // header offered; tx_hdr_ready can be low for an ARP timeout
        T_PAY,
        T_SINK   // a response with no header behind it
    } tx_state_e;

    tx_state_e   t_state;
    logic [31:0] peer_ip;
    logic [15:0] peer_port;

    always_comb hq_pop = (t_state == T_IDLE) && core_m_tvalid && !hq_empty;

    always_comb tx_hdr_valid    = (t_state == T_HDR);
    always_comb tx_ip_dscp      = 6'd0;
    always_comb tx_ip_ecn       = 2'd0;
    always_comb tx_ip_ttl       = REPLY_TTL;
    always_comb tx_ip_source_ip = LOCAL_IP;
    always_comb tx_ip_dest_ip   = peer_ip;
    always_comb tx_source_port  = LOCAL_PORT;
    always_comb tx_dest_port    = peer_port;

    // Both recomputed by udp_checksum_gen_64; whatever is driven here is
    // discarded, so nothing pretends to know the response length yet.
    always_comb tx_length   = 16'd0;
    always_comb tx_checksum = 16'd0;

    always_comb tx_payload_tdata  = core_m_tdata;
    always_comb tx_payload_tkeep  = core_m_tkeep;
    always_comb tx_payload_tlast  = core_m_tlast;
    always_comb tx_payload_tuser  = 1'b0;
    always_comb tx_payload_tvalid = (t_state == T_PAY) && core_m_tvalid;

    always_comb begin
        core_m_tready = 1'b0;
        case (t_state)
            T_PAY:   core_m_tready = tx_payload_tready;
            T_SINK:  core_m_tready = 1'b1;
            default: core_m_tready = 1'b0;
        endcase
    end

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            t_state         <= T_IDLE;
            peer_ip         <= 32'd0;
            peer_port       <= 16'd0;
            cnt_resp_orphan <= CNT_W'(0);
        end else begin
            case (t_state)
                // The core never starts a response for a request the receive
                // side rejected, so T_SINK is unreachable by the argument at
                // the head of this file. It is here because that argument is
                // the thing most likely to be wrong, and a reply carrying
                // some earlier peer's address is worse than no reply.
                T_IDLE: begin
                    if (core_m_tvalid) begin
                        if (hq_empty) begin
                            cnt_resp_orphan <= cnt_resp_orphan + CNT_W'(1);
                            t_state         <= T_SINK;
                        end else begin
                            peer_ip   <= hq_mem[hq_rd][47:16];
                            peer_port <= hq_mem[hq_rd][15:0];
                            t_state   <= T_HDR;
                        end
                    end
                end

                T_HDR: begin
                    if (tx_hdr_ready) t_state <= T_PAY;
                end

                T_PAY: begin
                    if (core_m_tvalid && tx_payload_tready && core_m_tlast)
                        t_state <= T_IDLE;
                end

                T_SINK: begin
                    if (core_m_tvalid && core_m_tlast) t_state <= T_IDLE;
                end

                default: t_state <= T_IDLE;
            endcase
        end
    end

endmodule

`resetall
