# OpenCrypto Accelerator (OCA)

## Phase 2: FPGA cores (in progress)

- `hw/rtl/chacha20.sv` — ChaCha20 stream cipher core (RFC 8439):
  one 64-byte block per `start` pulse, 12-cycle latency
  (2 rounds/cycle), little-endian bus layout documented in the header.
- `hw/rtl/poly1305.sv` — Poly1305 one-time authenticator (RFC 8439):
  `start` loads the one-time key, then 16-byte blocks via `blk`/`last`,
  3 cycles per block. r and s must be single-use (see header).
- `hw/rtl/chacha20_poly1305.sv` — AEAD_CHACHA20_POLY1305 engine
  (RFC 8439 2.8) combining the two cores: derives the Poly1305 key
  internally (ChaCha20 block, counter 0), encrypts with counter 1+,
  MACs aad‖pad‖ct‖pad‖le64(|aad|)‖le64(|ct|). Streaming interface:
  64-byte input blocks (AAD then plaintext), ciphertext blocks out,
  then the tag. `dec=1` decrypts: MACs the input (ciphertext) blocks,
  caller compares the tag.
- `hw/sim/test_chacha20.py` — cocotb testbench; vectors parsed from the
  same `tests/vectors/sources/rfc8439.txt` as the software tests
  (2.3.2 block function, 2.4.2 two-block encryption, decrypt round-trip).
- `hw/sim/test_poly1305.py` — 2.5.2 (partial final block) + all four
  A.3 MAC vectors (zero key, r=0, s=0, general case).
- `hw/sim/test_chacha20_poly1305.py` — 2.8.2 encryption, A.5 decryption,
  2.8.2 decrypt round-trip.
- `hw/sim/run_*.py` — run the tests under the project-local Verilator
  (`../tools/verilator`, built from source, branch `stable`).

Requirements: `../tools/verilator` built, `oca/.venv` with cocotb
(installed from cocotb git master — release 2.0.1 does not support
Python 3.14).

```sh
.venv/bin/python hw/sim/run_chacha20.py
.venv/bin/python hw/sim/run_poly1305.py
.venv/bin/python hw/sim/run_chacha20_poly1305.py
```

Current status: chacha20 3/3 tests pass, poly1305 5/5 vectors pass,
AEAD 3/3 tests pass; `verilator --lint-only -Wall` clean on all cores.

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
