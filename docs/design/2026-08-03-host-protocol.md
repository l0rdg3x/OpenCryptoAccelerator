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
