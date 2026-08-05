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
therefore delivers **0.561 Gbps at a 1500-byte MTU, 56% of line rate**,
and 1.121 Gbps across both ports.

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

**This decision is deferred to a measurement, not to taste.** Two
`oca_core` already occupy 52.9% of the LUTs, and this device stopped
routing entirely at 76.4% in the occupancy study — congestion, unmoved by
relaxing the clock from 100 MHz to 35. Whether two full Ethernet stacks
fit beside two cores is the question, and the published estimates for a
MAC-plus-stack swing by a factor of two. The project has already paid
once for multiplying instead of measuring, so `udp_complete_64` and the
MAC are being synthesised out-of-context on our own toolchain, and the
topology is chosen when that number exists.

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

The MAC's own clock domain crossing is upstream's, inside
`eth_mac_1g_rgmii_fifo`. **Ours is the boundary between that FIFO and the
UDP stack**, and the answer is the same module upstream uses:
`axis_async_fifo`. Nothing else in this design crosses a domain.

## 5. Where the width conversion goes, and why not where we said

AGENTS.md said the width converter belonged "between the MAC and
`oca_core`", in our clock domain. That is wrong, and the arithmetic says
so plainly: at 48 MHz an 8-bit stream carries **384 Mbps**, below the port
it is supposed to feed. The conversion has to happen on the 125 MHz side.

`eth_mac_1g_rgmii_fifo` with `AXIS_DATA_WIDTH = 64` does exactly that: it
instantiates `axis_async_fifo_adapter`, which upsizes before the FIFO in
the source domain and downsizes after it in the destination domain. So
receive converts 8→64 at 125 MHz, the asynchronous FIFO is already 64
bits wide, and our side never sees an 8-bit stream at all.

That configuration is not exercised by upstream's testbench, which pins
`AXIS_DATA_WIDTH = 8`, and no example uses it. **We would be the first,
so it needs a cocotb testbench of ours before it is trusted.**

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
by anyone we can find. All 203 pins referenced do exist in the CABGA381
package, checked against prjtrellis's IO database, and both RX clocks
land on clock-capable pins (`PCLKT6_0` and `PCLKT3_0`).

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
- **`eth_mac_1g_rgmii_fifo` at `AXIS_DATA_WIDTH=64`** gets its own
  testbench, because upstream has none for that configuration.
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

## 10. Still to be built before the board arrives

`openFPGALoader` is not in `tools/` — there is no programmer of any kind
in the tree. It has native support for the i9 (`colorlight-i9`, cable
`cmsisdap`), and it should be added to `scripts/build-toolchain.sh` with
the same pinning as everything else, well before 2026-08-17.
