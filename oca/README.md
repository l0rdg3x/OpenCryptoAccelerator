# OpenCrypto Accelerator (OCA)

## Phase 2: FPGA cores (in progress)

- `hw/rtl/chacha20.sv` — ChaCha20 stream cipher core (RFC 8439):
  one 64-byte block per `start` pulse, little-endian bus layout
  documented in the header. The 20 rounds alternate a column round and a
  diagonal round, but only one round datapath is built: a diagonal round
  is a column round on a row-rotated state, and rotating by a constant
  is wiring, so the state register alternates between the plain and the
  diagonalised frame instead of a 512-bit multiplexer choosing between
  two sets of adders. `ROUNDS_PER_CYCLE` chooses how many rounds a cycle
  covers, trading combinational path against cycle count. At the default
  1 a block costs 22 cycles; 2 restores the original 12 cycles and its
  longer path. Only those two values are implemented, and any other one
  fails elaboration rather than emitting a core that runs too few
  rounds.
- `hw/rtl/poly1305.sv` — Poly1305 one-time authenticator (RFC 8439):
  `start` loads the one-time key, then 16-byte blocks via `blk`/`last`.
  26-bit limb datapath — the accumulator and r are held as five 26-bit
  digits and the mod 2^130-5 reduction is folded into the accumulation,
  so no stage carries a 130x130 multiply. A block costs
  4 + ceil(5/`ROWS_PER_CYCLE`) cycles and uses 5 x `ROWS_PER_CYCLE`
  multiply operators: at the default `ROWS_PER_CYCLE = 1` that is
  9 cycles and 5 multiplies (20 ECP5 MULT18X18D), rising to 5 cycles
  and 25 multiplies at `ROWS_PER_CYCLE = 5`. Latency is independent of
  the data. r and s must be single-use (see header).
- `hw/rtl/chacha20_poly1305.sv` — AEAD_CHACHA20_POLY1305 engine
  (RFC 8439 2.8) combining the two cores: derives the Poly1305 key
  internally (ChaCha20 block, counter 0), encrypts with counter 1+,
  MACs aad‖pad‖ct‖pad‖le64(|aad|)‖le64(|ct|). Streaming interface:
  64-byte input blocks (AAD then plaintext), ciphertext blocks out,
  then the tag. `dec=1` decrypts: MACs the input (ciphertext) blocks,
  caller compares the tag. `in_len` above 64 is illegal and raises the
  sticky `err` output, which abandons the message rather than letting a
  malformed length stall the engine. The bytes past `in_len` of a partial
  block are ignored: they are zeroed on the 16-byte sub-block on its way
  into Poly1305, which is the only place the padding is ever read, rather
  than on the 512-bit buses feeding it.
- `hw/rtl/oca_keystore.sv` — key slots for the host protocol, `NUM_SLOTS`
  of them (default 8). The only place key material is retained by
  design: loading one also passes it through the receive buffer and
  `oca_proto`'s argument registers, which the secret zeroisation
  clears. Each slot
  carries a loaded bit, so reading a slot that was never written reports
  `rd_valid = 0` instead of handing back a key of zeros: a host mistake
  becomes a protocol error rather than a message encrypted under a key
  an attacker can guess. Keys and loaded bits are cleared on reset.
- `hw/rtl/oca_pktbuf.sv` — a two-bank 64-bit packet buffer (`BYTES` =
  2048 per bank, 256 words each), one bank filled from the stream while
  the other is read back, written sequentially with a 1..8 byte count
  and read at random word offsets. Writes past capacity are dropped and
  `wr_full` is raised, so a truncated packet becomes a length error rather
  than a silent wrap. The read port is registered and single, and the
  range check sits on the address rather than the read data, which is what
  makes the memory infer block RAM in pseudo dual-port mode — confirmed in
  synthesis, two DP16KD per buffer at 36 bits wide and no LUT RAM.
- `hw/rtl/oca_proto.sv` — the protocol FSM: parses the fixed 8-byte
  header, validates it, drives the engine and builds the response. Knows
  nothing of cryptography beyond driving `chacha20_poly1305` and
  comparing its tag — a single 128-bit equality, constant-time by
  construction. Store and forward, so a failed tag returns status `06`
  and no plaintext at all.
- `hw/rtl/oca_core.sv` — wiring only: the two packet buffers, the key
  store, the protocol FSM and the AEAD engine behind a pair of 64-bit
  AXI-Stream ports with `tkeep`. It knows nothing of any transport, and
  any width conversion belongs outside it. That boundary is why retiring
  the Ethernet route on 2026-08-12 cost nothing in this list.
- `hw/sim/chacha20_model.py` — ChaCha20 reference model (plain integer
  arithmetic from RFC 8439 2.3), the oracle for the randomised tests.
- `hw/sim/test_chacha20.py` — cocotb testbench; vectors parsed from the
  same `tests/vectors/sources/rfc8439.txt` as the software tests
  (2.3.2 block function, 2.4.2 two-block encryption, decrypt
  round-trip), the model checked against those vectors before it is
  trusted, then 100 randomised blocks with the counter randomised over
  its full 32 bits.
- `hw/sim/poly1305_model.py` — Poly1305 reference model (plain integer
  arithmetic from RFC 8439 2.5.1) and the RFC vector parser shared with
  the testbench.
- `hw/sim/test_poly1305.py` — the model checked against the official
  vectors before it is trusted, then 2.5.2 (partial final block) and all
  four A.3 MAC vectors (zero key, r=0, s=0, general case) against the
  RTL, digit-boundary and all-ff edge cases, and 200 randomised messages
  judged by the model.
- `hw/sim/test_chacha20_poly1305.py` — 2.8.2 encryption, A.5 decryption,
  2.8.2 decrypt round-trip, the AEAD model checked against both official
  vectors before it is trusted, then 40 randomised encryptions and 40
  randomised decryptions judged by it, over AAD and message lengths
  chosen around the 64-byte block and 16-byte MAC boundaries.
- `hw/sim/test_dirty_pad.py` — adversarial padding test, encrypt and
  decrypt: random garbage is driven into the bytes past `in_len` and
  neither the ciphertext nor the tag may move. The suite above zero-pads,
  so it is blind to that masking on the decrypt path: with the mask
  removed entirely, all of its decrypt tests still pass and only the
  three that encrypt fail, where the padding reaches Poly1305 XORed with
  keystream. Which is why the masking has a test of its own.
- `hw/sim/proto_model.py` — builds and parses host-protocol packets in
  Python, the reference for the wire format. The cryptography comes from
  `aead_model.py`, so no expected value is written by hand.
- `hw/sim/test_proto_model.py` — self-consistency checks for the model,
  run as plain Python: there is no DUT, and pulling a simulator into a
  pure Python check would be noise.
- `hw/sim/test_keystore.py` — a slot reads back what was written, an
  unwritten or out-of-range slot reports invalid, and reset clears both
  the keys and the loaded bits.
- `hw/sim/test_pktbuf.py` — bytes come back at the offset they went in,
  the counter tracks the write position, and the full flag fires at
  capacity without wrapping over what is already stored. `run_pktbuf.py`
  also elaborates the module at legal and illegal `BYTES` before it
  starts the simulator, because a cocotb test only ever sees the one
  value its build was elaborated with.
- `hw/sim/test_oca_core.py` — end-to-end: packets in, packets out, with
  no Ethernet in the simulation. Load-key then seal, a seal/open round
  trip, an unloaded slot refused, every header failure and every length
  failure, and 20 randomised round trips judged by the model. The one
  that matters is `test_corrupt_tag_yields_no_plaintext`, which asserts
  that a flipped tag bit yields status `06` and **zero bytes of body** —
  it asserts on the leak, not on the status, and was checked against a
  deliberately broken tag comparison. Beside it,
  `test_every_tag_byte_is_compared` flips one bit in each of the sixteen
  tag bytes: the first test alone passes against a comparison eight bits
  wide, because the bit it flips is in byte 0. Without the pair, the
  design's security property would be written down rather than held.
- `hw/sim/test_attack.py` — the same DUT attacked rather than confirmed:
  descriptor integrity under overlap, bank ownership, engine ownership,
  and the timing residual of a failed tag, several of them watching
  `oca_proto`'s registers because a descriptor moving under a pending
  hand-off reaches the wire only at the alignments that expose it.
- `hw/sim/test_proto_gate.py` — a round trip and the sixteen tag bytes,
  replayed on a synthesised `oca_proto` inside an otherwise unmodified
  `oca_core`. The tag comparison is combinational, so no flip-flop count
  in the synthesis flow can tell whether the mapper kept it.
- `hw/sim/run_*.py` — run the tests under the project-local Verilator
  (`../tools/verilator`, built from source, branch `stable`).
- `hw/syn/run_synth.py` — ECP5 synthesis and place & route with the
  project-local yosys and nextpnr-ecp5; see `hw/syn/README.md` for the
  flow and the first results on the LFE5U-45F.

Requirements: `../tools/verilator` built, `oca/.venv` with cocotb
(installed from cocotb git master — release 2.0.1 does not support
Python 3.14).

```sh
.venv/bin/python hw/sim/run_chacha20.py
.venv/bin/python hw/sim/run_poly1305.py
.venv/bin/python hw/sim/run_chacha20_poly1305.py
.venv/bin/python hw/sim/run_dirty_pad.py
.venv/bin/python hw/sim/run_secret_zeroise.py
.venv/bin/python hw/sim/run_keystore.py
.venv/bin/python hw/sim/run_pktbuf.py
.venv/bin/python hw/sim/run_oca_core.py
.venv/bin/python hw/sim/run_attack.py
.venv/bin/python hw/sim/run_clkrst.py
.venv/bin/python hw/sim/run_console.py
.venv/bin/python hw/sim/run_fifo.py
.venv/bin/python hw/sim/run_uart_rx.py
.venv/bin/python hw/sim/run_uart_tx.py
.venv/bin/python hw/sim/run_uart_console.py
.venv/bin/python hw/sim/run_uart_echo.py
.venv/bin/python hw/sim/run_slip_rx.py
.venv/bin/python hw/sim/run_slip_tx.py
.venv/bin/python hw/sim/run_uart_crypto.py     # the crypto over the real UART
.venv/bin/python hw/sim/run_keystore_gate.py   # post-synthesis
.venv/bin/python hw/sim/run_proto_gate.py      # post-synthesis
cd hw/sim && ../../.venv/bin/python test_proto_model.py
```

Those 21 runners are all the cocotb suites, and `test_proto_model.py`
above is plain Python (2 tests). Three more need no toolchain at all:
`pytest hw/host` (46 tests), `pytest hw/syn/test_run_synth.py` (4) and
the offline `hw/host/cli.py --fake selftest` (6 steps), measured
2026-08-13; the C backend's 126 known-answer checks sit behind the
`ctest` in the build section. They are different units and are not
summed. There is no aggregate runner, so a suite left off these lists
is a suite nobody runs. Four cocotb runners — `run_rgmii`,
`run_eth_mac`, `run_udp_seam` and `run_oca_path` — belonged to the
Ethernet route and were deleted with it on 2026-08-12, together with the
`oca/hw/vendor/` tree they built against. No suite here needs
`vendor_patches.py build`, or any vendored source, any more.

`run_attack.py` drives the same DUT as `run_oca_core.py` but is written
to break the packet overlap rather than to confirm it. The two
`*_gate.py` runners are the only ones that see a synthesised netlist:
everything else elaborates the SystemVerilog and is blind to what yosys
does with it, which is how a mis-mapped key store survived every green
test run this project ever had.

Current status: chacha20 5/5 tests pass at both `ROUNDS_PER_CYCLE`
values, poly1305 4/4 tests pass at both `ROWS_PER_CYCLE` values, AEAD
7/7 tests pass, dirty-padding 2/2, secret-zeroise 2/2, keystore 4/4,
pktbuf 12/12 (+3 at BYTES=16), oca_core 29/29, attack 16/16, clkrst
7/7, console 8/8, fifo 4/4, uart_console 4/4,
uart_echo 3/3, uart_rx 4/4, uart_tx 5/5, slip_rx 12/12 at both `BYTES`
values, slip_tx 7/7, uart_crypto 7/7 across its two `LED_BITS` builds —
**142**, plus twenty-nine re-runs at a second parameter — and
post-synthesis keystore 4/4 and oca_proto 2/2, which is **177 passing
executions over 21 runners** with no failures and two skips, measured
2026-08-12. This read 183 tests over 25 runners and 222 executions until
the Ethernet suites were deleted that day: rgmii 10/10, udp_seam 10/10
at both `HDR_Q_DEPTH` values, eth_mac 8/8 and oca_path 7/7 all passed
while they existed, and they are the 45 executions that went. The last
two built from the patched vendor tree at `hw/vendor/build/`, which is
gitignored, so in a fresh checkout or a new worktree they exited
non-zero instead and the same before-figure read 207 executions over 23
producing runners. Both numbers are real; they differed by whether
`vendor_patches.py build` had run. Neither reproduces here now — the
runners and that script were deleted on 2026-08-12, so either figure
needs `fd3059c` checked out first.
The protocol model checks pass as plain Python;
`verilator --lint-only -Wall` clean on all cores with `--top-module
oca_core`. Eight reworks are done — five on the engine, described next,
and three on the host datapath described below: the 64-bit widening,
then the overlap inside a command, then the overlap across packets. The
Poly1305 limb rework took the AEAD engine from 65 to 20 ECP5
multipliers (90% -> 28% of an LFE5U-45F) and
more than doubled the standalone Poly1305 Fmax (22.94 -> 52.68 MHz as
measured on the day; 55.41 MHz rebuilt on today's netlist, which has had
its secret registers cleared on reset since — `hw/syn/README.md`). The
ChaCha20 round-per-cycle rework then raised its standalone Fmax
28.66 -> 53.11 MHz, so the two cores are now balanced, and AEAD Fmax
26.10 -> 37.87 MHz. Rebuilding the wrapper's byte mask per byte instead
of as a 512-bit subtract — which had become the critical path — took the
engine to 50.08 MHz, level with the baseline's throughput at last.
Splitting the AEAD FSM in two, joined by a one-block buffer, then
overlapped the phases: block N is authenticated while block N+1 is
encrypted, so a 64-byte block costs **40 cycles instead of 57**
(measured in simulation) for -540 LUTs and +13 flip-flops. At 52.58 MHz
that is **~0.67 Gbps, +42% on the ~0.47 Gbps original baseline** and on
20 multipliers instead of 65 — the first point in the series where the
engine is ahead of where it started.

The fifth rework buys area rather than speed, and is the largest single
move in the series: **10041 -> 7358 LUTs, -26.7%**, with flip-flops
(5738), multipliers (20) and cycles per block (40, measured) all
unchanged. `chacha20.sv` now carries one round datapath instead of two —
a diagonal round is a column round on a row-rotated state and rotation is
wiring, so 16 of its 32 adders and the multiplexer choosing between them
are gone — and `chacha20_poly1305.sv` masks the padding on the 16-byte
sub-block Poly1305 reads instead of on the 512-bit buses feeding it, two
full-width masking stages becoming one quarter-width one. Three engines
now take 50.3% of an LFE5U-45F's LUTs instead of 68.7%. That does **not**
buy a fourth engine — 4 x 20 = 80 multipliers against the device's 72,
unchanged by this pass — and it does not buy throughput; what it buys is
headroom for the port logic, packet buffering and top-level glue that did
not exist when it was measured, none of which is in these out-of-context
numbers. Fmax is not
claimed either: over four placer seeds the engine means 50.72 -> 52.83
MHz while the standalone core shows no effect at all, and the critical
path is structurally the same quarter round it was.

**Three engines do not fit, and the reason is not area.** Placing the
configurations on 2026-08-04 instead of multiplying one core's report:
two `oca_core` route at 22313 LUTs (50.9%), 40 multipliers (55.6%) and
49.28 MHz, while three take 33484 LUTs (76.4%) and 60 multipliers
(83.3%) — both under budget — and **never route**, with roughly 50000
arcs left unrouted whether the constraint is 100, 45, 40 or 35 MHz. It
is congestion, not timing. **Every port and line-rate figure from here
to the end of this passage belongs to the Ethernet route, retired
2026-08-12; they are kept as measurements, not as a target.** What two
engines are worth depends on how
they are wired to the ports, and `oca_dual` gives each core its own
AXI-Stream pair, one per PHY: **0.569 Gbps per port at a 1500-byte MTU,
56.9% of line rate, and 1.138 Gbps across both** — the committed pair's
48.89 MHz mean through the measured cycle model, 1031 cycles for an
MTU-sized packet. That mean is an out-of-context Fmax and no PLL divider
produces it, so read these as cycle budgets: at the 48.0769 MHz
`oca_clkrst` delivers they are 0.560 per port. Both PHYs can be fed in cycle budget — whether two
MACs fit beside the cores is settled below, and they do not — and
neither port is saturated, and saturating one would need both cores
behind it — a distributor and a collector that do not exist. This
supersedes the 1.97-2.07 Gbps three-engine projection, and
`SPEC.md`'s MVP target is corrected to match (`hw/syn/README.md`, "The
occupancy study"). **Amended 2026-08-09: whether two ports fit was still
open here, and it is settled — they do not.** One GbE port costs 8422
LUTs measured (19.2% of the device), so two cores with two ports would
take 94.5%, and two cores behind one port 75.3% against the 76.4% at
which this device stopped routing above. The configuration that fitted
the RTL was **one core on one port, 0.581 Gbps at MTU** — one core
alone means the single core's own 49.91 MHz mean, not the pair's. That
clock was measured on the core placed **alone and out of context**: no
MAC beside it, no IO, no PLL, so it is the ceiling that configuration
could reach rather than a measurement of it. **That netlist was
built**: `oca_top` was one core and one port against the real pin
map, 18719 LUTs — 42.7%, not the 47.3% the sum predicted, because the
sum counts twice the logic the optimiser shares. **And it did not run
at 0.581 Gbps**, because it could not run at 49.91 MHz: `oca_clkrst`
delivers `clk_sys` at 625/13 = **48.0769 MHz**, and the next frequency
this PLL can offer, 50.00, has been built and does not close. At the
clock it gets, the same cycle model gives **0.560 Gbps at MTU**.

**No build of it ever closed timing, and the route is retired as of
2026-08-12** -- for want of an RJ45 socket on this board, not for want
of a clock. Connecting the raw-IP ready pins
on the UDP stack — required, or one non-UDP frame stops reception for
good — came with a third connection, `clear_arp_cache`, that brought
back 881 LUTs and 400 flip-flops of ARP logic yosys had been deleting,
around the receive path, and
across 32 placer seeds `rgmii_rx_clk` cleared its 125 MHz on none of
them, the best reaching 124.22. `AGENTS.md` carries the sweep and what
was ruled out. Every port figure above is a cycle budget for a
configuration the board never carried; the host interface is the
DAPLink USB serial on J17/H18 (`SPEC.md`, PHASE 2).

**The host protocol is implemented and verified**
(`docs/design/2026-08-03-host-protocol.md`): a request payload in, an
AEAD operation, a payload out, with no transport in the simulation. The
document says "UDP payload" throughout because that is what carried it
when it was written; `oca_core` takes a 64-bit AXI-Stream and the words
are interchangeable here. Its
datapath is **64 bits end to end inside `oca_core`**, which is the sixth
rework and the one that made the protocol layer stop being the limit.

As committed, `oca_core` synthesises to **12308 LUTs (28.1%), 12033 FF
(27.4%), 20 MULT18X18D (27.8%) and 4 DP16KD (3.7%)** at **49.91 MHz
mean over four seeds** (47.93 / 50.91 / 51.03 / 49.76, measured
2026-08-09, spread 6.5%) — reproducible with `hw/syn/run_synth.py
oca_core`, and re-measured because this read 11590 / 12043 / 48.52 MHz,
which was the build from before the secret zeroisation. That netlist has a
working key store in it; the 11429 / 11228 / 51.71 MHz figures below,
and every earlier area figure in this file, were measured before the
`cmp2lut` defect was found, when `oca_keystore.sv` was being deleted
from the netlist by the mapper (`hw/syn/README.md`, "The cmp2lut trap").
They are the right numbers for the comparison they make and the wrong
ones for anything else.

At 64 bits `oca_core` measured **11429 LUTs (26.1%), 11228 FF (25.6%),
20 MULT18X18D (27.8%) and 4 DP16KD (3.7%)** at **51.71 MHz** (seed 1;
50.69 MHz mean over seeds 1-4). Against the 8-bit version the widening
costs **+280 LUTs (+2.5%), +386 FF (+3.6%), no multipliers and no clock**
— the 8-bit mean was 50.59 MHz, so +0.2%, with the distributions on top
of each other. **Both packet buffers still infer block RAM** in
pseudo dual-port mode and there is not a single LUT RAM cell in the
netlist; the 2 -> 4 DP16KD is width, not capacity, because a DP16KD's
widest port is 36 bits and a 64-bit word spans two blocks. The protocol
layer still adds no multipliers, so it does not cost an engine, and it
was not on the critical path of that build: across all four seed
reports, no entry cites `oca_proto.sv`, `oca_pktbuf.sv`,
`oca_keystore.sv` or `oca_core.sv`. The path is inside `chacha20.sv` on
three seeds and inside `poly1305.sv` on the fourth. The protocol layer
did reach the worst path once — the `data_off` adder, most of its delay
one route across the die — but on the pre-zeroisation netlist, not the
committed one: at seed 1 the committed netlist's worst path is back
inside the engine, `poly1305.sv`'s multiply. One seed either way is a
placement result, not a property of the design (`hw/syn/README.md`).

**Throughput: 415 cycles per 64-byte block down to 40**, in three steps,
each measured differentially in simulation and exactly linear. Widening
the datapath took 415 to **64** — 8 to receive at 8 bytes per cycle, 48
through buffer, engine and buffer, 8 to transmit, strictly sequential
because the core was store and forward on one pair of buffers.
Overlapping feed, compute and drain inside a command took it to **56**,
and overlapping four packet stages across successive commands took it to
**40**: 231, 391, 551 and 711 cycles for 4, 8, 12 and 16 blocks. 40 is
the engine's own cost, so the protocol layer now adds nothing on top of
it, and that is the floor until the engine changes.

**What that is worth end to end is a projection, not a measurement, and
the ports it divides across were retired on 2026-08-12** for want of an
RJ45 socket on this board. The figures stay as measured cycle budgets.
At 1.6 bytes per cycle and the 48.53 MHz measured for the last 64-bit
pair,
two cores are 155 MB/s = **~1.24 Gbps** — but that is the two of them
added together, and `oca_dual` wires them to two ports, one core each.
One port therefore sees one core, **0.569 Gbps at a 1500-byte MTU** on
the committed pair's clock, and is not saturated. **Read the aggregate
as a cycle budget, not as a port cleared** — and the per-port figure the
same way, since that clock is an Fmax the PLL cannot deliver: 625/13 =
48.0769 MHz gives 0.560. The 48.53 MHz and the 22891 LUTs (52.2%), 40 multipliers
(55.6%) and 8 DP16KD beside it were measured on RTL from before the
packet overlap, in a build whose key store the mapper had deleted. That
older pair was also the tightest configuration in the study: two of its
four placer seeds did not route, each stopped after 3 h 22 min still
bouncing between 50 and 2300 unrouted arcs, so its 48.53 MHz mean is
over two seeds and not four. **Two cores of the current RTL have since
been placed and routed, on four seeds with every seed routing** —
`AGENTS.md` carries what they measure, and it is deliberately not
repeated here. **None of this has run on silicon.**

**The Ethernet integration was merged, retired and then deleted**
(all 2026-08-12), designed in
`docs/design/2026-08-05-ethernet-integration.md`, which carries a closed
header and stays as history: `verilog-ethernet` (MIT) as a submodule,
the RGMII front end written by us around the ECP5 DDR primitives, PLL,
reset, the Colorlight i9 pin constraints, the seam onto `oca_core`, and
`oca_top` placed and routed. Complete, it never closed timing, and no
bitstream of it survives. **What it established stays; the sources do
not** — the RTL, the four testbenches, the vendored tree and the three
`oca_top*` synthesis targets were removed, and `docs/STATUS.md` lists
the three pieces that deliberately stayed. The 8-to-64-bit width
conversion was not in our clock
domain: at ~48 MHz an 8-bit stream carries 384 Mbps, under the port it
was meant to feed, so it happened on the 125 MHz side inside
`eth_mac_1g_fifo` at `AXIS_DATA_WIDTH = 64` — not
`eth_mac_1g_rgmii_fifo`, which embeds the `rgmii_phy_if` that has no
ECP5 target. Upstream had no testbench for that configuration, so it had
one of ours (`run_eth_mac.py`), beside one for the RGMII wrapper
and one for the seam. **The whole path from a synthetic frame back out
to one had `run_oca_path.py`** — 7 tests, and the first run of it found
`oca_top` wedging its own receive path on any frame that is not UDP --
found on the first run, which is what a whole-path testbench is for.
`run_uart_crypto` is the whole-path test that took over, over the
serial line rather than over a frame.

## Phase 1: abstract API + software backend

Reference implementation of the OCA abstract crypto API with the
software backend (OpenSSL 3 EVP). The same API will be served by the
FPGA backend without application changes.

## Layout

- `include/oca/oca.h` — public API (AEAD, hash, MAC, one-shot)
- `src/oca.c` — API dispatch and argument validation
- `src/backend.h` — internal backend interface
- `src/backend_sw.c` — software backend (OpenSSL EVP)
- `tests/vectors/gen_vectors.py` — generates `vectors.h` from official
  sources (reproducible; sources in `tests/vectors/sources/`)
- `tests/test_vectors.c` — known-answer tests
- `bench/bench.c` — throughput benchmark

## Test vector sources

- RFC 8439 (ChaCha20-Poly1305 AEAD, Poly1305)
- RFC 4231 (HMAC-SHA-256, including the 128-bit truncation case)
- BLAKE2 official KAT, keyed BLAKE2s-256 (github.com/BLAKE2/BLAKE2)
- C2SP/wycheproof `aes_gcm_test.json` (AES-128/256-GCM, valid + invalid)
- SHA-256 / unkeyed BLAKE2s: cross-checked against Python hashlib

Regenerate with: `python3 tests/vectors/gen_vectors.py`

## Build, test, benchmark

```sh
cmake -B build -S .
cmake --build build
ctest --test-dir build --output-on-failure
./build/oca_bench
```

## Security notes

- AEAD tag verification is performed by OpenSSL in constant time.
- The API returns `OCA_ERR_AUTH` on tag mismatch and the caller must
  discard the output buffer.
- Standalone Poly1305 (`OCA_MAC_POLY1305`) is a one-time-key MAC:
  never reuse a key.
