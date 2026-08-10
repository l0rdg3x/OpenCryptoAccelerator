# Ethernet integration — design

The other half that `2026-08-03-host-protocol.md` section 7 declined to
design: everything between the RGMII pins and `oca_core`'s AXI-Stream
pair. Written 2026-08-05, twelve days before the board is expected, so
every claim here is either measured on the toolchain, quoted from a
source, or marked as an unknown that only the bench can close.

## 1. What upstream gives us, and what it does not

The MAC, ARP, IP and UDP come from `verilog-ethernet` (Alex Forencich,
MIT). That decision stands. What does not stand is the sentence AGENTS.md
carried until today — that the project "has working ECP5 support".
Checked against the repository: all 25 directories under `example/`
target Xilinx or Intel, a code search for `ecp5`, `lattice`,
`colorlight` and `trellis` returns nothing, and `rgmii_phy_if.v` accepts
only `SIM`, `GENERIC`, `XILINX` and `ALTERA`, falling through to
`GENERIC` on anything else without a warning.

`GENERIC` is not a slower path on this device, it is a broken one.
`oddr.v` drives one register from two `always` blocks on opposite edges;
`synth_ecp5` answers with conflicting drivers on every bit rather than
inferring `ODDRX1F`. `iddr.v` does elaborate, into fabric flip-flops on
both edges instead of `IDDRX1F` — plausible in simulation, not a way to
capture DDR at 125 MHz.

**So the RGMII front end is ours to write**, behind the wrapper SPEC.md's
portability rule requires for vendor primitives. It is the piece nearest
the pins, the piece with the least margin for error, and the piece we
have no upstream reference for on this device.

Two further facts, recorded so they are not rediscovered. The repository
is deprecated by its author in favour of `taxi` and has not moved since
2025-02-27; taxi is CERN-OHL-S 2.0 strongly reciprocal or commercial,
which would extend reciprocity across our whole design, so MIT
verilog-ethernet stays the choice and stops receiving fixes. And the
stack has a 64-bit variant — `udp_complete_64` and the `_64` modules
under it — which changes where the width conversion belongs (section 4).

## 2. Topology: what the two cores are actually wired to

`oca_dual.sv` exposes two independent AXI-Stream pairs, one per core,
sharing nothing — deliberately, because a shared key store would put one
port's keys within reach of the other port's traffic. One core per port
is therefore a cycle budget of **0.569 Gbps at a 1500-byte MTU, 56.9%
of line rate**, and 1.138 Gbps across both ports, on the committed
pair's 48.89 MHz mean. **A budget and not a rate**: 48.89 MHz is an
out-of-context Fmax, and `oca_clkrst` delivers `clk_sys` at 48.0769 MHz,
so a pinned build of this topology would run at 0.560 Gbps per port.
Two ports are out in any case, though on routability rather than on the
area sum alone:
94.5% of the device, by the table further down this section.

Saturating a single port needs both cores behind it, and that is not a
wiring change. It needs a distributor, a collector, and an answer to the
per-core key store: a slot is loaded into one core and only that core can
use it, so packets cannot be spread round-robin. The options are to
replicate every `load_key` to both cores — which produces two responses
for one request, since `oca_proto` answers every packet that carries a
readable header — to route statically by slot number, which makes load
balancing a function of which slots the host happens to use, or to lift
the key store out of `oca_core` and share it with arbitrated writes and
per-engine read ports. It also needs response reordering: two
independently paced cores complete out of order, and the current protocol
promises arrival order, tested in six places.

**The measurement was taken rather than argued, and it settles it.** One
complete port costs **8422 LUTs, 19.2% of the device**: `udp_complete_64`
7147, `eth_mac_1g_rgmii_fifo` at 64 bits 1214, ~61 for the RGMII front
end. Against two cores at 24602 (56.1%):

| configuration | LUTs | of device |
|---|---|---|
| two cores, two ports | 41446 | 94.5% |
| two cores, one port | 33024 | 75.3% |
| one core, one port | 20730 | 47.3% — **built: 17802, 40.6%** |

Two ports are out. Two cores behind one port land at 75.3%, against the
76.4% at which this device stopped routing in the occupancy study. The
last row has since been built as `oca_top` and came in **2928 LUTs
under its sum**, because adding a core measured alone to a port
measured alone counts twice the logic the optimiser shares; the two
rows above it are sums of the same kind and neither has been built.

**Two modules are missing from that figure, and from this document until
2026-08-09.** `oca_eth_mac_1g_fifo_64` hands out a raw AXI-Stream with
the Ethernet header still in the data; `udp_complete_64` expects the
header already parsed, on `s_eth_hdr_valid` and friends. What sits
between them upstream is `eth_axis_rx.v` and `eth_axis_tx.v`, which
`udp_complete_64` does not instantiate — it contains `ip_complete_64`
and `udp_64` and nothing else — and which upstream's own examples read
alongside the stack. Neither appears in the 8422 LUTs, in the file lists
of either probe, or anywhere in this document's plan.

They are also parametric, `DATA_WIDTH` defaulting to 8 with
`KEEP_ENABLE` and `KEEP_WIDTH` derived from it, so they fall on the same
side of the `read_verilog` / `read_slang` boundary that forced the two
wrappers we already have. **A third wrapper is needed**, fixing them at
64 bits, before any top level can elaborate. Found by an adversarial
review of a top level that had not been written yet, which is the
cheapest place this could have been found.

Turning off the UDP checksum generator recovers less than hoped:
`UDP_CHECKSUM_GEN_ENABLE=0` gives 6419 LUTs against 7147, three fewer
block RAMs — but 288 TRELLIS_RAMW appear, which is LUT fabric spent as
distributed RAM, so the net saving is smaller than the column suggests,
and Fmax falls from 81 to 72.9 MHz. One port becomes 7694 LUTs: two ports
91.2%, two cores and one port 73.7%. Better, decisive for neither.

**Three cores are now further out of reach than the occupancy study
found.** With the secret zeroisation they are ~36900 LUTs, 84.2%, and
nextpnr fails at *placement* — "unable to find legal placement for all
cells" — rather than exhausting itself in the router. Note what that does
not prove: it does not test `router2`, nextpnr's congestion-driven
router, because the run never reached routing. Whether router2 clears the
kind of congestion that stopped router1 is still unmeasured, and the
place to measure it is the two-core-plus-port build at 73.7%, once it
exists.

**So the MVP that fits today is one core on one port**, 0.581 Gbps at
MTU and 47.3% occupancy — with the rest of the device free for the
second core if a distributor is ever built, or for the second port if
the stack shrinks.

Both of those numbers were constructions, not a build. The 47.3% adds
two separately measured areas, and the 0.581 comes from the single
core's 49.91 MHz mean, measured on the core placed alone and
out of context: no MAC beside it, no IO, no PLL.

**Amended 2026-08-10: that netlist has been placed, and the two
constructions were wrong in opposite directions.** `oca_top` is one
core plus one port against `colorlight_i9.lpf`, with real IO and the
PLL.

**Area was pessimistic by 2928 LUTs**: **17802, 40.6%** rather than the
47.3% predicted, because the sum counts twice the logic the optimiser
shares.

**The clock was optimistic, and by more than a seed.** 0.581 Gbps came
from 49.91 MHz, and this design cannot run at 49.91 MHz: `oca_clkrst`
delivers `clk_sys` at 625/13 = **48.0769 MHz**, which seed 6 closes with
49.41 MHz of Fmax, 2.8% of margin. Through the same cycle model the
design delivers **0.560 Gbps at MTU**, 56.0% of line rate. An Fmax says
whether a clock closes; it is not a clock the design can be given.

**And the ladder above it is coarse, with the next rung unmeasured.**
`clk_tx` is an integer division of the same VCO, so the VCO must be a
multiple of 125 MHz; the 400-800 MHz band leaves 500, 625 and 750, and
from those `clk_sys` can be 45.45, 46.88, **48.08**, 50.00 or 52.08 —
nothing between 48.08 and 50.00. **Whether this design closes 50.00 has
not been built.** Every clock in the seed sweep was measured against a
48.08 MHz constraint, and at the one seed that closes both 125 MHz
clocks `clk_sys` reached 49.41; that makes 50.00 look tight and proves
nothing. The device carries four PLLs and this design uses one, which is
the other unopened door.

What the build also showed is that area was never the binding
constraint here: the design closes its two 125 MHz clocks on one placer
seed of thirteen.

## 3. The RGMII front end

One wrapper module per port, instantiating ECP5 primitives directly and
presenting the GMII-style interface `eth_mac_1g` expects. Vendor
primitives are permitted here and only here, per SPEC.md; everything
behind the wrapper stays inferred.

- **Receive**: `IDDRX1F` on RXD[3:0] and RX_CTL, clocked by RXC.
- **Transmit**: `ODDRX1F` on TXD[3:0] and TX_CTL, plus TXC.
- **The RX delay**: `DELAYG` with `DEL_MODE="SCLK_ALIGNED"` and an
  explicit `DEL_VALUE`.

RGMII requires a phase relationship between clock and data that neither
end provides by default. What the field does, in LiteEth's
`ecp5rgmii.py` and in the Colorlight projects built on it, is 2 ns on
receive and 0 on transmit, entirely on the FPGA side, with no PHY-side
strapping over MDIO. LiteEth computes taps as `delay / 25e-12`, so 2 ns
is 80 taps.

**Three things about that number are not established and must not be
presented as if they were.** The 25 ps per tap is LiteEth's empirical
constant, not a figure Lattice publishes. `DEL_VALUE` is a 7-bit field:
128 wraps silently to 0, with no error and no warning. And our own
timing database cannot check any of it — prjtrellis's `speed_6/cells.json`
contains no characterisation of `DELAYG` or `DELAYF` at all, so nextpnr's
timing report says nothing useful about this path. **The delay is a bench
parameter.** The design must therefore make it easy to change: a
parameter on the wrapper, not a constant buried in an instantiation, and
a documented procedure for sweeping it when the board arrives.

## 4. Clocks

The board has a 25 MHz oscillator on **P3**, which is `LLC_GPLL0T_IN` — a
dedicated PLL input, confirmed against prjtrellis's own IO database for
the CABGA381 package.

`ecppll -i 25 -o 125 --clkout1 125 --phase1 90` gives `CLKI_DIV=1`,
`CLKFB_DIV=5`, `CLKOP_DIV=5` with a 625 MHz VCO, comfortably inside the
400–800 MHz range, and the 90-degree copy as `CLKOS_DIV=5`,
`CLKOS_CPHASE=3`, `CLKOS_FPHASE=2`. The part has four PLLs; we need one.

Note that `ecppll` uses a minimum phase-frequency-detector rate of
3.125 MHz while the datasheet and LiteX both say 10 MHz. Our case is safe
— 25 MHz with `CLKI_DIV=1` is a 25 MHz PFD — but the tool will not stop
us building something out of specification if the input is ever divided.

Three domains, and the design must name them because a signal crossing
one of these boundaries without a synchroniser is the failure that
appears once every few hours on a bench and never in simulation:

| domain | rate | what lives there |
|---|---|---|
| `clk_rx` | 125 MHz, recovered from the PHY's RXC | RGMII receive, MAC receive |
| `clk_tx` | 125 MHz from the PLL, plus a 90° copy | RGMII transmit, MAC transmit |
| `clk_sys` | ~48 MHz | UDP stack, `oca_core`, everything else |

The MAC's own clock domain crossing is upstream's, inside the MAC FIFO
wrapper, and **it is the only one this design needs.**

> **Corrected 2026-08-08, after reading the wrapper rather than its
> description.** This paragraph read: "Ours is the boundary between that
> FIFO and the UDP stack, and the answer is the same module upstream
> uses: `axis_async_fifo`." That would have built a same-clock
> asynchronous FIFO — block RAM and two synchroniser latencies for
> nothing.
>
> The wrapper contains exactly two async FIFOs: a `tx_fifo` crossing
> `logic_clk` to `tx_clk`, and an `rx_fifo` crossing `rx_clk` to
> `logic_clk`. Its user-side AXI-Stream is **already on `logic_clk`**,
> and its status outputs are already brought into that domain by toggle
> synchronisers. So if `logic_clk` is `clk_sys` and `udp_complete_64`
> runs on `clk_sys` too, there is no boundary left between the MAC and
> the UDP stack. The crossing happens once, inside the wrapper, between
> the 125 MHz wire and our slower fabric.

## 5. Where the width conversion goes, and why not where we said

AGENTS.md said the width converter belonged "between the MAC and
`oca_core`", in our clock domain. That is wrong, and the arithmetic says
so plainly: at 48 MHz an 8-bit stream carries **384 Mbps**, below the port
it is supposed to feed. The conversion has to happen on the 125 MHz side.

The MAC FIFO wrapper with `AXIS_DATA_WIDTH = 64` does exactly that: it
instantiates `axis_async_fifo_adapter`, which upsizes before the FIFO in
the source domain and downsizes after it in the destination domain. So
receive converts 8→64 at 125 MHz, the asynchronous FIFO is already 64
bits wide, and our side never sees an 8-bit stream at all.

That configuration is not exercised by upstream's testbench, which pins
`AXIS_DATA_WIDTH = 8`, and no example uses it. **We would be the first,
so it needs a cocotb testbench of ours before it is trusted.**

> **Corrected 2026-08-08: the wrapper is `eth_mac_1g_fifo`, not
> `eth_mac_1g_rgmii_fifo`.** This section, section 10 and `AGENTS.md`
> all named the latter. It **embeds `rgmii_phy_if`**, so it cannot
> accept an RGMII front end of ours without editing a pinned vendor
> tree — and `rgmii_phy_if` is precisely the module that has no ECP5
> target, which is why we wrote `oca_rgmii.sv` at all.
>
> `eth_mac_1g_fifo` is the same wrapper one layer down: it takes GMII
> plus `rx_clk` and `tx_clk`, and carries the same two
> `axis_async_fifo_adapter` instances, so the 8↔64 conversion and both
> clock crossings are still upstream's. The area measurement is
> unaffected — it counted the RGMII layer separately in any case.

## 6. Traps carried forward from the reconnaissance

Recorded here because each one is cheap to design around now and
expensive to find on a bench.

- **An unconsumed RX header blocks the next packet.** `udp_ip_rx_64.v`
  holds `s_ip_hdr_ready` low while `m_udp_hdr_valid` is high. The header
  must always be accepted, even for a packet we intend to discard.
- **Transmit order is fixed**: header handshake first, payload after. The
  payload's `tready` does not rise until the header is taken.
- **Length and checksum are computed for us** when
  `UDP_CHECKSUM_GEN_ENABLE=1`, which means we may present a header before
  knowing the response length — at the cost of store-and-forward through
  a 2048-byte FIFO. Our responses are MTU-bounded, so this is affordable,
  but it is block RAM that must appear in the budget.
- **ARP timers are in clock cycles with 125 MHz defaults.**
  `ARP_REQUEST_RETRY_INTERVAL` and `ARP_REQUEST_TIMEOUT` must be rescaled
  for a 48 MHz `clk_sys` or every timeout is 2.6× longer than intended.
- **The RX FIFO drops silently.** `RX_FRAME_FIFO`,
  `RX_DROP_OVERSIZE_FRAME`, `RX_DROP_BAD_FRAME` and `RX_DROP_WHEN_FULL`
  all default on, and the MAC receive path has no back pressure. If our
  side stalls, frames vanish. The status outputs — `rx_fifo_overflow`,
  `rx_fifo_bad_frame`, `rx_error_bad_fcs` — must reach counters the host
  can read, not be left unconnected as the examples leave them. A drop
  the operator cannot see reads as success.
- **FIFO `DEPTH` is in bytes, not words**, when `KEEP_ENABLE` is set.
- **`axis_adapter` leaves stale data outside `tkeep`.** `oca_core` masks
  by `tkeep` already; this is a reason not to relax that.
- **The two PHYs share one MDIO bus and one reset line.** Neither can be
  reset or addressed independently.

## 7. Pins and IO banks

The i9 has no platform file of its own: it is a `revision="7.2"` variant
inside litex-boards' `colorlight_i5.py`, which deep-copies the 7.0 IO map
and changes only the LED. Everything else — clock, RGMII, MDIO, reset —
is inherited from the i5 and has not been re-verified against i9 silicon
by anyone we can find. Every pin that file references — the i9 IO map
and the PMOD connector tables both — does exist in the CABGA381 package,
checked against prjtrellis's IO database, and both RX clocks land on
clock-capable pins (`PCLKT6_0` and `PCLKT3_0`). This sentence read "all
203 pins referenced" until 2026-08-06: 203 is how many IO the package
itself carries in that database, not how many the platform file names.

**Port 0 is entirely in bank 6; port 1 entirely in bank 3.** And bank 6
also carries the LED. nextpnr enforces one VCCIO per bank across outputs,
so declaring the LED `LVCMOS25` (as the vendor's `blink.lpf` does) beside
RGMII outputs at `LVCMOS33` (as litex does) is a fatal error — reproduced:
`ERROR: incompatible IO voltages 3V3 and 2V5 on bank 6`. The two sources
we have contradict each other precisely on the pin that decides the
supply of the bank feeding Ethernet port 0.

Worse, the corollary is silent: an **input** declared at the wrong
voltage produces no warning at all. RGMII receive inputs declared
`LVCMOS33` in a bank actually at 2.5 V would pass synthesis and place and
route without a word.

**This must be measured on the board before the first bitstream that
drives those pins.** It is the one item on this list that could damage
hardware rather than merely fail to work.

Also unresolved: litex's `eth0`/`eth1` numbering is inverted with respect
to the connector silkscreen, by its own comment, because of the PHY
address straps. The pins agree; only the label is reversed. And the
"UART" at J17/H18 is two pins of the `pmodj` connector, not a documented
UART — the vendor's own tracker carries an open issue about exactly that
discrepancy on the i9.

## 8. Verification without a board

Everything below the RGMII pins can be simulated, and must be, because
the board arrives once and debugging starts then.

- **The RGMII wrapper** gets a testbench that drives DDR receive traffic
  and checks the recovered GMII stream, and one that checks the transmit
  side produces the expected DDR pattern. The `DELAYG` value cannot be
  validated this way — it has no simulation model worth trusting and no
  timing characterisation — so what the testbench proves is the data
  path, not the timing. Say so in the test, or someone will read a green
  run as a validated delay.
- **`eth_mac_1g_fifo` at `AXIS_DATA_WIDTH=64`** gets its own testbench,
  because upstream has none for that configuration. (This item said
  `eth_mac_1g_rgmii_fifo` until 2026-08-10; the correction box in
  section 5 rules that module out — it embeds `rgmii_phy_if`, which has
  no ECP5 target — and named section 10 and `AGENTS.md` but missed this
  one. The testbench that was written, `run_eth_mac.py`, drives
  `oca_eth_mac_1g_fifo_64`.)
- **The whole path**, from a synthetic Ethernet frame carrying a UDP
  packet with an OCA command, through the stack, through `oca_core`, and
  back out as a frame — driven by `proto_model.py`, which stays the
  definition of the wire format and is not modified.
- **The netlist checks extend to the new logic.** A green Verilator run
  says nothing about synthesis; that is the lesson the `cmp2lut` defect
  cost us 2313 flip-flops to learn. Whatever storage the integration adds
  gets a floor in `run_synth.py`.

## 9. What only the bench can close

Listed so that nobody mistakes the design for a validated one.

1. **The RGMII delay value.** Empirical, uncheckable by our STA, and the
   most likely cause of a link that comes up and passes no traffic.
2. **The IO bank voltages**, which decide whether the pin declarations
   above are right or quietly wrong.
3. **The B50612D's default configuration.** Its datasheet is only
   available as a scanned PDF we could not extract text from, so whether
   the PHY applies an internal delay by default is unknown. If it does,
   ours must not.
4. **Whether the i9 inherits the i5 pinout faithfully**, given that the
   platform file assumes it and nobody has verified it on i9 silicon.
5. **The JTAG pin identities**, and whether the flash arrives write
   protected.

None of these is a reason to delay the design. All of them are reasons
to build the delay as a parameter, the pin map as a file, and the bring-up
as a sequence that checks one thing at a time.

## 10. Built since, before the board arrives

`openFPGALoader` **is in the tree**: pinned at `85be4fa0` (v1.1.1) in
`scripts/build-toolchain.sh` like everything else, built into
`tools/openFPGALoader/`. It has native support for the i9
(`colorlight-i9`, cable `cmsisdap`). It now has something to load, too:
`run_synth.py` packs `oca_top.bit` and prints the command.

This section previously read "there is no programmer of any kind in the
tree", which was true when it was written on 2026-08-05 and stopped
being true with `4df1201`.

## 11. Amended 2026-08-08, from the toolchain rather than from memory

Everything below was checked against the installed yosys, nextpnr and
prjtrellis, or against a named line of a source file, before `oca_rgmii.sv`
was written. Two of the items would have gone into the RTL as written.

**Primitives that do not exist on this device.** `DELAYE` and `BUFG` are
not ECP5 cells. There is no `BUFG` to instantiate: the global buffer is
`DCCA` and nextpnr inserts it for any net with `IOLOGIC.CLK` or
`TRELLIS_FF.CLK` users.

**`IDDRX1F` has no `ECLK` port.** Its entire port list is `(D, SCLK, RST,
Q0, Q1)`. `ECLK` belongs to the x2 gearing primitives, so the phrase "the
RX clock delay and its ECLK routing" in `AGENTS.md` described a design we
are not building. **x2 gearing is rejected**, and the reason is recorded
so it is not rediscovered: the MAC wants 8-bit GMII at 125 MHz, so a
4-bit-per-pin-per-cycle stream would have to be re-serialised for nothing,
while `ECLKSYNCB`/`CLKDIVF` add two edge clocks per bank as a hard
resource and an `ALIGNWD` word-alignment problem with no obvious answer
here.

**The static timing analysis is blind to the whole interface, not just to
the delay elements.** Section 8 understates this. nextpnr never queries
the trellis IOLOGIC timing at all: it hardcodes setup 0.1 ns, hold 0 ns
and clock-to-Q 0.5 ns for IOLOGIC and SIOLOGIC, and `TRELLIS_IO` has no
delay model. The real characterisation is sitting unused in the chipdb —
`IDDRX1F` setup 401 ps and hold 235 ps, `ODDRX1F` clock-to-output about
985 ps. **A green timing report on the receive or transmit clock says
nothing whatsoever about RGMII capture or launch.** It covers fabric paths
only.

**The nibble order is settled, from source.** litex's `DDROutput(i1, i2)`
becomes `ODDRX1F(D0=i1, D1=i2)` and `DDRInput(o1, o2)` becomes
`IDDRX1F(Q0=o1, Q1=o2)`; LiteEth passes the low nibble as `i1`/`o1`. So
`D0`/`Q0` carry GMII bits 3:0 on the rising edge and a recovered byte is
`{Q1, Q0}`. Note also that RGMII has **four** data lines plus control, not
eight — eight is GMII.

**The receive delay is `DELAYF`, and it sits on the data lines.** `DELAYF`
is the same element as `DELAYG` plus `LOADN`/`MOVE`/`DIRECTION`/`CFLAG`,
and nextpnr moves all four onto the IOLOGIC and sets
`IOLOGIC.LOADNMUX=LOADN` when `LOADN` is connected. The tap count is
therefore movable while the design runs, which turns a bench search that
would otherwise need one bitstream per candidate into a sweep. It is on
the data and not the clock because a delay cell on `RXC` moves the clock
net onto the IOLOGIC's `INDD` output, after which nextpnr re-runs its
dedicated-routing test from there; failing it puts a 125 MHz clock on
general fabric routing.

**Corrected 2026-08-09: the two are not interchangeable.** This paragraph
read "delaying data by +2 ns and delaying the clock by 2 ns are congruent
at a 4 ns unit interval, so nothing is lost". Delaying data by +D is
delaying the clock by −D, and what decides which nibble reaches `Q0` is
the 8 ns clock period rather than the 4 ns UI: the two choices sit 4 ns
apart, exactly the `Q0`-to-`Q1` spacing, so they centre the sample in an
eye equally well and disagree about which nibble is the rising one. The
data side is right by precedent, not by geometry: LiteEth delays
`rx_ctl` and `rx_data[0..3]`, leaves the receive clock alone, decodes
in-band status from the rising captures as we do, and runs at 1 Gbps on
this board family. If that precedent does not carry, no tap value repairs
it — 0..127 taps span 3.175 ns, under one UI, and in the wrong direction
— and the fixes are inverting the `SCLK` into the `IDDRX1F` or moving the
delay onto `RXC`. The discriminator at the bench is `link_up`: with a
one-UI error it decodes the falling in-band nibble, which RGMII holds at
0000 during an inter-frame gap, so **`link_up` low while the PHY's own
link LED is on means nibble alignment, not tap count**, and the sweep can
be skipped.

**A PLL cannot cleanly take the recovered clock.** The dedicated PLL
reference inputs on CABGA381 are A4/A5, A6/B6, A19/B20, C18/D17, P3/P4 and
U16/T17. Neither RX clock pin (H2, L19) is among them, so phase-shifting
`RXC` with a PLL would drive `EHXPLLL.CLKI` from general routing for no
benefit a delay element does not already give.

**The transmit clock comes from LiteEth's arrangement, not upstream's.**
An `ODDRX1F` fed from a constant rising/falling pair, clocked by the same
transmit clock as the data, then delayed. One PLL output instead of two,
and no `clk`-to-`clk90` handoff that nextpnr never analysed.

**A falsifiable prediction about the PHY, answering open question 3.**
litex-boards drives this board family with `tx_delay = 0` and
`rx_delay = 2 ns` and that is reported working. For the FPGA-to-PHY
direction this is only possible if the B50612D adds an internal delay on
its own receive side, and symmetrically that it adds none on its transmit
side. **Predicted bench outcome: with zero transmit taps the transmit
direction works.** If instead transmit is dead while receive is fine, the
prediction is wrong and the transmit taps are what to move. Stating it
this way means the first hour at the bench resolves it rather than
confusing it with a receive problem.

**Two constraint facts that change how the `.lpf` must be read.** `BANK`
and `BANK_VCC` in an `IOBUF` statement are parsed, stored and then never
read again by nextpnr — there is **no way to declare a bank's VCCIO** from
the `.lpf`, for any bank but bank 8. And a `LOCATE COMP` or `IOBUF PORT`
naming a cell that does not exist is skipped **with no message at all**;
only the separate unconstrained-IO check catches the consequence.

**The bank 6 collision is created by the i9 revision itself.** On the i5
the LED is U16, in bank 3. The v7.2 deep-copy moves it to L2, which is
bank 6 — the same bank as Ethernet port 0. And because nextpnr's bank
consistency check runs only over outputs and referenced types, the seven
receive inputs in that bank neither vote nor are checked. **The blink test
of bring-up step 2 therefore cannot detect a wrong bank declaration**: it
already fixes bank 6's VCCIO from the LED, and the LED lights either way.

**A toolchain limitation that shapes every future vendor integration.** A
module that reaches `read_slang` by way of `read_verilog` arrives already
elaborated, so a parameter override from SystemVerilog fails — including
for yosys's own `DELAYF`, whose parameters `cells_bb.v` does declare. Two
ways out: declare the blackbox ourselves so the parameters are in front of
the frontend that elaborates our RTL (`oca/hw/rtl/ecp5_prims.sv` does this
for the four IO primitives), or wrap the vendor module in a thin Verilog
wrapper that fixes its parameters and instantiate the wrapper. Both were
verified end to end by synthesising to an ECP5 netlist.
