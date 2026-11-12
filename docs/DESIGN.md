# Freehold — core design (step 1)

Two things are frozen here: the **manifest schema** and the **storage driver
interface**. Everything else in the core is implementation detail.

## 1. Layering

```
clients (CLI / daemon / mobile / dashboard)
        |
   core engine      chunk -> dedup -> encrypt -> manifest -> driver
        |
 StorageDriver  (put/get/list/delete)   <-- the only thing backends implement
        |
 local | github | s3 (r2/b2) | ...
```

The core never imports a driver module directly; it resolves one through the
registry by `driver_id` and hands it opaque ciphertext. A driver never sees a
plaintext byte, a file path, or a file name.

## 2. Data flow

1. **Chunk** — FastCDC-style content-defined chunking (gear hash, 64-byte
   window). min 256 KiB / avg 1 MiB / max 4 MiB. The gear table is generated
   from a fixed seed, so boundaries are reproducible on any machine — that is
   what makes dedup work across devices and makes RECOVERY.md possible.
2. **Dedup** — `chunk_hash = SHA-256(plaintext chunk)`. If the hash is already
   in the manifest with a live location, nothing leaves the device.
3. **Encrypt** — AES-256-GCM, fresh 12-byte nonce per chunk, AAD =
   `chunk_hash`. Blob layout: `nonce(12) || ciphertext || tag(16)`.
4. **Address** — the backend key is `blob_id = HMAC-SHA256(K_id, chunk_hash)`,
   hex. The backend therefore never learns a plaintext hash, so it cannot
   confirm "does this user store file X?" by hashing a candidate.
5. **Store** — `driver.put(blob_id, blob) -> location`. The location string is
   opaque to the core and recorded in the manifest.

### Keys

One 32-byte master key the user holds (`key.json`, optionally wrapped with
scrypt + AES-GCM under a passphrase). Subkeys by HKDF-SHA256 over the master:

| subkey  | info string     | use                     |
|---------|-----------------|-------------------------|
| `K_enc` | `freehold:enc`  | AES-256-GCM chunk key   |
| `K_id`  | `freehold:id`   | HMAC blob-id derivation |

Losing the master key loses the data. That is the trade for zero knowledge.

## 3. Manifest schema (SQLite)

Source of truth for restores. Backends are dumb blob stores underneath it.

```sql
meta(key TEXT PK, value TEXT)                  -- schema_version, manifest_id

backend(id PK, name UNIQUE, driver_id, config_json, enabled,
        bytes_stored, chunk_count, created_at)

chunk(hash BLOB PK,            -- SHA-256 of the PLAINTEXT chunk
      size, enc_size, refcount, created_at)

chunk_location(chunk_hash -> chunk, backend_id -> backend,
               location TEXT,  -- opaque, driver-defined
               stored_at, verified_at,
               PK(chunk_hash, backend_id))     -- a chunk may live in N backends

device(id PK, name UNIQUE, platform, created_at)

snapshot(id PK, device_id -> device, root, started_at, completed_at,
         status,                                -- running|complete|failed
         file_count, total_size, new_bytes)

file(id PK, snapshot_id -> snapshot, path,      -- relative, POSIX separators
     mode, size, mtime_ns, content_hash,        -- SHA-256 of whole file
     symlink_target, UNIQUE(snapshot_id, path))

file_chunk(file_id -> file, idx, chunk_hash -> chunk, offset,
           PK(file_id, idx))
```

Notes that matter:

- `chunk` is global and content-addressed; `file_chunk` is the only per-file
  state. A 1-byte edit rewrites one `file` row and one `file_chunk` row, and
  uploads one chunk.
- `chunk_location` is many-to-many on purpose: the same chunk can be mirrored
  into GitHub *and* R2, and a degraded restore just picks whichever backend
  answers.
- `refcount` counts `file_chunk` references across all snapshots. Pruning a
  snapshot decrements; a chunk is deletable only at 0.
- Nothing in the schema names GitHub. Repo pooling and rotation is entirely
  inside the GitHub driver's `location` string and its own `config_json`.

## 4. Storage driver interface

```python
class StorageDriver(ABC):
    driver_id: ClassVar[str]              # "local", "github", "s3"

    def __init__(self, config: dict, state: DriverState | None = None): ...

    def put(self, blob_id: str, data: bytes) -> str: ...   # -> location
    def get(self, location: str) -> bytes: ...
    def list(self) -> Iterator[str]: ...                   # -> locations
    def delete(self, location: str) -> bool: ...

    # optional, defaults provided
    def stats(self) -> BackendStats: ...   # dashboard: used/quota/detail
    def drain_warnings(self) -> list[str]: ...  # quota nearly spent, etc.
    def health(self) -> None: ...          # raise BackendUnavailable
    def close(self) -> None: ...
```

`state` is a per-backend key/value store persisted in the manifest
(`backend_state`). The core writes nothing into it and reads nothing out; it
exists so a driver's own bookkeeping survives a restart. `stats().detail` and
`drain_warnings()` are the two channels by which a driver can say something
vendor-specific without the core having to understand it.

Contract the core relies on:

- `put` is idempotent for a given `blob_id`; re-putting returns a location that
  `get` resolves to the same bytes.
- `location` is any string the driver can resolve later, and the core treats it
  as opaque. `local` uses `ab/abcdef...`; `github` will use
  `repo:owner/name#release/asset_id`; `s3` will use `bucket/key`. Repo rotation
  is invisible to the core because the repo is *part of the location*.
- Errors: drivers raise `BackendUnavailable` (transient/offline/rate-limited —
  the engine degrades and keeps going) or `ChunkNotFound` (that location is
  gone — the engine reports the chunk and the backend by name).
- `put` receives ciphertext. A driver that needed plaintext would be a bug in
  the design, not in the driver.

## 5. Concurrency

A backup to a network backend is latency-bound, not bandwidth-bound: one HTTP
round trip per chunk, serially, measured 36 KB/s against live GitHub. So chunk
transfers run on a bounded thread pool (`workers`, default 8).

What runs where:

- **Calling thread**: walking, chunking, hashing, encryption, and every
  manifest decision — dedup lookups, `file` and `file_chunk` rows, snapshot
  bookkeeping. All of it ordered and single-threaded.
- **Worker threads**: only `driver.put()` and `driver.get()`.

Three consequences worth stating, because they are contracts and not details:

1. **Drivers must be safe to call concurrently.** `put` and `get` may run in
   parallel on one instance. A driver guards its own bookkeeping and does the
   network call outside that lock. `list`, `delete`, `stats` and `health` are
   only ever called from the calling thread.
2. **The manifest serialises itself.** One reentrant lock around every public
   method, because driver state writes come from worker threads. Manifest
   calls take microseconds while the transfers they bracket take seconds, so
   the lock costs nothing measurable.
3. **In-flight work is bounded** by a semaphore at `workers * 2` sealed
   chunks, so a backup cannot queue an entire tree into memory.

### A chunk that cannot be stored

Ordering changes under concurrency: a `file` row is written before its chunks
have finished uploading (the `chunk` row goes in first, so the foreign key
holds). If a chunk then fails on every backend, the file it belongs to is
**deleted from the snapshot** before the snapshot is marked complete, and
reported as an error. A complete snapshot therefore never lists a file it
cannot restore.

Dedup keys off `chunk_location`, not `chunk`, so a chunk recorded but never
stored is simply re-uploaded on the next run.

## 6. Restore

Three modes, one lookup path: resolve a snapshot -> filter `file` rows ->
walk `file_chunk` -> `chunk_location` -> driver. Full, selective and
point-in-time differ only in which snapshot id and which path filter is used.

- Existing paths get a `.freehold-restored-N` sibling unless `--overwrite`.
- Every chunk is hash-checked after decryption. Chunks are **streamed to a
  temp file** as they arrive, hashing as they go, and the file is moved into
  place only once the digest matches what the manifest recorded — so a 10 GB
  restore does not need 10 GB of RAM, and a failed verify leaves nothing
  behind.
- One unreachable backend never fails the run. Files whose chunks are all
  reachable restore; the rest are listed with the chunk and backend that
  failed.
- Files are fetched through a sliding window, so chunks of several files are
  in flight at once rather than one file at a time.

## 7. The manifest is backed up too

The manifest is the only record of file names and chunk order, so a device
that dies taking it along leaves a pile of authentic chunks and no way to
know what they are. It is therefore stored in every backend as an ordinary
blob, encrypted under the same key, at an address derived from that key:

```
blob_id = HMAC-SHA256(K_id, "freehold:manifest")
```

Derived rather than recorded, which is the point: a replacement machine can
compute where to look knowing only the key. `freehold manifest push` does it
by hand and the agent does it after every successful pass.

Recovering it needs the backend to answer "do you hold this blob id?" without
a manifest to consult. `StorageDriver.find(blob_id)` is optional — the
default scans `list()`, since every driver embeds the blob id in its
locations. The GitHub driver implements it properly, rebuilding its repo pool
from the GitHub API, because on a replacement machine it does not even know
which repos exist.

## 8. Build order status

- [x] 1. core engine + local-disk driver
- [x] 2. GitHub driver (Releases + repo pool + LFS fallback)
- [x] 3. S3 driver (R2/B2) — required zero core changes, as it had to
- [x] 4. desktop agent (daemon, folder watch list, link-aware but unrestricted)
- [x] 5. dashboard (localhost, token-guarded, restore browser)
- [x] 6. mobile client (shared library + bridge; Android viable, iOS needs a
      core port — see docs/MOBILE.md)
