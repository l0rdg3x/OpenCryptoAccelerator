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

**Measure the IO bank voltages.** This is the only item here that can
damage hardware rather than waste time, and it must happen before any
bitstream drives those pins.

Ethernet port 0 and the LED share **bank 6**; port 1 is entirely in
**bank 3**. Our two sources for this board contradict each other exactly
on the LED's standard (the vendor's `blink.lpf` says `LVCMOS25`, litex
says the RGMII pins are `LVCMOS33`), and nextpnr enforces one VCCIO per
bank across outputs, so the wrong combination is a fatal build error.

The corollary is worse and silent: **an input declared at the wrong
voltage produces no diagnostic at all**. RGMII receive pins declared
`LVCMOS33` in a bank actually at 2.5 V will synthesise, place, route and
program without a word.

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
failure this step exists to catch. Half of it is now excluded and half
of it is not, and no JTAG read closes the other half. What does is the
marking on the chip itself: read it and compare against `BOM-MVP.md`
before trusting a single LOCATE.

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

This proves the clock is where we think and that the bitstream path works
end to end, and it settles the LED's polarity, which no source has ever
measured. **Read the duty cycle, not the blink**: the counter is one
eighth on and seven eighths off, so under the active-low assumption you
see a short flash every 1.34 s. A long lit period
with a short gap is the same bitstream on an active-high LED. That
asymmetry is the only way this step can settle polarity, since a square
wave looks identical either way round.

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

Add `oca_clkrst`'s two outputs, 125 MHz and 48 MHz, and divide one down
to something visible. Proves the PLL locks from a 25 MHz input on the
dedicated `LLC_GPLL0T_IN` pin before any logic depends on it. There is
no 90-degree copy to build: this step asked for one until 2026-08-09,
and `oca_rgmii.sv` makes the transmit clock from an `ODDRX1F` fed by a
constant edge pair instead.

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
