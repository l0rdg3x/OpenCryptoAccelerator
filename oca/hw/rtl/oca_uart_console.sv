// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * The diagnostic console on the board: UART both ways, a FIFO on each
 * side, and oca_console between them.
 *
 * WHICH FIFO ACTUALLY FIXES THE ECHO, since only one of them does.
 *
 * THE OUTPUT ONE. oca_uart_echo dropped alternate bytes because its
 * transmitter was still busy when the next byte arrived; here the
 * console pushes into a buffer and is free again in a few cycles, and
 * the transmitter drains at its own pace.
 *
 * THE INPUT ONE IS INSURANCE AND IS NOT LOAD BEARING TODAY, which this
 * comment claimed it was until 2026-08-11. The console empties a
 * response into the output FIFO at about a byte a cycle -- four cycles
 * for `p`, twenty-eight for `s` -- while bytes arrive every 2170. So
 * `sending` has fallen long before the next byte lands, and no sequence
 * a UART can deliver at this baud reaches the input queue at all. It
 * earns its place when a command's answer outgrows the output FIFO or
 * takes real time to produce, which is what happens as soon as there is
 * a subsystem to poll. Until then the honest statement is that the test
 * suite cannot exercise it through the UART, and does not claim to.
 *
 * DEPTHS. 16 in, 32 out. The output holds the longest response, 28
 * bytes, so a line that starts into an empty FIFO never stalls mid-way;
 * one that starts while a previous line is still draining can. The
 * input is sized for a human typing, and O is what says when it was
 * not enough.
 *
 * RESET. There is no reset pin on this board and no PLL here to lock, so
 * rst_n comes from a small power-on counter: ECP5 flip-flops come out of
 * configuration cleared, so the counter starts at zero and releases
 * reset once. Without it the FIFOs would come up with pointers that are
 * defined but never deliberately set, which is the same value today and
 * an assumption tomorrow.
 */
`default_nettype none

module oca_uart_console (
    input  var logic clk25,
    output var logic led_n,
    output var logic uart_tx,
    input  var logic uart_rx
);

    localparam int DIV = 217;

    logic [3:0] por_count;
    logic       rst_n;

    always_ff @(posedge clk25) begin
        if (por_count != 4'd15) begin
            por_count <= por_count + 4'd1;
        end
    end

    always_comb rst_n = (por_count == 4'd15);

    logic [7:0] rx_byte;
    logic       rx_valid, rx_frame_error;

    oca_uart_rx #(.DIV (DIV)) u_rx (
        .clk         (clk25),
        .rx          (uart_rx),
        .data        (rx_byte),
        .valid       (rx_valid),
        .frame_error (rx_frame_error)
    );

    logic [7:0] cmd_byte;
    logic       cmd_avail, cmd_pop, rx_fifo_overflow;
    logic       rx_fifo_empty;

    // Three FIFO outputs this design deliberately does not read, named
    // rather than left empty so that -Wall stays clean without a waiver.
    // The input FIFO's full flag says nothing its overflow does not,
    // and oca_console counts the overflow; the output FIFO's overflow
    // cannot assert at all, because tx_push is gated on tx_ready and
    // tx_ready is that FIFO's own full flag inverted; neither level is
    // published anywhere.
    logic       rx_fifo_full, tx_fifo_overflow;
    logic [4:0] rx_fifo_level;
    logic [5:0] tx_fifo_level;
    logic       unused_ok;
    always_comb unused_ok = rx_fifo_full | tx_fifo_overflow
                          | (|rx_fifo_level) | (|tx_fifo_level);

    oca_fifo #(.WIDTH (8), .DEPTH (16)) u_rx_fifo (
        .clk      (clk25),
        .rst_n    (rst_n),
        .wr_data  (rx_byte),
        .push     (rx_valid),
        .full     (rx_fifo_full),
        .overflow (rx_fifo_overflow),
        .rd_data  (cmd_byte),
        .pop      (cmd_pop),
        .empty    (rx_fifo_empty),
        .level    (rx_fifo_level)
    );

    always_comb cmd_avail = !rx_fifo_empty;

    logic [7:0] tx_byte;
    logic       tx_push, tx_fifo_full, tx_fifo_empty;
    logic [7:0] tx_fifo_data;
    logic       tx_busy;

    oca_console u_console (
        .clk         (clk25),
        .rst_n       (rst_n),
        .rx_data     (cmd_byte),
        .rx_valid    (cmd_avail),
        .rx_pop      (cmd_pop),
        // Straight off the receiver, not off the queue, so R counts what
        // arrived rather than what was consumed.
        .rx_delivered (rx_valid),
        .frame_error (rx_frame_error),
        .rx_overflow (rx_fifo_overflow),
        .tx_data     (tx_byte),
        .tx_push     (tx_push),
        .tx_ready    (!tx_fifo_full)
    );

    logic tx_fifo_pop;

    oca_fifo #(.WIDTH (8), .DEPTH (32)) u_tx_fifo (
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

    // D2 toggles per byte the receiver delivers, so a dead terminal and
    // a dead receiver look different from across the room.
    always_ff @(posedge clk25) begin
        if (rx_valid) begin
            led_n <= ~led_n;
        end
    end

endmodule

`default_nettype wire
