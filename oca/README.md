# OpenCrypto Accelerator (OCA)

## Phase 2: FPGA cores (in progress)

- `hw/rtl/chacha20.sv` — ChaCha20 stream cipher core (RFC 8439):
  one 64-byte block per `start` pulse, little-endian bus layout
  documented in the header. The 20 rounds alternate a column round and a
  diagonal round over one state register; `ROUNDS_PER_CYCLE` chooses how
  many rounds a cycle covers, trading combinational path against cycle
  count. At the default 1 a block costs 22 cycles; 2 restores the
  original 12 cycles and its longer path.
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
  caller compares the tag.
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
  2.8.2 decrypt round-trip.
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
```

Current status: chacha20 5/5 tests pass, poly1305 4/4 tests pass, AEAD
3/3 tests pass; `verilator --lint-only -Wall` clean on all cores.
Two datapath reworks are done. The Poly1305 limb rework took the AEAD
engine from 65 to 20 ECP5 multipliers (90% -> 28% of an LFE5U-45F) and
more than doubled the standalone Poly1305 Fmax (22.94 -> 52.68 MHz).
The ChaCha20 round-per-cycle rework then raised its standalone Fmax
28.66 -> 53.11 MHz, so the two cores are now balanced, and AEAD Fmax
26.10 -> 37.87 MHz (+41% over the 26.77 MHz baseline). A 64-byte block
costs 57 cycles, measured in simulation, so throughput is ~0.34 Gbps:
above the ~0.28 Gbps of the previous state but still 28% below the
~0.47 Gbps baseline, because the cycle count grew faster than the
clock. The critical path is now in neither core but in the AEAD
wrapper's `mask_bytes()` (a 512-bit subtract), and the two phases still
run strictly in sequence — overlapping them is the next step
(`hw/syn/README.md`).

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
