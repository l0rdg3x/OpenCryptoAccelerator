# yosys patches

Local patches to the project's yosys build (`tools/src/yosys`). Apply
with `git apply` from that directory. `tools/` is not committed, so a
freshly built toolchain has none of these — `run_synth.py` probes for
the ones that matter and refuses to run without them.

## yosys-cmp2lut-signed-negative-constant.patch

Fixes a mis-compilation in `techlibs/common/cmp2lut.v` that silently
deletes this project's key store. Present in yosys 0.67+ (git
`41a4b5a03`) and still in upstream `main` as of 2026-08-04 — checked
against the raw file, which still reads `i_cst = operand;`. A search of
the yosys tracker for `cmp2lut` returns #771, #1201, #1202, #1484,
#1648, #3587 and #4028, none of them about signedness; that is a
search, not a proof that nothing was ever filed.

`techlibs/common/cmp2lut.v` is read at run time, so an already-built
yosys is fixed by copying the patched file over
`tools/yosys/share/yosys/cmp2lut.v` — no rebuild needed.

Background and blast radius: `../README.md`, "The cmp2lut trap".

### Draft report for YosysHQ/yosys — NOT SUBMITTED

Nothing has been posted upstream. This is a draft awaiting review.

---

**Title:** `cmp2lut` mis-maps signed comparisons against negative
constants

**Body:**

`techmap -map +/cmp2lut.v` produces a wrong truth table for any signed
`$lt`/`$le`/`$gt`/`$ge` cell whose constant operand is negative. The
result is silent: no warning, and the wrong constant propagates into
ordinary, correct optimisation, so the damage surfaces far from its
cause.

In `gen_lut`, the variable operand is sign-interpreted but the constant
operand is not:

```verilog
if (sign)
    i_var = n[width-1:0];
else
    i_var = n;
i_cst = operand;            // never sign-extended
```

`_TECHMAP_CONSTVAL_B_` carries the constant's raw unsigned value, so
for any constant with its MSB set the two sides are compared under
different interpretations.

**Reproducer** (two lines of Verilog-2005, using the methodology of
`tests/lut/check_map.ys`):

```verilog
module top(input [3:0] a, output y);
    assign y = $signed(a) >= $signed(4'sb1000);   // a >= -8 : always TRUE
endmodule
```

```
yosys -p "read_verilog tiny.v; prep -top top; \
          equiv_opt -assert techmap -D LUT_WIDTH=4 -map +/cmp2lut.v"
```

```
Found 1 $equiv cells in equiv:
  Of those cells 0 are proven and 1 are unproven.
ERROR: Found 1 unproven $equiv cells in 'equiv_status -assert'.
```

The cell maps to `$lut WIDTH=4 LUT=16'h0000` — constant false where the
correct function is constant true.

**Extent.** Sweeping every `$lt/$le/$gt/$ge` the pass accepts, both
signedness settings, constant on either side, all constant values, and
checking each generated LUT mask bit by bit against the comparison's
true value: **480 of 1920 cells wrong at `LUT_WIDTH=4`, 3024 of 12096
at `LUT_WIDTH=6`.** Every failure is a signed comparison against a
negative constant; unsigned cells and non-negative constants are
unaffected.

**Why CI does not see it.** `tests/lut/map_cmp.v` exercises signed
comparisons only against `{(LUT_WIDTH/2){2'sb01}}` (+5) and
`{LUT_WIDTH{1'sb0}}` (0), both non-negative.

**Impact.** `cmp2lut` runs in the `coarse` stage of `synth_lattice`
(ecp5, xo2, xo3, nexus), `synth_ice40`, `synth_intel`,
`synth_intel_alm`, `synth_gatemate`, `synth_nanoxplore`,
`synth_quicklogic`, `synth_xilinx`, `synth_microchip`,
`synth_fabulous`, `synth_analogdevices` and generic `synth -lut`. In
our design (an ECP5 crypto accelerator) a frontend lowered an unpacked
array's index bounds check into `$signed(idx) >= $signed(4'b1000)`, a
tautology. Mapped to constant false, it zeroed the array's write mask;
`opt_expr` then reduced the write mux to `D = Q` and `opt_dff`
correctly deleted 2048 bits of key storage. Simulation could not see
it — Verilator does not run yosys — and the build reported success.
Note `cmp2softlogic.v` declines this case outright
(`if (Y_WIDTH != 1 || A_SIGNED || B_SIGNED) wire _TECHMAP_FAIL_ = 1;`);
`cmp2lut` claims signed support and gets it wrong.

**Fix.** Sign-extend the constant with its own width, mirroring what is
already done for the variable operand (patch attached). With it the
reproducer is proven equivalent, all 14016 swept cells are correct at
both LUT widths, and `tests/lut/map_cmp.v` still passes at
`LUT_WIDTH` 4 and 6.

**Suggested regression** for `tests/lut/map_cmp.v`, which the current
file never covers:

```verilog
output o5_1 = $signed(a) >= $signed({1'b1, {(LUT_WIDTH-1){1'b0}}});
output o5_2 = $signed(a) <  $signed({1'b1, {(LUT_WIDTH-1){1'b0}}});
```

Found on yosys 0.67+ (`41a4b5a03`); `main` still carries the line.
