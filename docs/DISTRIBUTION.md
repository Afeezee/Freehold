# Sharing Freehold with other people

Short answer: **yes, and nothing you hold needs to travel with it.** The apps
contain no tokens, no keys, and no account of yours. Each person who installs
Freehold generates their own encryption key on their own device and supplies
their own storage credentials.

This is not a precaution bolted on for distribution — it falls out of the
design. Freehold has no server, so there is no account to share and nothing
of yours for a stranger's copy to reach.

## What is in the files you hand out

| | In the artefact? |
|---|---|
| Your encryption key | **No.** Generated on first run, on the user's machine. |
| Your GitHub token or S3 keys | **No.** Read from the user's environment or their own `credentials.json`. |
| Your manifest, file names, folders | **No.** Created locally when they first back up. |
| Your repository names or account | **No.** The pool is named from *their* configured prefix under *their* account. |

Prove it yourself before publishing anything:

```bash
# nothing that looks like a token in the executable
python - <<'PY'
import re
blob = open("dist/Freehold.exe", "rb").read()
hits = re.findall(rb"ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}", blob)
print("token-shaped strings found:", len(hits))
PY
```

It should print `0`. The release workflow builds from a clean checkout on a
fresh runner, which never sees your machine, so there is nothing for it to
pick up — but checking costs nothing.

## The straightforward route: GitHub Releases

Already set up in `.github/workflows/release.yml`. Tag a version and GitHub
builds both apps and attaches them to a release page:

```bash
git tag v0.1.0
git push origin v0.1.0
```

That produces:

- **`Freehold.exe`** — Windows, about 14 MB, no Python needed. Built on a
  clean `windows-latest` runner, and the workflow runs the test suite and
  checks the binary starts before publishing it.
- **`app-debug.apk`** — Android, built on `ubuntu-latest`.

Anyone can then download them from
`https://github.com/YOURNAME/Freehold/releases` — **the repository must be
public for that**, or downloads require a GitHub login with access.

Each release carries the note that the apps contain no credentials and that
the user's key is theirs to look after.

## Two things people will hit, and what to tell them

**Windows SmartScreen.** An unsigned executable from a small project will
show "Windows protected your PC". The user clicks *More info* → *Run anyway*.
This is normal and not a sign of anything wrong. Removing it means buying a
code-signing certificate (roughly $100–400 a year, and an EV certificate for
immediate reputation). Worth it only if you are distributing widely.

**Android "unknown developer".** The APK is signed with the standard debug
key, so Android warns and asks the user to allow installs from unknown
sources. To avoid it, generate a release keystore, add it to the repository's
secrets, and sign in CI:

```bash
keytool -genkey -v -keystore freehold.jks -keyalg RSA \
        -keysize 2048 -validity 10000 -alias freehold
```

Then add `KEYSTORE_BASE64`, `KEYSTORE_PASSWORD` and `KEY_ALIAS` as GitHub
secrets and a signing config to `android/app/build.gradle.kts`. The Play
Store needs that plus a developer account ($25, once).

## Other ways to distribute

- **Direct download** — put the two files anywhere that serves files. They
  are self-contained.
- **winget / Chocolatey / Scoop** — Windows package managers. Scoop is the
  least ceremony; winget wants a signed installer.
- **F-Droid** — a good fit for a non-commercial Android app, but it requires
  a reproducible build and an OSI-approved licence. PolyForm Noncommercial
  does not qualify, so this would mean relicensing.
- **PyPI** — `pip install freehold` for people who already have Python.
  Note that PyPI expects a licence it recognises; a non-commercial licence is
  allowed but will show as "other".

## The licence is the real constraint on sharing

Freehold is under **PolyForm Noncommercial 1.0.0**. Anyone may use it for
personal, educational, charitable or research purposes, and may share it on.
Commercial use is not granted.

That restriction means it is **source-available, not open source** in the
formal sense, which has practical consequences worth knowing before you
publish:

- GitHub will not show a standard licence badge.
- F-Droid and most Linux distributions will not package it.
- Some companies filter out non-OSI dependencies automatically.
- Contributors who only work on open-source projects may pass.

None of that stops an individual downloading and using it, which is the main
case. If you later want the formal label, Apache-2.0 or AGPL-3.0 would give
it — AGPL still discourages a company taking your work closed. Changing is
easy now and hard once other people have contributed code.

## A note on supporting people

Once strangers use this, you will get questions. The one that matters:

> I lost my key file. How do I get my files back?

There is no answer. That is the design — nobody, including you, can decrypt
their blocks without their key. Make sure this is unmissable wherever you
publish, because a user who learns it after losing data will feel misled even
though the README said so. The first-run output prints it in capitals for
exactly that reason.
