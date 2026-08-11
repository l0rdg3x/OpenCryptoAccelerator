// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * 8N1 receiver, the other half of the diagnostic channel.
 *
 * TWO FLOPS ON THE INPUT, and they are not optional. rx arrives from the
 * DAPLink's own oscillator with no relationship to clk25, so the pad is
 * a genuine asynchronous boundary: sampling it directly puts a
 * metastable value into the state machine's next-state logic, where it
 * can resolve two ways in the same cycle and split the state. No
 * simulation shows this and no testbench below can, which is exactly why
 * the structure is fixed here rather than argued about later.
 *
 * SAMPLING IS MID-BIT, and the alignment is what makes a receiver
 * tolerate clock error. On seeing the start edge the counter waits
 * DIV/2 to land in the middle of the start bit, checks it is still low
 * -- a lone glitch on an idle line is otherwise a byte -- and then takes
 * one sample every DIV from there. The last sample, the stop bit, is
 * therefore 9.5 bit times after the edge, so the two ends may differ by
 * up to about 5% before the stop bit lands outside its window. 217
 * against a true 115200 costs 0.006% of that budget.
 *
 * A FRAMING ERROR DROPS THE BYTE AND SAYS SO. If the stop bit is not
 * high the byte did not arrive as framed, and valid stays low while
 * frame_error pulses. Delivering it anyway would put a plausible wrong
 * byte into whatever reads this, which for a diagnostic channel means a
 * command nobody typed.
 */
`default_nettype none

module oca_uart_rx #(
    parameter int DIV = 217
) (
    input  var logic       clk,
    input  var logic       rx,
    output var logic [7:0] data,
    output var logic       valid,
    output var logic       frame_error
);

    localparam int DIV_W = $clog2(DIV);

    logic [1:0] sync;
    logic       rx_s;

    always_ff @(posedge clk) begin
        sync <= {sync[0], rx};
    end

    always_comb rx_s = sync[1];

    typedef enum logic [1:0] { IDLE, START, DATA, STOP } state_e;

    state_e           state;
    logic [DIV_W-1:0] div_count;
    logic [2:0]       bit_index;
    logic [7:0]       shifter;

    always_ff @(posedge clk) begin
        valid       <= 1'b0;
        frame_error <= 1'b0;

        case (state)
            IDLE: begin
                // Idle is high, so a start bit is the line going low.
                if (!rx_s) begin
                    state     <= START;
                    div_count <= '0;
                end
            end

            START: begin
                if (div_count != DIV_W'(DIV / 2)) begin
                    div_count <= div_count + DIV_W'(1);
                end else begin
                    div_count <= '0;
                    if (rx_s) begin
                        // Gone again by the midpoint: a glitch, not a
                        // frame. Back to idle without emitting anything.
                        state <= IDLE;
                    end else begin
                        state     <= DATA;
                        bit_index <= 3'd0;
                    end
                end
            end

            DATA: begin
                if (div_count != DIV_W'(DIV - 1)) begin
                    div_count <= div_count + DIV_W'(1);
                end else begin
                    div_count <= '0;
                    shifter   <= {rx_s, shifter[7:1]};   // LSB first
                    if (bit_index == 3'd7) begin
                        state <= STOP;
                    end else begin
                        bit_index <= bit_index + 3'd1;
                    end
                end
            end

            STOP: begin
                if (div_count != DIV_W'(DIV - 1)) begin
                    div_count <= div_count + DIV_W'(1);
                end else begin
                    div_count <= '0;
                    state     <= IDLE;
                    if (rx_s) begin
                        data  <= shifter;
                        valid <= 1'b1;
                    end else begin
                        frame_error <= 1'b1;
                    end
                end
            end

            default: state <= IDLE;
        endcase
    end

endmodule

`default_nettype wire
