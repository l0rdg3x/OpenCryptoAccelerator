/* SPDX-License-Identifier: MIT */
/* Internal backend interface: one implementation per backend type. */
#ifndef OCA_BACKEND_H
#define OCA_BACKEND_H

#include <oca/oca.h>

typedef struct {
    const char *name;

    int (*aead_encrypt)(const oca_ctx *ctx, oca_aead_alg alg,
                        const uint8_t *key, size_t key_len,
                        const uint8_t *nonce, size_t nonce_len,
                        const uint8_t *aad, size_t aad_len,
                        const uint8_t *in, size_t in_len,
                        uint8_t *out, uint8_t *tag, size_t tag_len);

    int (*aead_decrypt)(const oca_ctx *ctx, oca_aead_alg alg,
                        const uint8_t *key, size_t key_len,
                        const uint8_t *nonce, size_t nonce_len,
                        const uint8_t *aad, size_t aad_len,
                        const uint8_t *in, size_t in_len,
                        const uint8_t *tag, size_t tag_len,
                        uint8_t *out);

    int (*hash)(const oca_ctx *ctx, oca_hash_alg alg,
                const uint8_t *in, size_t in_len,
                uint8_t *out, size_t out_len);

    int (*mac)(const oca_ctx *ctx, oca_mac_alg alg,
               const uint8_t *key, size_t key_len,
               const uint8_t *in, size_t in_len,
               uint8_t *out, size_t out_len);
} oca_backend;

struct oca_ctx {
    const oca_backend *be;
};

const oca_backend *oca_backend_software(void);

#endif /* OCA_BACKEND_H */
