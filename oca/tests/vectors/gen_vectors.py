#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Generate tests/vectors/vectors.h from official test vector sources.

Sources (downloaded into tests/vectors/sources/):
  - rfc8439.txt           ChaCha20, Poly1305, ChaCha20-Poly1305 AEAD
  - rfc4231.txt           HMAC-SHA-256 test cases
  - blake2s-kat.txt       BLAKE2s-256 keyed KAT (official BLAKE2 repo)
  - wycheproof_aes_gcm.json  AES-GCM (C2SP/wycheproof)

SHA-256 and unkeyed BLAKE2s references are computed with Python hashlib
(so our C code is checked against an independent implementation, not
against itself).
"""

import hashlib
import json
import re
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "sources"
OUT = Path(__file__).resolve().parent / "vectors.h"


def die(msg):
    sys.exit(f"gen_vectors: {msg}")


# ---------------------------------------------------------------- hexdump
def hexdump_after(lines, start, marker, first_n=16):
    """Collect bytes from RFC hexdump lines ('000  4c 61 ...') following marker."""
    out = []
    i = start
    while i < len(lines) and marker not in lines[i]:
        i += 1
    if i == len(lines):
        die(f"marker not found: {marker!r}")
    i += 1
    while i < len(lines):
        m = re.match(r"^\s*\d{3}\s+(.*)$", lines[i])
        if not m:
            break
        toks = m.group(1).split()
        taken = 0
        for t in toks:
            if re.fullmatch(r"[0-9a-f]{2}", t) and taken < first_n:
                out.append(int(t, 16))
                taken += 1
            else:
                break
        i += 1
    if not out:
        die(f"no hexdump after marker: {marker!r}")
    return bytes(out)


def colonhex_after(lines, start, marker):
    """Collect bytes from a 'aa:bb:cc:' style line following marker."""
    i = start
    while i < len(lines) and marker not in lines[i]:
        i += 1
    if i == len(lines):
        die(f"marker not found: {marker!r}")
    pat = r"([0-9a-f]{2}(?::[0-9a-f]{2})+)"
    m = re.search(pat, lines[i])
    if not m:
        m = re.search(pat, lines[i + 1])
    if not m:
        die(f"no colon-hex after marker: {marker!r}")
    return bytes(int(b, 16) for b in m.group(1).split(":"))


def region(text, start_marker, end_marker):
    # Section headings appear twice in RFC text (TOC + body): use the
    # last occurrence of the start marker to land in the body.
    a = text.rfind(start_marker)
    if a < 0:
        die(f"region start not found: {start_marker!r}")
    b = text.find(end_marker, a + len(start_marker))
    if b < 0:
        die(f"region end not found: {end_marker!r}")
    return text[a:b].splitlines()


# ---------------------------------------------------------------- RFC 8439
def parse_rfc8439():
    text = (SRC / "rfc8439.txt").read_text()
    vecs = {"aead": [], "poly1305": []}

    # Section 2.8.2: AEAD encryption vector
    r = region(text, "2.8.2.  Example and Test Vector", "3.  Implementation Advice")
    pt = hexdump_after(r, 0, "Plaintext:")
    aad = hexdump_after(r, 0, "AAD:")
    key = hexdump_after(r, 0, "Key:")
    iv = hexdump_after(r, 0, "IV:")
    fixed = hexdump_after(r, 0, "32-bit fixed-common part:")
    nonce = fixed + iv
    ct_start = next(i for i, l in enumerate(r) if "keystream bytes:" in l)
    ct = hexdump_after(r, ct_start, "Ciphertext:")
    tag = colonhex_after(r, 0, "Tag:")
    vecs["aead"].append(("RFC8439-2.8.2", key, nonce, aad, pt, ct, tag, 1))

    # Appendix A.5: AEAD decryption vector
    r = region(text, "A.5.  ChaCha20-Poly1305 AEAD Decryption", "Appendix B.")
    key = hexdump_after(r, 0, "The ChaCha20 Key")
    ct = hexdump_after(r, 0, "Ciphertext:")
    nonce = hexdump_after(r, 0, "The nonce:")
    aad = hexdump_after(r, 0, "The AAD:")
    tag = hexdump_after(r, 0, "Received Tag:")
    pt = hexdump_after(r, 0, "Plaintext::")
    vecs["aead"].append(("RFC8439-A.5", key, nonce, aad, pt, ct, tag, 1))

    # Appendix A.3: Poly1305 vectors #1-#4 (hexdump format)
    r = region(text, "A.3.  Poly1305 Message Authentication Code", "A.4.  Poly1305 Key Generation")
    for n in (1, 2, 3, 4):
        off = next(i for i, l in enumerate(r) if f"Test Vector #{n}:" in l)
        key = hexdump_after(r, off, "One-time Poly1305 Key:")
        msg = hexdump_after(r, off, "Text to MAC:")
        tag = hexdump_after(r, off, "Tag:")
        vecs["poly1305"].append((f"RFC8439-A.3-{n}", key, msg, tag))
    return vecs


# ---------------------------------------------------------------- RFC 4231
def parse_rfc4231():
    text = (SRC / "rfc4231.txt").read_text()
    # Keep only the last occurrence of each case number (TOC vs body).
    marks = []
    seen = {}
    for m in re.finditer(r"4\.(\d)\.\s+Test Case (\d)", text):
        seen[int(m.group(2))] = m.start()
    marks = sorted((pos, num) for num, pos in seen.items())
    vecs = []
    hextok = re.compile(r"^[0-9a-f]+$")
    for idx, (pos, num) in enumerate(marks):
        end = marks[idx + 1][0] if idx + 1 < len(marks) else text.find("\n5.  ", pos)
        block = text[pos:end]

        def field(a, b):
            # 'a' may be "Key =" or just "Key" (RFC 4231 case 3 omits '=')
            ma = re.search(rf"^\s*{re.escape(a)}\s", block, re.M)
            mb = block.find(b)
            seg = block[ma.start():mb]
            # Strip '(...)' comments: ASCII notes may contain tokens that
            # look like hex (e.g. the word "a").
            seg = re.sub(r"\([^)]*\)", "", seg)
            return bytes.fromhex("".join(t for t in seg.split() if hextok.match(t)))

        key = field("Key", "Data =")
        data = field("Data =", "HMAC-SHA-224 =")
        expect = field("HMAC-SHA-256 =", "HMAC-SHA-384 =")
        if not key or not expect or len(expect) not in (16, 32):
            die(f"RFC4231 case {num}: bad parse")
        vecs.append((f"RFC4231-{num}", key, data, expect))
    return vecs


# ---------------------------------------------------------------- BLAKE2s
def parse_blake2s_kat():
    want = {0, 1, 2, 3, 15, 16, 17, 63, 64, 65, 127, 128, 129, 255}
    vecs = []
    cur = {}
    for line in (SRC / "blake2s-kat.txt").read_text().splitlines():
        if line.startswith("in:"):
            cur["in"] = bytes.fromhex(line.split("\t", 1)[1])
        elif line.startswith("key:"):
            cur["key"] = bytes.fromhex(line.split("\t", 1)[1])
        elif line.startswith("hash:"):
            cur["hash"] = bytes.fromhex(line.split("\t", 1)[1])
            if len(cur["in"]) in want:
                vecs.append((f"BLAKE2s-KAT-{len(cur['in'])}",
                             cur["key"], cur["in"], cur["hash"]))
            cur = {}
    if len(vecs) != len(want):
        die(f"BLAKE2s KAT: got {len(vecs)}, want {len(want)}")
    return vecs


# ---------------------------------------------------------------- AES-GCM
def parse_wycheproof():
    data = json.loads((SRC / "wycheproof_aes_gcm.json").read_text())
    vecs = []
    for g in data["testGroups"]:
        if g["ivSize"] != 96 or g["tagSize"] != 128 or g["keySize"] not in (128, 256):
            continue
        taken_valid = taken_invalid = 0
        for t in g["tests"]:
            entry = (
                f"wycheproof-tc{t['tcId']}",
                bytes.fromhex(t["key"]), bytes.fromhex(t["iv"]),
                bytes.fromhex(t["aad"]), bytes.fromhex(t["msg"]),
                bytes.fromhex(t["ct"]), bytes.fromhex(t["tag"]),
                1 if t["result"] == "valid" else 0,
            )
            msglen = len(t["msg"]) // 2
            if t["result"] == "valid" and (msglen <= 32 or t["tcId"] % 7 == 0) \
                    and taken_valid < 60:
                vecs.append(entry)
                taken_valid += 1
            elif t["result"] == "invalid" and taken_invalid < 12:
                vecs.append(entry)
                taken_invalid += 1
    if not vecs:
        die("wycheproof: no vectors selected")
    return vecs


# ---------------------------------------------------------------- emit
BLOBS = []


def blob(b, label):
    if len(b) == 0:
        return ("vec_empty", 0)
    name = f"vec_{len(BLOBS)}"
    BLOBS.append((name, b, label))
    return (name, len(b))


def c_bytes(b):
    return ",".join(f"0x{x:02x}" for x in b)


def main():
    rfc8439 = parse_rfc8439()
    rfc4231 = parse_rfc4231()
    blake2s = parse_blake2s_kat()
    aesgcm = parse_wycheproof()

    sha256_vecs = [
        ("sha256-empty", b"", hashlib.sha256(b"").digest()),
        ("sha256-abc", b"abc", hashlib.sha256(b"abc").digest()),
    ]
    blake2s_raw = [
        ("blake2s-empty", b"", hashlib.blake2s(b"").digest()),
        ("blake2s-abc", b"abc", hashlib.blake2s(b"abc").digest()),
    ]

    out = []
    out.append("/* Auto-generated by tests/vectors/gen_vectors.py - do not edit. */")
    out.append("/* Sources: RFC 8439, RFC 4231, BLAKE2 official KAT, C2SP/wycheproof. */")
    out.append("#ifndef OCA_VECTORS_H")
    out.append("#define OCA_VECTORS_H")
    out.append("#include <stddef.h>")
    out.append("#include <stdint.h>")
    out.append("")
    out.append("typedef struct {")
    out.append("    const char *name;")
    out.append("    const uint8_t *key;   size_t key_len;")
    out.append("    const uint8_t *nonce; size_t nonce_len;")
    out.append("    const uint8_t *aad;   size_t aad_len;")
    out.append("    const uint8_t *pt;    size_t pt_len;")
    out.append("    const uint8_t *ct;")
    out.append("    const uint8_t *tag;   size_t tag_len;")
    out.append("    int expect_ok;")
    out.append("} oca_vec_aead;")
    out.append("")
    out.append("typedef struct {")
    out.append("    const char *name;")
    out.append("    const uint8_t *key; size_t key_len;")
    out.append("    const uint8_t *in;  size_t in_len;")
    out.append("    const uint8_t *out; size_t out_len;")
    out.append("} oca_vec_mac;")
    out.append("")
    out.append("typedef struct {")
    out.append("    const char *name;")
    out.append("    const uint8_t *in;  size_t in_len;")
    out.append("    const uint8_t *out; size_t out_len;")
    out.append("} oca_vec_hash;")
    out.append("")
    out.append("static const uint8_t vec_empty[1] = {0};")

    aead_entries = []
    for name, key, nonce, aad, pt, ct, tag, ok in rfc8439["aead"] + aesgcm:
        kn, kl = blob(key, name + ":key")
        nn, nl = blob(nonce, name + ":nonce")
        an, al = blob(aad, name + ":aad")
        pn, pl = blob(pt, name + ":pt")
        cn, _ = blob(ct, name + ":ct")
        tn, tl = blob(tag, name + ":tag")
        aead_entries.append(
            f'    {{"{name}", {kn},{kl}, {nn},{nl}, {an},{al}, {pn},{pl}, {cn}, {tn},{tl}, {ok}}}')

    poly_entries = []
    for name, key, msg, tag in rfc8439["poly1305"]:
        kn, kl = blob(key, name + ":key")
        mn, ml = blob(msg, name + ":msg")
        tn, tl = blob(tag, name + ":tag")
        poly_entries.append(f'    {{"{name}", {kn},{kl}, {mn},{ml}, {tn},{tl}}}')

    hmac_entries = []
    for name, key, data_b, expect in rfc4231:
        kn, kl = blob(key, name + ":key")
        dn, dl = blob(data_b, name + ":data")
        en, el = blob(expect, name + ":expect")
        hmac_entries.append(f'    {{"{name}", {kn},{kl}, {dn},{dl}, {en},{el}}}')

    b2mac_entries = []
    for name, key, inb, expect in blake2s:
        kn, kl = blob(key, name + ":key")
        inn, inl = blob(inb, name + ":in")
        en, el = blob(expect, name + ":expect")
        b2mac_entries.append(f'    {{"{name}", {kn},{kl}, {inn},{inl}, {en},{el}}}')

    hash_entries = {"sha256": [], "blake2s": []}
    for name, inb, expect in sha256_vecs:
        inn, inl = blob(inb, name + ":in")
        en, el = blob(expect, name + ":expect")
        hash_entries["sha256"].append(f'    {{"{name}", {inn},{inl}, {en},{el}}}')
    for name, inb, expect in blake2s_raw:
        inn, inl = blob(inb, name + ":in")
        en, el = blob(expect, name + ":expect")
        hash_entries["blake2s"].append(f'    {{"{name}", {inn},{inl}, {en},{el}}}')

    out.append("")
    for name, b, label in BLOBS:
        out.append(f"static const uint8_t {name}[{max(len(b),1)}] = {{{c_bytes(b)}}}; /* {label} */")
    out.append("")
    out.append("static const oca_vec_aead vecs_chacha20_poly1305[] = {")
    out.append(",\n".join(aead_entries[:2] + [e for e in aead_entries[2:] if "wycheproof" not in e]))
    out.append("};")
    out.append("static const oca_vec_aead vecs_aes_gcm[] = {")
    out.append(",\n".join(e for e in aead_entries if "wycheproof" in e))
    out.append("};")
    out.append("static const oca_vec_mac vecs_poly1305[] = {")
    out.append(",\n".join(poly_entries))
    out.append("};")
    out.append("static const oca_vec_mac vecs_hmac_sha256[] = {")
    out.append(",\n".join(hmac_entries))
    out.append("};")
    out.append("static const oca_vec_mac vecs_blake2s_keyed[] = {")
    out.append(",\n".join(b2mac_entries))
    out.append("};")
    out.append("static const oca_vec_hash vecs_sha256[] = {")
    out.append(",\n".join(hash_entries["sha256"]))
    out.append("};")
    out.append("static const oca_vec_hash vecs_blake2s[] = {")
    out.append(",\n".join(hash_entries["blake2s"]))
    out.append("};")
    out.append("")
    for arr in ("chacha20_poly1305", "aes_gcm"):
        out.append(f"#define N_VECS_{arr.upper()} (sizeof(vecs_{arr})/sizeof(vecs_{arr}[0]))")
    for arr in ("poly1305", "hmac_sha256", "blake2s_keyed"):
        out.append(f"#define N_VECS_{arr.upper()} (sizeof(vecs_{arr})/sizeof(vecs_{arr}[0]))")
    for arr in ("sha256", "blake2s"):
        out.append(f"#define N_VECS_{arr.upper()} (sizeof(vecs_{arr})/sizeof(vecs_{arr}[0]))")
    out.append("")
    out.append("#endif /* OCA_VECTORS_H */")

    OUT.write_text("\n".join(out) + "\n")
    print(f"vectors.h: {len(rfc8439['aead'])} chacha20-poly1305, "
          f"{sum(1 for e in aead_entries if 'wycheproof' in e)} aes-gcm, "
          f"{len(poly_entries)} poly1305, {len(hmac_entries)} hmac-sha256, "
          f"{len(b2mac_entries)} blake2s-keyed, "
          f"{len(hash_entries['sha256'])} sha256, {len(hash_entries['blake2s'])} blake2s")


if __name__ == "__main__":
    main()
