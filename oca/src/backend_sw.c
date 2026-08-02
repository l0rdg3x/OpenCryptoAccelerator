/* SPDX-License-Identifier: MIT */
/*
 * Software backend: OpenSSL 3 EVP. This is the reference backend used
 * to validate test vectors and to benchmark against the FPGA backend.
 */
#include <oca/oca.h>
#include "backend.h"

#include <openssl/evp.h>
#include <openssl/hmac.h>
#include <openssl/core_names.h>
#include <openssl/params.h>

#include <string.h>

#define OCA_GCM_NONCE_LEN 12
#define OCA_TAG_LEN 16

static const EVP_CIPHER *aead_cipher(oca_aead_alg alg, size_t key_len)
{
    switch (alg) {
    case OCA_AEAD_CHACHA20_POLY1305:
        return key_len == 32 ? EVP_chacha20_poly1305() : NULL;
    case OCA_AEAD_AES_128_GCM:
        return key_len == 16 ? EVP_aes_128_gcm() : NULL;
    case OCA_AEAD_AES_256_GCM:
        return key_len == 32 ? EVP_aes_256_gcm() : NULL;
    default:
        return NULL;
    }
}

static int sw_aead_crypt(int enc, oca_aead_alg alg,
                         const uint8_t *key, size_t key_len,
                         const uint8_t *nonce, size_t nonce_len,
                         const uint8_t *aad, size_t aad_len,
                         const uint8_t *in, size_t in_len,
                         uint8_t *tag, size_t tag_len,
                         uint8_t *out)
{
    const EVP_CIPHER *cipher = aead_cipher(alg, key_len);
    if (!cipher || !key || !nonce || nonce_len != OCA_GCM_NONCE_LEN ||
        tag_len != OCA_TAG_LEN)
        return OCA_ERR_INVALID_ARG;

    EVP_CIPHER_CTX *cctx = EVP_CIPHER_CTX_new();
    if (!cctx)
        return OCA_ERR_BACKEND;

    int ok, len, total = 0, ret = OCA_ERR_BACKEND;
    ok = EVP_CipherInit_ex(cctx, cipher, NULL, NULL, NULL, enc);
    ok = ok && EVP_CIPHER_CTX_ctrl(cctx, EVP_CTRL_GCM_SET_IVLEN,
                                   (int)nonce_len, NULL);
    ok = ok && EVP_CipherInit_ex(cctx, NULL, NULL, key, nonce, enc);
    if (!ok)
        goto out;

    if (aad && aad_len) {
        ok = EVP_CipherUpdate(cctx, NULL, &len, aad, (int)aad_len);
        if (!ok)
            goto out;
    }
    if (in_len) {
        ok = EVP_CipherUpdate(cctx, out, &len, in, (int)in_len);
        if (!ok)
            goto out;
        total = len;
    }
    if (enc) {
        ok = EVP_CipherFinal_ex(cctx, out + total, &len);
        ok = ok && EVP_CIPHER_CTX_ctrl(cctx, EVP_CTRL_GCM_GET_TAG,
                                       OCA_TAG_LEN, tag);
        ret = ok ? OCA_OK : OCA_ERR_BACKEND;
    } else {
        ok = EVP_CIPHER_CTX_ctrl(cctx, EVP_CTRL_GCM_SET_TAG,
                                 (int)tag_len, tag);
        ok = ok && EVP_CipherFinal_ex(cctx, out + total, &len);
        /* OpenSSL verifies the tag in constant time internally. */
        ret = ok ? OCA_OK : OCA_ERR_AUTH;
    }

out:
    EVP_CIPHER_CTX_free(cctx);
    return ret;
}

static int sw_aead_encrypt(const oca_ctx *ctx, oca_aead_alg alg,
                           const uint8_t *key, size_t key_len,
                           const uint8_t *nonce, size_t nonce_len,
                           const uint8_t *aad, size_t aad_len,
                           const uint8_t *in, size_t in_len,
                           uint8_t *out, uint8_t *tag, size_t tag_len)
{
    (void)ctx;
    return sw_aead_crypt(1, alg, key, key_len, nonce, nonce_len,
                         aad, aad_len, in, in_len, tag, tag_len, out);
}

static int sw_aead_decrypt(const oca_ctx *ctx, oca_aead_alg alg,
                           const uint8_t *key, size_t key_len,
                           const uint8_t *nonce, size_t nonce_len,
                           const uint8_t *aad, size_t aad_len,
                           const uint8_t *in, size_t in_len,
                           const uint8_t *tag, size_t tag_len,
                           uint8_t *out)
{
    (void)ctx;
    return sw_aead_crypt(0, alg, key, key_len, nonce, nonce_len,
                         aad, aad_len, in, in_len,
                         (uint8_t *)tag, tag_len, out);
}

static int sw_hash(const oca_ctx *ctx, oca_hash_alg alg,
                   const uint8_t *in, size_t in_len,
                   uint8_t *out, size_t out_len)
{
    (void)ctx;
    const EVP_MD *md;
    switch (alg) {
    case OCA_HASH_SHA256:     md = EVP_sha256(); break;
    case OCA_HASH_BLAKE2S256: md = EVP_blake2s256(); break;
    default:                  return OCA_ERR_UNSUPPORTED;
    }
    if (out_len != (size_t)EVP_MD_get_size(md))
        return OCA_ERR_INVALID_ARG;

    unsigned int mdlen = 0;
    if (!EVP_Digest(in, in_len, out, &mdlen, md, NULL))
        return OCA_ERR_BACKEND;
    return OCA_OK;
}

static int sw_mac_evp(const char *name, const uint8_t *key, size_t key_len,
                      const uint8_t *in, size_t in_len,
                      uint8_t *out, size_t out_len)
{
    EVP_MAC *mac = EVP_MAC_fetch(NULL, name, NULL);
    if (!mac)
        return OCA_ERR_UNSUPPORTED;
    EVP_MAC_CTX *mctx = EVP_MAC_CTX_new(mac);
    EVP_MAC_free(mac);
    if (!mctx)
        return OCA_ERR_BACKEND;

    OSSL_PARAM params[] = {
        OSSL_PARAM_octet_string(OSSL_MAC_PARAM_KEY, (void *)key, key_len),
        OSSL_PARAM_END
    };
    size_t written = 0;
    int ok = EVP_MAC_init(mctx, NULL, 0, params);
    ok = ok && EVP_MAC_update(mctx, in, in_len);
    ok = ok && EVP_MAC_final(mctx, out, &written, out_len);
    EVP_MAC_CTX_free(mctx);
    return (ok && written == out_len) ? OCA_OK : OCA_ERR_BACKEND;
}

static int sw_mac(const oca_ctx *ctx, oca_mac_alg alg,
                  const uint8_t *key, size_t key_len,
                  const uint8_t *in, size_t in_len,
                  uint8_t *out, size_t out_len)
{
    (void)ctx;
    switch (alg) {
    case OCA_MAC_HMAC_SHA256: {
        if (out_len != 32)
            return OCA_ERR_INVALID_ARG;
        unsigned int len = 0;
        if (!HMAC(EVP_sha256(), key, (int)key_len, in, in_len, out, &len))
            return OCA_ERR_BACKEND;
        return OCA_OK;
    }
    case OCA_MAC_POLY1305:
        if (key_len != 32 || out_len != 16)
            return OCA_ERR_INVALID_ARG;
        return sw_mac_evp("POLY1305", key, key_len, in, in_len, out, out_len);
    case OCA_MAC_BLAKE2S256:
        if (key_len == 0 || key_len > 32 || out_len != 32)
            return OCA_ERR_INVALID_ARG;
        return sw_mac_evp("BLAKE2SMAC", key, key_len, in, in_len, out, out_len);
    default:
        return OCA_ERR_UNSUPPORTED;
    }
}

static const oca_backend sw_backend = {
    .name = "software (OpenSSL EVP)",
    .aead_encrypt = sw_aead_encrypt,
    .aead_decrypt = sw_aead_decrypt,
    .hash = sw_hash,
    .mac = sw_mac,
};

const oca_backend *oca_backend_software(void)
{
    return &sw_backend;
}
