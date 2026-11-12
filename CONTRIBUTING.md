# Contributing

## Before anything else

Run the tests. They are the specification.

```bash
pip install -e ".[dev]"
python -m pytest tests -q
```

143 tests, about two minutes. No network access and no credentials are
needed: the GitHub and S3 backends are exercised against an in-memory API and
a local HTTP server respectively.

## The one rule that matters

**`freehold/core/` must never learn what a backend is.**

A test enforces it (`test_core_engine_has_no_github_knowledge`) by reading
the core modules and failing if the words *github*, *repo*, *bucket*,
*asset*, *lfs*, *s3* or *r2* appear — in code or in comments. If you need the
core to know something vendor-specific in order to add a backend, the
abstraction is wrong and the fix belongs in the interface, not in a special
case.

A driver gets exactly two channels for things the core cannot understand:
`state` (key/value storage in the manifest) and `stats().detail` /
`drain_warnings()` for reporting. The GitHub driver runs a whole rotating
repo pool through those and the core is none the wiser.

## Adding a storage backend

1. New module in `freehold/drivers/`, subclass `StorageDriver`, implement
   `put`, `get`, `list`, `delete`. Optionally `stats`, `health`, `find`,
   `drain_warnings`, `close`.
2. `@register` it and import it in `freehold/drivers/__init__.py`.
3. Thread safety: `put` and `get` are called concurrently. Guard your own
   bookkeeping, and do the network call *outside* that lock.
4. Never require plaintext. You receive ciphertext and a blinded blob id.
   A driver that wants the real filename or hash is a design error.
5. Tests: copy the shape of `tests/test_s3.py`, which runs the real driver
   against a local server over real HTTP. Mocks that only agree with your own
   code prove very little.

If you touched `freehold/core/` to make it work, say so in the pull request
and explain why — that is a conversation about the interface, not a detail.

## Style

Plain Python, standard library first. The only runtime dependency is
`cryptography`, and adding a second one needs a reason. Lines under 80
columns. Comments explain *why*, not what.

## What a good pull request looks like

- A test that fails before and passes after.
- An honest note about what you did **not** verify. "Tested against a fake,
  not against the real endpoint" is useful; silence is not.
- No reformatting of code you did not otherwise change.

## Security

Found a flaw in the crypto, the key handling, or the dashboard's access
control? Open a private security advisory on GitHub rather than a public
issue. Please include what an attacker would have to already have.

Things that are deliberate, not bugs: the backend learns chunk sizes and
upload timing; the manifest is not hidden from someone holding your key; and
an unattended agent must hold an unwrapped key.
