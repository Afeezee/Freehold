# Freehold

Backup software that keeps your files readable only by you, and leaves you
free to change where they are stored at any time.

Files are split into blocks, scrambled on your own computer, and sent to
whichever storage you choose — a USB drive, a second disk, a GitHub account,
Cloudflare R2, Backblaze B2. The storage provider receives blocks it cannot
read and cannot identify. Changing provider is a copy operation, not a
migration.

Freehold has no servers, no accounts and no subscription. Nothing is sent
anywhere except the storage you configure yourself.

---

## Contents

- [How it works in one minute](#how-it-works-in-one-minute)
- [Installing](#installing)
- [Getting started](#getting-started)
- [Choosing where backups are kept](#choosing-where-backups-are-kept)
- [Tokens and passwords](#tokens-and-passwords)
- [Backing up](#backing-up)
- [Getting files back](#getting-files-back)
- [Automatic backups](#automatic-backups)
- [Recovering after a computer is lost](#recovering-after-a-computer-is-lost)
- [Changing or leaving a storage provider](#changing-or-leaving-a-storage-provider)
- [Command reference](#command-reference)
- [Mobile](#mobile)
- [How it works inside](#how-it-works-inside)
- [What has been verified](#what-has-been-verified)
- [Licence](#licence)

---

## How it works in one minute

1. A folder is scanned and its contents split into blocks of about a
   megabyte, divided at boundaries determined by the content itself.
2. Each block is identified by a fingerprint of what it contains. Blocks that
   have been seen before are not sent again.
3. Each new block is encrypted on this computer, with a key that never
   leaves it.
4. The encrypted blocks are uploaded. The provider sees only scrambled data
   under a meaningless name.
5. A small database called the **manifest** records which blocks make up
   which file. It is kept locally and also stored, encrypted, alongside the
   blocks so that a replacement computer can recover it.

Editing one document re-uploads roughly one block, not the whole file.

Three consequences follow from this design:

- **A storage provider cannot read the files.** Encryption happens first.
- **Losing the encryption key loses the data.** There is no reset, no support
  address, no recovery. This is the cost of the previous point.
- **Nothing is locked in.** [RECOVERY.md](RECOVERY.md) documents every format
  precisely enough to rebuild files with a short script and standard tools.

---

## Installing

### Windows

Download `Freehold.exe` from the
[releases page](https://github.com/Afeezee/Freehold/releases) and run it.
Nothing else is required.

Windows may show "Windows protected your PC" because the file is not
code-signed. Choose **More info**, then **Run anyway**.

### Any platform, from source

Requires Python 3.9 or later.

```bash
git clone https://github.com/Afeezee/Freehold.git
cd Freehold
pip install -e .
```

Commands below are written as `freehold …`. If that is not on the system
path, use `python -m freehold.cli …` instead; the two are equivalent.

### Where Freehold keeps its own files

A single folder, `~/.freehold` by default, containing the encryption key and
the manifest. To use a different location, set `FREEHOLD_HOME`:

```bash
# Windows PowerShell
$env:FREEHOLD_HOME = "D:\freehold"

# macOS and Linux
export FREEHOLD_HOME=/home/you/freehold
```

---

## Getting started

Running `Freehold.exe` with no arguments opens a page in the browser. Every
task below can be done there; the equivalent commands are given for people
who prefer a terminal.

On first run Freehold creates an encryption key and reports its location.

> **Copy that key file somewhere safe and offline before continuing.**
> A USB stick kept in a drawer, a password manager, or printed on paper.
> It is the only thing that can decrypt the backups. If it is lost, the
> backups cannot be recovered by anyone.

To protect the key with a passphrase as well:

```bash
freehold init --passphrase "a phrase that will not be forgotten"
```

Subsequent commands then require `--passphrase`. Note that a passphrase-
protected key cannot be unlocked by the unattended background agent.

---

## Choosing where backups are kept

At least one storage location is required. Several may be configured, in
which case each can hold a complete copy.

**In the browser:** open the page, select **Add a place**, choose a kind of
storage and fill in the settings.

**At the command line:**

| Storage | Command |
|---|---|
| A folder, disk or USB drive | `freehold backend add usb local path=E:/backups` |
| A network drive or NAS | `freehold backend add nas local path=//server/share/backups` |
| GitHub | `freehold backend add gh github owner=YOURNAME` |
| Cloudflare R2 | `freehold backend add r2 s3 endpoint=https://ACCOUNT.r2.cloudflarestorage.com bucket=freehold region=auto` |
| Backblaze B2 | `freehold backend add b2 s3 endpoint=https://s3.us-west-004.backblazeb2.com bucket=freehold region=us-west-004` |

The first word after `add` is a name of the user's choosing; it has no
meaning to Freehold.

### How GitHub storage works

GitHub is not a backup service, but it can act as one. Freehold creates
private repositories named `freehold-blobs-001`, `-002` and so on, attaches
encrypted blocks to releases within them, and moves to a new repository when
one reaches about 1.5 GB. This is handled automatically; the repositories do
not need to be managed by hand.

Storage limits are GitHub's, not Freehold's, and are subject to change.

### Free storage allowances at the time of writing

| Provider | Storage | Downloads |
|---|---|---|
| Cloudflare R2 | ~10 GB | free |
| Backblaze B2 | ~10 GB | free up to 3× stored volume per month |
| A disk already owned | whatever it holds | free |

---

## Tokens and passwords

Only online storage requires credentials. A local disk requires none.

Credentials are never written into the manifest, and therefore never travel
to the provider they authenticate with when the manifest is backed up.

### GitHub

A **classic** personal access token is required. Fine-grained tokens cannot
create repositories, which Freehold needs to do as storage grows.

1. Open <https://github.com/settings/tokens>.
2. Select **Tokens (classic)**, then **Generate new token (classic)**.
3. Tick the **`repo`** scope.
4. Copy the token. GitHub displays it only once.

### S3-compatible storage

Cloudflare R2: create a bucket, then an API token with *Object Read & Write*.
The endpoint is `https://<account-id>.r2.cloudflarestorage.com` and the
region is `auto`.

Backblaze B2: create a bucket and an application key. Use the S3-compatible
endpoint shown in the bucket details, and the region contained in that
hostname.

### Where to put them

Freehold looks in two places, in this order.

**Saved on this computer** — the simpler option, and the only practical one
when using `Freehold.exe`:

```bash
freehold credentials set FREEHOLD_GITHUB_TOKEN
```

The value is prompted for, not echoed, and does not enter shell history. In
the browser, the **Add a place** form presents a box for whichever
credentials the chosen storage requires.

**An environment variable** — takes precedence if both are present:

```bash
# Windows PowerShell, permanently
setx FREEHOLD_GITHUB_TOKEN "ghp_..."

# macOS and Linux
export FREEHOLD_GITHUB_TOKEN=ghp_...
```

Saved credentials are held in `credentials.json` inside the Freehold folder,
restricted to the current user account. `freehold credentials list` shows
which are set and where they came from; it never prints values.

---

## Backing up

**In the browser:** select **Choose a folder…**, navigate to the folder, and
select **Back up this folder**. Progress is shown as it runs.

**At the command line:**

```bash
freehold backup ~/Documents
```

Run it again at any time. Only changes are sent; unchanged files are
recognised without being read.

To write every block to two storage locations at once:

```bash
freehold backup ~/Documents --redundancy 2
```

Each run records a **snapshot** — the state of the folder at that moment,
which can be returned to later. Snapshots are cheap: a second snapshot of an
unchanged folder sends nothing at all.

---

## Getting files back

**In the browser:** open **Get files back**, choose how far back to go, tick
what is needed, give a destination, and start.

**At the command line:**

```bash
freehold snapshots                                  # what is available
freehold restore ~/Recovered                        # everything, newest
freehold restore ~/Recovered documents/report.pdf   # selected items only
freehold restore ~/Recovered --at "2026-01-15"      # as it was on a date
```

Three behaviours apply to every restore:

- **Existing files are never overwritten.** A recovered copy is saved beside
  the original as `report.pdf.freehold-restored-1`. Pass `--overwrite` to
  replace files in place.
- **Every file is verified** against the fingerprint recorded when it was
  backed up, as it is written. A file that does not match is not saved, so a
  silently truncated document cannot result.
- **One unreachable storage location does not stop a restore.** Everything
  available is recovered; anything that is not is listed by name.

---

## Automatic backups

The agent rescans chosen folders on a schedule and backs up what changed.

```bash
freehold agent watch ~/Documents --interval 900   # every 15 minutes
freehold agent watch                              # list watched folders
freehold agent once                               # one pass now
freehold agent run                                # run continuously
```

Folders backed up through the browser are added to this list automatically.

To start the agent when the computer starts:

```bash
freehold agent install
```

This **prints** a service definition appropriate to the platform — a
scheduled task on Windows, a systemd user unit on Linux, a LaunchAgent on
macOS. It is printed rather than installed so that it can be read first.

### Network use

The agent backs up over any connection, including mobile data, with no size
limit. A Wi-Fi-only default means no backups at all where mobile data is the
usual connection, and backup software that silently does nothing is worse
than none.

To wait for an unmetered connection instead:

```bash
freehold agent run --no-cellular
```

Data use is concentrated in the **first** backup of each folder, which may be
large. Later runs are small, because only changed blocks are sent.

If a connection is lost during a backup, the agent stops cleanly. Blocks
already uploaded are kept and the next run continues from that point. An
incomplete snapshot is never offered for restore.

---

## Recovering after a computer is lost

Two things are required: the **encryption key file**, and the **credentials
for the storage**. The old computer, the manifest and any Freehold
configuration are not needed.

```bash
# 1. On the replacement computer, install Freehold.
# 2. Place the saved key file at $FREEHOLD_HOME/key.json
# 3. Provide the storage credentials.
freehold credentials set FREEHOLD_GITHUB_TOKEN

# 4. Rebuild the manifest from the storage, using only the key.
freehold manifest pull github owner=YOURNAME

# 5. Recover the files.
freehold snapshots
freehold restore ~/Recovered
```

Step 4 works because the manifest is stored alongside the blocks, encrypted,
at a location derived from the encryption key. A computer that has never seen
the storage before can therefore work out where to look. For GitHub, the set
of repositories is rediscovered through the API.

This requires that the manifest has been stored at least once. The agent and
the browser interface both do this after every successful backup. When using
`freehold backup` manually, run `freehold manifest push` from time to time.

### Recovering without Freehold

[RECOVERY.md](RECOVERY.md) documents the key file format, the key derivation,
how blocks are named and encrypted, and the manifest schema — in enough
detail to reimplement. `tools/recover.py` is a single file that does exactly
that using only the Python standard library and `cryptography`, importing
nothing from Freehold.

---

## Changing or leaving a storage provider

Deduplication is global: once a block has been stored anywhere, later backups
do not send it again. Adding a second storage location therefore leaves it
empty until its contents are copied across explicitly.

```bash
freehold backend add r2 s3 endpoint=... bucket=...   # add the new place
freehold sync r2                                     # copy everything into it
freehold verify --deep                               # confirm it can serve it
freehold backend remove gh                           # retire the old place
```

`sync` reads each block from whichever location responds, verifies it, and
re-encrypts it with a fresh random value for the destination. The two
locations therefore hold different encrypted data for the same block, and an
observer with access to both cannot match them up.

Removing a storage location that holds the only copy of the data is refused.

---

## Command reference

Every command accepts `--home`, `--key`, `--passphrase` and `--workers`.
Run `freehold <command> --help` for full details.

### Setup

| Command | Purpose |
|---|---|
| `init` | Create the encryption key and manifest. Run once. |
| `backend drivers` | List the kinds of storage available. |
| `backend add <name> <kind> k=v …` | Register a storage location. |
| `backend list` | Show configured storage and how much each holds. |
| `backend info <name>` | Query the storage directly; reports if unreachable. |
| `backend enable` / `disable <name>` | Resume or pause writing. Existing data stays readable. |
| `backend remove <name>` | Forget a storage location. |
| `credentials set <NAME>` | Save a token. Prompts; never echoes or prints values. |
| `credentials list` / `remove <NAME>` | Manage saved credentials. |

### Backing up

| Command | Purpose |
|---|---|
| `backup <folder>` | Back up a folder. Only changes are sent. |
| `backup <folder> --redundancy 2` | Write to two storage locations at once. |
| `backup <folder> --backend gh` | Use specific storage rather than the default. |
| `manifest push` | Store the manifest for disaster recovery. |
| `sync <backend>` | Copy every block into one storage location. |

### Inspecting

| Command | Purpose |
|---|---|
| `status` | Totals, usage per location, last backup per computer. |
| `snapshots` | All snapshots, newest first. |
| `ls` | Files in a snapshot. |
| `verify` | Check every block a snapshot needs is reachable. |
| `verify --deep` | Download and authenticate every block. |

### Recovering

| Command | Purpose |
|---|---|
| `restore <folder>` | Recover everything from the newest snapshot. |
| `restore <folder> <paths…>` | Recover named files or folders only. |
| `restore <folder> --at <date>` | Recover the state at a past date. |
| `restore <folder> --snapshot <n>` | Recover a specific snapshot. |
| `restore <folder> --overwrite` | Replace existing files in place. |
| `manifest pull <kind> k=v …` | Rebuild the manifest on a new computer. |

### Interfaces

| Command | Purpose |
|---|---|
| `dashboard` | Open the browser interface on this computer. |
| `agent watch` / `once` / `run` | Manage automatic backups. |
| `agent network` | Report the current connection and whether it is permitted. |
| `agent install` | Print a service definition for starting at boot. |

---

## Mobile

The library an Android application uses is complete and tested; the
application itself is written but has not yet been built or run. See
[android/README.md](android/README.md).

A phone joins an existing Freehold by receiving the encryption key directly
from the computer — never through storage, since storage is what the key
protects.

iOS is not supported. Embedding Python in an iOS application is impractical
enough that the realistic approach is porting the core engine to a language
that cross-compiles. The formats are documented precisely enough in
[RECOVERY.md](RECOVERY.md) for a second implementation to interoperate.

---

## How it works inside

**Content-defined chunking.** Files are divided at boundaries determined by a
rolling hash of the content, not at fixed offsets. Inserting text at the
start of a document shifts only the first boundary; the rest resynchronise,
so unchanged blocks remain unchanged.

**Deduplication.** Each block is addressed by the SHA-256 of its contents.
Identical blocks are stored once, whether they come from two folders, two
snapshots or two computers sharing a manifest.

**Encryption.** Each block is sealed with AES-256-GCM under a key derived
from the user's key, with a fresh random nonce. The name a block is stored
under is `HMAC-SHA256(key, block hash)` rather than the hash itself, so a
provider cannot determine whether a known file is present by hashing a copy.

**The manifest.** A SQLite database mapping file → ordered blocks → storage
location. It is the authoritative record; storage locations are interchangeable
beneath it.

**Concurrency.** Transfers run eight at a time by default (`--workers`).

| Document | Contents |
|---|---|
| [docs/DESIGN.md](docs/DESIGN.md) | Database schema, storage interface, concurrency model |
| [docs/GITHUB.md](docs/GITHUB.md) | Repository pool, release assets, LFS, measured throughput |
| [docs/S3.md](docs/S3.md) | R2 and B2 configuration, request signing, verification status |
| [docs/AGENT.md](docs/AGENT.md) | Scheduling, network behaviour, running as a service |
| [docs/DASHBOARD.md](docs/DASHBOARD.md) | Browser interface and how it is secured |
| [docs/MOBILE.md](docs/MOBILE.md) | Shared library, platform bridge, Android and iOS |
| [docs/DISTRIBUTION.md](docs/DISTRIBUTION.md) | Distributing the applications to other people |
| [RECOVERY.md](RECOVERY.md) | File formats, for recovery without this software |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Architectural constraints and adding storage support |

---

## What has been verified

**Against the live GitHub API** (`tools/live_e2e_test.py`, 23 of 23 checks):
backup across a rotating pool of private repositories with the size limit
respected, blocks distributed across repositories, a single-byte edit
re-uploading one block of twenty, the browser interface reporting usage and
completing a point-in-time restore, the manifest stored in both locations,
and every block read back and authenticated. The test then deletes the local
installation entirely, makes one storage location unreachable, recovers the
manifest from GitHub alone, and restores the folder byte-for-byte identical.

**Automated tests:** 159 covering degraded restores, corrupted block
detection, interrupted backups resuming, concurrency limits, the standalone
recovery tool, credential handling, and the architectural constraint that the
core contains no provider-specific code.

**Not verified:**

- The S3 driver against a live R2 or B2 endpoint. Request signing matches the
  published AWS test vector and the full path is exercised over real HTTP
  against a local server, but provider-specific behaviour is untested.
- The Android application, which has never been built.
- Datasets larger than a few megabytes in live testing.
- Chunking throughput, which is approximately 12 MB/s in pure Python —
  adequate for incremental backups, slow for a large first backup.

---

## Licence

**GNU Affero General Public License v3.0 or later** — see [LICENSE](LICENSE).

Open source in the formal sense: OSI-approved, with no restriction on who
may use it or for what. Anyone may run, study, modify and redistribute it,
commercial users included.

What the AGPL asks in return is reciprocity. Distribute a modified version,
or run one where other people use it over a network, and the source of that
version must be available under the same terms. Using Freehold privately,
however extensively, carries no such obligation.

That is a deliberate choice rather than an attempt to block commercial use.
Permissive terms would let a company take this, build a paid service on it,
and contribute nothing back. The AGPL does not forbid that; it requires that
the improvements come back with it.

**If the copyleft obligation does not suit you**, a separate commercial
licence is available without it — see [COPYRIGHT](COPYRIGHT).

Contributions require a sign-off; see [CLA.md](CLA.md) for why, and what it
does and does not ask of you.
