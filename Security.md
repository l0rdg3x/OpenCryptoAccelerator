# Security — OpenCrypto Accelerator

Threat scope and side-channel limits of the OCA hardware, as required by
`SPEC.md` (sections CONSTRAINTS and DOCUMENTATION).

This document describes the RTL as it stands on this branch: the three
engine cores `oca/hw/rtl/chacha20.sv`, `oca/hw/rtl/poly1305.sv` and
`oca/hw/rtl/chacha20_poly1305.sv`, and the host protocol layer
`oca_keystore.sv`, `oca_pktbuf.sv`, `oca_proto.sv` and `oca_core.sv`
that wraps them. It is not a statement about a finished product: there
is no Ethernet MAC, no driver and no board yet, and the parts that do
not exist cannot be assessed here. Every claim below was checked against
the RTL; where a property is a caller obligation rather than something
the hardware enforces, it says so.

Section 6 covers what the host protocol exposes, and is the section to
read before putting a board on a network.

## 1. Threat model

### What the hardware is meant to defend against

- **A passive network adversary.** The cores implement
  AEAD_CHACHA20_POLY1305 exactly as RFC 8439 specifies it, verified
  against the RFC's own test vectors and against randomised messages
  compared with an independent model. Confidentiality and integrity of
  the encrypted traffic rest on the construction, not on anything OCA
  adds.
- **An active network adversary.** Forgery is caught by the Poly1305
  tag. Traffic through `oca_core` is checked on the FPGA:
  `oca_proto.sv` compares the received tag against the computed one as a
  single 128-bit equality and returns status `06` with an empty body on
  a mismatch. A caller wiring `chacha20_poly1305.sv` directly gets no
  comparison and must do its own, as section 4 item 1 requires.
- **Timing observation of the accelerator.** The time an operation takes
  reveals message and AAD lengths, and — for traffic through
  `oca_core` — whether a tag verified, which its status byte states in
  the clear on the same wire. It is independent of key, nonce, plaintext
  and ciphertext values. See section 3.

### What it is not meant to defend against

- **An adversary with physical access to the board**, in any form:
  power and electromagnetic measurement, clock or voltage glitching,
  laser fault injection, probing, chip decapsulation. See section 2.
- **An untrusted or compromised host.** The engine has no notion of
  ownership, session or privilege. Whoever can drive the interface can
  encrypt, decrypt and authenticate with whatever key they present, and
  can read back whatever the engine last produced. Isolation between
  users of the accelerator is a job for the driver and the host, and
  neither exists yet.
- **A malicious FPGA bitstream or toolchain.** The build flow neither
  signs nor encrypts the bitstream, and the MVP board loads it from a
  commodity SPI flash (W25Q64, see `BOM-MVP.md`). Anyone who can
  reprogram the device owns it.
- **Key management.** `oca_keystore.sv` holds eight slots, each with a
  loaded bit, and clears them on reset — but it is storage, not
  protection. Keys reach it in the clear, in the payload of a load-key
  packet; there is no key wrapping, no key agreement and no access
  control, so anyone who can reach the interface can overwrite any slot
  and use any slot another client loaded. A caller wiring
  `chacha20_poly1305.sv` directly has no store at all: the key arrives
  on a 256-bit input bus and stays in registers. See section 6.
- **Denial of service.** A caller that never completes a handshake
  simply stalls the engine.

## 2. Side channels

**Advanced side-channel resistance is explicitly out of scope for
v1.** Differential power analysis (DPA), simple power analysis,
electromagnetic (EM) analysis, and fault-injection attacks are not
addressed by this design, were not considered when it was written, and
must be assumed to succeed against it. There is no masking, no
randomisation of the datapath schedule, no power balancing, no
redundancy or fault detection. SPEC.md places this out of scope for v1
and this document states it as such.

The practical consequence: **do not deploy OCA where an attacker can
measure the power consumption or the EM emissions of the FPGA.** The v1
MVP is an external board on a bench or in a rack, reached over an
Ethernet cable and carrying an attached JTAG debugger (`BOM-MVP.md`),
so nothing about its form factor mitigates this: for v1 the physical
access exclusion of section 1 is accepted, not addressed. "A
host-attached accelerator inside a machine the operator controls" —
which this paragraph asserted until 2026-08-09 — describes the Phase 3
PCIe card, not the board being built.

**"Constant time" in this project means timing invariance, and nothing
more.** A reader coming from software cryptography will usually read
"constant-time" as shorthand for "side-channel resistant"; here it does
not carry that meaning. The guarantee in section 3 is that the number
of clock cycles an operation takes does not depend on secret values.
It says nothing about how much current the device draws while taking
them, and a data-independent schedule is exactly the situation DPA is
designed to attack.

## 3. What is guaranteed: data-independent latency

The latency of all three cores depends only on lengths and on the
handshakes with the caller — never on the value of a key, a nonce, a
plaintext, a ciphertext or the Poly1305 accumulator.

This was established by enumerating every state transition in the three
modules:

- **`chacha20.sv`** runs a fixed schedule: one cycle to load the state,
  `20 / ROUNDS_PER_CYCLE` round cycles counted by `round_cnt`, one
  cycle to add the initial state and XOR the data. 22 cycles per
  64-byte block at the default `ROUNDS_PER_CYCLE = 1`. The only
  transition condition that is not a counter is `start`.
- **`poly1305.sv`** runs a fixed schedule per 16-byte block: one cycle
  to add the block into the accumulator, `ceil(5 / ROWS_PER_CYCLE)`
  multiply-row cycles counted by `row`, one to drain the multiplier
  pipeline, two to normalise the carries. 9 cycles per block at the
  default `ROWS_PER_CYCLE = 1`. Its transition conditions are `start`,
  `blk`, `last` and the `row` counter. No stage iterates on the value
  of the accumulator: the carry normalisation propagates a fixed number
  of digits whatever they contain, and the final reduction mod
  2^130 - 5 is a **single conditional subtract implemented as a
  fixed-duration combinational mux** (`S_FIN2`), not a branch and not a
  loop. It costs the same cycle whether it fires or not.
- **`chacha20_poly1305.sv`** schedules the two cores. Its transition
  conditions are `start`, `in_valid`, `in_aad`, `in_last`, `in_len`,
  the `c_done` / `p_done` / `p_blk_ready` handshakes, the buffer
  occupancy flag, and the sub-block counter derived from the block
  length. `dec` selects a multiplexer input, not a path. A 64-byte
  block costs 40 cycles regardless of content.
- **`oca_proto.sv`** sits above the three and is in scope here too. The
  tag comparison is one fixed-width equality consumed in a one-cycle
  state whose two arms both fall through to the same successor, so the
  check costs the same cycle whether it passes or fails. What the
  outcome does change is the length of the response — a failed open
  answers with 8 bytes instead of 8 plus the message — and, because the
  four stages overlap, the next packet's engine start waits on that
  response being published. So the packet behind a failed open can be
  answered earlier than it would have been behind a successful one, by
  at most the difference in response length. `oca/hw/sim/test_attack.py`
  measures both ends of that bound in
  `test_tag_outcome_timing_residual_is_bounded`: exactly the difference
  for a successor cheap enough to see it, zero for one whose own message
  hides it. That signal is the pass/fail bit, which status `06` puts on
  the wire in the clear on the same segment, ahead of the response it
  shifts: it reveals nothing an observer does not already have, and it
  is a residual of the overlap, not of the comparison.

An observer who can time operations therefore learns the AAD length,
the message length and the block boundaries — which are not secret
under RFC 8439 in any case — and, through `oca_core`, the pass/fail bit
that same response states outright. Never a key, a nonce, a plaintext or
a ciphertext value.

Two caveats on how far this reaches. The property is a **design
property of the RTL**, verified by inspection and by the simulated
cycle counts, not by a formal proof; it has not been machine-checked.
And it is a property of cycle counts only, which is the point of
section 2.

## 4. Caller obligations

These are not recommendations. Ignoring any of them breaks the
construction, and the hardware cannot detect or prevent the mistake.

1. **Compare the tag in constant time — if you instantiate
   `chacha20_poly1305.sv` directly.** That module computes the tag and
   presents it on the `tag` output; there is no expected-tag input and
   no comparison anywhere in it. A caller wiring to it must compare the
   engine's `tag` with the received tag using a constant-time
   comparison — one that examines every byte and does not return
   early — and discard the plaintext on mismatch. SPEC.md requires tag
   comparison to be constant-time; this is where that requirement lands.

   **This obligation does not apply to traffic through `oca_core`.**
   `oca_proto.sv` compares the tag on the FPGA, as a single 128-bit
   equality against the 16 bytes carried in the open command — fixed-width
   combinational logic, so constant-time by construction rather than by
   the caller's discipline. Because the protocol is store and forward,
   the plaintext is complete in the transmit buffer before anything is
   sent, so a failed comparison returns status `06` and **zero bytes of
   body**.

   Two tests hold that property, and between them they cover what one
   alone did not. `test_corrupt_tag_yields_no_plaintext` in
   `oca/hw/sim/test_oca_core.py` asserts on the leak rather than on the
   status, but it flips a bit in tag byte 0, so it passes against a
   comparison only eight bits wide. `test_every_tag_byte_is_compared`
   in the same file flips one bit per tag byte, all sixteen of them, and
   opens the intact tag afterwards so that a comparison stuck at false
   is not mistaken for a working one; a 120-bit comparison that drops
   tag byte 7 passes every other test in both suites and fails this one.
   `oca/hw/sim/run_proto_gate.py` replays it on the synthesised netlist,
   because the comparison is combinational and nothing in the synthesis
   flow can otherwise see whether it survived the mapper.

2. **Never repeat a (key, nonce) pair. The core cannot detect reuse.**
   The engine keeps no history: `key` and `nonce` are latched on
   `start` and used as given. Reusing a pair breaks confidentiality —
   the same keystream encrypts two messages — and breaks authentication
   as well, because the Poly1305 one-time key (r, s) is derived from
   the same ChaCha20 block. Two messages authenticated under the same
   (r, s) let an attacker solve for r and forge tags at will.

3. **`start` is level-sensitive, not edge-triggered.** All three cores
   test `start` as a level in their idle state. Holding it high does
   not queue one operation: as soon as the engine returns to idle it
   begins another. With `key` and `nonce` unchanged that regenerates
   the same one-time (r, s) and emits a second MAC under it — case 2
   above, self-inflicted. Pulse `start` for one cycle and drop it.

4. **Respect the message length limit.** RFC 8439's AEAD construction
   uses ChaCha20 block counter 0 to derive the Poly1305 key and
   counters 1 upwards for the message, so one message under a given
   (key, nonce) may not exceed 2^32 - 1 blocks — 274,877,906,880 bytes,
   about 256 GiB. **The engine does not enforce this**; see limitation
   1.

5. **On decryption, treat the output as unverified until the tag
   checks out.** The engine streams recovered plaintext on `out_valid`
   while the message is still being authenticated; `done` and the tag
   arrive afterwards. The caller must buffer that plaintext and release
   it only after a successful constant-time comparison, or accept the
   consequences of releasing unauthenticated data.

## 5. Known limitations

Each of these is a property of the RTL on this branch, verified against
it. They are listed rather than hidden because a caller cannot work
around a limitation it has not been told about.

1. **The ChaCha20 block counter is 32 bits with no guard.** In
   `chacha20_poly1305.sv` the counter `ctr` is a 32-bit register,
   initialised to 1 and incremented per block, with nothing that
   detects the wrap. Past 2^32 - 1 message blocks it rolls over to 0 —
   which is the counter used to derive the Poly1305 one-time key. That
   block's keystream would then encrypt message data, exposing r and s
   to anyone who can guess or influence the corresponding plaintext,
   and forgery follows immediately. The limit coincides exactly with
   RFC 8439's, so a caller that respects the RFC never reaches it, but
   the engine will not stop a caller that does not. Enforcing it is
   deferred to the host interface, which does not exist yet.

2. **There is no `tag_valid`, and `done` is a single-cycle pulse.** The
   `tag` output is a register that holds the *previous* message's tag
   until a new one completes. Reset clears it (limitation 4), but
   nothing else does: not a new message starting, not a message being
   aborted. Callers must
   latch `tag` on the cycle `done` pulses. Polling `tag` and assuming
   it belongs to the message in flight will return a stale tag from an
   earlier message — which is both a correctness bug and a way to
   accept a forgery.

3. **`out_data` beyond `out_len` carries keystream XORed with whatever
   the caller drove into the padding bytes.** Nothing masks the input:
   `chacha20_poly1305.sv` latches the block whole (`c_data_in <=
   in_data`) and `chacha20.sv` XORs all sixteen words of it, so the
   bytes past `in_len` reach the XOR. The only surviving mask is
   `mask_sub`, applied to the 16-byte sub-block on its way into
   `poly1305.sv`, which is where RFC 8439 requires the padding to be
   zero and where the masking therefore stops. A caller that zero-pads
   reads back the raw keystream for that (key, nonce, counter); a
   caller that leaves anything else there reads that keystream XORed
   with its own bytes, so the remainder carries both — whatever sat in
   the padding leaves the engine together with the keystream masking
   it. Callers must truncate the output to `out_len` and must never
   transmit or store the remainder: it is key material for a counter
   block that may still be in use.

4. **Secrets are cleared on reset, in the flip-flops.** All three
   modules zeroise their secret state when `rst_n` is asserted:
   `chacha20.sv` clears `st`, `st_init` and `data_out`; `poly1305.sv`
   clears r, the precomputed 5r, s, the accumulator, every intermediate
   derived from them, the registered products and the tag;
   `chacha20_poly1305.sv` clears `key_r`, `nonce_r`, the derived
   Poly1305 key `p_key`, `c_data_in`, `mac_buf`, `out_data` and
   `p_data_in`. 5866 bits per core, named one by one and checked one by
   one: `hw/sim/test_secret_zeroise.py` proves each register holds a
   secret *before* the reset and zero after, because a test that only
   looked after the reset would pass on a core that was never loaded.

   The packet buffers are block RAM and cannot be cleared the same way;
   they are walked instead, one word per cycle, and section 6 gives
   that.

   Two things this does not reach. **223 bits of metadata survive**, in
   registers holding no key material but describing the last message:
   `aad_len`, `ct_len`, `ctr`, `c_counter`, `out_len`, `cur_len`,
   `mac_len`, `sub_idx` and four flags in `chacha20_poly1305.sv`, plus
   `row` and `last_r` in `poly1305.sv`. None is read before it is
   written, so nothing malfunctions; what they leak after a reset is the
   exact size of the last AAD and ciphertext. And clearing is only as
   good as the reset: nothing zeroises on power loss, and a design that
   stops being clocked keeps whatever it held.

5. **An illegal `in_len` aborts the message.** `in_len` above 64 does
   not fit the 64-byte datapath and cannot be terminated by the MAC
   FSM, so presenting one raises the `err` output. The recovery
   contract is: the message is abandoned on the spot — `busy` drops,
   `in_ready` drops, `done` never pulses, and **no tag and no further
   ciphertext** are produced for it. `err` is sticky and stays high
   until the next `start`, which clears it and begins a fresh message.
   While `err` is high the Poly1305 core is held in reset, so the
   abandoned message's one-time key and accumulator cannot leak into
   the next one. A caller that ignores `err` loses the message, never
   the engine — but it will wait forever for a `done` that is not
   coming, so `err` must be monitored alongside `done`.

   **The blocks accepted before the illegal one have already left the
   engine, and that governs how a message may be retried.** `len_bad`
   is evaluated in `S_ACCEPT` only, so a message aborted at block *N*
   has already pulsed `out_valid` with the ciphertext of blocks 1 to
   *N*−1. This entry read "no ciphertext or tag is produced for it"
   until 2026-08-09, which invited the reading that an aborted message
   left nothing behind. **A retry after an abort must use a fresh
   nonce.** Retrying under the same (key, nonce) with any difference in
   the plaintext — a corrected length, different padding, a
   re-serialised record — puts two plaintexts under one keystream,
   which is the failure item 2 of section 4 exists to prevent. Nothing
   in the engine enforces this; it is the caller's obligation, and the
   protocol layer above does not retry on its own.

## 6. The host protocol: what it exposes

`oca_core` turns a UDP payload into an AEAD operation
(`docs/design/2026-08-03-host-protocol.md`). Three things it does
**not** protect, recorded here because they are properties of the
protocol rather than of the engine:

1. **The key crosses the wire in the clear.** The load-key command
   carries 32 raw key bytes. It happens once per key rather than once
   per packet, but there is no key wrapping, no key agreement and no
   transport encryption: anyone who can observe that network segment
   reads the key, and from then on holds everything it protects.

2. **There is no authentication of the requester.** Anyone who can send
   a UDP packet to the board can seal and open with whatever slots are
   loaded, and can overwrite any slot with a key of their own. The
   accelerator trusts whoever talks to it, exactly as a PCIe
   accelerator trusts its host — except that an Ethernet cable is far
   easier to reach than a PCIe slot. **The intended deployment is a
   direct host-to-board link, not a shared network.** There is no
   session, no ownership and no privilege: the isolation between users
   noted in section 1 is absent here too. No command reads a key back —
   the four opcodes are load-key, seal, open and stats — but a slot
   loaded by one client is *usable* by any other, which for an attacker
   is as good as holding the key.

3. **The nonce comes from the host.** `oca_proto` passes the 12 nonce
   bytes of the request to the engine as given. Reuse of a (key, nonce)
   pair remains a host error the hardware cannot detect, with the
   consequences set out in section 4 item 2 — and the protocol widens
   the blast radius, because the host that must get it right is now
   anyone on the wire rather than a local driver.

What the protocol layer does add, against the engine alone:

- **Key slots carry a loaded bit and are cleared on reset.** Using a
  slot that was never written returns status `04` rather than
  encrypting under a key of zeros, and `rst_n` clears both the key
  material and the loaded bits. `oca_proto.sv` clears its own secret
  registers too — `eng_key`, `ks_wr_key`, the block being assembled,
  the block draining back, the parsed arguments and the received tag
  are all zeroed on reset.

  **The packet buffers are walked, not reset.** `oca_pktbuf.sv` holds
  4096 bytes each — two packets each, in two banks: plaintext,
  ciphertext, and for a load-key command the 32 raw key bytes, which
  necessarily pass through the receive buffer on their way to the slot.
  Block RAM has no reset on its contents, so the array is zeroed one
  word per cycle out of reset: 512 words, 512 cycles, about 10 µs at
  50 MHz, once. A loop over the array in the reset branch would be a
  512-way simultaneous write that no memory primitive expresses, and
  yosys would answer it by lowering both buffers to 65536 flip-flops —
  which is why the clear and the writer meet in a multiplexer and the
  array keeps its single write port.

  While the walk runs the buffer's writer is inert and `clr_busy` is
  high, and `oca_core` masks **both halves** of the request handshake on
  it. Masking only the outgoing `tready` is not enough and the suite
  proves it: AXI-Stream forbids a master from waiting for `tready`
  before asserting `tvalid`, so a request can sit on the bus from the
  cycle reset ends, and `oca_proto`'s receive stage — which reaches its
  accepting state one cycle after reset — consumes beats the master
  never saw taken. With only `tready` gated, 24 of 29 core tests fail.
- **The tag is compared on the FPGA in constant time, and a failure
  emits no plaintext.** See section 4 item 1.
- **Failures are reported rather than dropped silently.** Every request
  whose header can be read is answered with a status code, and the
  packets too malformed to answer increment a counter readable through
  the stats command.

Neither the load-key exposure nor the missing authentication is fixable
inside this protocol; both need a transport that does not exist yet.
Until it does, treat the link between host and board as part of the
trust boundary.

## 7. Reporting a vulnerability

Report security issues privately by email to Gennaro Cimmino
<gcimmino@rayonra.net> rather than opening a public issue on
<https://github.com/l0rdg3x/OpenCryptoAccelerator>. Please allow time
for a fix before public disclosure.

**The hardware has had no third-party security audit.** The cores have
been verified for functional correctness against RFC 8439's official
test vectors and against randomised messages, and reviewed internally;
that is not an audit, and none is planned for v1. Treat OCA as
pre-release research hardware.
