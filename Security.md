# Security — OpenCrypto Accelerator

Threat scope and side-channel limits of the OCA hardware, as required by
`SPEC.md` (sections CONSTRAINTS and DOCUMENTATION).

This document describes the RTL cores as they stand on this branch:
`oca/hw/rtl/chacha20.sv`, `oca/hw/rtl/poly1305.sv` and
`oca/hw/rtl/chacha20_poly1305.sv`. It is not a statement about a
finished product: there is no host interface, no driver and no board
yet, and the parts that do not exist cannot be assessed here. Every
claim below was checked against the RTL; where a property is a caller
obligation rather than something the hardware enforces, it says so.

## 1. Threat model

### What the hardware is meant to defend against

- **A passive network adversary.** The cores implement
  AEAD_CHACHA20_POLY1305 exactly as RFC 8439 specifies it, verified
  against the RFC's own test vectors and against randomised messages
  compared with an independent model. Confidentiality and integrity of
  the encrypted traffic rest on the construction, not on anything OCA
  adds.
- **An active network adversary.** Forgery is caught by the Poly1305
  tag — provided the caller compares it as section 4 requires, because
  the RTL does not compare it.
- **Timing observation of the accelerator.** The time an operation
  takes reveals message and AAD lengths, and nothing else: latency is
  independent of key, nonce, plaintext and ciphertext values. See
  section 3.

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
- **Key management.** Keys arrive on a 256-bit input bus in the clear
  and stay in registers. There is no key store, no key wrapping and no
  access control.
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
measure the power consumption or the EM emissions of the FPGA.** The
intended v1 deployment is a host-attached accelerator inside a machine
the operator controls.

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

An observer who can time operations therefore learns the AAD length,
the message length and the block boundaries — which are not secret
under RFC 8439 in any case — and learns nothing else.

Two caveats on how far this reaches. The property is a **design
property of the RTL**, verified by inspection and by the simulated
cycle counts, not by a formal proof; it has not been machine-checked.
And it is a property of cycle counts only, which is the point of
section 2.

## 4. Caller obligations

These are not recommendations. Ignoring any of them breaks the
construction, and the hardware cannot detect or prevent the mistake.

1. **Compare the tag in constant time. The RTL does not compare it.**
   `chacha20_poly1305.sv` computes the tag and presents it on the `tag`
   output; there is no expected-tag input and no comparison anywhere in
   the RTL. On decryption the caller must compare the engine's `tag`
   with the received tag using a constant-time comparison — one that
   examines every byte and does not return early — and discard the
   plaintext on mismatch. SPEC.md requires tag comparison to be
   constant-time; this is where that requirement lands.

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
   until a new one completes; it is not cleared on reset, nor when a
   new message starts, nor when a message is aborted. Callers must
   latch `tag` on the cycle `done` pulses. Polling `tag` and assuming
   it belongs to the message in flight will return a stale tag from an
   earlier message — which is both a correctness bug and a way to
   accept a forgery.

3. **`out_data` beyond `out_len` carries raw keystream.** On a partial
   final block the engine masks the *input* to `in_len` bytes before
   the XOR, so the unused high bytes of `out_data` come out as
   plaintext keystream for that (key, nonce, counter). Callers must
   truncate the output to `out_len` and must never transmit or store
   the remainder: it is key material for a counter block that may still
   be in use.

4. **Secrets are not cleared on reset.** None of the three modules
   zeroises its secret state when `rst_n` is asserted. After a reset,
   `chacha20.sv` still holds the key and nonce inside `st` and
   `st_init` and the last block in `data_out`; `poly1305.sv` still
   holds the clamped r, the precomputed 5r, s, the accumulator and the
   tag; `chacha20_poly1305.sv` still holds `key_r`, `nonce_r`, the
   derived Poly1305 key `p_key`, the buffered block and `out_data`.
   Reset restores control state, not confidentiality. Anything that can
   read the fabric after a reset — readback, a subsequent bitstream, a
   later design sharing the device — can recover them.

5. **An illegal `in_len` aborts the message.** `in_len` above 64 does
   not fit the 64-byte datapath and cannot be terminated by the MAC
   FSM, so presenting one raises the `err` output. The recovery
   contract is: the message is abandoned on the spot — `busy` drops,
   `in_ready` drops, `done` never pulses, and no ciphertext or tag is
   produced for it. `err` is sticky and stays high until the next
   `start`, which clears it and begins a fresh message. While `err` is
   high the Poly1305 core is held in reset, so the abandoned message's
   one-time key and accumulator cannot leak into the next one. A caller
   that ignores `err` loses the message, never the engine — but it will
   wait forever for a `done` that is not coming, so `err` must be
   monitored alongside `done`.

## 6. Reporting a vulnerability

Report security issues privately by email to Gennaro Cimmino
<gcimmino@rayonra.net> rather than opening a public issue on
<https://github.com/l0rdg3x/OpenCryptoAccelerator>. Please allow time
for a fix before public disclosure.

**The hardware has had no third-party security audit.** The cores have
been verified for functional correctness against RFC 8439's official
test vectors and against randomised messages, and reviewed internally;
that is not an audit, and none is planned for v1. Treat OCA as
pre-release research hardware.
