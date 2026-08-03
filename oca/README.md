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
```

Current status: chacha20 5/5 tests pass, poly1305 4/4 tests pass, AEAD
7/7 tests pass, dirty-padding 2/2; `verilator --lint-only -Wall` clean on
all cores. Five reworks are done. The Poly1305 limb rework took the AEAD
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
path is structurally the same quarter round it was. Next: replicate the
engine — three fit for 1.97-2.07 Gbps aggregate over those seeds, which
straddles the >= 2 Gbps MVP target (`hw/syn/README.md`).

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
