# The GitHub driver

GitHub as a dumb encrypted blob store, with a pool of private repos underneath
that the core engine never learns about.

## Setup

A classic PAT with `repo` scope, or a fine-grained token with
*Administration: read and write* (to create repos) and *Contents: read and
write* (for releases and LFS) on the account that will own the pool.

```bash
export FREEHOLD_GITHUB_TOKEN=ghp_...
python -m freehold.cli backend add gh github owner=yourname
python -m freehold.cli backup ~/Documents
python -m freehold.cli backend info gh
```

Keep the token in the environment. `token_env=` (default
`FREEHOLD_GITHUB_TOKEN`) is read at use time, so the token never lands in
`manifest.db`. `token=` is accepted for convenience and stores it in the
manifest; prefer not to.

### Options

| option | default | meaning |
|---|---|---|
| `owner` | — | user or org that owns the pool |
| `org=1` | off | create repos under an org instead of a user |
| `prefix` | `freehold-blobs` | repo names become `<prefix>-001`, `-002`, … |
| `soft_cap` | 1 500 000 000 | bytes per repo before it is sealed |
| `tag` | `freehold-blobs` | the release every chunk is attached to |
| `private` | on | repos are created private |
| `lfs` | off | use Git LFS for blobs under `lfs_max` |
| `lfs_max` | 1 MiB | largest blob eligible for the LFS path |
| `lfs_quota` | 10 GiB | account-wide free LFS allowance to meter against |

## Where chunks go

**Release assets** are the primary path. They take files up to the per-asset
cap (2 GB on Free/Pro) and are not subject to the guidance on repository size
that committed history is. Chunks are at most a few MiB, so every chunk is one
asset, named by its blinded blob id.

**Git LFS** is the optional fallback for small blobs. It is off by default,
because LFS storage *and* bandwidth are pooled across the whole account against
a fixed free monthly allowance (10 GiB / 10 GiB on Free), and overrunning it
costs money or starts failing. When enabled, the driver counts every byte it
writes against `lfs_quota` and stops using LFS — falling back to releases —
before the allowance runs out, rather than discovering the limit at upload
time. An LFS object is stored with a pointer file at `lfs/<blob_id>`, and
`.gitattributes` routes `lfs/**` through the LFS filter.

**Never** are raw blobs committed into ordinary git history. That would hit the
100 MiB hard block and bloat the repo permanently. Only pointer files and
`.gitattributes` are ever committed.

## The repo pool

```
fh-001  sealed   1.5 GB   412 chunks
fh-002  sealed   1.5 GB   398 chunks
fh-003  active   240 MB    61 chunks   <- new chunks go here
```

Bytes written per repo are tracked in the manifest, in the driver's private
`backend_state` rows. When the active repo would cross `soft_cap`, it is marked
`sealed`, a new private repo is provisioned through the API, and that becomes
`active`. Sealed repos stay fully readable; nothing is ever moved.

Restores do not search. Every location string names its own repo:

```
owner/fh-002#freehold-blobs/3f9a…   <- release asset
owner/fh-002#lfs/3f9a…              <- lfs pointer
```

The manifest records that string per chunk, so a restore goes straight to the
repo holding it. This is also why rotation needed no schema change and no core
change: the repo is part of the location.

To test rotation without waiting for 1.5 GB, drop the cap:

```bash
python -m freehold.cli backend add gh github owner=you soft_cap=50000000
```

## Failure behaviour

- **Rate limiting** (403 with `x-ratelimit-remaining: 0`, or 429) raises
  `BackendUnavailable` with the retry window. The engine marks the backend
  unreachable for the run and keeps going — a backup falls back to other
  backends, a restore recovers every file whose chunks live elsewhere and names
  the rest.
- **5xx and connection errors** are retried with exponential backoff before
  being treated as unavailable.
- **Re-uploading an existing asset** (422 `already_exists`) is treated as
  success, so an interrupted backup can be re-run safely.
- **Approaching the LFS quota, and every repo seal**, surface as notes on the
  backup output and in `backend info`.

## Verified against the live API

`tools/live_github_test.py` has been run against real GitHub with a classic PAT
(`repo` + `delete_repo`). 2.14 MB across 5 files, chunked small (128 KiB
average) and with `soft_cap` set to 1 MB so rotation would fire:

```
backup    5 files, 25 chunks, 25 uploaded, 58.9s
pool      freehold-live-test-001  sealed   996 603 bytes  12 chunks
          freehold-live-test-002  sealed   997 576 bytes  11 chunks
          freehold-live-test-003  active   146 530 bytes   2 chunks
rerun     5 unchanged files, 0 bytes uploaded
incr      1 of 25 chunks re-uploaded after a one-byte edit
verify    deep verify: every chunk readable
restore   5 files, 2 140 009 bytes, 83.5s -- byte-identical
```

Both seals landed just under the cap, chunks really did spread across three
repos, and the restore resolved each chunk straight to its own repo. Stored
bytes (2 242 026 over 26 chunks) reconcile exactly: the source, plus 28 bytes
of nonce and tag per chunk, plus the one chunk the incremental added.

### Throughput: measured, serial vs 8 workers

Identical data, chunk sizes and soft cap, run twice against the live API:

| phase   | serial | 8 workers | speedup |
|---------|--------|-----------|---------|
| backup  | 58.9 s | 28.0 s    | 2.1x    |
| restore | 83.5 s | 7.7 s     | 10.8x   |

Per chunk that is 2.36 s -> 1.12 s uploading, and 3.34 s -> 0.31 s
downloading. Both runs were byte-identical and rotated the pool the same way.

**Restore behaved as predicted** — roughly 8 chunks in flight collapsing the
per-chunk round trip, slightly better than 8x on variance. That path was
latency-bound and now is not.

**Backup did not**, and an instrumented run settled why. `live_github_test.py`
wraps the API client and counts transfers genuinely in flight:

```
[up  ] 25 uploads, 2.0 MB,  69 KB/s, peak 8 in flight (of 8 workers)
[down] 25 downloads, 2.0 MB, 334 KB/s, peak 8 in flight (of 8 workers)
```

Both directions reach full concurrency, over the same link, in the same run.
The driver is not serialising anything — the link is asymmetric, roughly
0.55 Mbit/s up against 2.7 Mbit/s down. That 4.8x ratio accounts for the
2.1x-versus-10.8x speedup almost exactly.

So **backup is bandwidth-bound and there is nothing further to win from more
workers.** More would only add queueing. The figures above are a property of
the connection, not of Freehold: on a symmetric link, expect backup to scale
like restore did.

Worth keeping the instrumentation in mind for later: if a future change makes
uploads peak below `--workers`, something has started serialising them.

## Getting the blobs back without Freehold

See [RECOVERY.md](../RECOVERY.md). In short:

```bash
gh release download freehold-blobs -R owner/fh-001 -D blobs/
gh release download freehold-blobs -R owner/fh-002 -D blobs/
python tools/recover.py --manifest manifest.db --key key.json \
                        --blobs blobs/ --out restored/
```

Downloaded assets are already named by blob id, which is exactly what
`recover.py` expects.
