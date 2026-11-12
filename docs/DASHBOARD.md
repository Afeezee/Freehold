# The dashboard

```bash
python -m freehold.cli dashboard
# dashboard: http://127.0.0.1:8731/?token=...
```

Shows connected devices, folders under backup and when each was last done,
storage per backend (with the per-repo breakdown for GitHub), and a restore
browser with point-in-time selection. Backends can be added, disabled,
checked and removed from the page.

## It is not a privileged path

The dashboard can do exactly what the CLI can, through the same API over the
same manifest. There is no separate code path with more authority, and no
endpoint that bypasses a check the CLI makes.

It also holds your key in memory, because restores need it. So:

- It binds to **127.0.0.1** only.
- It mints a **token** at startup and requires it on every API call. The URL
  printed at launch contains it; the page passes it as a header afterwards.
- It refuses **cross-origin writes**, so a page you have open in another tab
  cannot drive it.
- Static serving cannot escape its own folder.

`--no-token` exists for scripting and prints a warning. Do not use it on a
machine other people can reach.

## The restore browser

Pick a device, then a point in time — every complete snapshot is listed with
its timestamp, root and file count, newest first. Walk the tree one level at
a time, tick whole folders or individual files, give a destination, and
start.

Selecting nothing restores the whole snapshot, which the button says
explicitly rather than leaving you to guess.

Existing files are **kept** by default: the restored copy is written beside
the original as `name.freehold-restored-1`, and the job reports how many were
renamed. Overwriting in place needs the checkbox.

Restores run in a background thread and report live: files done, bytes
written, and the file currently being written. A failed restore lists each
file it could not recover, with the chunk and which backends were tried —
the same degraded-restore reporting the CLI gives, because it is the same
code underneath.

## GitHub is one card among several

The dashboard has no concept of a repo, a release or a bucket. Each backend
card shows name, driver, chunk count and bytes; anything vendor-specific
arrives in the driver's own `stats().detail` and is rendered generically —
which is why the GitHub card can show its repo pool, sealed repos and all,
without the dashboard knowing what a repo is. A test asserts GitHub appears
as an ordinary row and that `repos` is not a shared concept.

Quota bars appear only where a driver reports one, so S3 shows usage against
its free tier and the local disk does not pretend to have a limit.

## Moving off a backend

Removing a backend that holds the only copy of your chunks is refused. The
intended sequence for leaving a provider is:

```bash
python -m freehold.cli backend add r2 s3 endpoint=... bucket=...
python -m freehold.cli sync r2          # copy every chunk into the new one
python -m freehold.cli verify --deep    # confirm it can serve them
python -m freehold.cli backend remove github
```

`sync` exists because deduplication is global: once a chunk is stored
anywhere, a later backup will not upload it again, so simply adding a backend
and running a backup leaves the new one empty. Replication reads each chunk
from whichever backend answers, verifies it, and re-seals it with a fresh
nonce for the destination — so the two backends hold different ciphertext for
the same chunk, and an observer with access to both cannot match them up by
comparing bytes.

That command is what makes vendor independence real rather than theoretical.
