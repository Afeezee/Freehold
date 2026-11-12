# Freehold

Personal backup that no storage vendor owns.

Your files are split, deduplicated and encrypted **on your own machine**.
Backends only ever receive opaque encrypted blocks — they never see a
filename, a file, or even the hash of one. Which backend holds them is a line
of configuration, not an architectural decision, so you can add one, mirror to
two, and drop the first without re-uploading anything.

```
your files  →  split into chunks  →  SHA-256 (dedup)  →  AES-256-GCM
                                                              ↓
                              GitHub  /  Cloudflare R2  /  Backblaze B2
                              /  any S3 store  /  a USB disk  /  a NAS
```

**Status:** working, and verified end to end against the live GitHub API —
including a simulated dead machine recovered from nothing but an encryption
key. 143 automated tests.

---

## Contents

- [What problem this solves](#what-problem-this-solves)
- [Do I need an .exe? Is it a website?](#do-i-need-an-exe-is-it-a-website)
- [Install](#install)
- [Five-minute start](#five-minute-start)
- [Tokens and credentials — where to get them, where to put them](#tokens-and-credentials)
- [Every command, explained](#every-command-explained)
- [The dashboard](#the-dashboard)
- [The background agent](#the-background-agent)
- [Mobile](#mobile)
- [Restoring](#restoring)
- [If your computer dies](#if-your-computer-dies)
- [How it works inside](#how-it-works-inside)
- [What is proven, and what is not](#what-is-proven-and-what-is-not)
- [Licence](#licence)

---

## What problem this solves

Most backup tools make one company load-bearing. If that account is
suspended, the price changes, or the company folds, your files are hostage to
getting them out.

Freehold treats storage as interchangeable. The part that matters — the map
of which blocks make up which file — lives with you, in a small SQLite file
called the **manifest**. Backends are reduced to dumb warehouses for
encrypted blocks. Swapping one out is a copy operation, not a migration.

Three consequences worth stating up front:

1. **The backend cannot read your files.** Encryption happens before anything
   leaves the machine. This is not a promise about the provider's conduct; it
   is a property of the design.
2. **Losing your key means losing your data.** There is no recovery, no
   support email, no reset. That is the cost of the previous point.
3. **You can leave.** [RECOVERY.md](RECOVERY.md) documents every format in
   enough detail to rebuild your files with a short script, so Freehold is
   the convenient path and not the only one.

---

## Do I need an .exe? Is it a website?

Short answer: **right now Freehold is a command-line program plus a local web
page. There is no installer, no `.exe`, and no mobile app yet.**

| What you might expect | What exists today |
|---|---|
| A Windows `.exe` you double-click | Not yet. You run it with Python: `python -m freehold.cli …`. It can be packaged into a single `.exe` with PyInstaller — nothing in the design prevents it, it just has not been done. |
| A website you log into | **No, and deliberately not.** There is no Freehold server, no account, no cloud. Nothing of yours touches any machine I control. |
| A web page | Yes — but it runs **on your own computer**. `freehold dashboard` starts a small server on `127.0.0.1` and you open it in your browser. It is not reachable from the internet. |
| A phone app | Not yet. The shared library a phone app would use is built and tested; the app itself is not. Android is practical today, iOS needs more work. See [Mobile](#mobile). |

So: a **desktop tool**, driven from a terminal, with a **local** web page for
browsing and restoring. If you want the double-clickable version, that is a
packaging job and a reasonable next step.

---

## Install

You need **Python 3.9 or newer**. Check with `python --version`.

```bash
git clone https://github.com/Afeezee/Freehold.git
cd Freehold
pip install -e .
```

That installs one dependency, `cryptography`. Nothing else.

You can now use either form, whichever you prefer:

```bash
freehold status                 # if the script landed on your PATH
python -m freehold.cli status   # always works
```

This README uses the `python -m freehold.cli` form because it works
everywhere.

### Where Freehold keeps its own files

One folder, which holds your key and your manifest:

```bash
# Windows PowerShell
$env:FREEHOLD_HOME = "$HOME\.freehold"

# macOS / Linux
export FREEHOLD_HOME=~/.freehold
```

If you do not set it, `~/.freehold` is used. Set it permanently on Windows
with `setx FREEHOLD_HOME "%USERPROFILE%\.freehold"` (then open a new
terminal).

---

## Five-minute start

Backing up to a second disk, with no accounts or tokens involved:

```bash
python -m freehold.cli init
python -m freehold.cli backend add disk local path=D:/freehold-store
python -m freehold.cli backup "C:/Users/you/Documents"
python -m freehold.cli status
```

`init` creates two things in `FREEHOLD_HOME`:

- **`key.json`** — your encryption key. **Copy this somewhere safe and
  offline now.** A USB stick in a drawer, a password manager, printed on
  paper. Without it your backups are permanently unreadable, by anyone,
  including you.
- **`manifest.db`** — the map of what is backed up where.

To wrap the key with a passphrase so the file alone is not enough:

```bash
python -m freehold.cli init --passphrase "a long phrase you will not forget"
```

Every later command then needs `--passphrase`. Note the trade-off: an
unattended background agent cannot unlock a wrapped key by itself.

Restore it all somewhere new:

```bash
python -m freehold.cli restore "C:/Users/you/Restored"
```

---

## Tokens and credentials

Freehold never asks you to create an account with anyone. You bring
credentials for storage you already control. **No token is ever written into
the manifest unless you explicitly pass it inline** — the normal path reads
it from an environment variable at the moment it is used.

### GitHub

GitHub works as a backup target because release assets are not subject to the
repository size guidance, and a pool of private repositories can hold a
great deal between them.

**Which token:** a **classic** personal access token. Fine-grained tokens
cannot create repositories, and Freehold creates them as the pool grows.

1. Go to <https://github.com/settings/tokens> → **Tokens (classic)** →
   **Generate new token (classic)**.
2. Give it an expiry you are comfortable with.
3. Tick **`repo`** (the whole block). Tick **`delete_repo`** as well only if
   you want Freehold's test scripts to be able to clean up after themselves.
4. Copy the token — GitHub shows it once.

Put it in your environment, not in a file:

```bash
# Windows PowerShell (this session only)
$env:FREEHOLD_GITHUB_TOKEN = "ghp_your_token_here"

# Windows, permanently (open a new terminal afterwards)
setx FREEHOLD_GITHUB_TOKEN "ghp_your_token_here"

# macOS / Linux
export FREEHOLD_GITHUB_TOKEN=ghp_your_token_here
```

Then add the backend. `owner` is your GitHub username:

```bash
python -m freehold.cli backend add gh github owner=YOURNAME
```

Freehold creates private repositories named `freehold-blobs-001`, `-002`, …
as it needs them, seals each at about 1.5 GB, and moves on to the next. You
never manage them by hand.

<details>
<summary>GitHub options you can tune</summary>

| Option | Default | Meaning |
|---|---|---|
| `owner` | required | your username, or an organisation |
| `org=1` | off | the owner is an organisation |
| `prefix` | `freehold-blobs` | naming for the pool repositories |
| `soft_cap` | `1500000000` | bytes per repo before sealing it |
| `token_env` | `FREEHOLD_GITHUB_TOKEN` | which variable holds the token |
| `token` | — | the token inline; **stored in the manifest**, avoid |
| `lfs` | off | also use Git LFS for small blocks |
| `lfs_quota` | 10 GiB | free LFS allowance to meter against |

</details>

### Cloudflare R2, Backblaze B2, or any S3-compatible store

**R2** — create a bucket, then an API token with *Object Read & Write*. Your
endpoint is `https://<account-id>.r2.cloudflarestorage.com` and the region is
`auto`.

**B2** — create a bucket and an application key. Use the S3-compatible
endpoint shown in the bucket details, and the region embedded in that
hostname (for example `us-west-004`).

```bash
# Windows PowerShell
$env:FREEHOLD_S3_ACCESS_KEY_ID = "..."
$env:FREEHOLD_S3_SECRET_ACCESS_KEY = "..."

# macOS / Linux
export FREEHOLD_S3_ACCESS_KEY_ID=...
export FREEHOLD_S3_SECRET_ACCESS_KEY=...
```

```bash
python -m freehold.cli backend add r2 s3 \
    endpoint=https://<account-id>.r2.cloudflarestorage.com \
    bucket=freehold region=auto
```

Both free tiers are around 10 GB. R2 charges nothing for downloads, which
makes it a good place to restore from.

### A disk, a NAS, or a mounted drive

No credentials at all:

```bash
python -m freehold.cli backend add usb local path=E:/freehold
```

### Checking a token works

```bash
python -m freehold.cli backend info gh
```

This asks the backend directly and prints what it is holding — for GitHub,
every repository in the pool and how full each one is. If the token is wrong
or expired you get a plain error, not a silent failure.

---

## Every command, explained

Run `python -m freehold.cli <command> --help` for the exact flags.

### Setting up

| Command | What it does |
|---|---|
| `init` | Creates your key and manifest. Run once. `--passphrase` wraps the key. |
| `backend drivers` | Lists backend types available (`local`, `github`, `s3`) and their options. |
| `backend add <name> <type> k=v …` | Registers a backend. The name is yours to choose. |
| `backend list` | What is configured, and how much each holds. |
| `backend info <name>` | Asks the backend itself — live usage, GitHub's repo pool, and whether it is reachable at all. |
| `backend disable <name>` / `enable` | Stop or resume writing to a backend. Existing data stays readable. |
| `backend remove <name>` | Forgets a backend. Refused if it holds the only copy of your data. |

### Backing up

| Command | What it does |
|---|---|
| `backup <folder>` | Backs up a folder. Run it again later and only changes are sent. |
| `backup <folder> --redundancy 2` | Writes every block to two backends at once. |
| `backup <folder> --backend gh` | Use specific backends rather than the default. |
| `--workers N` | Simultaneous transfers, default 8. `1` makes it serial. |

Each run creates a **snapshot** — a point in time you can return to.

### Looking at what you have

| Command | What it does |
|---|---|
| `status` | Totals, usage per backend, last backup per device. |
| `snapshots` | Every snapshot, newest first, with date and file count. |
| `ls` | Files in the newest snapshot. `--snapshot 3` or `--at 2026-01-15` for an older one. |
| `verify` | Checks every block a snapshot needs is still reachable. |
| `verify --deep` | Actually downloads and authenticates every block. Slower, and the real check. |

### Restoring

| Command | What it does |
|---|---|
| `restore <folder>` | Everything from the newest snapshot. |
| `restore <folder> docs/report.pdf` | Just the paths you name. |
| `restore <folder> --at "2026-01-15"` | As things were on that date. |
| `restore <folder> --snapshot 7` | A specific snapshot by number. |
| `restore <folder> --overwrite` | Replace existing files instead of writing copies beside them. |

### Staying free of any one vendor

| Command | What it does |
|---|---|
| `sync <backend>` | Copies **every** block into that backend, so it holds a complete set. |
| `manifest push` | Stores your manifest, encrypted, in every backend. |
| `manifest pull <type> k=v …` | Rebuilds the manifest on a new machine from your key alone. |

`sync` matters more than it looks. Deduplication is global: once a block is
stored anywhere, a later backup will not send it again. So simply adding a
second backend leaves it **empty**. `sync` is what fills it, and therefore
what makes "I can stop using GitHub tomorrow" true rather than aspirational.

Leaving a provider, in full:

```bash
python -m freehold.cli backend add r2 s3 endpoint=... bucket=...
python -m freehold.cli sync r2            # copy everything across
python -m freehold.cli verify --deep      # prove the new one can serve it
python -m freehold.cli backend remove gh  # now safe to drop
```

---

## The dashboard

```bash
python -m freehold.cli dashboard
# dashboard: http://127.0.0.1:8731/?token=7f2c...
```

Open the printed URL. You get devices, folders under backup, usage per
backend (including each GitHub repository), and a restore browser where you
can pick a point in time, tick files or folders, and restore.

It is locked down on purpose, because it holds your key in memory in order to
restore:

- listens only on `127.0.0.1` — not reachable from your network
- requires the token in the URL; without it, every request is refused
- refuses requests from other websites open in your browser
- cannot serve files outside its own folder

`--no-token` exists for scripting and prints a warning. Do not use it on a
shared machine.

---

## The background agent

So you do not have to remember to run `backup`.

```bash
python -m freehold.cli agent watch "C:/Users/you/Documents" --interval 900
python -m freehold.cli agent watch "C:/Users/you/Pictures" --interval 3600
python -m freehold.cli agent watch          # list what is watched
python -m freehold.cli agent once           # one pass now, then exit
python -m freehold.cli agent run            # keep running
```

It rescans each folder on its interval. Unchanged files are skipped without
being read, so a scan is cheap even over tens of thousands of files.

**Networks.** It backs up over whatever connection you have, **including
mobile data, with no size limit**. This is a deliberate departure from the
usual Wi-Fi-only default: where mobile data is the normal connection, a
Wi-Fi-only agent simply never runs, and a backup tool that quietly does
nothing is worse than none at all. If you would rather it waited for Wi-Fi,
`python -m freehold.cli agent run --no-cellular`.

Worth knowing: the expensive run is the **first** one for each folder, which
may be gigabytes. After that, an edited document sends about 100 KB. If data
cost matters, do the first run on Wi-Fi by hand and let the agent handle the
rest.

**Running it at startup:**

```bash
python -m freehold.cli agent install
```

This **prints** a service definition for your platform — a `schtasks` command
on Windows, a systemd user unit on Linux, a LaunchAgent on macOS. Read it,
then run it yourself. It deliberately does not install anything for you.

If the connection drops mid-backup, the agent stops cleanly. Nothing is
wasted: the blocks already uploaded stay, and the next run continues from
there. A half-finished snapshot is never offered for restore.

---

## Mobile

There is no app in the stores. What exists is the **library an app would
use**, finished and tested — so that a phone and a desktop share one engine
rather than two implementations that drift apart.

- **Android is practical today.** The core runs under Chaquopy in an ordinary
  Android build. Drive it from WorkManager and pass the battery and network
  state in from the platform.
- **iOS needs more work.** Embedding Python in an App Store build is
  unpleasant enough that the honest path is porting the core to Rust or Go
  first. [RECOVERY.md](RECOVERY.md) specifies every format precisely enough
  to do that, and the chunking is deterministic, so a second implementation
  would deduplicate against this one.

A phone joins an existing Freehold by receiving your key out of band — a QR
code from the desktop, or a typed string. Never from a backend, since the
backends are what the key protects. Details in [docs/MOBILE.md](docs/MOBILE.md).

---

## Restoring

Three ways to come back, all the same underneath:

**Everything**, to a new machine or a new folder:

```bash
python -m freehold.cli restore "D:/Recovered"
```

**Only what you need:**

```bash
python -m freehold.cli restore "D:/Recovered" "docs/thesis.docx" "photos/2026"
```

**As it was on a date**, for when the damage was a bad edit rather than a
dead disk:

```bash
python -m freehold.cli snapshots
python -m freehold.cli restore "D:/Recovered" --at "2026-01-15 09:00"
```

Three behaviours that are not accidents:

- **Existing files are never silently overwritten.** The restored copy lands
  beside the original as `report.pdf.freehold-restored-1`. Use `--overwrite`
  when you actually mean it.
- **Every file is checked** against the hash recorded at backup time, as it
  is written. A file that does not match is not placed at all, so you never
  get a silently truncated document.
- **One dead backend does not stop a restore.** Everything reachable comes
  back; whatever cannot is listed by name, with the block and the backends
  that were tried.

---

## If your computer dies

This is the case the whole project is shaped around, and it is tested against
live infrastructure, not simulated.

You need two things: your **key file**, and your **backend credentials**.
Not the old machine, not the manifest, not a backup of Freehold itself.

```bash
# 1. on the new machine
pip install -e .
$env:FREEHOLD_HOME = "$HOME\.freehold"
$env:FREEHOLD_GITHUB_TOKEN = "ghp_..."

# 2. put the key file you kept safe into $FREEHOLD_HOME\key.json

# 3. rebuild the manifest from the backend, using only that key
python -m freehold.cli manifest pull github owner=YOURNAME

# 4. restore
python -m freehold.cli snapshots
python -m freehold.cli restore "D:/Recovered"
```

Step 3 works because the manifest is stored in your backends too, encrypted,
at an address derived from your key — so a new machine can work out where to
look without being told. The GitHub backend even rediscovers its repository
pool from the API, since the new machine has never heard of those repos.

**This only works if the manifest was pushed.** The agent does it after every
successful run. If you only ever use `backup` by hand, run
`python -m freehold.cli manifest push` occasionally — it takes seconds.

### Without Freehold at all

If this project disappears, [RECOVERY.md](RECOVERY.md) specifies every
format — the key file, the key derivation, how blocks are named and
encrypted, the database schema — in enough detail to rebuild your files from
scratch. `tools/recover.py` is a single file that does exactly that, using
only Python's standard library and `cryptography`, importing nothing from
Freehold. It is tested by running it in isolated mode, so it cannot
accidentally pass by borrowing code it should not have.

---

## How it works inside

**Chunking.** Files are split on content, not at fixed offsets, using a
rolling hash (FastCDC). Insert a paragraph at the top of a document and the
boundaries resynchronise almost immediately, so only the blocks that really
changed are new. Average block is 1 MiB.

**Deduplication.** Each block is identified by the SHA-256 of its contents.
Identical blocks — the same photo in two folders, an unchanged file across
snapshots — are stored once.

**Encryption.** Each block is sealed with AES-256-GCM under a key derived
from yours, with a fresh random nonce. The backend's name for a block is
`HMAC-SHA256(your key, block hash)`, not the hash itself — so a provider
cannot test whether you store a particular known file by hashing a copy of
it.

**The manifest.** A SQLite database mapping file → ordered blocks → which
backend holds each one and where. The source of truth; backends are
warehouses underneath it.

**Concurrency.** Transfers run eight at a time by default. Measured against
live GitHub, this was 2.1× faster for uploads and 10.8× for downloads; the
difference is an asymmetric home connection, not the code.

More in [docs/DESIGN.md](docs/DESIGN.md), which also records the two things
deliberately frozen: the database schema, and the four-method interface every
backend implements.

| Document | Covers |
|---|---|
| [docs/DESIGN.md](docs/DESIGN.md) | schema, driver interface, concurrency model |
| [docs/GITHUB.md](docs/GITHUB.md) | repo pool, release assets, LFS, measured speeds |
| [docs/S3.md](docs/S3.md) | R2 / B2 setup, request signing, what is verified |
| [docs/AGENT.md](docs/AGENT.md) | scheduling, network behaviour, running as a service |
| [docs/DASHBOARD.md](docs/DASHBOARD.md) | what it shows, how it is secured, leaving a vendor |
| [docs/MOBILE.md](docs/MOBILE.md) | the shared library, the bridge, Android vs iOS |
| [RECOVERY.md](RECOVERY.md) | every format, for rebuilding without this software |
| [CONTRIBUTING.md](CONTRIBUTING.md) | the one architectural rule, and how to add a backend |

---

## What is proven, and what is not

Being precise about this is more useful than a feature list.

**Verified against the live GitHub API** (`tools/live_e2e_test.py`, 23 of 23
checks): backup across a rotating pool of real private repositories, the
size cap respected, blocks spread across repos, a one-byte edit re-uploading
one block of twenty, the dashboard reporting usage and completing a
point-in-time restore, the manifest stored in both backends, and a deep
verify reading every block back. Then the machine deleted outright, one
backend made unreachable, the manifest recovered **from GitHub alone**, and
the whole tree restored byte-identical.

**Verified locally, 143 automated tests:** degraded restores, corrupted
blocks detected, interrupted backups resuming, concurrency limits, the
standalone recovery tool, and that the core contains no vendor-specific code.

**Not verified:**

- **The S3 driver against a real R2 or B2 endpoint.** Request signing matches
  AWS's own published test vector and the whole path runs over real HTTP to a
  local server, but provider quirks are untested. If you try it, please open
  an issue either way.
- **iOS.** See [Mobile](#mobile).
- **Very large datasets.** The largest live test was a few megabytes. The
  design holds, but nobody has run a terabyte through it.
- **Chunking speed.** Pure Python, roughly 12 MB/s. Fine for incremental
  backups, slow for a first run over a large library. Replaceable without
  touching anything else.

---

## Licence

**PolyForm Noncommercial 1.0.0** — see [LICENSE](LICENSE).

In plain terms: **free for any non-commercial use.** Personal backups, study,
research, hobby projects, charities, schools, universities, public bodies.
You may read it, change it, and share it, provided the licence travels with
it.

**Commercial use is not granted.** That includes selling it, offering it as a
paid service, or using it internally at a for-profit company. Want that? Ask
the copyright holder.

One honest note, since the two words are often used interchangeably: a
licence that bars commercial use is **source-available**, not *open source*
in the formal sense — the Open Source Definition does not allow restrictions
on who may use software or for what. GitHub will label this repository
"non-OSI" or show no licence badge for that reason. Nothing is wrong; it
simply is not the same thing, and the distinction matters to some people who
may want to use this.

If you would rather have the formal open-source label, Apache-2.0 or AGPL-3.0
would give it — AGPL being the one that still discourages a company from
taking it closed. Changing your mind later is easy while the project has one
author and hard once it has contributors.
