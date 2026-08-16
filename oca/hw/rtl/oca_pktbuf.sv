// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * Packet buffer for the OCA host protocol.
 *
 * 64 bits wide, written sequentially from the packet stream and read at
 * random word offsets. A 64-byte block costs 8 reads plus the read
 * pipeline, against the AEAD engine's 36 cycles to process one, so the
 * buffer no longer sets the pace: at one byte per cycle it needed 66
 * cycles to assemble a block the engine consumed in 40, which is why the
 * datapath moved to 64 bits (amendment of 2026-08-04 in
 * docs/design/2026-08-03-host-protocol.md).
 *
 * Two banks, addressed by wr_bank and rd_bank, so one packet can be
 * received while another is being read. Each bank carries its own byte
 * count and its own clear: a clear reaches only the bank its writer
 * currently owns, because a clear aimed at the wrong bank truncates a
 * packet in flight in silence and produces a short response under a
 * valid tag — the failure mode that is indistinguishable from success.
 * There are two counters and never one, for the same reason: the byte
 * count a length check is validated against must be the count of the
 * packet being checked, not of the packet still arriving behind it.
 *
 * Two banks and not three. A ring would have been the alternative and is
 * the wrong shape here: the protocol reads at arbitrary word offsets
 * (header, arguments, then a funnel walking the payload across the
 * AAD/message boundary), so it is not a FIFO consumer, and a region that
 * wrapped would make the very want_len and rx_len comparisons modular
 * that exist to stop a field being read from bytes the packet never
 * wrote. A third bank would also double the block RAM, where two are
 * free (below), and it would only help under overload — where the right
 * answer is back-pressure, not more memory.
 *
 * wr_count stays a byte count because rx_len, want_len and resp_len all
 * compare against it, and that comparison is what keeps a field from
 * being read out of bytes the packet never wrote. wr_bytes carries 1..8
 * valid bytes, so only the final write of a packet may be partial: a
 * partial write mid-stream leaves the count off a word boundary and the
 * next write lands back on the word just written. oca_proto fails such a
 * packet closed rather than issue one.
 *
 * Writes past BYTES are dropped and rd_full is raised for the reader of
 * that bank: a truncated packet becomes a length error at the protocol
 * layer rather than a silent wrap that corrupts what is already stored.
 *
 * BYTES is eight times a power of two, from 16 to 2048, and anything else
 * is refused at elaboration. The bank base is 2**ADDR_W while the array
 * is 2*WORDS entries long, so a WORDS that is not a power of two leaves
 * the upper bank running off the end of the array: writes there are
 * dropped, reads come back empty, and oca_proto answers status 00 over
 * whatever the register held — the failure indistinguishable from success
 * once more, and this one reaches the wire. Above 2048 the 12-bit byte
 * counters cannot represent BYTES, 12'(BYTES) truncates and both full
 * flags jam high. Nothing downstream can see either case, and BYTES=1536
 * is exactly the number someone sizing the buffer against an Ethernet
 * MTU would reach for, so the module refuses to build instead.
 *
 * Reads are registered, one word per cycle, and there is exactly one
 * write port and one read port, which is what lets yosys map the memory
 * through $__PDPW16KD_ — pseudo dual-port, 36 bits wide. That mode is
 * 512 x 36 and one bank used only 256 words of it, so the second bank is
 * free: 512 x 64 is 32 Kbit, exactly 2 DP16KD filled to the last bit,
 * which is what one bank already cost. A second read port, an
 * asynchronous read, or a multiplexer between the memory and its output
 * register would take it to 4 DP16KD or into LUT RAM (SPEC.md
 * portability rule: inferred, never instantiated).
 *
 * The array is zeroed out of reset, one word per cycle over both banks,
 * and clr_busy is high until the last of them is done. It holds the
 * request and the response of whatever ran last — plaintext, ciphertext
 * and the 32 raw key bytes of a load-key command — and a reset that
 * restored the counters alone left every byte of it in the block RAM
 * (Security.md). The counters are cleared by the reset itself, so what
 * this walk exists for is the payload, which nothing else can reach.
 *
 * The clear owns the write port while it runs and the writer is inert:
 * no word lands and no count moves, because a count that advanced over
 * dropped data would have the packet answered at its full length out of
 * words the buffer never wrote. clr_busy is what lets the level above
 * hold the traffic off instead — oca_core gates s_axis_tready on it.
 *
 * That single port is also why the clear is a walk and not a loop over
 * the array in the reset branch: a 512-way simultaneous write is not
 * something a memory primitive expresses, and yosys would answer it by
 * lowering `mem` to 32768 flip-flops. The clear and the writer meet in
 * the always_comb below, so the always_ff keeps exactly one assignment
 * to `mem`.
 */
module oca_pktbuf #(
    parameter int BYTES = 2048
) (
    input  logic        clk,
    input  logic        rst_n,
    // write side: one bank at a time, sequential, byte-counted
    input  logic        wr_bank,
    input  logic        wr_en,
    input  logic [63:0] wr_data,
    input  logic [ 3:0] wr_bytes,
    input  logic        wr_clear,
    output logic [11:0] wr_count,
    // read side: the other bank, random word offsets
    input  logic        rd_bank,
    input  logic [ 8:0] rd_addr,
    output logic [63:0] rd_data,
    output logic [11:0] rd_count,
    output logic        rd_full,
    // high while the memory is being zeroed. The write side is inert
    // while it is; the read side is not gated and answers out of words
    // the walk has not reached yet, so a reader that cannot wait for
    // this must not believe what it gets.
    output logic        clr_busy
);

    // Offsets are carried as a 12-bit byte quantity at the protocol
    // layer; the memory is indexed by the word address derived from it,
    // which is only ever used once the wide one has been range-checked.
    localparam int WORDS  = BYTES / 8;
    localparam int ADDR_W = $clog2(WORDS);

    // The constraint from the header, checked where it is cheap to check.
    // An elaboration-time $fatal inside a generate block: verilator and
    // slang both stop on it, so the illegal parameter fails the build and
    // the lint rather than the board.
    if ((BYTES < 16) || (BYTES > 2048) || (8 * WORDS != BYTES)
        || (2 ** ADDR_W != WORDS)) begin : gen_illegal_bytes
        $fatal(1, "oca_pktbuf: BYTES must be 8 * 2**k, 16..2048 (got %0d)",
               BYTES);
    end

    logic [63:0] mem [2*WORDS];

    // One address per word of the array, both banks. WORDS is a power of
    // two, so the last one is all ones and the walk ends on it.
    localparam int CLR_W = ADDR_W + 1;

    logic             clearing;
    logic [CLR_W-1:0] clr_addr;

    always_comb clr_busy = clearing;

    // One counter per bank, written only by the owner of that bank's
    // write port. Kept as two named registers rather than an array so
    // that "no bank ever touches the other's count" is visible here
    // rather than inferred from an index.
    logic [11:0] count0, count1;
    logic [ADDR_W:0] rd_index;
    logic        wr_full;

    always_comb wr_count = wr_bank ? count1 : count0;
    always_comb rd_count = rd_bank ? count1 : count0;
    always_comb wr_full  = (wr_count >= 12'(BYTES));
    always_comb rd_full  = (rd_count >= 12'(BYTES));

    // The range check sits on the address, not on the read data: a
    // multiplexer between the memory and its output register would stop
    // the read port being registered, and with it the block RAM. It
    // clamps inside the addressed bank and not to absolute word zero,
    // because word zero of bank zero is another packet's header: a
    // bounds failure must degrade to this packet's own bytes, never to
    // its neighbour's.
    always_comb rd_index = {rd_bank, (rd_addr < 9'(WORDS))
                                     ? rd_addr[ADDR_W-1:0] : {ADDR_W{1'b0}}};

    // The write port, and the only place the clear and the writer choose
    // between each other. Both reach `mem` through the single assignment
    // below, which is what keeps the array a block RAM.
    logic             mem_we;
    logic [CLR_W-1:0] mem_addr;
    logic [     63:0] mem_din;

    always_comb begin
        mem_we   = clearing ? 1'b1 : (wr_en && !wr_clear && !wr_full);
        mem_addr = clearing ? clr_addr : {wr_bank, wr_count[ADDR_W+2:3]};
        mem_din  = clearing ? 64'd0 : wr_data;
    end

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            count0   <= '0;
            count1   <= '0;
            rd_data  <= '0;
            clearing <= 1'b1;
            clr_addr <= '0;
        end else begin
            if (clearing) begin
                clr_addr <= clr_addr + CLR_W'(1);
                if (clr_addr == {CLR_W{1'b1}}) clearing <= 1'b0;
            end else if (wr_clear) begin
                if (wr_bank) count1 <= '0;
                else         count0 <= '0;
            end else if (wr_en && !wr_full) begin
                if (wr_bank) count1 <= wr_count + 12'(wr_bytes);
                else         count0 <= wr_count + 12'(wr_bytes);
            end
            if (mem_we) mem[mem_addr] <= mem_din;
            rd_data <= mem[rd_index];
        end
    end

endmodule
