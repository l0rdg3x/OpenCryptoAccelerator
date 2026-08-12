// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * SLIP decoder, RFC 1055: a byte stream in, oca_core's request stream out.
 *
 * The board has no Ethernet socket, so the host interface is the DAPLink
 * serial line. What UDP used to give the protocol for free was the frame
 * boundary -- oca_core knows the end of a request only as `tlast`, and a
 * byte stream has no end. This supplies it, and nothing else: the wire
 * format above it is unchanged, because SPEC.md's PHASE 3 makes that
 * format the contract later drivers and boards depend on.
 *
 * SLIP AND NOT A LENGTH PREFIX. A prefix would be a second length beside
 * the header's own aad_len and msg_len, and the protocol has no status
 * code for the two disagreeing -- there is no answer to give. SLIP has no
 * second length, and it resynchronises by itself: a byte lost on the line
 * costs one frame, not the session, because the next END is a frame
 * boundary whatever state the decoder was left in.
 *
 * STORE AND FORWARD, and the reason is that one refusal cannot be
 * streamed. Of the three frames this refuses, two are only knowable after
 * bytes have gone by: a bad escape can land in byte 3 of a thousand, and
 * the length is only over BYTES at byte BYTES+1. A streaming decoder
 * would have put those beats on the bus already, and AXI-Stream has no
 * abort -- the request would reach oca_core, be answered, and the host
 * would get a status about a request it never sent. So the frame is
 * assembled whole and delivered only once it is known to be deliverable.
 * The cost is a second copy of the request, in the buffer below, beside
 * the one oca_pktbuf keeps. At 115200 baud the drain is invisible: a beat
 * per cycle at 25 MHz against a byte every 2170.
 *
 * ITS CEILING, stated before it is discovered. No byte is accepted while
 * a frame is draining, so the decoder is half-duplex: it cannot receive
 * frame N+1 while delivering frame N. Draining is at most BYTES/8 cycles
 * -- 256 -- against 2170 cycles per byte on the line, so nothing backs
 * up unless the sink holds tready low for longer than a byte time. When
 * that does happen the bytes wait in the FIFO upstream and are counted by
 * its own overflow if it fills, which is the only place they can be
 * counted honestly: this module cannot count a byte it never saw.
 *
 * WHAT THE OUTPUT MUST LOOK LIKE, each item read off oca_proto rather
 * than assumed:
 *
 *   Byte 0 of the frame is tdata[7:0] and the beat is little-endian.
 *   oca_proto reads MAGIC out of hdr[15:0] (:335, :792) and :215 pins
 *   byte 0 at 0x4F.
 *
 *   Every beat before the last carries tkeep 8'hFF. Anything else sets
 *   keep_bad at :644-645 and the request comes back as status 05.
 *
 *   The last beat's tkeep is contiguous and right-justified. :293-298 is
 *   a priority encoder that takes the highest bit set plus one, so a
 *   keep with a gap in it over-counts -- and does so in silence.
 *
 *   tlast is the only end of frame oca_core understands.
 *
 * THREE REFUSALS, ONE COUNTER EACH, and never two counters for one
 * frame. The counters saturate rather than wrap, for oca_console's
 * reason: a wrapped counter reads like a healthy one. Their sum is the
 * number of frames refused, which is only true because the reasons are
 * taken in priority order -- a bad escape first, because after one the
 * decoded length means nothing and reporting it as short or long would
 * be reporting a symptom of the escape.
 *
 *   cnt_esc    ESC followed by anything but ESC_END or ESC_ESC, a
 *              dangling ESC before an END included. RFC 1055 leaves the
 *              reaction open; forwarding the frame would put a byte the
 *              host never sent into a request it is answered about, and
 *              on an `open` that reads back as an authentication
 *              failure -- a line error wearing a cryptographic status.
 *   cnt_long   more than BYTES bytes. oca_pktbuf would drop the excess
 *              and answer about the prefix.
 *   cnt_short  fewer than MIN_BYTES. oca_proto sends such a request to
 *              P_DROP and answers NOTHING AT ALL (:779-785); the
 *              retired UDP front end made that path unreachable with a
 *              minimum-length guard of its own, and on a byte stream
 *              nothing else stands between it and a host waiting for
 *              ever.
 *
 * MIN_BYTES is oca_proto's HDR_LEN written down a second time and
 * nothing across the module boundary enforces that they agree -- an
 * honest residual, inherited from the guard this replaced rather than
 * introduced here. What the guard buys is that the request oca_proto
 * cannot answer never reaches it, not that it tracks a change in
 * oca_proto.
 *
 * AN EMPTY FRAME IS NOT AN ERROR. Two ENDs in a row are ordinary SLIP:
 * the RFC has senders emit a leading END to flush line noise, so
 * counting one would move a counter on healthy traffic, and a counter
 * that moves for healthy traffic is a counter the operator stops
 * reading. It is discarded and nothing is said.
 *
 * END ALWAYS TERMINATES, a pending escape included. Letting an escape
 * swallow an END would lose the one thing SLIP is chosen for: the frame
 * boundary would move, and every frame after it with it. So an ESC
 * immediately before an END ends the frame and counts as a bad escape,
 * rather than reaching forward into the next one.
 *
 * THE BUFFER IS WALKED OUT OF RESET. It holds whatever the host last
 * sent, and for a load-key command that is the 32 raw key bytes on their
 * way to the slot. oca_pktbuf is walked for exactly this reason
 * (Security.md, "The packet buffers are walked, not reset") and this
 * buffer holds the same bytes one stage earlier; block RAM has no reset
 * on its contents, and a loop over the array in the reset branch is a
 * WORDS-way simultaneous write that no memory primitive expresses --
 * yosys would answer it by lowering the array to flip-flops. So the
 * clear and the writer meet in the multiplexer below and the array keeps
 * its single write port. It costs WORDS cycles, once, during which no
 * byte is accepted.
 */
`default_nettype none

module oca_slip_rx #(
    parameter int BYTES     = 2048,
    parameter int MIN_BYTES = 8
) (
    input  var logic        clk,
    input  var logic        rst_n,

    // Byte stream in: oca_fifo's read port. A byte is present while
    // rx_valid is high and leaves on the edge where rx_pop is high.
    // rx_pop is never raised without rx_valid.
    input  var logic [ 7:0] rx_data,
    input  var logic        rx_valid,
    output var logic        rx_pop,

    // One decoded frame per tlast.
    output var logic [63:0] m_axis_tdata,
    output var logic [ 7:0] m_axis_tkeep,
    output var logic        m_axis_tvalid,
    input  var logic        m_axis_tready,
    output var logic        m_axis_tlast,

    // Frames refused here, saturating at 0xFFFF.
    output var logic [15:0] cnt_short,
    output var logic [15:0] cnt_long,
    output var logic [15:0] cnt_esc
);

    localparam logic [7:0] END     = 8'hC0;
    localparam logic [7:0] ESC     = 8'hDB;
    localparam logic [7:0] ESC_END = 8'hDC;
    localparam logic [7:0] ESC_ESC = 8'hDD;

    localparam int WORDS  = BYTES / 8;
    localparam int ADDR_W = $clog2(WORDS);
    localparam int LEN_W  = $clog2(BYTES + 1);

    // BYTES is what oca_core carries, so it is refused where oca_pktbuf
    // refuses it and for the same reasons: a WORDS that is not a power of
    // two puts the word address off the end of the array, and above 2048
    // it is larger than the buffer this feeds.
    if ((BYTES < 16) || (BYTES > 2048) || (8 * WORDS != BYTES)
        || (2 ** ADDR_W != WORDS)) begin : gen_illegal_bytes
        $fatal(1, "oca_slip_rx: BYTES must be 8 * 2**k, 16..2048 (got %0d)",
               BYTES);
    end

    // MIN_BYTES above BYTES refuses every frame the buffer can hold, and
    // it does it by counting rather than by answering, so the host would
    // wait for ever on a link that looks alive.
    if ((MIN_BYTES < 1) || (MIN_BYTES > BYTES)) begin : gen_illegal_min
        $fatal(1, "oca_slip_rx: MIN_BYTES must be 1..BYTES (got %0d)",
               MIN_BYTES);
    end

    logic [63:0] mem [WORDS];

    typedef enum logic [1:0] {
        S_CLEAR,   // walking the buffer out of reset
        S_RECV,    // decoding bytes into it
        S_PRIME,   // the registered read of word 0 is in flight
        S_DRAIN    // handing the frame over, one beat per tready
    } state_e;

    state_e            state;
    logic [ADDR_W-1:0] clr_addr;

    logic [LEN_W-1:0]  len;        // bytes decoded into this frame so far
    logic [63:0]       asm_w;      // bytes 0..6 of the word being filled
    logic              esc_pend;
    logic              err_esc, err_long;

    logic [ADDR_W-1:0] rd_ptr, rd_next;
    logic [63:0]       rd_q;
    logic [ADDR_W:0]   beats_left;
    logic [7:0]        last_keep;

    // ------------------------------------------------------------------
    // The byte on the wire
    // ------------------------------------------------------------------
    logic byte_take, is_end, is_esc, esc_ok, at_cap, do_store;

    always_comb rx_pop    = rx_valid && (state == S_RECV);
    always_comb byte_take = rx_pop;
    always_comb is_end    = (rx_data == END);
    always_comb is_esc    = (rx_data == ESC);
    always_comb esc_ok    = (rx_data == ESC_END) || (rx_data == ESC_ESC);
    always_comb at_cap    = (len == LEN_W'(BYTES));
    always_comb do_store  = byte_take && !is_end
                            && (esc_pend ? esc_ok : !is_esc);

    logic [7:0] store_byte;
    always_comb store_byte = esc_pend ? ((rx_data == ESC_END) ? END : ESC)
                                      : rx_data;

    logic [5:0] asm_off;
    always_comb asm_off = {len[2:0], 3'b000};

    // ------------------------------------------------------------------
    // The single write port: the clear and the writer choose between each
    // other here, which is what keeps `mem` a block RAM.
    // ------------------------------------------------------------------
    logic              mem_we, word_wr, flush_wr;
    logic [ADDR_W-1:0] mem_addr;
    logic [63:0]       mem_din;

    always_comb word_wr  = do_store && !at_cap && (len[2:0] == 3'd7);
    always_comb flush_wr = byte_take && is_end && (len[2:0] != 3'd0);

    always_comb begin
        if (state == S_CLEAR) begin
            mem_we   = 1'b1;
            mem_addr = clr_addr;
            mem_din  = 64'd0;
        end else if (word_wr) begin
            mem_we   = 1'b1;
            mem_addr = len[ADDR_W+2:3];
            mem_din  = {store_byte, asm_w[55:0]};
        end else begin
            // The tail of a frame whose length is not a whole number of
            // words. asm_w is cleared on every word boundary, so the
            // bytes past the count are zero rather than the previous
            // frame's: tkeep says they are not there and the word says
            // so too.
            mem_we   = flush_wr;
            mem_addr = len[ADDR_W+2:3];
            mem_din  = asm_w;
        end
    end

    // ------------------------------------------------------------------
    // The stream out
    // ------------------------------------------------------------------
    // tvalid comes off the state register alone: no path from tready to
    // tvalid, and the beat stands unchanged until it is taken.
    always_comb m_axis_tvalid = (state == S_DRAIN);
    always_comb m_axis_tdata  = rd_q;
    always_comb m_axis_tlast  = (beats_left == (ADDR_W+1)'(1));
    always_comb m_axis_tkeep  = m_axis_tlast ? last_keep : 8'hFF;

    always_comb begin
        rd_next = rd_ptr;
        if ((state == S_DRAIN) && m_axis_tready)
            rd_next = rd_ptr + ADDR_W'(1);
    end

    // Contiguous and right-justified by construction, which is the shape
    // oca_proto's priority encoder can read. The all-ones branch is the
    // aligned case: a keep built as (1 << len[2:0]) - 1 alone is 8'h00
    // there, and a beat carrying no bytes leaves the packet eight bytes
    // short with every length check still agreeing.
    logic [7:0] end_keep;
    always_comb begin
        for (int i = 0; i < 8; i++)
            end_keep[i] = (len[2:0] == 3'd0) || (3'(i) < len[2:0]);
    end

    logic frame_bad_esc;
    always_comb frame_bad_esc = err_esc || esc_pend;

    function automatic logic [15:0] bump(input logic [15:0] c);
        bump = (c == 16'hFFFF) ? c : c + 16'd1;
    endfunction

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state      <= S_CLEAR;
            clr_addr   <= '0;
            len        <= '0;
            asm_w      <= '0;
            esc_pend   <= 1'b0;
            err_esc    <= 1'b0;
            err_long   <= 1'b0;
            rd_ptr     <= '0;
            rd_q       <= '0;
            beats_left <= '0;
            last_keep  <= '0;
            cnt_short  <= '0;
            cnt_long   <= '0;
            cnt_esc    <= '0;
        end else begin
            rd_ptr <= rd_next;
            rd_q   <= mem[rd_next];
            if (mem_we) mem[mem_addr] <= mem_din;

            case (state)
                S_CLEAR: begin
                    clr_addr <= clr_addr + ADDR_W'(1);
                    if (clr_addr == {ADDR_W{1'b1}}) state <= S_RECV;
                end

                S_RECV: if (byte_take) begin
                    if (is_end) begin
                        len      <= '0;
                        asm_w    <= '0;
                        esc_pend <= 1'b0;
                        err_esc  <= 1'b0;
                        err_long <= 1'b0;
                        // One counter per refused frame, in this order,
                        // so that the three of them sum to the number of
                        // frames refused.
                        if (frame_bad_esc) begin
                            cnt_esc <= bump(cnt_esc);
                        end else if (err_long) begin
                            cnt_long <= bump(cnt_long);
                        end else if (len == '0) begin
                            // an empty frame, which is not an error
                            state <= S_RECV;
                        end else if (len < LEN_W'(MIN_BYTES)) begin
                            cnt_short <= bump(cnt_short);
                        end else begin
                            rd_ptr     <= '0;
                            beats_left <= (ADDR_W+1)'((len + LEN_W'(7)) >> 3);
                            last_keep  <= end_keep;
                            state      <= S_PRIME;
                        end
                    end else begin
                        esc_pend <= is_esc && !esc_pend;
                        if (esc_pend && !esc_ok) begin
                            // Sticky for the rest of the frame: the
                            // position in the stream is no longer known,
                            // so nothing after it can be trusted either.
                            err_esc <= 1'b1;
                        end else if (do_store) begin
                            if (at_cap) begin
                                err_long <= 1'b1;
                            end else begin
                                len <= len + LEN_W'(1);
                                if (len[2:0] == 3'd7) asm_w <= '0;
                                else asm_w[asm_off +: 8] <= store_byte;
                            end
                        end
                    end
                end

                // The read of word 0 issued on the END is landing during
                // this cycle; rd_q is not the frame's first word until
                // the edge that ends it.
                S_PRIME: state <= S_DRAIN;

                default: if (m_axis_tready) begin
                    if (beats_left == (ADDR_W+1)'(1)) state <= S_RECV;
                    else beats_left <= beats_left - (ADDR_W+1)'(1);
                end
            endcase
        end
    end

endmodule

`default_nettype wire
