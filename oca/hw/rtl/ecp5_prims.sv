// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * Blackbox declarations for the ECP5 IO primitives this design instantiates.
 *
 * They exist in yosys already, in ecp5/cells_bb.v, complete with their
 * parameters — but that file has to be read by read_verilog, and a module
 * that reaches read_slang through read_verilog arrives already elaborated
 * with its parameters resolved. Overriding one from SystemVerilog then
 * fails with "parameter 'DEL_VALUE' does not exist in 'DELAYF'". Declaring
 * them here instead puts the parameters in front of slang, which is the
 * frontend that elaborates our RTL, and yosys carries the blackbox straight
 * through to the netlist for nextpnr to place.
 *
 * The port lists and parameter defaults are copied from
 * tools/yosys/share/yosys/ecp5/cells_bb.v and must stay identical to it:
 * nextpnr matches on cell type and parameter name, so a divergence here is
 * a cell that places wrongly rather than one that fails to build.
 *
 * Do not read cells_bb.v alongside this file. Two declarations of the same
 * module is the one way to make this worse.
 */

/* verilator lint_off DECLFILENAME */

(* blackbox *)
module DELAYF #(
    parameter DEL_MODE  = "USER_DEFINED",
    parameter DEL_VALUE = 0
) (
    input  A,
    input  LOADN,
    input  MOVE,
    input  DIRECTION,
    output Z,
    output CFLAG
);
endmodule

(* blackbox *)
module DELAYG #(
    parameter DEL_MODE  = "USER_DEFINED",
    parameter DEL_VALUE = 0
) (
    input  A,
    output Z
);
endmodule

(* blackbox *)
module IDDRX1F (
    input  D,
    input  SCLK,
    input  RST,
    output Q0,
    output Q1
);
endmodule

(* blackbox *)
module ODDRX1F (
    input  D0,
    input  D1,
    input  SCLK,
    input  RST,
    output Q
);
endmodule

/* verilator lint_on DECLFILENAME */
