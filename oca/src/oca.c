/* SPDX-License-Identifier: MIT */
#include <oca/oca.h>
#include "backend.h"

#include <limits.h>
#include <stdlib.h>

oca_ctx *oca_init(oca_backend_type type)
{
    oca_ctx *ctx = calloc(1, sizeof(*ctx));
    if (!ctx)
        return NULL;

    switch (type) {
    case OCA_BACKEND_SOFTWARE:
        ctx->be = oca_backend_software();
        break;
    case OCA_BACKEND_FPGA:
    default:
        free(ctx);
        return NULL;
    }
    if (!ctx->be) {
        free(ctx);
        return NULL;
    }
    return ctx;
}

void oca_free(oca_ctx *ctx)
{
    free(ctx);
}

const char *oca_backend_name(const oca_ctx *ctx)
{
    return (ctx && ctx->be) ? ctx->be->name : "none";
}

const char *oca_strerror(int status)
{
    switch (status) {
    case OCA_OK:             return "success";
    case OCA_ERR_INVALID_ARG: return "invalid argument";
    case OCA_ERR_UNSUPPORTED: return "algorithm not supported by backend";
    case OCA_ERR_AUTH:        return "authentication failed";
    case OCA_ERR_BACKEND:     return "backend failure";
    default:                  return "unknown error";
    }
}

static int check_common(const oca_ctx *ctx)
{
    return (ctx && ctx->be) ? OCA_OK : OCA_ERR_INVALID_ARG;
}

/*
 * Not every backend call takes a size_t for these lengths: some cast
 * to a bare int first, with no bounding check ahead of the cast
 * (AEAD in/aad length in EVP_CipherUpdate, HMAC key length). Reject
 * what that cast cannot express.
 */
static int fits_int(size_t n)
{
    return n <= (size_t)INT_MAX;
}

int oca_aead_encrypt(const oca_ctx *ctx, oca_aead_alg alg,
                     const uint8_t *key, size_t key_len,
                     const uint8_t *nonce, size_t nonce_len,
                     const uint8_t *aad, size_t aad_len,
                     const uint8_t *in, size_t in_len,
                     uint8_t *out,
                     uint8_t *tag, size_t tag_len)
{
    if (check_common(ctx) != OCA_OK || !key || !nonce ||
        (!in && in_len) || (!aad && aad_len) ||
        !out || !tag || !fits_int(in_len) || !fits_int(aad_len))
        return OCA_ERR_INVALID_ARG;
    return ctx->be->aead_encrypt(ctx, alg, key, key_len, nonce, nonce_len,
                                 aad, aad_len, in, in_len, out, tag, tag_len);
}

int oca_aead_decrypt(const oca_ctx *ctx, oca_aead_alg alg,
                     const uint8_t *key, size_t key_len,
                     const uint8_t *nonce, size_t nonce_len,
                     const uint8_t *aad, size_t aad_len,
                     const uint8_t *in, size_t in_len,
                     const uint8_t *tag, size_t tag_len,
                     uint8_t *out)
{
    if (check_common(ctx) != OCA_OK || !key || !nonce ||
        (!in && in_len) || (!aad && aad_len) ||
        !tag || !out || !fits_int(in_len) || !fits_int(aad_len))
        return OCA_ERR_INVALID_ARG;
    return ctx->be->aead_decrypt(ctx, alg, key, key_len, nonce, nonce_len,
                                 aad, aad_len, in, in_len, tag, tag_len, out);
}

int oca_hash(const oca_ctx *ctx, oca_hash_alg alg,
             const uint8_t *in, size_t in_len,
             uint8_t *out, size_t out_len)
{
    if (check_common(ctx) != OCA_OK || (!in && in_len) || !out)
        return OCA_ERR_INVALID_ARG;
    return ctx->be->hash(ctx, alg, in, in_len, out, out_len);
}

int oca_mac(const oca_ctx *ctx, oca_mac_alg alg,
            const uint8_t *key, size_t key_len,
            const uint8_t *in, size_t in_len,
            uint8_t *out, size_t out_len)
{
    if (check_common(ctx) != OCA_OK || !key || (!in && in_len) || !out ||
        !fits_int(key_len))
        return OCA_ERR_INVALID_ARG;
    return ctx->be->mac(ctx, alg, key, key_len, in, in_len, out, out_len);
}
