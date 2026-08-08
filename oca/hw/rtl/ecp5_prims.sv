// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * Blackbox declarations for the ECP5 hard primitives this design
 * instantiates: the four IO cells of the RGMII front end, and the PLL.
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

/*
 * A blackbox has no body, so every port is unread and every output
 * undriven and every parameter unused. Those four warnings are the
 * declaration doing its job, and they are waived here rather than
 * globally so that a real one elsewhere still stops the build. Without
 * this the ECP5 branch of oca_rgmii.sv has no lint gate at all: the
 * command AGENTS.md documents passes only because --top-module oca_core
 * never reaches these two files.
 */
/* verilator lint_off DECLFILENAME */
/* verilator lint_off UNUSEDSIGNAL */
/* verilator lint_off UNUSEDPARAM */
/* verilator lint_off UNDRIVEN */

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

/*
 * The PLL. Same reason as the four above — its 37 parameters have to be
 * in front of slang for oca_clkrst.sv to override any of them — plus one
 * of its own: nextpnr reads five of the analogue settings from cell
 * ATTRIBUTES rather than parameters (ecp5/bitstream.cc:1279-1286), and
 * defaults every one of them to zero. ICP_CURRENT=0 is a charge pump
 * with no current. So the instantiation carries those as attributes and
 * they are not, and must not become, parameters here.
 *
 * Only the ports and parameters exist below; the analogue attributes
 * belong to the instance.
 */
(* blackbox *)
module EHXPLLL #(
    parameter CLKI_DIV = 1,
    parameter CLKFB_DIV = 1,
    parameter CLKOP_DIV = 8,
    parameter CLKOS_DIV = 8,
    parameter CLKOS2_DIV = 8,
    parameter CLKOS3_DIV = 8,
    parameter CLKOP_ENABLE = "ENABLED",
    parameter CLKOS_ENABLE = "DISABLED",
    parameter CLKOS2_ENABLE = "DISABLED",
    parameter CLKOS3_ENABLE = "DISABLED",
    parameter CLKOP_CPHASE = 0,
    parameter CLKOS_CPHASE = 0,
    parameter CLKOS2_CPHASE = 0,
    parameter CLKOS3_CPHASE = 0,
    parameter CLKOP_FPHASE = 0,
    parameter CLKOS_FPHASE = 0,
    parameter CLKOS2_FPHASE = 0,
    parameter CLKOS3_FPHASE = 0,
    parameter FEEDBK_PATH = "CLKOP",
    parameter CLKOP_TRIM_POL = "RISING",
    parameter CLKOP_TRIM_DELAY = 0,
    parameter CLKOS_TRIM_POL = "RISING",
    parameter CLKOS_TRIM_DELAY = 0,
    parameter OUTDIVIDER_MUXA = "DIVA",
    parameter OUTDIVIDER_MUXB = "DIVB",
    parameter OUTDIVIDER_MUXC = "DIVC",
    parameter OUTDIVIDER_MUXD = "DIVD",
    parameter PLL_LOCK_MODE = 0,
    parameter PLL_LOCK_DELAY = 200,
    parameter STDBY_ENABLE = "DISABLED",
    parameter REFIN_RESET = "DISABLED",
    parameter SYNC_ENABLE = "DISABLED",
    parameter INT_LOCK_STICKY = "ENABLED",
    parameter DPHASE_SOURCE = "DISABLED",
    parameter PLLRST_ENA = "DISABLED",
    parameter INTFB_WAKE = "DISABLED"
) (
    input  CLKI,
    input  CLKFB,
    input  PHASESEL1,
    input  PHASESEL0,
    input  PHASEDIR,
    input  PHASESTEP,
    input  PHASELOADREG,
    input  STDBY,
    input  PLLWAKESYNC,
    input  RST,
    input  ENCLKOP,
    input  ENCLKOS,
    input  ENCLKOS2,
    input  ENCLKOS3,
    output CLKOP,
    output CLKOS,
    output CLKOS2,
    output CLKOS3,
    output LOCK,
    output INTLOCK,
    output REFCLK,
    output CLKINTFB
);
endmodule

/* verilator lint_on UNDRIVEN */
/* verilator lint_on UNUSEDPARAM */
/* verilator lint_on UNUSEDSIGNAL */
/* verilator lint_on DECLFILENAME */
