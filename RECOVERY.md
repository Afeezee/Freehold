# Recovering your data without Freehold

Freehold is meant to be the convenient path to your files, never the only one.
This document describes the on-disk formats completely enough that you — or
anyone holding your key — can rebuild every file with a short script and
standard crypto primitives, even if this project is gone.

You need three things:

1. **The master key** (`key.json`, or the 32 raw bytes it holds).
2. **The manifest** (`manifest.db`, SQLite). Back this up; it is small.
3. **The encrypted blobs**, downloaded out of whichever backends hold them.

`tools/recover.py` in this repo is a single-file implementation of everything
below. It imports `sqlite3` from the standard library and `cryptography` for
AES-GCM and HKDF, and nothing from Freehold.

```
python tools/recover.py --manifest manifest.db --key key.json \
                        --blobs ./downloaded_blobs --out ./restored
```

`--blobs` is any directory tree containing the blob files, named by their blob
id. Every driver names blobs that way, so dumping a GitHub release's assets or
an S3 bucket into one folder is enough.

---

## 1. The key file

```json
{ "format": "freehold-key-v1", "wrapped": false, "master": "<64 hex chars>" }
```

If `wrapped` is true, the master key is encrypted under a passphrase:

```json
{ "format": "freehold-key-v1", "wrapped": true,
  "kdf": {"name":"scrypt","n":32768,"r":8,"p":1,"salt":"<hex>"},
  "nonce": "<24 hex>", "ciphertext": "<hex>" }
```

Unwrap it with `scrypt(passphrase, salt, n, r, p) -> 32-byte KEK`, then
`AES-256-GCM(key=KEK, nonce=nonce, aad=b"freehold-key")` over `ciphertext`
(which carries its 16-byte tag at the end, as GCM output normally does).

## 2. Subkeys

Two keys are derived from the master with HKDF-SHA256, salt `b"freehold"`,
length 32:

| key     | info             | used for                        |
|---------|------------------|---------------------------------|
| `K_enc` | `b"freehold:enc"` | AES-256-GCM chunk encryption    |
| `K_id`  | `b"freehold:id"`  | naming blobs inside backends    |

## 3. Blob naming

A chunk's plaintext SHA-256 is `H` (32 raw bytes, as stored in the manifest).
Its name in every backend is:

```
blob_id = hex( HMAC-SHA256(key = K_id, message = H) )
```

The blinding is why a backend operator cannot hash a candidate file and check
whether you store it.

## 4. Blob format

Each blob is exactly:

```
nonce (12 bytes) || AES-256-GCM ciphertext || GCM tag (16 bytes)
```

Decrypt with key `K_enc`, that nonce, and **AAD = H** (the chunk's plaintext
SHA-256, raw bytes). After decrypting, `SHA-256(plaintext) == H` must hold.

There is no compression and no outer container. The plaintext of a blob is a
literal slice of the original file.

## 5. The manifest

SQLite. Tables that matter for recovery:

```sql
snapshot(id, device_id, root, started_at, completed_at, status, ...)
device(id, name, platform, created_at)
file(id, snapshot_id, path, mode, size, mtime_ns, content_hash, symlink_target)
file_chunk(file_id, idx, chunk_hash, offset)
chunk(hash, size, enc_size, refcount, created_at)
chunk_location(chunk_hash, backend_id, location, stored_at, verified_at)
backend(id, name, driver_id, config_json, ...)
```

- `file.path` is relative to `snapshot.root`, with `/` separators.
- `file.content_hash` is SHA-256 of the whole original file.
- `file_chunk.idx` gives the order; `chunk_hash` is the raw 32-byte `H`.
- `chunk_location.location` is a driver-specific string telling you *where* a
  blob lives. You only need it to go fetch the blobs; once they are on disk,
  the blob id alone identifies them.

## 6. The algorithm

For each row of `file` in the snapshot you want:

1. Read its `file_chunk` rows in `idx` order.
2. For each `chunk_hash` H: compute `blob_id`, fetch that blob, decrypt it with
   `K_enc` / its nonce / AAD=H, check `SHA-256(plaintext) == H`.
3. Concatenate the plaintexts in order.
4. Check the result against `file.content_hash`, then write it to
   `<out>/<file.path>`.

That is the whole scheme.

## 7. Getting the blobs out of a backend by hand

| driver   | `location` looks like             | how to fetch without Freehold |
|----------|-----------------------------------|-------------------------------|
| `local`  | `ab/abcdef...`                    | it is a path under the configured directory |
| `github` | `owner/repo#freehold-blobs/<id>`  | `gh release download freehold-blobs -R owner/repo -D blobs/`, or the REST releases API with a PAT |
| `github` | `owner/repo#lfs/<id>`             | read `lfs/<id>` in that repo (an LFS pointer), then fetch the oid over the LFS batch API, or just `git clone` the repo with git-lfs installed |
| `s3`     | `bucket/key`                      | `aws s3 sync s3://bucket ./downloaded_blobs`, or any S3 client |

For GitHub, run one `gh release download` per repo in the pool into the same
output folder. `SELECT DISTINCT substr(location, 1, instr(location, '#') - 1)
FROM chunk_location` on the manifest lists exactly which repos you need. The
downloaded files are already named by blob id.

In every case the file you end up with is named by its blob id, which is what
`recover.py --blobs` expects.

## 8. The manifest blob

The manifest is stored in every backend too, so a replacement machine needs
nothing but the key and the backend credentials. It lives at an address
derived from the key, which is how you find it without already having it:

```
blob_id = hex( HMAC-SHA256(key = K_id, message = b"freehold:manifest") )
```

The blob is laid out as:

```
"FHM1" (4 bytes) || nonce (12 bytes) || AES-256-GCM ciphertext || tag (16)
```

Decrypt with `K_enc`, that nonce, and **AAD = b"freehold:manifest"**. The
plaintext is the SQLite manifest file, **gzip-compressed** — decompress it and
you have `manifest.db`, openable with any SQLite tool.

`tools/recover.py --manifest-blob <file>` does exactly this and writes the
database out, so the whole recovery is:

```bash
# 1. pull every blob out of the backend by hand (see the table above)
gh release download freehold-blobs -R owner/repo-001 -D blobs/

# 2. work out the manifest's blob id from your key, then decrypt that one
python tools/recover.py --key key.json --manifest-blob blobs/<blob_id> \
                        --out-db manifest.db

# 3. restore the files with the manifest you just recovered
python tools/recover.py --manifest manifest.db --key key.json \
                        --blobs blobs/ --out restored/
```

Run `python tools/recover.py --key key.json --manifest-id` to print the blob
id to look for.

## 9. If the manifest is lost

The manifest is the only thing that records file names and chunk order, so
losing it means losing the structure even though the data is still there. Two
mitigations, both of which Freehold does:

- Back the manifest up like any other file — it is small, and it goes into the
  same backends under the same key.
- Keep a copy of `key.json` offline and separate from the backends.

Without the manifest you would still hold a pile of authentic, decryptable
chunks, but rebuilding files from them would be forensics, not recovery.
