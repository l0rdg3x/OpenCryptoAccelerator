---
name: bringup
description: Use when the board is on the bench for the first time, or when a bitstream that should work does not. Gives the order to bring up the Colorlight i9 board, and what must be verified before each step.
---

# Board bring-up

Written before the board existed, while the reasoning was fresh, because
the alternative is reconstructing it with the device on the table and a
bitstream that does not work.

**One thing at a time, and each step proves itself before the next.** A
step that "seems fine" and is not becomes a symptom three steps later,
where it looks like something else entirely.

## Bank 6, and why it still matters

**Bank 6 is at 3.3 V**, 3.28 V read on a driven pad on 2026-08-11, so
`LVCMOS33` throughout `colorlight_i9.lpf` is right. The step that made
this urgent is retired with the Ethernet route, so what is left here is
a measured fact and a method, not a gate on anything.

Keep it for two reasons. Bank 6 carries the LED and the 25 MHz clock,
which every design here still uses, so the rail is not academic. And the
method generalises: it is how to read ANY bank's VCCIO on a board whose
regulators you cannot identify, and banks 2 and 7 -- where the console's
UART lives -- are still unmeasured.

This section used to be called "Before power" and used to be first,
which stopped describing it once the measurement became a bitstream:
what follows needs the board powered and an FPGA configured.

What it was never is a damage risk, and this paragraph said it was until
2026-08-11. An output pad is powered from its own bank rail and cannot
be made to exceed it by a line of text; what a wrong rail costs is a
build error in one direction and receiver margin in the other. Both are
below, and the second is the one worth the meter.

**Why it mattered, kept because the shape recurs.** Bank 6 carried the
LED and, on the retired route, Ethernet port 0. Two sources contradicted
each other on the LED's standard (the vendor's `blink.lpf` says
`LVCMOS25`, litex says `LVCMOS33`), and nextpnr enforces one VCCIO per
bank across outputs, so the wrong pair is a fatal build error.

The corollary is worse and silent, and it is the part worth carrying to
any future bank: **an input declared at the wrong voltage produces no
diagnostic at all**. It will synthesise, place, route and program
without a word, and show up as a link that comes up and drops packets --
or, on a slow interface, as nothing at all until the day it matters.

**How it was measured, since the rail is a plane inside the module and
no capacitor on it is identifiable from anything we hold.** An LVCMOS
output driven high sits at its own bank's VCCIO, and a ten megohm meter
loads it with well under a microamp, so a driven open pad reads the rail
directly.

```sh
(cd oca && .venv/bin/python hw/syn/run_synth.py oca_vccio)
tools/openFPGALoader/bin/openFPGALoader -b colorlight-i9 -m \
    oca/hw/syn/build/oca_vccio.bit
```

That drives eight free bank 6 balls, toggling in step with D2 so a
swinging reading beside a visible blinking LED is the signature that
finds the hole: nothing undriven swings on cue. Black probe on the
USB-C shell, red probe walking the header holes.

Two surfaced on **connector P4** (the carrier's, not ball P4, which is
the PHY reset), at holes 8 and 25, both 3.28 V high and 0 V low. Those
two are **F1 and K4**: of the eight probes, `colorlight_i5.py` puts only
those on P4, the other six being on P6.

Repeat it on any board that is not this one.

## 1. Transport, before anything else

Do not load a design. Establish that the programmer talks to the part.
Name the cable: with neither `-c` nor `-b`, openFPGALoader does not probe
anything, it warns and hard-defaults to `ft2232` (`src/main.cpp:231`),
which is not on this carrier. This board's is the DAPLink (`0d28:0204`),
which is also what the `colorlight-i9` board profile selects
(`src/board.hpp:144`), so `-b colorlight-i9` would serve as well.

```sh
tools/openFPGALoader/bin/openFPGALoader --detect -c cmsisdap
```

Run on 2026-08-11 this read `idcode 0x41112043`, `lattice ECP5
LFE5U-45`, `irlength 8`, agreeing with all three tables in the tree:
`prjtrellis/database/devices.json`,
`prjtrellis/docs/architecture/bitstream_format.rst:164` and
`openFPGALoader/src/part.hpp:389`.

**An IDCODE settles the die and says nothing about the package.**
prjtrellis lists six packages against that one code, `caBGA256` among
them, and a 256-ball part carrying a 381-ball pinout is the documented
failure this step exists to catch. No JTAG read closes that half. The
marking on the chip does, and on 2026-08-11 it reads:

    LATTICE
    LFE5U-45F
    6BG381C
    A3421RL1

`6BG381C` is speed grade 6, caBGA381, commercial temperature, which is
what `run_synth.py` targets by default (`--package CABGA381 --speed 6`).
The alternative that mattered would have marked `6BG256C`, differing in
all three digits at once, so a misreading cannot produce this string.
Die and package both agree with `BOM-MVP.md` and every LOCATE rests on
the right ball map. Read the marking again on any board that is not this
one: nothing in the flow rereads it for you.

Expect a bare `empty` on the first line of output. It is an
unconditional debug `printf` in openFPGALoader's own `src/main.cpp:1205`
reporting whether `--mcufw` was given, and it says nothing about the
board. When it was introduced is not something this tree can answer:
`tools/src/openFPGALoader` is a depth-1 shallow clone, so `git log -S`
there attributes the whole file to the one commit present. This said it
shipped in the v1.1.1 release commit until 2026-08-11, which was that
artefact and not a finding.

A bitstream that "loaded" but did not is the most expensive hour at the
bench.

## 2. Something trivial that blinks

`oca_blink.sv`: the 25 MHz clock on **P3**, a counter, the LED on **L2**,
its own two-pin `colorlight_i9_blink.lpf`. No PLL, no Ethernet.

Build it from `oca/` and load it from the repository root. Not because
`run_synth.py` cares: it derives every path from `Path(__file__)` and has
no working-directory dependency at all. It is the two relative paths in
the commands themselves, `.venv/bin/python` and `tools/`, that live in
different places. Two commands, two working directories:

```sh
(cd oca && .venv/bin/python hw/syn/run_synth.py oca_blink)
tools/openFPGALoader/bin/openFPGALoader -b colorlight-i9 \
    oca/hw/syn/build/oca_blink.bit
```

`-m --write-sram` is openFPGALoader's default, so that command loads
volatile and leaves the board's flash untouched. Nothing here should
touch flash.

This proves the clock is where we think, that the bitstream path works
end to end, and the LED's polarity. **Read the duty cycle, not the
blink**: the counter is one eighth on and seven eighths off, so an
active-low LED shows a short flash every 1.34 s and an active-high one
shows a long lit period with a short gap. That asymmetry is the only way
this step can settle polarity, since a square wave looks identical either
way round.

Run here 2026-08-11: D2 gave the short flash, so the LED is active low
and litex's `user_led_n` is right. A steady LED is the other reading
worth knowing, because a frozen counter leaves `led_n` low: no clock on
P3 shows as steadily lit, not as nothing at all.

If it does not blink, nothing after this is diagnosable.

**Drive the indicator from one counter and nothing else**, which is what
`oca_blink` does. The rule was paid for by `oca_top_stub`, deleted with
the Ethernet route on 2026-08-12: its `led_n` was the AND of seven
terms, `beat[24]`, `pll_locked`, `phy_ready`, `link_up`,
`link_full_duplex`, `link_speed != 3` and `dly_cflag_seen`. The last was
a sticky bit fed by
the receive delays' CFLAG, and the stub tied their MOVE low, so no move
ever commanded a pulse and whether CFLAG rests high is characterised in
neither yosys, prjtrellis nor nextpnr.

The consequence was not which way it read, it is that **one reading
covers two states**: `led_n` sat high and steady when the PLL never
locked, and could sit high and steady on a board where everything
worked. The comment in that file claimed the reading distinguished them,
until 2026-08-11. Any future bring-up indicator gated on more than one
term inherits the same defect.

## 3. The PLL

`oca_pll.sv` and its two-pin `colorlight_i9_pll.lpf`. Proves the PLL
locks from the 25 MHz input on the dedicated `LLC_GPLL0T_IN` pin before
any logic depends on it.

```sh
(cd oca && .venv/bin/python hw/syn/run_synth.py oca_pll)
tools/openFPGALoader/bin/openFPGALoader -b colorlight-i9 -m \
    oca/hw/syn/build/oca_pll.bit
```

**Do not build the indicator on `LOCK`.** `EHXPLLL` raises it when the
loop closed, not when it closed on the frequency you asked for, so a
lock LED reports a PLL multiplying by four exactly as it reports one
multiplying by five. D2 is driven instead from a counter on `clk_tx`
that counts 62,500,000, halving 125 MHz on a decimal boundary:

| D2 | meaning |
|----|---------|
| 1 Hz, symmetric | locked, and `clk_tx` is 125 MHz |
| ~3 Hz flicker | live and clocked, PLL **not** locked |
| static | no bitstream or no `clk25`, which step 2 separates |

The flicker is counted on `clk25` and carries no reset on purpose:
`rst_n_sys` is gated on `pll_locked`, so a reset-driven fallback would
be dead in the one case it exists to report.

**Then time it.** Start a stopwatch on one rising edge and count thirty:
thirty seconds. That is what turns "about 1 Hz" into 125 MHz to a
fraction of a percent, and it retro-tightens step 2, which fixed the
oscillator only to the precision of a blink counted by eye. A 24 MHz
crystal drifts this 2.4 s in a minute.

Run here 2026-08-11: 1 Hz observed, stopwatch not run.

`clk_sys` needs no separate measurement. It is the same VCO over
`CLKOS_DIV`, and `run_synth.py` checks all four dividers in the netlist,
so a measured `CLKOP` makes 625/13 = 48.0769 MHz a conclusion. That
check reaches `check_pll` only through `NETLIST_PRIM_COUNT`; a top
missing from that table used to skip both in silence.

There is no 90-degree copy to build: this step asked for one until
2026-08-09, and `oca_rgmii.sv` made the transmit clock from an
`ODDRX1F` fed by a constant edge pair instead. That file was deleted on
2026-08-12 with the route, and `ecp5_prims.sv` kept only `EHXPLLL`,
which is the one primitive this step needs.

## 4 and 5, which were RGMII and traffic: RETIRED 2026-08-12

**Do not do these. There is nothing to plug a cable into.** The i9 v7.2
carries both B50612D PHYs on the module and routes their MDI pairs to
the SO-DIMM edge, but the RJ45 sockets and the magnetics live on a
carrier no kit sold with this module includes. And the die is an LFE5U,
with no SERDES, so this part can never be the PCIe platform either: it
is a vehicle for proving the core on silicon, not a prototype of the
product.

The host interface is the board's USB serial, which `SPEC.md` PHASE 2
allowed from the start. It is on **J17 (tx) and H18 (rx)**, 115200 8N1,
appearing as `/dev/ttyACM0` once `cdc_acm` is loaded. `oca_uart_console`
is the design that uses it.

What those two steps used to say is not lost: the delay-sweep reasoning,
the DELAYF characterisation gap, the nibble-alignment tell and the drop
counters are all in `docs/design/2026-08-05-ethernet-integration.md`,
which is marked closed and kept as history. Read it if the route ever
reopens on different hardware; do not work from it here.

**The code behind those steps was deleted on 2026-08-12**: the RGMII
front end, the seam, the three `oca_top*` designs, their runners and the
vendored `verilog-ethernet` tree. Two pieces stayed on purpose and this
skill depends on both. `oca/hw/syn/colorlight_i9.lpf` is kept verbatim,
its fifteen RGMII and PHY pins included (twelve `rgmii_*` plus
`phy_mdc`, `phy_mdio` and `phy_rst_n`, out of the seventeen `LOCATE`s
the file carries), because 270 of its 327 lines are
the ECP5 analysis the bank section above rests on and because
`run_synth.py` and the other `.lpf` files cite it; no surviving design
builds against it. And `oca_clkrst.sv` keeps its B50612D reset
sequencer, tested against the datasheet's Table 86, because it sits
inside `oca_pll`, which step 3 above loads onto the board.

## Recording what you find

Every number found at the bench goes into `docs/RECORD.md`, beside the
step that produced it, with what it does NOT establish stated next to
it. A value discovered and not written down will be rediscovered; a
value written down without its limits will be trusted further than it
earns.

It used to say these went to section 9 of the Ethernet design document.
That document is closed, and nothing new belongs in it. Until the
measurement record was split out, it said `AGENTS.md`; that file keeps
the rules and the how-to and takes no bench numbers.
