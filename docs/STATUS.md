<!-- SPDX-License-Identifier: MIT -->
# Project status

**Updated 2026-08-16.** Where the project is, in one page. Update this at
every merge and at every design gate; a tracker that drifts is worse than
none.

This is deliberately short. `docs/RECORD.md` carries the measurements,
their limits and the history of how each was arrived at; `AGENTS.md`
carries the rules, the layout and the build and test commands, and is the
document to read before touching anything. This one answers only: what is
done, what is being worked on, what is next.

## Done

**Phase 1 — software reference.** The C API over OpenSSL, 126/126 checks,
113 of them driven by official vectors.

**Phase 2 — RTL.** ChaCha20, Poly1305 and the AEAD engine, verified
against RFC 8439. The host protocol — key store, packet buffers,
protocol FSM — behind a 64-bit AXI-Stream pair. **185 passing
executions over 25 cocotb runners, six of them on a synthesised
netlist**, every runner in the tree run on 2026-08-16: no failures, and
every runner exits 0. That figure is the
simulator's alone. Outside it, and outside every count this page gave
before 2026-08-13: **69 tests in `hw/host/`, 4 in
`hw/syn/test_run_synth.py`, 4 in `hw/sim/test_proto_model.py`** and the
126 known-answer checks of Phase 1 above. They are not one unit and are
not summed here. Nothing in any of them has run on hardware. No suite
needs a vendored tree to build any more. `verilator
--lint-only -Wall` is clean. **One test skips, by design**: the
fabric's fail-closed check needs RTL whose two cores can disagree, so
it runs only against a source copy that makes them — `AGENTS.md` gives
the invocation. On that copy it passes and the clean-broadcast test
cannot, the two being mutually exclusive by construction.

Earlier counts — 222 executions before the Ethernet removal, 207 in a
fresh clone, and two different 177s that never measured the same thing
— belong to earlier trees and populations. `docs/RECORD.md` carries
that counting history, each figure with its date and what it counted;
today's figure is the 185 above, over 186 executions.

**The open ECP5 toolchain**, built locally in `tools/`: yosys with the
slang frontend, nextpnr-ecp5, prjtrellis, Verilator, openFPGALoader.

**Bring-up on silicon**, six readings taken on the Colorlight i9 v7.2
that arrived 2026-08-11. The ladder in `.claude/skills/bringup` numbers
three steps; steps 4 and 5 were Ethernet and are retired.

| rung | result |
|---|---|
| IDCODE over the DAPLink | `0x41112043`, LFE5U-45 — settles the die, not the package |
| package | read off the chip: `6BG381C`, caBGA381, speed grade 6 |
| LED and oscillator | D2 gives the short flash, so the LED is active low; P3 is clocking |
| bank 6 VCCIO | 3.28 V measured on a driven pad, so `LVCMOS33` is right |
| PLL | locked, 1 Hz off a `clk_tx` counter — a frequency, not a lock flag |
| serial console | `PIN=J17` returned at 115200 8N1; the diagnostic console answers |

**The crypto on the FPGA, built and packed.** `oca_uart_crypto` is the
AEAD core behind SLIP framing on the board's DAPLink serial line;
`feat/crypto-console` delivered it as a board top at 25 MHz with no PLL,
and `feat/crypto-pll` put `oca_clkrst` in front of it. **The board top
is `oca_crypto_pll`**: the datapath runs on `clk_sys` at 48.0769 MHz
instead of the 25 MHz pin and the heartbeat stays behind on that
reference. The pins went with it, so `colorlight_i9_crypto.lpf`
constrains that top and no other.

**The engine costs 36 cycles per 64-byte block, measured, and the
board can answer with its own cycle counts** (2026-08-16, commit
`4f879ee`). The one-cycle `p_blk` handshake bubble is gone — the
wrapper's side made combinational, one side only, `poly1305.sv`
untouched — taking a seal's marginal block from 40 to 36 cycles and
the packet intercept from 71 to 70: 214/358/502/646 cycles for
4/8/12/16-block seals, measured differentially by the new
`run_aead_cycles` suite, both of whose checks fail by mutation. Opcode
05 is an on-chip benchmark — N re-feeds of one 64-byte block behind
the same fail-closed key check as seal, answering a 32-bit duration
(engine-take to `eng_done`, intercept 66) and a 64-bit timestamp — and
in simulation its counter agrees with the testbench's instrument on
the marginal 36.00. At the PLL's 48.0769 MHz the cycle budget is **934
cycles per 1500-byte MTU packet (0.618 Gbps per engine) and 106 per
64-byte packet (0.232)**, against 1031 / 0.560 and 111 / 0.222 before.

**Two cuts to the critical path, and neither costs a cycle**
(2026-08-16, `a529470` and `a307d87`). The first: the request's two
lengths are latched from the argument word that carries them, two
cycles before `P_ARGS` decides, so the 18-bit adder, the length
compares and the first-block comparators no longer resolve on the
deciding edge — `+65 flip-flops` per core. The second: Poly1305's
multiplier B operand is chosen one cycle ahead, for the row that will
be multiplied next, taking a five-way mux off the path into the DSP
column — **+134 flip-flops** per core for the operand register, and no
state added to the schedule: the board top went 12589 → 12788
flip-flops for the two cuts together. `run_aead_cycles` measures the same
36 cycles at the margin and the same intercept of 66 after both, and
three mutations show the suites can see the cuts. What they bought is
in `docs/RECORD.md`: on the fabric, the difference between failing its
constraint on every seed measured and clearing it on three of four; on
the board top, **nothing four seeds can see**.

**And it closes 48.0769 MHz**, re-measured 2026-08-16 on that commit
over four placer seeds on yosys `f77ddfb87`, LFE5U-45F CABGA381 speed
6:

| `oca_crypto_pll`, four seeds, 2026-08-16 | |
|---|---|
| LUT | 13381 — 30.5% of the device |
| flip-flops | 12589 — 28.7% |
| block RAM | 6 of 108 |
| multipliers | 20 of 72 |
| PLL, clock buffers, IO | 1 EHXPLLL of 4, 2 DCCA, 4 TRELLIS_IO |
| `clk_sys` | 49.19 / 51.21 / 52.99 / 51.55 MHz — mean 51.23, spread 7.7% |
| against the 48.0769 the PLL delivers | all four clear it; the tightest by 2.31% |

Area is identical across the seeds, as synthesis requires; the clock is
the one with the least margin over its own constraint, not the fastest
in the report. Against the 2026-08-15 netlist (13062 / 12529) this is
**+319 LUTs and +60 flip-flops**: the bubble removed ~131 flip-flops,
the bench counter added 192 plus its muxes, and the finer decomposition
was not chased. The bitstream sizes on record (427699 / 426793 bytes)
belong to that earlier netlist and its seeds. **The PLL and the
diagnostics cost 32 LUTs and 11 flip-flops**, measured between the two
netlists of 2026-08-14/15: the pre-PLL `oca_uart_crypto`, seed 1,
2026-08-14, same toolchain, measured 13030 LUTs, 12518 flip-flops, the
same 6 block RAMs
and 20 multipliers, 49.85 MHz against a 25.00 MHz constraint, and a
423971-byte bitstream — so the datapath clock went from 25 to 48.0769
MHz, a factor 1.923. **No target in the tree reproduces that pre-PLL
row today**: `oca_uart_crypto` lost both its pins and its heartbeat
counter, and a design with no `.lpf` gets neither a timing check nor a
packed bitstream, so what it reports now is area alone. `build/` holds
one pass, since a rebuild overwrites the last, so re-run rather than
trusting any table. `docs/RECORD.md` carries every measurement in full.

**Two engines behind one serial line, built and measured — and not
shipped** (2026-08-16). `oca_dispatch` routes each request to a core on
the opcode in its first beat and holds the route to `tlast`,
broadcasting only load-key so both private key stores hold every slot;
`oca_collect` merges the two answers fail-closed, with a sticky
`trouble` on divergence or a full expectation queue. It simulates
clean. On silicon terms it does not qualify: four placer seeds give
**50.28 / 50.66 / 46.23 / 51.15 MHz** against the same 48.0769, so one
seed misses by 3.84% and by this project's rule the constraint is not
closed. The spread, 10.6%, is the widest recorded here — a device at
60% occupancy. The fabric is kept as measured, not shipped, exactly as
the 50.00 MHz rung is.

## Not established

**No bitstream containing crypto has been loaded onto the board.** What
is proved is that it builds, that it closes 48.0769 MHz in place and
route, and that it answers the protocol correctly in simulation down to
UART bit timing — including a forged tag returning no plaintext, proved
by mutation rather than asserted. That clock is a report about a
netlist, not a board that answered.

**The margin changed character, and it is now small.** The pre-PLL top
closed 49.85 MHz against a 25.00 MHz requirement, a 99% margin that
could not fail; this one asks for 48.0769 and the worst of four seeds
gives 2.31% on `4f879ee` and **2.04%** after the two cuts. Any RTL
change has to be re-synthesised before it is
believed, and a change that looks harmless may not close.

**Four seed spreads exist for this design's tops and none orders
another.** A four-seed sweep of `oca_crypto_pll` before the reset and
heartbeat corrections spread 8.1%; the 2026-08-15 netlist spread 6.9%;
`4f879ee` spreads 7.7%; the netlist with both cuts spreads 8.7%. No
ordering is claimed:
four-seed draws cannot rank netlists, which is what
`.claude/skills/synth-sweep` says in as many words. Each is recorded
against the netlist it belongs to and the comparison is refused — and
that refusal has teeth: seed 1 alone reads 49.19 against 51.06 across
the cuts, which looks like +3.8% and is noise, the four-seed means
being 51.23 and 51.30.

**The 50.00 MHz rung was asked a second time and answered the same.**
On the netlist with both cuts, four seeds give 52.58 / 51.59 / 50.58 /
49.26 — three close, the fourth misses by 1.47%, where the first
asking missed by 0.78%. The board top stays at 48.0769.

**No place-and-route option helped the fabric.**
`--placer-heap-timingweight 35` made it worse (44.15 against 46.40);
`--tmg-ripup`, `router2 --tmg-ripup` and the two together each ran past
the design's four-hour bound without finishing. The gain came from RTL,
and this avenue is closed with evidence rather than opinion.

**Banks 2 and 7 are unmeasured.** J17 and H18 live in bank 2. The console
has been talking through them at 115200 since 2026-08-11, so this is not
a blocker; the `oca_vccio` method applies if anything faster ever lands
there.

**The PLL's 1 Hz was never timed with a stopwatch**, so what is
established is lock and the absence of a gross error, not 125.0 MHz to
three digits.

**Throughput over the serial line is about 11.5 KB/s.** That is a figure
about the transport and never about the accelerator, which is aimed at
gigabit. Any rate measured over this link must say which of the two it
describes.

## Next

1. **Load `oca_crypto_pll` onto the board and run the vectors through
   it** with `oca/hw/host/cli.py selftest`. This is the step the whole
   project exists to reach: the crypto answering on silicon.
   `oca_uart_crypto` is not the target to load — it lost the pins to
   this top and packs no bitstream any more. With the board answering,
   read opcode 05's durations (`cli.py bench`) — the first performance
   figure this project can take on silicon, expected 36 N + 66 cycles.

   **Watch D2 for a few seconds before the host opens the port.** The
   fast heartbeat latches on any malformed UART frame, and the edge a
   host puts on the line when it opens `/dev/ttyACM0` is enough to
   produce one. Fast before any traffic is line noise or a PLL that has
   not locked, which is a seventh cause of the rate and the only one
   that is not sticky; fast only after traffic is the reading the rate
   is for. `oca_crypto_pll.sv`'s header has the full table.
2. Time the PLL with a stopwatch, and measure bank 2 while the meter is
   out.
3. **Decide what the two-engine fabric is for.** It closes on three
   seeds of four at 48.0769 and is therefore not shippable there. The
   next rung down is 625/14 = 44.643 MHz, where the worst seed measured
   would sit 3.5% inside — two engines at that clock carry 1.86 times
   one engine at 48.0769, and the sweep that would settle it has not
   been run. The alternative is to keep cutting: both tops now bind on
   the same carry chain inside the AEAD, **18.76 to 21.63 ns** over the
   eight seeds of the two tops. Its logic sits between 7.06 and 7.71 ns
   whichever seed it is, so the ~2.9 ns of spread is mostly routing —
   but not entirely, and not on every pair: between the fabric's seeds
   1 and 4 the whole difference is logic, and routing moves the other
   way by 0.02 ns.

## Closed

**Ethernet, 2026-08-12.** No RJ45 socket on this kit, and the die is an
LFE5U with no SERDES, so this part can never be the PCIe platform either.
`SPEC.md` PHASE 2 records both reasons.

**And the code is gone, deleted the same day.** The condition set for
removing it was that the console carry an equivalent end-to-end test,
and `run_uart_crypto` does: a request shifted in bit by bit over the
real UART and the response recovered the same way. What went: the RTL
(`oca_rgmii.sv`, `oca_udp_seam.sv`, `oca_top.sv`, `oca_top_mac.sv`,
`oca_top_stub.sv`, and under `oca/hw/rtl/vendor/` the three
parameter-fixing wrappers — `oca_eth_axis_64.v`,
`oca_eth_mac_1g_fifo_64.v`, `oca_udp_complete_64.v` — plus the two
synthesis probes `oca_eth_mac_1g_fifo_64_probe.sv` and
`oca_udp_complete_64_probe.sv`, which were not wrappers but
frontend-compatibility checks that also report area — **not** the
apparatus the 8422-LUT figure was measured with, which is a nextpnr
out-of-context run and is not reproduced by any script in this tree;
see `docs/design/2026-08-12-ethernet-measurement-provenance.md`), the four
runner/testbench pairs, the whole
`oca/hw/vendor/` tree — the `verilog-ethernet` submodule, its patches
and `vendor_patches.py` — the three `oca_top*` synthesis targets with
their netlist census tables, and the four DDR and delay primitives from
`ecp5_prims.sv`, which keeps `EHXPLLL`. **The measurements are kept**,
in `docs/RECORD.md` and `oca/hw/syn/README.md`; the code they were taken
on is not.

**Three pieces of it survive on purpose, and this is where they are
declared.**

- **`oca_clkrst.sv` keeps its B50612D PHY reset sequencer**, and
  `test_clkrst.py` keeps the tests that hold it to the datasheet's
  Table 86 minimums and their order. `oca_clkrst` sits inside `oca_pll`,
  a design already measured on silicon at bring-up step 3, and reopening
  it would put a validated design at risk for no functional gain.
  **Declared with it: the file's comments are now full of dangling
  references, and they stay.** Its header describes a wiring that exists
  nowhere in the tree. `:36` and `:196` name `oca_rgmii.sv`; `:89-90`
  name `oca_top`; `:106-107` name `verilog-ethernet`, `eth_mac_1g_fifo`
  and `udp_complete_64`; `:209-214` is a WIRING block routing clocks to
  `udp_complete_64`, `eth_mac_1g_fifo` and `oca_rgmii`; `:245` reads
  "The same three inverted, for verilog-ethernet" above ports that are
  still live. This is a known, bounded limitation, recorded here rather
  than fixed: correcting the comments means editing a
  silicon-validated design, which is the one thing this bullet exists to
  avoid.
- **`oca/hw/syn/colorlight_i9.lpf` is kept verbatim**, its fifteen-pin
  RGMII/PHY pinout included — twelve `rgmii_*` plus `phy_mdc`,
  `phy_mdio` and `phy_rst_n`; the file has seventeen `LOCATE`s in all
  and calls them "these seventeen pins" without separating out `clk25`
  and `led_n` — because 270 of its 327 lines are ECP5
  analysis that surviving code cites: `run_synth.py` cites it in three
  places, all seven of the other `.lpf` files take their balls and
  IO_TYPEs from it, and `.claude/skills/bringup/SKILL.md` rests on its
  bank-consistency argument. **No surviving design uses it as a
  constraint file** — every pinned design carries its own, from two pins
  for `oca_blink` to ten for `oca_vccio`.
- **The submodule's object store under `.git/modules` was deliberately
  not purged.** Upstream is public and still serves the pin (`77320a94`)
  as the head of master, so this is insurance rather than rescue: the
  commit is literally its "Add deprecation notice", the author has moved
  to `taxi` and the repository has not advanced since 2025-02-27. 6.3 MB
  is the price of not depending on a deprecated third-party repository
  staying up, and every published Ethernet figure was measured on those
  sources.
