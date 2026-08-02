/* SPDX-License-Identifier: MIT */
/*
 * Throughput benchmark: MB/s and ops/s per algorithm and buffer size.
 * Sizes include 1500 (Ethernet MTU, the WireGuard packet case).
 *
 * Method: per (algorithm, size) run encrypt/hash/mac in a loop for
 * ~BENCH_SECONDS after a short warm-up, then compute throughput.
 */
#include <oca/oca.h>

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define BENCH_SECONDS 0.5
#define WARMUP_SECONDS 0.1

static const size_t sizes[] = {64, 256, 1500, 4096, 16384, 65536};
#define N_SIZES (sizeof(sizes) / sizeof(sizes[0]))

static double now_sec(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
}

typedef struct {
    const char *name;
    int (*fn)(const oca_ctx *, const uint8_t *, size_t, uint8_t *);
} bench_op;

static uint8_t key[32];
static uint8_t nonce[12];
static uint8_t aad[16];
static uint8_t tag[16];
static uint8_t *in_buf, *out_buf;

static int op_chacha20poly1305(const oca_ctx *c, const uint8_t *in, size_t n, uint8_t *out)
{
    return oca_aead_encrypt(c, OCA_AEAD_CHACHA20_POLY1305, key, 32, nonce, 12,
                            aad, 16, in, n, out, tag, 16);
}

static int op_aes128gcm(const oca_ctx *c, const uint8_t *in, size_t n, uint8_t *out)
{
    return oca_aead_encrypt(c, OCA_AEAD_AES_128_GCM, key, 16, nonce, 12,
                            aad, 16, in, n, out, tag, 16);
}

static int op_aes256gcm(const oca_ctx *c, const uint8_t *in, size_t n, uint8_t *out)
{
    return oca_aead_encrypt(c, OCA_AEAD_AES_256_GCM, key, 32, nonce, 12,
                            aad, 16, in, n, out, tag, 16);
}

static uint8_t hash_scratch[32];

static int op_sha256(const oca_ctx *c, const uint8_t *in, size_t n, uint8_t *out)
{
    (void)out;
    return oca_hash(c, OCA_HASH_SHA256, in, n, hash_scratch, 32);
}

static int op_blake2s(const oca_ctx *c, const uint8_t *in, size_t n, uint8_t *out)
{
    (void)out;
    return oca_hash(c, OCA_HASH_BLAKE2S256, in, n, hash_scratch, 32);
}

static int op_hmac_sha256(const oca_ctx *c, const uint8_t *in, size_t n, uint8_t *out)
{
    (void)out;
    return oca_mac(c, OCA_MAC_HMAC_SHA256, key, 32, in, n, hash_scratch, 32);
}

static int op_poly1305(const oca_ctx *c, const uint8_t *in, size_t n, uint8_t *out)
{
    (void)out;
    return oca_mac(c, OCA_MAC_POLY1305, key, 32, in, n, tag, 16);
}

static const bench_op ops[] = {
    {"chacha20-poly1305", op_chacha20poly1305},
    {"aes-128-gcm", op_aes128gcm},
    {"aes-256-gcm", op_aes256gcm},
    {"sha256", op_sha256},
    {"blake2s-256", op_blake2s},
    {"hmac-sha256", op_hmac_sha256},
    {"poly1305", op_poly1305},
};
#define N_OPS (sizeof(ops) / sizeof(ops[0]))

static uint8_t hash_scratch[32];

static void run_one(const oca_ctx *ctx, const bench_op *op, size_t size)
{
    double t0 = now_sec();
    while (now_sec() - t0 < WARMUP_SECONDS) {
        if (op->fn(ctx, in_buf, size, out_buf) != OCA_OK) {
            printf("%-18s %7zu  ERROR\n", op->name, size);
            return;
        }
    }

    size_t iters = 0;
    t0 = now_sec();
    double elapsed;
    do {
        for (int i = 0; i < 64; i++)
            op->fn(ctx, in_buf, size, out_buf);
        iters += 64;
        elapsed = now_sec() - t0;
    } while (elapsed < BENCH_SECONDS);

    double mbps = (double)iters * (double)size / elapsed / (1024.0 * 1024.0);
    double ops_s = (double)iters / elapsed;
    printf("%-18s %7zu  %10.1f  %12.0f\n", op->name, size, mbps, ops_s);
}

int main(void)
{
    oca_ctx *ctx = oca_init(OCA_BACKEND_SOFTWARE);
    if (!ctx) {
        fprintf(stderr, "cannot init software backend\n");
        return 1;
    }
    printf("backend: %s\n\n", oca_backend_name(ctx));

    size_t max_size = sizes[N_SIZES - 1];
    in_buf = malloc(max_size);
    out_buf = malloc(max_size + 64);
    if (!in_buf || !out_buf) {
        fprintf(stderr, "out of memory\n");
        return 1;
    }
    for (size_t i = 0; i < max_size; i++)
        in_buf[i] = (uint8_t)i;
    for (size_t i = 0; i < sizeof(key); i++)
        key[i] = (uint8_t)i;

    printf("%-18s %7s  %10s  %12s\n", "algorithm", "size(B)", "MB/s", "ops/s");
    for (size_t o = 0; o < N_OPS; o++) {
        for (size_t s = 0; s < N_SIZES; s++)
            run_one(ctx, &ops[o], sizes[s]);
        printf("\n");
    }

    free(in_buf);
    free(out_buf);
    oca_free(ctx);
    return 0;
}
