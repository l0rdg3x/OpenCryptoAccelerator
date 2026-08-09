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

```sh
openFPGALoader --detect
```

Read the IDCODE and confirm it is the part you think you have. There is
a documented case in this family of modules silkscreened as one revision
carrying a different die and package entirely: a 256-pin part where the
pinout assumes 381. If the IDCODE disagrees with `BOM-MVP.md`, stop:
every pin constraint downstream is wrong.

A bitstream that "loaded" but did not is the most expensive hour at the
bench.

## 2. Something trivial that blinks

Smallest possible design: the 25 MHz clock on **P3**, a counter, the LED
on **L2**. No PLL, no Ethernet.

This proves the clock is where we think, the bitstream path works end to
end, and the LED polarity (**active low**) is what we assumed. If it
does not blink, nothing after this is diagnosable.

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
