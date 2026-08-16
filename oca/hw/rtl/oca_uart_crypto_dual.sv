// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * Two AEAD cores behind the board's one serial line.
 *
 * oca_uart_rx -> oca_fifo -> oca_slip_rx -> oca_dispatch -> 2x oca_core
 * -> oca_collect -> oca_slip_tx -> oca_fifo -> oca_uart_tx8. The front
 * end is oca_uart_crypto's, leaf for leaf and number for number; what
 * changed is the middle, where one core became a dispatcher, two cores
 * and a collector. The wire format is untouched: the host tells this
 * design from the single-core one by throughput and by nothing on the
 * wire.
 *
 * A NEW TOP, NOT A REFACTOR OF oca_uart_crypto, deliberately. Cutting
 * that module into a front-end module plus core would have kept these
 * ~150 lines single-sourced, but it would have rewritten the RTL behind
 * the figures docs/RECORD.md carries for the single-core top — and an
 * Fmax belongs to the commit it was measured on, so the committed
 * numbers would have stopped describing any buildable design. It is
 * also the house pattern: oca_uart_console, oca_uart_echo and
 * oca_uart_crypto each wire the same leaves themselves. What this file
 * duplicates is instantiation, not logic, and every derivation behind
 * the numbers — the stop-bit sampling guard, the FIFO depth arithmetic,
 * the two-reset shape, the sticky trouble rationale — lives in
 * oca_uart_crypto.sv and is not restated here.
 *
 * THIS IS THE DATAPATH, NOT THE BOARD TOP. oca_crypto_dual holds
 * oca_clkrst and the LED, for oca_crypto_pll's testability reason: a
 * PLL in here would leave a simulation of this module fabricating the
 * clock it exists to test.
 *
 * WHAT THE FRONT-END NUMBERS STILL MEAN WITH TWO CORES. BYTES stays one
 * localparam passed to the decoder and both cores, so no core can be
 * answered about a prefix. The FIFO depths (16/16) were sized in
 * oca_uart_crypto against how long the decoder is away from S_RECV, and
 * the fabric only shortens that: oca_dispatch adds one dead cycle per
 * frame, and a frame drains whenever either core can take it — the
 * single-core bound is the case where both are the busier core.
 * MIN_BYTES = 8 carries one more reader than it did: oca_slip_rx's
 * refusal of sub-header frames is also what guarantees oca_dispatch a
 * full header in every first beat, and what makes every dispatched
 * frame answerable — the ground under oca_collect's no-timeout
 * argument. The residual that MIN_BYTES is oca_proto's HDR_LEN written
 * down again is recorded in oca_slip_rx.sv and is one copy, not three:
 * this file passes the same constant to all of them.
 *
 * TROUBLE IS STILL ONE STICKY BIT, now with seven sources: the six of
 * oca_uart_crypto (frame error, two FIFO overflows, three SLIP
 * refusals) plus oca_collect's fault bit — a broadcast the two cores
 * answered differently, which identical RTL on identical input cannot
 * do, or an expectation queue overflowed, which the dispatcher's
 * tag_full stall exists to prevent; either way it is latched as a
 * fault rather than resolved. The collector's
 * bit is itself sticky, but it feeds the latch here so that all seven
 * reach the LED through one wire, exactly as before.
 */
`default_nettype none

module oca_uart_crypto_dual #(
    // The frequency of `clk`, in hertz. oca_crypto_dual passes
    // oca_clkrst's clk_sys; the default is the 25 MHz board oscillator
    // a standalone build runs on.
    parameter int CLK_HZ = 25_000_000
) (
    input  var logic clk,
    // Asynchronous, active low: see oca_uart_crypto's header for the
    // two-reset shape this file repeats. Tie high where there is
    // nothing to gate on.
    input  var logic rst_n,
    output var logic uart_tx,
    input  var logic uart_rx,
    // Low while the datapath is held; a register in this clock domain,
    // which is what the board top's heartbeat reads it for.
    output var logic rst_n_core,
    // Sticky: something was refused, lost, or diverged. Seven sources,
    // per the header.
    output var logic trouble
);

    localparam int BAUD_HZ = 115_200;
    localparam int DIV     = CLK_HZ / BAUD_HZ;

    /*
     * The stop-bit sampling guard, verbatim from oca_uart_crypto.sv,
     * where its derivation, its 2026-08-15 measurement under Verilator
     * and its blind spot (a CLK_HZ the clock does not carry) are
     * documented. It is repeated rather than shared because the two
     * tops are deliberately separate files; the constants must move
     * together.
     */
    localparam longint MAX_SAMPLE_ERR_PPM = 25_000;

    localparam longint STOP_CYCLES_EARLY =
        longint'(DIV) / 2 + 9 * longint'(DIV) + 3;
    localparam longint STOP_CYCLES_LATE = STOP_CYCLES_EARLY + 1;

    localparam longint SAMPLE_ERR_EARLY_PPM =
        ((2 * STOP_CYCLES_EARLY * BAUD_HZ - 19 * longint'(CLK_HZ)) * 1_000_000)
        / (2 * longint'(CLK_HZ));
    localparam longint SAMPLE_ERR_LATE_PPM =
        ((2 * STOP_CYCLES_LATE * BAUD_HZ - 19 * longint'(CLK_HZ)) * 1_000_000)
        / (2 * longint'(CLK_HZ));

    if (SAMPLE_ERR_LATE_PPM > MAX_SAMPLE_ERR_PPM
        || SAMPLE_ERR_EARLY_PPM < -MAX_SAMPLE_ERR_PPM) begin : gen_bad_clk_hz
        $fatal(1,
            "oca_uart_crypto_dual: CLK_HZ %0d, DIV %0d, stop bit sampled %0d to %0d ppm off 9.5 bit times (max %0d)",
            CLK_HZ, DIV, SAMPLE_ERR_EARLY_PPM, SAMPLE_ERR_LATE_PPM,
            MAX_SAMPLE_ERR_PPM);
    end

    // One number for the decoder and both cores, so they cannot drift.
    localparam int BYTES = 2048;
    // oca_proto's HDR_LEN. A copy, not a link (oca_slip_rx.sv) — and
    // oca_dispatch's header-in-first-beat guarantee rides on it too.
    localparam int MIN_BYTES = 8;

    localparam int RX_DEPTH = 16;
    localparam int TX_DEPTH = 16;

    logic [3:0] por_count;

    // rst_n is the asynchronous clear and the count is the only
    // release: no combinational term reaches the reset net
    // (oca_uart_crypto.sv, "THE TWO ARE NOT ANDed").
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

    // Flags this design does not read, named so -Wall stays clean
    // without a waiver (oca_uart_crypto.sv).
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
    // The fabric and the two engines
    // ------------------------------------------------------------------
    logic [63:0] d_tdata;
    logic [ 7:0] d_tkeep;
    logic        d_tlast;
    logic        d0_tvalid, d0_tready, d1_tvalid, d1_tready;
    logic        push0, push1, tag_full0, tag_full1;

    oca_dispatch u_dispatch (
        .clk       (clk),
        .rst_n     (rst_n_core),
        .s_tdata   (req_tdata),
        .s_tkeep   (req_tkeep),
        .s_tvalid  (req_tvalid),
        .s_tready  (req_tready),
        .s_tlast   (req_tlast),
        .m_tdata   (d_tdata),
        .m_tkeep   (d_tkeep),
        .m_tlast   (d_tlast),
        .m0_tvalid (d0_tvalid),
        .m0_tready (d0_tready),
        .m1_tvalid (d1_tvalid),
        .m1_tready (d1_tready),
        .push0     (push0),
        .push1     (push1),
        .tag_full0 (tag_full0),
        .tag_full1 (tag_full1)
    );

    logic [63:0] r0_tdata, r1_tdata;
    logic [ 7:0] r0_tkeep, r1_tkeep;
    logic        r0_tvalid, r0_tready, r0_tlast;
    logic        r1_tvalid, r1_tready, r1_tlast;

    oca_core #(.BYTES (BYTES)) u_core0 (
        .clk           (clk),
        .rst_n         (rst_n_core),
        .s_axis_tdata  (d_tdata),
        .s_axis_tkeep  (d_tkeep),
        .s_axis_tvalid (d0_tvalid),
        .s_axis_tready (d0_tready),
        .s_axis_tlast  (d_tlast),
        .m_axis_tdata  (r0_tdata),
        .m_axis_tkeep  (r0_tkeep),
        .m_axis_tvalid (r0_tvalid),
        .m_axis_tready (r0_tready),
        .m_axis_tlast  (r0_tlast)
    );

    oca_core #(.BYTES (BYTES)) u_core1 (
        .clk           (clk),
        .rst_n         (rst_n_core),
        .s_axis_tdata  (d_tdata),
        .s_axis_tkeep  (d_tkeep),
        .s_axis_tvalid (d1_tvalid),
        .s_axis_tready (d1_tready),
        .s_axis_tlast  (d_tlast),
        .m_axis_tdata  (r1_tdata),
        .m_axis_tkeep  (r1_tkeep),
        .m_axis_tvalid (r1_tvalid),
        .m_axis_tready (r1_tready),
        .m_axis_tlast  (r1_tlast)
    );

    logic [63:0] rsp_tdata;
    logic [ 7:0] rsp_tkeep;
    logic        rsp_tvalid, rsp_tready, rsp_tlast;
    logic        fabric_trouble;

    oca_collect u_collect (
        .clk       (clk),
        .rst_n     (rst_n_core),
        .s0_tdata  (r0_tdata),
        .s0_tkeep  (r0_tkeep),
        .s0_tvalid (r0_tvalid),
        .s0_tready (r0_tready),
        .s0_tlast  (r0_tlast),
        .s1_tdata  (r1_tdata),
        .s1_tkeep  (r1_tkeep),
        .s1_tvalid (r1_tvalid),
        .s1_tready (r1_tready),
        .s1_tlast  (r1_tlast),
        .m_tdata   (rsp_tdata),
        .m_tkeep   (rsp_tkeep),
        .m_tvalid  (rsp_tvalid),
        .m_tready  (rsp_tready),
        .m_tlast   (rsp_tlast),
        .push0     (push0),
        .push1     (push1),
        .tag_full0 (tag_full0),
        .tag_full1 (tag_full1),
        .trouble   (fabric_trouble)
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
    // The sticky trouble latch: oca_uart_crypto's six sources plus the
    // collector's fault bit
    // ------------------------------------------------------------------
    always_ff @(posedge clk or negedge rst_n_core) begin
        if (!rst_n_core) begin
            trouble <= 1'b0;
        end else if (rx_frame_error || rx_fifo_overflow || tx_fifo_overflow
                     || (|cnt_short) || (|cnt_long) || (|cnt_esc)
                     || fabric_trouble) begin
            trouble <= 1'b1;
        end
    end

endmodule

`default_nettype wire
