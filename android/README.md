# Freehold for Android

The same Python engine the desktop runs, embedded with
[Chaquopy](https://chaquo.com/chaquopy/) and driven by a thin Kotlin layer.
Chunking, deduplication, encryption and the manifest are **not**
reimplemented here — `app/build.gradle.kts` copies `../freehold` into the
build at compile time, so there is one implementation and it cannot drift.

## Honest status

**This has not been built or run.** It was written without an Android SDK to
hand, so treat it as a complete starting point rather than a tested app. The
Python half underneath it *is* tested — see `tests/test_mobile.py` — and the
bridge it calls is exercised end to end from enrolment to a byte-identical
restore.

Expect to spend the first build fixing version numbers. The likely sticking
point is Chaquopy's prebuilt `cryptography` wheel: if the version it offers
does not match, pin it in the `pip { install(...) }` block.

## Building

Open `android/` in Android Studio and press Run. Or from a terminal with the
SDK installed:

```bash
cd android
gradle assembleDebug
# app/build/outputs/apk/debug/app-debug.apk
```

You do **not** need a local toolchain to get an APK: pushing a `v*` tag makes
GitHub Actions build one and attach it to a release. See
[../docs/DISTRIBUTION.md](../docs/DISTRIBUTION.md).

## How it fits together

```
MainActivity.kt   one screen: enrol, add a backend, pick a folder, sync
SyncWorker.kt     WorkManager job for background backups
Freehold.kt       the only place Kotlin talks to Python
      ↓  one function, JSON in and JSON out
freehold/mobile/bridge.py
      ↓
freehold/core/    the same engine as the desktop
```

`Freehold.kt` also reports what Python cannot see — whether the handset is
charging, the battery level, what the radio is doing — and the Python side
decides what to do with it. The policy lives in one place rather than two.

## Enrolment

A phone joins an existing Freehold by receiving the user's key, pasted or
scanned from the desktop. Never from a backend: the backends are the thing
the key protects. Until a key is present the app can do nothing, which is
deliberate.

## What is deliberately minimal

- **One screen.** Enrol, configure a GitHub backend, name a folder, sync.
- **A typed folder path**, not a system folder picker. Android's picker
  hands back a content URI, and the engine walks ordinary filesystem paths.
  Bridging the two properly means either `MANAGE_EXTERNAL_STORAGE` (which
  Play review dislikes) or copying files through the picker. Both are real
  work and neither belongs in a first version.
- **No restore UI.** The bridge supports `browse` and `restore`; the screen
  does not use them yet.

## Things to get right before anyone else installs it

1. A real app icon, replacing the stock one in `AndroidManifest.xml`.
2. A foreground service notification for long backups — Android will
   otherwise kill a lengthy sync, which is survivable (it resumes) but looks
   like it stalled.
3. Runtime permission requests for `READ_MEDIA_IMAGES` on Android 13+. The
   manifest declares them; nothing asks for them yet.
4. Release signing, so installs do not warn about an unknown developer.
