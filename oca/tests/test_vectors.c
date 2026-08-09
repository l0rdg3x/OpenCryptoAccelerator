/* SPDX-License-Identifier: MIT */
/* Known-answer tests against official test vectors (see vectors.h). */
#include <oca/oca.h>
#include "vectors/vectors.h"

#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int failures;
static int passed;

#define MAX_BUF 4096

static void hexdiff(const char *what, const uint8_t *got,
                    const uint8_t *want, size_t len)
{
    fprintf(stderr, "  %s mismatch:\n    got:  ", what);
    for (size_t i = 0; i < len; i++)
        fprintf(stderr, "%02x", got[i]);
    fprintf(stderr, "\n    want: ");
    for (size_t i = 0; i < len; i++)
        fprintf(stderr, "%02x", want[i]);
    fprintf(stderr, "\n");
}

static void run_aead(const oca_ctx *ctx, oca_aead_alg alg, size_t want_key_len,
                     const oca_vec_aead *v, size_t n)
{
    uint8_t out[MAX_BUF], tag[16], dec[MAX_BUF];
    for (size_t i = 0; i < n; i++) {
        if (!v[i].expect_ok)
            continue;  /* negative vectors are handled separately */
        if (want_key_len && v[i].key_len != want_key_len)
            continue;  /* AES-128 and AES-256 share one vector array */
        if (v[i].pt_len > MAX_BUF) {
            fprintf(stderr, "  vector %s too large for test buffer\n", v[i].name);
            failures++;
            continue;
        }
        int rc = oca_aead_encrypt(ctx, alg, v[i].key, v[i].key_len,
                                  v[i].nonce, v[i].nonce_len,
                                  v[i].aad, v[i].aad_len,
                                  v[i].pt, v[i].pt_len, out, tag, sizeof(tag));
        if (rc != OCA_OK) {
            fprintf(stderr, "FAIL %s: encrypt rc=%d (%s)\n",
                    v[i].name, rc, oca_strerror(rc));
            failures++;
            continue;
        }
        if (memcmp(out, v[i].ct, v[i].pt_len) != 0) {
            fprintf(stderr, "FAIL %s:\n", v[i].name);
            hexdiff("ciphertext", out, v[i].ct, v[i].pt_len);
            failures++;
            continue;
        }
        if (memcmp(tag, v[i].tag, 16) != 0) {
            fprintf(stderr, "FAIL %s:\n", v[i].name);
            hexdiff("tag", tag, v[i].tag, 16);
            failures++;
            continue;
        }
        rc = oca_aead_decrypt(ctx, alg, v[i].key, v[i].key_len,
                              v[i].nonce, v[i].nonce_len,
                              v[i].aad, v[i].aad_len,
                              v[i].ct, v[i].pt_len, v[i].tag, 16, dec);
        if (rc != OCA_OK) {
            fprintf(stderr, "FAIL %s: decrypt rc=%d (%s)\n",
                    v[i].name, rc, oca_strerror(rc));
            failures++;
            continue;
        }
        if (memcmp(dec, v[i].pt, v[i].pt_len) != 0) {
            fprintf(stderr, "FAIL %s:\n", v[i].name);
            hexdiff("plaintext", dec, v[i].pt, v[i].pt_len);
            failures++;
            continue;
        }
        passed++;
    }
}

/* Vectors marked expect_ok=0 must fail decryption with OCA_ERR_AUTH. */
static void run_aead_negative(const oca_ctx *ctx, oca_aead_alg alg, size_t want_key_len,
                              const oca_vec_aead *v, size_t n)
{
    uint8_t dec[MAX_BUF];
    for (size_t i = 0; i < n; i++) {
        if (v[i].expect_ok)
            continue;
        if (want_key_len && v[i].key_len != want_key_len)
            continue;
        if (v[i].pt_len > MAX_BUF)
            continue;
        int rc = oca_aead_decrypt(ctx, alg, v[i].key, v[i].key_len,
                                  v[i].nonce, v[i].nonce_len,
                                  v[i].aad, v[i].aad_len,
                                  v[i].ct, v[i].pt_len, v[i].tag, 16, dec);
        if (rc != OCA_ERR_AUTH) {
            fprintf(stderr, "FAIL %s: expected auth failure, got rc=%d\n",
                    v[i].name, rc);
            failures++;
            continue;
        }
        passed++;
    }
}

static void run_mac(const oca_ctx *ctx, oca_mac_alg alg,
                    const oca_vec_mac *v, size_t n, size_t out_len)
{
    uint8_t out[32];
    for (size_t i = 0; i < n; i++) {
        int rc = oca_mac(ctx, alg, v[i].key, v[i].key_len,
                         v[i].in, v[i].in_len, out, out_len);
        if (rc != OCA_OK) {
            fprintf(stderr, "FAIL %s: mac rc=%d (%s)\n",
                    v[i].name, rc, oca_strerror(rc));
            failures++;
            continue;
        }
        if (memcmp(out, v[i].out, v[i].out_len) != 0) {
            fprintf(stderr, "FAIL %s:\n", v[i].name);
            hexdiff("mac", out, v[i].out, v[i].out_len);
            failures++;
            continue;
        }
        passed++;
    }
}

static void run_hash(const oca_ctx *ctx, oca_hash_alg alg,
                     const oca_vec_hash *v, size_t n)
{
    uint8_t out[32];
    for (size_t i = 0; i < n; i++) {
        int rc = oca_hash(ctx, alg, v[i].in, v[i].in_len, out, v[i].out_len);
        if (rc != OCA_OK) {
            fprintf(stderr, "FAIL %s: hash rc=%d (%s)\n",
                    v[i].name, rc, oca_strerror(rc));
            failures++;
            continue;
        }
        if (memcmp(out, v[i].out, v[i].out_len) != 0) {
            fprintf(stderr, "FAIL %s:\n", v[i].name);
            hexdiff("digest", out, v[i].out, v[i].out_len);
            failures++;
            continue;
        }
        passed++;
    }
}

/* Tampering with one tag byte must always produce OCA_ERR_AUTH. */
static void run_tamper(const oca_ctx *ctx)
{
    const oca_vec_aead *v = &vecs_chacha20_poly1305[0];
    uint8_t tag[16], dec[MAX_BUF];
    memcpy(tag, v->tag, 16);
    tag[0] ^= 0x01;
    int rc = oca_aead_decrypt(ctx, OCA_AEAD_CHACHA20_POLY1305,
                              v->key, v->key_len, v->nonce, v->nonce_len,
                              v->aad, v->aad_len, v->ct, v->pt_len,
                              tag, 16, dec);
    if (rc == OCA_ERR_AUTH) {
        passed++;
    } else {
        fprintf(stderr, "FAIL tamper-test: expected auth failure, got rc=%d\n", rc);
        failures++;
    }
}

/* A NULL pointer with a non-zero length must be rejected, never
 * silently treated as empty: aad = NULL with aad_len > 0 used to
 * produce a valid tag covering no AAD at all. The NULL, 0 pair must
 * stay legal. */
static void run_bad_args(const oca_ctx *ctx)
{
    const oca_vec_aead *v = &vecs_chacha20_poly1305[0];
    uint8_t out[MAX_BUF], tag[16];
    size_t huge_len = (size_t)INT_MAX + 1;
    int rc = oca_aead_encrypt(ctx, OCA_AEAD_CHACHA20_POLY1305,
                              v->key, v->key_len, v->nonce, v->nonce_len,
                              NULL, 5, v->pt, v->pt_len, out, tag, sizeof(tag));
    if (rc == OCA_ERR_INVALID_ARG) {
        passed++;
    } else {
        fprintf(stderr, "FAIL bad-args: encrypt aad=NULL, aad_len=5 gave rc=%d\n", rc);
        failures++;
    }
    rc = oca_aead_decrypt(ctx, OCA_AEAD_CHACHA20_POLY1305,
                          v->key, v->key_len, v->nonce, v->nonce_len,
                          NULL, 5, v->ct, v->pt_len, v->tag, 16, out);
    if (rc == OCA_ERR_INVALID_ARG) {
        passed++;
    } else {
        fprintf(stderr, "FAIL bad-args: decrypt aad=NULL, aad_len=5 gave rc=%d\n", rc);
        failures++;
    }
    rc = oca_aead_encrypt(ctx, OCA_AEAD_CHACHA20_POLY1305,
                          v->key, v->key_len, v->nonce, v->nonce_len,
                          NULL, 0, v->pt, v->pt_len, out, tag, sizeof(tag));
    if (rc == OCA_OK) {
        passed++;
    } else {
        fprintf(stderr, "FAIL bad-args: encrypt aad=NULL, aad_len=0 gave rc=%d\n", rc);
        failures++;
    }

    /* key and nonce must never be NULL: AEAD key/nonce sizes are
     * fixed, so unlike aad there is no NULL-with-zero-length case
     * to keep legal.
     *
     * These four cannot fail while the software backend is the only
     * one: it rejects the same inputs itself, so deleting the guard in
     * oca.c leaves them green. They pin the public contract for the
     * backend that does not repeat the check, and become load-bearing
     * then. Measured, not assumed: with the boundary guard removed the
     * suite still passes 126/126. */
    rc = oca_aead_encrypt(ctx, OCA_AEAD_CHACHA20_POLY1305,
                          NULL, v->key_len, v->nonce, v->nonce_len,
                          v->aad, v->aad_len, v->pt, v->pt_len, out, tag, sizeof(tag));
    if (rc == OCA_ERR_INVALID_ARG) {
        passed++;
    } else {
        fprintf(stderr, "FAIL bad-args: encrypt key=NULL gave rc=%d\n", rc);
        failures++;
    }
    rc = oca_aead_encrypt(ctx, OCA_AEAD_CHACHA20_POLY1305,
                          v->key, v->key_len, NULL, v->nonce_len,
                          v->aad, v->aad_len, v->pt, v->pt_len, out, tag, sizeof(tag));
    if (rc == OCA_ERR_INVALID_ARG) {
        passed++;
    } else {
        fprintf(stderr, "FAIL bad-args: encrypt nonce=NULL gave rc=%d\n", rc);
        failures++;
    }
    rc = oca_aead_decrypt(ctx, OCA_AEAD_CHACHA20_POLY1305,
                          NULL, v->key_len, v->nonce, v->nonce_len,
                          v->aad, v->aad_len, v->ct, v->pt_len, v->tag, 16, out);
    if (rc == OCA_ERR_INVALID_ARG) {
        passed++;
    } else {
        fprintf(stderr, "FAIL bad-args: decrypt key=NULL gave rc=%d\n", rc);
        failures++;
    }
    rc = oca_aead_decrypt(ctx, OCA_AEAD_CHACHA20_POLY1305,
                          v->key, v->key_len, NULL, v->nonce_len,
                          v->aad, v->aad_len, v->ct, v->pt_len, v->tag, 16, out);
    if (rc == OCA_ERR_INVALID_ARG) {
        passed++;
    } else {
        fprintf(stderr, "FAIL bad-args: decrypt nonce=NULL gave rc=%d\n", rc);
        failures++;
    }

    /* A length above INT_MAX must be rejected before the backend
     * casts it to int. The guard compares the length alone, so
     * pairing a real small buffer with a bogus huge length is safe:
     * the pointer is never dereferenced past the guard. */
    rc = oca_aead_encrypt(ctx, OCA_AEAD_CHACHA20_POLY1305,
                          v->key, v->key_len, v->nonce, v->nonce_len,
                          v->aad, huge_len, v->pt, v->pt_len, out, tag, sizeof(tag));
    if (rc == OCA_ERR_INVALID_ARG) {
        passed++;
    } else {
        fprintf(stderr, "FAIL bad-args: encrypt aad_len>INT_MAX gave rc=%d\n", rc);
        failures++;
    }
    rc = oca_aead_encrypt(ctx, OCA_AEAD_CHACHA20_POLY1305,
                          v->key, v->key_len, v->nonce, v->nonce_len,
                          v->aad, v->aad_len, v->pt, huge_len, out, tag, sizeof(tag));
    if (rc == OCA_ERR_INVALID_ARG) {
        passed++;
    } else {
        fprintf(stderr, "FAIL bad-args: encrypt in_len>INT_MAX gave rc=%d\n", rc);
        failures++;
    }
    rc = oca_aead_decrypt(ctx, OCA_AEAD_CHACHA20_POLY1305,
                          v->key, v->key_len, v->nonce, v->nonce_len,
                          v->aad, huge_len, v->ct, v->pt_len, v->tag, 16, out);
    if (rc == OCA_ERR_INVALID_ARG) {
        passed++;
    } else {
        fprintf(stderr, "FAIL bad-args: decrypt aad_len>INT_MAX gave rc=%d\n", rc);
        failures++;
    }
    rc = oca_aead_decrypt(ctx, OCA_AEAD_CHACHA20_POLY1305,
                          v->key, v->key_len, v->nonce, v->nonce_len,
                          v->aad, v->aad_len, v->ct, huge_len, v->tag, 16, out);
    if (rc == OCA_ERR_INVALID_ARG) {
        passed++;
    } else {
        fprintf(stderr, "FAIL bad-args: decrypt in_len>INT_MAX gave rc=%d\n", rc);
        failures++;
    }
    rc = oca_mac(ctx, OCA_MAC_HMAC_SHA256, v->key, huge_len,
                v->pt, v->pt_len, out, 32);
    if (rc == OCA_ERR_INVALID_ARG) {
        passed++;
    } else {
        fprintf(stderr, "FAIL bad-args: mac key_len>INT_MAX gave rc=%d\n", rc);
        failures++;
    }
}

int main(void)
{
    oca_ctx *ctx = oca_init(OCA_BACKEND_SOFTWARE);
    if (!ctx) {
        fprintf(stderr, "cannot init software backend\n");
        return 1;
    }
    printf("backend: %s\n", oca_backend_name(ctx));

    run_aead(ctx, OCA_AEAD_CHACHA20_POLY1305, 32,
             vecs_chacha20_poly1305, N_VECS_CHACHA20_POLY1305);
    run_aead(ctx, OCA_AEAD_AES_128_GCM, 16, vecs_aes_gcm, N_VECS_AES_GCM);
    run_aead(ctx, OCA_AEAD_AES_256_GCM, 32, vecs_aes_gcm, N_VECS_AES_GCM);
    run_aead_negative(ctx, OCA_AEAD_AES_128_GCM, 16, vecs_aes_gcm, N_VECS_AES_GCM);
    run_aead_negative(ctx, OCA_AEAD_AES_256_GCM, 32, vecs_aes_gcm, N_VECS_AES_GCM);
    run_tamper(ctx);
    run_bad_args(ctx);
    run_mac(ctx, OCA_MAC_POLY1305, vecs_poly1305, N_VECS_POLY1305, 16);
    run_mac(ctx, OCA_MAC_HMAC_SHA256, vecs_hmac_sha256, N_VECS_HMAC_SHA256, 32);
    run_mac(ctx, OCA_MAC_BLAKE2S256, vecs_blake2s_keyed, N_VECS_BLAKE2S_KEYED, 32);
    run_hash(ctx, OCA_HASH_SHA256, vecs_sha256, N_VECS_SHA256);
    run_hash(ctx, OCA_HASH_BLAKE2S256, vecs_blake2s, N_VECS_BLAKE2S);

    oca_free(ctx);
    printf("test_vectors: %d passed, %d failed\n", passed, failures);
    return failures ? 1 : 0;
}
