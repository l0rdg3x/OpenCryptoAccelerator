/* SPDX-License-Identifier: MIT */
/*
 * OpenCrypto Accelerator (OCA) - abstract crypto API.
 *
 * Applications code against this API only. The backend (software or
 * FPGA) is selected at context creation; no application changes are
 * needed to switch backends.
 */
#ifndef OCA_H
#define OCA_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define OCA_VERSION_MAJOR 0
#define OCA_VERSION_MINOR 1
#define OCA_VERSION_PATCH 0

typedef enum {
    OCA_OK = 0,
    OCA_ERR_INVALID_ARG = -1,  /* NULL pointer or wrong key/nonce/tag size */
    OCA_ERR_UNSUPPORTED = -2,  /* algorithm not available on this backend */
    OCA_ERR_AUTH = -3,         /* AEAD tag verification failed */
    OCA_ERR_BACKEND = -4       /* backend-internal failure */
} oca_status;

typedef enum {
    OCA_BACKEND_SOFTWARE = 0,  /* OpenSSL EVP reference backend */
    OCA_BACKEND_FPGA = 1       /* reserved: FPGA device backend */
} oca_backend_type;

/* All AEAD algorithms use a 96-bit nonce and produce a 128-bit tag. */
typedef enum {
    OCA_AEAD_CHACHA20_POLY1305 = 0,  /* key: 32 bytes */
    OCA_AEAD_AES_128_GCM = 1,        /* key: 16 bytes */
    OCA_AEAD_AES_256_GCM = 2         /* key: 32 bytes */
} oca_aead_alg;

typedef enum {
    OCA_HASH_SHA256 = 0,      /* output: 32 bytes */
    OCA_HASH_BLAKE2S256 = 1   /* output: 32 bytes */
} oca_hash_alg;

typedef enum {
    OCA_MAC_HMAC_SHA256 = 0,  /* any key size, output: 32 bytes */
    OCA_MAC_POLY1305 = 1,     /* one-time key: 32 bytes, output: 16 bytes */
    OCA_MAC_BLAKE2S256 = 2    /* key: up to 32 bytes, output: 32 bytes */
} oca_mac_alg;

typedef struct oca_ctx oca_ctx;

oca_ctx *oca_init(oca_backend_type backend);
void oca_free(oca_ctx *ctx);

const char *oca_backend_name(const oca_ctx *ctx);
const char *oca_strerror(int status);

/*
 * One-shot AEAD. `out` must have room for in_len bytes, `tag` for
 * tag_len bytes. Decrypt returns OCA_ERR_AUTH if the tag does not
 * verify; in that case the contents of `out` are undefined and must
 * be discarded.
 */
int oca_aead_encrypt(const oca_ctx *ctx, oca_aead_alg alg,
                     const uint8_t *key, size_t key_len,
                     const uint8_t *nonce, size_t nonce_len,
                     const uint8_t *aad, size_t aad_len,
                     const uint8_t *in, size_t in_len,
                     uint8_t *out,
                     uint8_t *tag, size_t tag_len);

int oca_aead_decrypt(const oca_ctx *ctx, oca_aead_alg alg,
                     const uint8_t *key, size_t key_len,
                     const uint8_t *nonce, size_t nonce_len,
                     const uint8_t *aad, size_t aad_len,
                     const uint8_t *in, size_t in_len,
                     const uint8_t *tag, size_t tag_len,
                     uint8_t *out);

/* One-shot hash. */
int oca_hash(const oca_ctx *ctx, oca_hash_alg alg,
             const uint8_t *in, size_t in_len,
             uint8_t *out, size_t out_len);

/* One-shot MAC (keyed). */
int oca_mac(const oca_ctx *ctx, oca_mac_alg alg,
            const uint8_t *key, size_t key_len,
            const uint8_t *in, size_t in_len,
            uint8_t *out, size_t out_len);

#ifdef __cplusplus
}
#endif

#endif /* OCA_H */
