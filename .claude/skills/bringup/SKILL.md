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

## Before power

**Measure bank 6's VCCIO before the RGMII pins are ever driven.**
Measured on this board 2026-08-11 and it is **3.3 V** (3.28 V read on a
driven pad), so `LVCMOS33` throughout `colorlight_i9.lpf` is right and
what follows in this section is why it mattered, not an open item.

What it is not is a damage risk, and this paragraph said it was until
2026-08-11. An output pad is powered from its own bank rail and cannot
be made to exceed it by a line of text; what a wrong rail costs is a
build error in one direction and receiver margin in the other. Both are
below, and the second is the one worth the meter.

Ethernet port 0 and the LED share **bank 6**; port 1 is entirely in
**bank 3**. Our two sources for this board contradict each other exactly
on the LED's standard (the vendor's `blink.lpf` says `LVCMOS25`, litex
says the RGMII pins are `LVCMOS33`), and nextpnr enforces one VCCIO per
bank across outputs, so the wrong combination is a fatal build error.

The corollary is worse and silent: **an input declared at the wrong
voltage produces no diagnostic at all**. RGMII receive pins declared
`LVCMOS33` in a bank actually at 2.5 V will synthesise, place, route and
program without a word. That is the case the measurement ruled out.

**How it was measured, since the rail is a plane inside the module and
no capacitor on it is identifiable from anything we hold.** An LVCMOS
output driven high sits at its own bank's VCCIO, and a ten megohm meter
loads it with well under a microamp, so a driven open pad reads the rail
directly. `oca_vccio` drives eight free bank 6 balls, toggling in step
with D2 so that a swinging reading beside a visible blinking LED is the
signature that finds them: nothing undriven swings on cue. Two surfaced
on the carrier's P4, pins 8 and 25, both 3.28 V high and 0 V low.

Repeat it on any board that is not this one. It is one bitstream and two
readings.

## 1. Transport, before anything else

Do not load a design. Establish that the programmer talks to the part.
Name the cable: `--detect` alone probes every cable it knows, and this
board's is the DAPLink on the carrier (`0d28:0204`), which is also what
the `colorlight-i9` board profile selects.

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
reporting whether `--mcufw` was given, and it shipped in the v1.1.1
release commit (`85be4fa`). It says nothing about the board.

A bitstream that "loaded" but did not is the most expensive hour at the
bench.

## 2. Something trivial that blinks

`oca_blink.sv`: the 25 MHz clock on **P3**, a counter, the LED on **L2**,
its own two-pin `colorlight_i9_blink.lpf`. No PLL, no Ethernet.

Build it from `oca/`, which is the only directory `run_synth.py`
resolves against, and load it from the repository root, which is where
`tools/` is. Two commands, two working directories:

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

**Do not use `oca_top_stub` for this.** Its `led_n` is the AND of seven
terms: `beat[24]`, `pll_locked`, `phy_ready`, `link_up`,
`link_full_duplex`, `link_speed != 3` and `dly_cflag_seen`. The last one
is a sticky bit fed by the receive delays' CFLAG, and the stub ties their
MOVE low, so no move ever commands a pulse and whether CFLAG rests high
is characterised in neither yosys, prjtrellis nor nextpnr.

The consequence is not which way it reads, it is that **one reading
covers two states**: `led_n` sits high and steady when the PLL never
locked, and can sit high and steady on a board where everything worked.
The comment in that file claimed the reading distinguished them, until
2026-08-11.

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
2026-08-09, and `oca_rgmii.sv` makes the transmit clock from an
`ODDRX1F` fed by a constant edge pair instead.

## 4. RGMII, and the delay sweep

This is the step with an unknown in it, and the unknown is a number.

**Our static timing analysis cannot check this path.** prjtrellis's
timing database contains no characterisation of `DELAYG` or `DELAYF` at
all, so nextpnr's report says nothing useful about it. The value is
empirical and must be found at the bench.

Start where the field is: **RX 2 ns (80 taps at LiteEth's 25 ps per
tap), TX 0**. Then sweep the RX delay and find the window where the link
is stable, rather than accepting the first value that brings the link
up. A link that works at exactly one tap is a link that will fail on a
warm day.

Two traps: `DEL_VALUE` is a 7-bit field, so **128 wraps silently to 0**;
and the 25 ps per tap is LiteEth's empirical constant, not a Lattice
figure, so the tap count that corresponds to 2 ns may not be 80.

Also unverified: whether the B50612D applies an internal delay by
default. Its datasheet is a scanned image. If the PHY delays and we
delay too, the link will not come up and nothing in the FPGA will say
why.

**Before sweeping, decide whether a sweep can help at all.** Our delay
is on the data lines and not on the clock, which is LiteEth's
arrangement and is right by their precedent rather than by geometry: the
two choices sit one unit interval apart, so they disagree about which
nibble the `IDDRX1F` captures on the rising edge. If that alignment is
wrong, no tap value repairs it. 0 to 127 taps span about 3.2 ns, under
one 4 ns UI, and in the wrong direction.

The tell is `link_up`. With a one-UI error it decodes the falling
in-band nibble, which RGMII holds at 0000 between frames. **`link_up`
reading 0 while the PHY's own link LED is lit means nibble alignment,
not tap count**, and the fix is to invert the `SCLK` into the `IDDRX1F`
or move the delay onto `RXC`, not to keep sweeping.

## 5. Traffic

Only now: a frame in, a frame out, at the MAC level before the UDP
stack. Then the stack. Then a real OCA command through `proto_model.py`.

**Wire the drop counters to something visible before running traffic**,
not after. The MAC's receive FIFO defaults to dropping oversized frames,
bad frames and frames arriving when full, and the receive path has no
back pressure. If our side stalls, frames vanish with no indication.
Reading `rx_fifo_overflow`, `rx_fifo_bad_frame` and `rx_error_bad_fcs`
is the difference between "it does not work" and knowing why.

## Recording what you find

Every number found at the bench goes back into
`docs/design/2026-08-05-ethernet-integration.md` section 9, which lists
these as open. A value discovered and not written down will be
rediscovered.
