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
  of them (default 8). The only place key material lives. Each slot
  carries a loaded bit, so reading a slot that was never written reports
  `rd_valid = 0` instead of handing back a key of zeros: a host mistake
  becomes a protocol error rather than a message encrypted under a key
  an attacker can guess. Keys and loaded bits are cleared on reset.
- `hw/rtl/oca_pktbuf.sv` — one byte-wide packet buffer (default 2048
  bytes), written sequentially from the stream and read at random
  offsets. Writes past capacity are dropped and `wr_full` is raised, so a
  truncated packet becomes a length error rather than a silent wrap. The
  read port is registered and the range check sits on the address rather
  than the read data, which is what makes the memory infer block RAM —
  confirmed in synthesis, one DP16KD per buffer and no LUT RAM.
- `hw/rtl/oca_proto.sv` — the protocol FSM: parses the fixed 8-byte
  header, validates it, drives the engine and builds the response. Knows
  nothing of cryptography beyond driving `chacha20_poly1305` and
  comparing its tag — a single 128-bit equality, constant-time by
  construction. Store and forward, so a failed tag returns status `06`
  and no plaintext at all.
- `hw/rtl/oca_core.sv` — wiring only: the two packet buffers, the key
  store, the protocol FSM and the AEAD engine behind a pair of 8-bit
  AXI-Stream ports. This is the module the Ethernet integration will
  instantiate.
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
  neither the ciphertext nor the tag may move. The suite above cannot
  see this — it zero-pads, so it passes with the engine's input masking
  removed entirely — which is why the masking has a test of its own.
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
  capacity without wrapping over what is already stored.
- `hw/sim/test_oca_core.py` — end-to-end: packets in, packets out, with
  no Ethernet in the simulation. Load-key then seal, a seal/open round
  trip, an unloaded slot refused, every header failure and every length
  failure, and 20 randomised round trips judged by the model. The one
  that matters is `test_corrupt_tag_yields_no_plaintext`, which asserts
  that a flipped tag bit yields status `06` and **zero bytes of body** —
  it asserts on the leak, not on the status, and was checked against a
  deliberately broken tag comparison. Without it the design's security
  property would be written down rather than held.
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
.venv/bin/python hw/sim/run_keystore.py
.venv/bin/python hw/sim/run_pktbuf.py
.venv/bin/python hw/sim/run_oca_core.py
cd hw/sim && ../../.venv/bin/python test_proto_model.py
```

Current status: chacha20 5/5 tests pass, poly1305 4/4 tests pass, AEAD
7/7 tests pass, dirty-padding 2/2, keystore 4/4, pktbuf 3/3, oca_core
9/9, and the protocol model checks pass as plain Python;
`verilator --lint-only -Wall` clean on all cores with `--top-module
oca_core`. Five reworks are done. The Poly1305 limb rework took the AEAD
engine from 65 to 20 ECP5 multipliers (90% -> 28% of an LFE5U-45F) and
more than doubled the standalone Poly1305 Fmax (22.94 -> 52.68 MHz). The
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
headroom for the GbE MAC, packet buffering and top-level glue that do not
exist yet, none of which is in these out-of-context numbers. Fmax is not
claimed either: over four placer seeds the engine means 50.72 -> 52.83
MHz while the standalone core shows no effect at all, and the critical
path is structurally the same quarter round it was. Three engines fit,
for 1.97-2.07 Gbps aggregate over those seeds, which straddles the
>= 2 Gbps MVP target (`hw/syn/README.md`).

**The host protocol is implemented and verified**
(`docs/design/2026-08-03-host-protocol.md`): a UDP payload in, an AEAD
operation, a payload out, with no Ethernet in the simulation. `oca_core`
synthesises to **11149 LUTs (25.4%), 10842 FF (24.7%), 20 MULT18X18D
(27.8%) and 2 DP16KD (1.9%)** at **50.95 MHz** (seed 1; 50.59 MHz mean
over seeds 1-4). **Both packet buffers infer block RAM** — one DP16KD
each, and not a single LUT RAM cell in the netlist — which was the open
question, because 4096 bytes in LUTs would have been a serious area
regression. The protocol layer adds no multipliers at all, so it does
not cost an engine, and it is not on the critical path: every entry in
nextpnr's report still cites `chacha20.sv`.

Throughput is the honest disappointment. Measured differentially in
simulation, a 64-byte block costs **415 cycles end to end** — 64 to
receive at a byte per cycle, 159 through buffer, engine and buffer, and
**192 to transmit at three cycles per byte** — which at the measured
clock is **~0.062 Gbps: 9% of the engine's own ~0.68 Gbps and 6% of the
GbE link**. The implementation plan predicted 168 cycles and was 2.5x
optimistic, having modelled only the middle term and omitted both stream
transfers. The response path is the largest single cost and is a
handshake rather than a bandwidth limit, so it is also the cheapest
thing to fix: holding `m_tvalid` up and pipelining the buffer read, as
the receive side already does, would take a block to 287 cycles and
throughput to ~0.090 Gbps. Widening the buffers to 32 bits and
overlapping the phases is the structural fix. **None of this has run on
silicon.**

Next: the Ethernet integration, which needs the board —
`verilog-ethernet` (MIT) as a submodule, the RGMII wrapper with its ECP5
DDR primitives, PLL, reset and the Colorlight i9 pin constraints.

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
