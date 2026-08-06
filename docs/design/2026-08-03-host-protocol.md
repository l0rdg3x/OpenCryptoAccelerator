# Host protocol and engine adapter — design

Date: 2026-08-03. Status: approved, not yet implemented.
Scope: the packet protocol and the logic between a UDP payload and
`chacha20_poly1305.sv`. Everything here is verifiable in simulation.

Out of scope, and covered by a separate design: the Ethernet MAC, the
RGMII interface, PLL, reset and pin constraints. See section 7.

## 1. Why this shape

SPEC.md phase 2 asks for "a simple command queue to the host
(documented protocol over UDP or USB FIFO)". The MVP board is a
Colorlight i9 v7.2 whose only host link is Gigabit Ethernet, so the
queue is a UDP protocol.

The Ethernet MAC, the RGMII interface and the IP/ARP/UDP stack are
**not written here**. They come from `verilog-ethernet` (Alex
Forencich, MIT licence), a widely used open-source core with working
ECP5 support. Writing a 1G MAC from scratch — preamble, CRC32,
interframe gap, clock domain crossing — is weeks of work on ground that
is already well trodden, and every bug in it presents as "the link does
not come up", which is the slowest class of problem to diagnose without
a logic analyser. The dependency is declared in the licence files.

That choice draws a clean boundary: `verilog-ethernet` hands over the
UDP payload as an 8-bit AXI-Stream (`tdata`, `tvalid`, `tready`,
`tlast`), and everything in this document sits behind that interface.
It can therefore be tested by injecting packets on that stream, with no
Ethernet in the simulation at all.

> **Amended 2026-08-06, and this one retracts a claim rather than
> refining it.** Section 1 calls `verilog-ethernet` "a widely used
> open-source core with working ECP5 support". The first half stands;
> **the second is false.** AGENTS.md carried the same sentence and
> retracted it on 2026-08-05, checked against the repository:
>
> - none of the 25 directories under `example/` targets Lattice, and a
>   code search for `ecp5`, `lattice`, `colorlight` and `trellis`
>   returns nothing;
> - `rgmii_phy_if.v` accepts only `SIM`, `GENERIC`, `XILINX` and
>   `ALTERA`, and falls through to `GENERIC` on anything else without a
>   warning;
> - `GENERIC` is not a slower path on this device but a broken one:
>   `oddr.v` drives one register from two `always` blocks on opposite
>   edges, so `synth_ecp5` reports conflicting drivers on every bit
>   rather than inferring `ODDRX1F`.
>
> **The consequence is a schedule item, not a footnote: the RGMII front
> end is ours to write**, behind the wrapper SPEC.md's portability rule
> requires for vendor primitives — the piece nearest the pins, and the
> one this document's section 7 assumed away by naming
> `verilog-ethernet` as a submodule. It is designed in
> `docs/design/2026-08-05-ethernet-integration.md`, which also lists what
> only the bench can close about it.
>
> The choice of `verilog-ethernet` itself is unchanged, and so is this
> section's boundary: the MAC, ARP, IP and UDP above the pins are still
> upstream's, and the UDP payload still arrives as an AXI-Stream that
> needs no Ethernet in the simulation.

## 2. Store and forward

The received packet is buffered whole before the engine sees any of it,
and the response is assembled whole before a byte leaves.

The reason is not simplicity. An Ethernet frame cannot be trusted until
its CRC has been checked, and that happens at the end; a cut-through
design would have encrypted data that may be corrupt. The AEAD engine
also has no way to abandon a message halfway — its only exit is `err`,
which aborts everything. And on a 1 Gbps link the added latency is a
few microseconds, against an engine that already produces about twice
what the wire can carry.

Store-and-forward also buys the security property in section 5: the
plaintext is entirely in memory before anything is sent, so it can
simply not be sent.

> **Amended 2026-08-04, after the datapath was measured. The 8-bit
> boundary is settled outside `oca_core` and is being abandoned inside
> it.** Sections 1 and 2 above read as if one width served both, which
> is how the whole protocol path came to be a byte wide. It cannot feed
> the engine.
>
> - **At 8 bits the buffer needs 66 cycles to assemble a 64-byte block
>   the engine consumes in 40** — 64 bytes at one byte per cycle behind
>   the two-deep valid pipeline of the reader in `oca_proto.sv`. Even
>   with everything else free, the feed sets the pace and an engine
>   could be busy at most 40/66 of the time. It is not free: measured
>   end to end, a block costs **415 cycles**, ~0.062 Gbps per core,
>   because store and forward serialises receive, process and transmit
>   (`oca/hw/syn/README.md`, "The host protocol layer: oca_core").
> - **So the datapath moves to 64 bits inside `oca_core`.** Eight bytes
>   per cycle put a block in the buffer in 8 cycles plus the pipeline —
>   an estimate, roughly 10 against the engine's 40, comfortably clear
>   of it. What section 1 fixes is the *external* boundary: the 1G MAC
>   in `verilog-ethernet` hands over an 8-bit AXI-Stream and that is not
>   in question. The width conversion belongs at that boundary, not
>   carried through the buffers and the protocol FSM.
> - **Section 2's "about twice what the wire can carry" is no longer
>   true.** The device holds two engines, not three: ~1.26 Gbps of
>   crypto against 1 Gbps of wire, 1.26x rather than 2x, and only once
>   the engines can be fed. Three `oca_core` instances do not route at
>   all — see `oca/hw/syn/README.md`, "The occupancy study", and the
>   corrected MVP target in `SPEC.md`. The rest of section 2 stands
>   unchanged: the CRC argument, the engine's inability to abandon a
>   message and the security property all hold at any width, and none
>   of them depends on that comparison.

> **Amended again 2026-08-04, after the 64-bit datapath was built and
> measured.** The amendment above said what would be done; this says what
> was done and what it cost. Plan:
> `docs/design/2026-08-04-datapath64-plan.md`; measurements:
> `oca/hw/syn/README.md`, "After the 64-bit host datapath".
>
> - **Built.** `oca_pktbuf` is 256 x 64 with a 1..8 byte count on writes
>   and a 9-bit word read address; `oca_proto` reads the header as one
>   word and the arguments as four, aligns the one misaligned boundary
>   (AAD to message) with a single 128-to-64 funnel shifter, and streams
>   the response through three stages under one clock enable instead of
>   the fetch-then-present pair of states; `oca_core` exposes a 64-bit
>   AXI-Stream pair with `tkeep`. `proto_model.py` was **not** modified
>   and the wire format is unchanged, as the plan required.
> - **A block costs 64 cycles end to end, not 415.** Measured
>   differentially in simulation and exactly linear: **8 in + 48 through
>   + 8 out**. The estimate above — "roughly 10 against the engine's 40"
>   for the buffer feed — was close: the middle term is 48, of which the
>   engine is 40. Both stream phases now run at the full 8 bytes per
>   cycle. The overall factor is 6.5x, **below** the 8x of the width,
>   because the engine's 40 cycles never scaled with the host datapath —
>   only 24 of the 64 that remain are protocol. The response path is the
>   one phase that beat the width, 192 cycles to 8 where width alone
>   gives 24, because its three-cycle handshake was replaced by the
>   clock-enabled pipeline at the same time.
> - **It cost no clock and almost no area.** `oca_core` is +280 LUTs
>   (+2.5%) and +386 flip-flops (+3.6%), with multipliers unchanged at 20
>   and Fmax unchanged (50.59 -> 50.69 MHz over four seeds, +0.2%, the
>   distributions overlapping). Block RAM went 2 -> 4 DP16KD per core,
>   which is width and not capacity: a DP16KD's widest port is 36 bits,
>   so a 64-bit word spans two blocks.
> - **The engine can now be fed, and the host port still cannot be
>   filled.** That was the point of the exercise and it is only half
>   done. At 64 cycles per 64-byte block a core moves exactly one byte
>   per cycle, so two cores at the measured 48.53 MHz (two of four placer
>   seeds; see below) are 97.1 MB/s, **~0.78 Gbps — 78% of a bare GbE
>   port's 125 MB/s, and 62% of the ~1.26 Gbps `SPEC.md` asks for, which
>   is one port saturated *with margin*. The target is missed by 38%.**
>   The remaining factor is not width and not clock: it is that the three
>   phases are **serialised** by store and forward, one packet at a time
>   through one pair of buffers.
> - **So the next step is packet-level pipelining**, and section 2's
>   store-and-forward argument does not stand in its way. That argument
>   is about *one packet* — the response is built whole before a byte
>   leaves, so a failed tag returns no plaintext — and it is untouched by
>   receiving packet N+1 while N is processed and N-1 transmitted. Each
>   packet is still received whole, then processed whole, then
>   transmitted. What it needs is a second buffer in each direction per
>   core (4 -> 8 DP16KD, 16 of 108 for the MVP pair). It takes a block
>   from 64 cycles to `max(8, 48, 8) = 48` and two cores to ~1.04 Gbps —
>   **enough to pass the port by 2-5% and not enough to call it margin.**
>   The margin needs the 8 cycles of feed and drain off the loop as well,
>   leaving the engine's 40 and ~1.24 Gbps. Both are recorded in
>   `SPEC.md`, whose MVP bullet has been corrected again.
> - **Two placer seeds of the two-core build did not route**: stopped
>   after 3 h 22 min each, still bouncing between 50 and 2300 unrouted
>   arcs rather than descending. Two cores fit this device; they are no
>   longer comfortable on it, and adding buffers for the pipelining above
>   has to be measured rather than assumed.
> - **Section 3 below is superseded on one point** and is left as written
>   for the record: `oca_pktbuf.sv` no longer converts "between single
>   bytes and the 64-byte blocks the engine consumes". It is 256 x 64
>   with a 1..8 byte count on writes and a word read address, and the
>   byte-to-word conversion belongs at the MAC boundary. Everything else
>   in sections 3 to 6 — the module split, the packet format, the status
>   codes and the security properties — is unchanged by this rework.

> **Amended a third time, 2026-08-04, after the pipelining above was
> built.** The step the previous amendment called next is done, in two
> commits: feed, compute and drain overlap inside a command, and four
> stages — receive, process, drain, transmit — overlap across successive
> commands.
>
> - **A block costs 40 cycles**, measured the same way and exactly
>   linear: 231, 391, 551, 711 cycles for 4, 8, 12 and 16 blocks,
>   marginal 40.00. 56 was the intermediate step. 40 is the engine's own
>   cost, so the protocol layer no longer adds anything to it and this is
>   the floor until the engine changes.
> - **What that is worth end to end is still a projection.** Two cores at
>   1.6 bytes/cycle and the last measured pair clock (48.53 MHz) are
>   155 MB/s, ~1.24 Gbps, 24% over a GbE port. That clock was measured on
>   the pre-overlap RTL, in a build whose key store the mapper had
>   deleted, and **two cores of the current RTL have never been placed
>   and routed** — the pair was already the configuration where two of
>   four placer seeds failed to route. Cycle budget: measured. Fit and
>   clock at two cores: not.
> - **`oca_pktbuf` now carries two banks**, so it is 512 x 64 in the same
>   pair of block RAMs — the second bank was free because 36-bit mode is
>   512 x 36 and one bank used half of it. `BYTES` is constrained to
>   eight times a power of two, 16 to 2048, and the module refuses
>   anything else at elaboration: the bank base is `2**ADDR_W` while the
>   array is `2*WORDS` entries, so a non-power-of-two `WORDS` puts half
>   the upper bank off the end of the array and the protocol layer
>   answers status 00 over it, and above 2048 the 12-bit byte counters
>   truncate.
> - **Section 6's acceptance criterion needed sharpening, not
>   relaxing.** "A corrupted tag must produce status 06 and zero bytes of
>   plaintext ... without that test it is not actually held" was met in
>   its weakest sense: the test flipped a bit in tag byte 0, so a
>   comparison of that byte alone satisfied it, and a comparison of 120
>   bits satisfied every test in both suites. It now takes
>   `test_every_tag_byte_is_compared` — one flipped bit per tag byte, all
>   sixteen, and the intact tag opened afterwards so a comparison stuck
>   at false cannot pass — plus `run_proto_gate.py`, which replays the
>   same sixteen cases on a synthesised `oca_proto`, because the
>   comparison is combinational and no flip-flop census in the synthesis
>   flow can see whether the mapper kept it.

## 3. Modules

Four modules, one responsibility each, all under `oca/hw/rtl/`:

- **`oca_proto.sv`** — speaks the protocol. Parses the header off the
  incoming stream, validates it, decides what to do, and builds the
  response header. Knows nothing about cryptography.
- **`oca_keystore.sv`** — `NUM_SLOTS` key slots (default 8, a
  parameter). Written only by the load-key command, read by index. The
  only place key material lives. Cleared on reset, and each slot carries
  a loaded bit so that using a slot that was never written is an error
  (status `04`) rather than encrypting under a key of zeros.
- **`oca_pktbuf.sv`** — the two ~2 KB BRAM buffers, receive and
  transmit, and the conversion between single bytes and the 64-byte
  blocks the engine consumes.
- **`oca_core.sv`** — ties them together and instantiates
  `chacha20_poly1305`.

Flow: the packet lands in the receive buffer; once the frame is
complete and valid, `oca_proto` reads the header, fetches the key from
its slot, and feeds the message to the engine in 64-byte blocks;
ciphertext and tag land in the transmit buffer; the response goes out.

## 4. Packet format

One 8-byte header, identical in both directions, so there is a single
parser and the host always finds fields at the same offsets.

| offset | size | field |
|--------|------|-------|
| 0 | 2 | magic, `4F 43` ("OC") |
| 2 | 1 | version, `01` |
| 3 | 1 | opcode |
| 4 | 2 | request id, echoed unchanged |
| 6 | 1 | key slot |
| 7 | 1 | status — zero in requests, a code in responses |

All multi-byte numbers are little-endian, matching the rest of the
project: ChaCha20 and Poly1305 already work that way, and mixing
endianness is a reliable way to introduce bugs.

### Commands

**`01` load key.** 32 key bytes follow. Response: header only.

**`02` seal.** Followed by nonce (12), AAD length (2), message length
(2), then AAD and message. Response: header, **tag (16)**, ciphertext.

**`03` open.** Nonce (12), AAD length (2), **ciphertext length** (2),
the received tag (16), then AAD and ciphertext. The tag sits before the
data. Response: header and plaintext — but only if the tag matches;
otherwise the header with status `06` and nothing else.

**`04` stats.** Returns four 32-bit counters: packets received, packets
dropped for an invalid header, commands completed, authentication
failures.

The tag precedes the payload in both directions so the host finds it at
a fixed offset without first computing lengths.

### Status codes

| code | meaning |
|------|---------|
| `00` | success |
| `01` | bad magic |
| `02` | unsupported version |
| `03` | unknown opcode |
| `04` | key slot out of range, or never loaded |
| `05` | lengths inconsistent, or larger than the buffer |
| `06` | authentication failed (open only) |
| `07` | engine error (`err` asserted) |

The module **always answers** if it could read the header. If even that
is unreadable the packet is dropped, but a counter readable through the
stats command is incremented — a silent drop, in an accelerator that
simply appears not to answer, is the slowest failure there is to
diagnose. That command exists for exactly this reason, and is the only
thing here beyond the strict minimum.

### Declared limit

One command fits in one packet. At standard MTU that is roughly 1430
bytes of usable message, which covers the WireGuard case. Anything
larger gets status `05`. There is no fragmentation and no reassembly in
v1.

The UDP port is a parameter, default 20291 (`0x4F43`, the magic read as
a number).

## 5. Security

Three things this design does **not** protect, which belong in
`Security.md` before the code exists rather than after:

- **The key crosses the wire in the clear** in the load-key command.
  Once per key rather than once per packet, but in the clear. Anyone who
  can observe that segment reads it.
- **There is no authentication.** Anyone who can send packets to the
  FPGA can encrypt and decrypt with whatever slots are loaded. The
  accelerator trusts whoever talks to it, exactly as a PCIe accelerator
  trusts its host — except an Ethernet cable is easier to reach than a
  PCIe slot. The MVP deployment is a direct host-to-board link.
- **The nonce comes from the host**, and reuse remains a host error the
  hardware cannot detect. This is already recorded for the engine and
  applies unchanged here.

Two things it does do:

- **Key slots are cleared on reset.**
- **The tag is compared on the FPGA, in constant time, and the
  plaintext is not emitted when it fails.** A 128-bit comparison in
  hardware is fixed-width combinational logic, so it is constant-time by
  construction; delegating it to the host means hoping nobody writes
  `memcmp`. This supersedes the caller obligation currently recorded in
  `Security.md` section 4 item 1 for traffic that goes through this
  protocol — the obligation remains for anyone instantiating
  `chacha20_poly1305.sv` directly.

## 6. Verification

A Python model builds and parses packets, using the existing
`oca/hw/sim/aead_model.py` as the cryptographic oracle — no expected
values are hand-written. The testbench injects bytes on the 8-bit
stream and reads the response, so it runs with no Ethernet in the
simulation.

The cases that matter are the failures:

- one packet per status code;
- **a corrupted tag must produce status `06` and zero bytes of
  plaintext** — this is the security property of the whole design, and
  without that test it is not actually held;
- a packet truncated mid-header;
- lengths that disagree with the packet size;
- a slot that was never loaded;
- a load-key command arriving while the engine is busy.

Plus randomised seal/open round trips judged by the model, and the
usual proof that the tests can fail: break the tag comparison and
confirm the plaintext leaks, then restore.

## 7. The other half, not designed here

The Ethernet integration is a separate spec: `verilog-ethernet` as a
submodule, the RGMII wrapper with its ECP5 DDR primitives (permitted
behind a wrapper by SPEC.md's portability rule), PLL, reset, and the
Colorlight i9 pin constraints. It can be designed now but can only be
*closed* with the board on the bench, expected around 2026-08-17.
