// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * Synchronous FIFO, one clock, power-of-two depth.
 *
 * The echo dropped every other byte because a byte arriving during a
 * transmission had nowhere to wait. This is where it waits.
 *
 * ONE EXTRA BIT ON THE POINTERS, which is what makes full and empty
 * distinguishable. With DEPTH-wide pointers the two conditions are both
 * "read equals write" and no comparison separates them; carrying the
 * wrap bit makes empty pointers equal and full pointers equal except in
 * that bit. The alternative, keeping a count, costs the same flops and
 * an adder on both edges.
 *
 * A WRITE TO A FULL FIFO IS REFUSED AND COUNTED, not absorbed. `push`
 * with `full` high does nothing and raises `overflow` for one cycle.
 * Silently overwriting the oldest byte would turn a console that ran out
 * of room into a console that answers a command nobody typed; silently
 * dropping the newest turns it into one that misses a command with no
 * trace. Both are worse than a counter the operator can read.
 */
`default_nettype none

module oca_fifo #(
    parameter int WIDTH = 8,
    parameter int DEPTH = 16
) (
    input  var logic             clk,
    input  var logic             rst_n,

    input  var logic [WIDTH-1:0] wr_data,
    input  var logic             push,
    output var logic             full,
    output var logic             overflow,

    output var logic [WIDTH-1:0] rd_data,
    input  var logic             pop,
    output var logic             empty,

    output var logic [$clog2(DEPTH+1)-1:0] level
);

    localparam int PTR_W = $clog2(DEPTH);

    if (DEPTH < 2 || (DEPTH & (DEPTH - 1)) != 0) begin : gen_bad_depth
        $fatal(1, "oca_fifo: DEPTH must be a power of two and at least 2");
    end

    logic [WIDTH-1:0] mem [DEPTH];
    logic [PTR_W:0]   wr_ptr, rd_ptr;

    always_comb empty = (wr_ptr == rd_ptr);
    always_comb full  = (wr_ptr[PTR_W] != rd_ptr[PTR_W]) &&
                        (wr_ptr[PTR_W-1:0] == rd_ptr[PTR_W-1:0]);
    always_comb level = (PTR_W+1)'(wr_ptr - rd_ptr);

    // Read data is combinational off the memory rather than registered:
    // the console decides what to do with a byte in the same cycle it
    // sees it, and a registered output would need a skid buffer to say
    // the same thing.
    always_comb rd_data = mem[rd_ptr[PTR_W-1:0]];

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            wr_ptr   <= '0;
            rd_ptr   <= '0;
            overflow <= 1'b0;
        end else begin
            overflow <= 1'b0;
            if (push) begin
                if (full) begin
                    overflow <= 1'b1;
                end else begin
                    mem[wr_ptr[PTR_W-1:0]] <= wr_data;
                    wr_ptr <= wr_ptr + (PTR_W+1)'(1);
                end
            end
            if (pop && !empty) begin
                rd_ptr <= rd_ptr + (PTR_W+1)'(1);
            end
        end
    end

endmodule

`default_nettype wire
