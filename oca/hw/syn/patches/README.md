# yosys patches

Local patches to the project's yosys build (`tools/src/yosys`).
`scripts/build-toolchain.sh` applies them as it fetches, and `git apply`
from that directory does it by hand. `tools/` is not committed, so a
toolchain built any other way has none of these — both that script and
`run_synth.py` probe for the ones that matter and refuse to run without
them.

## yosys-cmp2lut-signed-negative-constant.patch

Fixes a mis-compilation in `techlibs/common/cmp2lut.v` that silently
deletes this project's key store. Present in yosys 0.67+ (git
`41a4b5a03`) and still in upstream `main` at `7a2bd64c` on 2026-08-05,
whose blob for this file is byte-identical to ours. The file has not
been touched since 2021.

Not a duplicate, checked rather than assumed: the seven `cmp2lut` hits
in the tracker (#771, #1201, #1202, #1484, #1648, #3587, #4028) were
read individually and none concerns signedness — #1201/#1202 are the
LUT mask width, #1484 drops `$eq`/`$ne`, #1648 is `cmp2lcu.v`, #3587 is
a CLI error with this pass only in its log, #4028 adds a `synth_lattice`
option without touching `gen_lut`. Comparing the file's blob hash across
all 137 branches and 103 open pull requests finds four that differ, all
carrying the same pre-2021 revision with the same defective line. Two
near misses are not this: #6063 is `alumacc` folding a signed compare
with an unsigned subtract, #3187 is an unsigned Nexus/ccu2 compare.

`techlibs/common/cmp2lut.v` is read at run time, so an already-built
yosys is fixed by copying the patched file over
`tools/yosys/share/yosys/cmp2lut.v` — no rebuild needed.

Background and blast radius: `../README.md`, "The cmp2lut trap".

### Reported upstream: YosysHQ/yosys#6085

Filed 2026-08-05, after re-checking that the defect is still in `main`
and that nothing equivalent was already open. The text below is what was
posted, kept here so the repository carries its own record of it. If it
needs correcting, the correction goes in a new comment on the issue —
this file is not the public record and editing it changes nothing there.

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

**Why CI does not see it — the signed branch is dead code.**
`tests/lut/map_cmp.v` writes its signed cases as
`{(LUT_WIDTH/2){2'sb01}}` (+5) and `{LUT_WIDTH{1'sb0}}` (0), both
non-negative. But the constants are the smaller half of it: a
concatenation is unsigned (IEEE 1364 §5.5.1), so `{...} <= $signed(a)`
degrades the whole comparison to unsigned. Running that file through
`prep; simplemap` and reading the RTLIL, **all 16 `$lt/$le/$gt/$ge`
cells carry `A_SIGNED 0` and `B_SIGNED 0`**, at LUT_WIDTH 4 and 6
alike. `gen_lut` takes `sign = A_SIGNED && B_SIGNED`, so its `if (sign)`
branch is never executed by the suite at all — which is how this
survived since the pass landed in 2019.

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

**More cases**, mapped against the pristine upstream file:

| expression | correct mask | produced |
|---|---|---|
| `a >= -8` (tautology) | `16'hFFFF` | `16'h0000` |
| `a < -8` (impossible) | `16'h0000` | `16'hFFFF` |
| `a >= -1` | `16'h80FF` | `16'h00FE` |
| `a < -4` | `16'h0F00` | `16'hFF0F` |

`wreduce` narrows the constant first, so the sign bit sits at
`B_WIDTH-1` and not at `A_WIDTH-1`: `-1` arrives as `B_WIDTH=1` value
`1'b1`, `-4` as `B_WIDTH=3` value `3'b100`, and `_TECHMAP_CONSTVAL_B_`
hands over the raw unsigned 1, 4 and 8.

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
