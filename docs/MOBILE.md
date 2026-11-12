# The mobile client

```bash
python -m freehold.cli dashboard      # the desktop side, for comparison
```

```python
from freehold.mobile import handle
handle('{"cmd": "sync", "home": "/data/user/0/dev.freehold/files"}')
```

## What this is, and what it is not

**It is** the same engine. `freehold/mobile/` contains no chunker, no cipher,
no SQL — a test asserts that, by reading the source and checking which modules
it is allowed to import. Chunking, deduplication, encryption and the manifest
are `freehold.core`, identical to the CLI. The folder list and network policy
are `freehold.agent`, identical to the desktop agent. What this module adds is
only the adaptation to how a phone behaves.

**It is not** a shipped app. There is no Kotlin or Swift here, no Gradle
project, no App Store build. What exists is the library a native layer calls
and the single entry point it calls it through, both tested end to end in
Python. Writing a UI I could not compile or run would have been pretending.

## The three things a phone needs that a desktop does not

**No daemon.** The OS hands you a short background window and may kill you
mid-sentence, so `sync()` takes a time budget (default 240s) and a byte
budget, stops cleanly when either runs out, and returns `more_to_do: true`.
Being interrupted is cheap: chunks are content-addressed, so the next window
resumes rather than restarting, and the interrupted snapshot is never
offered for restore.

**The platform knows things Python cannot.** Whether the handset is charging,
the battery level, and what the radio is actually doing. So those are passed
in as `Conditions` rather than guessed:

```json
{"cmd": "sync", "home": "...",
 "conditions": {"network": "cellular", "charging": false, "battery_pct": 62}}
```

Defaults: syncs on any link including cellular, unrestricted — the same
decision as the desktop agent, for the same reason. Defers below 15% battery
unless charging. `require_charging` is available and off.

**Enrolment, not account creation.** A phone joins an existing Freehold by
receiving the user's 32-byte key out of band — a QR code shown by the
desktop, or a typed string. Never from a backend, because the backends are
the thing the key protects.

```json
{"cmd": "enrol", "home": "...", "key": "<64 hex chars>", "device": "pixel"}
```

## The bridge

One entry point, JSON in and JSON out, because every additional FFI symbol is
another thing to keep in step across two languages:

```kotlin
val py = Python.getInstance()
val bridge = py.getModule("freehold.mobile.bridge")
val reply = JSONObject(bridge.callAttr("handle", request.toString()).toString())
if (!reply.getBoolean("ok")) showError(reply.getString("error"))
```

Commands: `enrol`, `status`, `add_folder`, `remove_folder`, `should_sync`,
`sync`, `snapshots`, `browse`, `restore`, `close`. Every reply is
`{"ok": true, ...}` or `{"ok": false, "error": "..."}`. The bridge never
raises into the host — a test feeds it malformed JSON, arrays, missing
fields and unknown commands and asserts it always answers.

`should_sync` exists so the native scheduler can ask cheaply whether a window
is worth spending before it starts one.

## Platforms, honestly

**Android works.** Chaquopy embeds CPython in an ordinary Gradle build, and
this library runs as-is. Point `home` at the app's private files directory,
drive `sync` from WorkManager, and read the battery and network state from
the platform APIs into `Conditions`. `MANAGE_EXTERNAL_STORAGE` is not needed
for the app's own media access via the photo picker or scoped storage.

**iOS does not, and I will not pretend otherwise.** Embedding CPython in an
App Store build is possible but unpleasant, and the cost of being wrong about
a backup client is high. The realistic path is porting the core to a language
that cross-compiles cleanly — Rust or Go — and calling it from both platforms.

That port is tractable *because of how this is built*, not despite it. The
formats are specified in [RECOVERY.md](../RECOVERY.md) in enough detail to
reimplement from scratch: FastCDC with a documented gear table, SHA-256
content addressing, AES-256-GCM with a documented layout, HKDF subkeys, and a
SQLite schema. A second implementation that agrees with this one is a
few hundred lines and a shared test vector away, and the chunker's
determinism means both implementations dedup against each other.

So: Android now, iOS after a core port. Saying "mobile is done" would be the
kind of claim that only surfaces as a problem when someone needs their photos
back.

## What is tested

Enrolment with a real key and rejection of malformed ones; the battery floor
and the charging override; cellular syncing by default and offline refused;
a byte budget and a time budget each stopping cleanly, reporting what they
actually uploaded, and resuming to a complete snapshot on the next window;
browse and selective restore; the full bridge round trip from `enrol` to a
byte-identical `restore`; and that the phone and the desktop share one
manifest and one key — a photo backed up by the phone restores on the desktop
with plain `freehold.core`, and a desktop folder restores through the phone's
session.
