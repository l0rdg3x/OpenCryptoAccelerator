// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * The diagnostic console on the board: UART both ways, a FIFO on each
 * side, and oca_console between them.
 *
 * WHY A FIFO ON EACH SIDE, since the console answers one command at a
 * time. On the receive side, because a byte arriving while a response is
 * going out has nowhere else to wait: that is exactly what oca_uart_echo
 * demonstrated on 2026-08-11, where "OCA" typed at speed came back "OA".
 * On the transmit side, because oca_console produces a byte per cycle
 * when allowed and the transmitter takes ten bit times to move one; with
 * no buffer the console would stall for the whole line, which is
 * harmless here and would not be once it has a subsystem to poll.
 *
 * DEPTHS. 16 in, 32 out. The output holds the longest response, 28
 * bytes, with room to spare, so a full line never stalls mid-way. The
 * input is sized for a human typing and will overflow under a paste,
 * which is what the O counter is for.
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

    oca_fifo #(.WIDTH (8), .DEPTH (16)) u_rx_fifo (
        .clk      (clk25),
        .rst_n    (rst_n),
        .wr_data  (rx_byte),
        .push     (rx_valid),
        .full     (),
        .overflow (rx_fifo_overflow),
        .rd_data  (cmd_byte),
        .pop      (cmd_pop),
        .empty    (rx_fifo_empty),
        .level    ()
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
        .overflow (),
        .rd_data  (tx_fifo_data),
        .pop      (tx_fifo_pop),
        .empty    (tx_fifo_empty),
        .level    ()
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
