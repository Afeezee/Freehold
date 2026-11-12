"""Replication: the thing that makes dropping a vendor actually possible.

Dedup is global, so a second backup with a new backend selected uploads
nothing. Without replication, "add R2 and stop using GitHub" would leave the
new backend empty and the old one still load-bearing.
"""
from __future__ import annotations

import filecmp
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freehold.core import Engine, KeyRing, Manifest  # noqa: E402
from freehold.core.errors import FreeholdError  # noqa: E402

SMALL = {"min_size": 2048, "avg_size": 4096, "max_size": 8192}


def rnd(n: int, seed: int) -> bytes:
    return random.Random(seed).randbytes(n)


def dirs_equal(a: Path, b: Path) -> bool:
    left = sorted(p.relative_to(a).as_posix() for p in a.rglob("*")
                  if p.is_file())
    right = sorted(p.relative_to(b).as_posix() for p in b.rglob("*")
                   if p.is_file())
    return left == right and all(
        filecmp.cmp(a / r, b / r, shallow=False) for r in left)


@pytest.fixture
def two_backends(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.bin").write_bytes(rnd(70_000, 21))
    (src / "b.txt").write_text("vendor independence\n" * 300)
    m = Manifest(tmp_path / "m.db")
    k = KeyRing.generate()
    m.add_backend("old", "local", {"path": str(tmp_path / "old")})
    m.add_backend("new", "local", {"path": str(tmp_path / "new")})
    m.set_backend_enabled("new", False)      # not a write target yet
    eng = Engine(m, k, chunk_params=SMALL)
    first = eng.backup(src, device="pc")
    return eng, m, k, src, tmp_path, first


def test_a_plain_backup_does_not_populate_a_new_backend(two_backends):
    """The behaviour that makes replication necessary, pinned down."""
    eng, m, k, src, tmp, first = two_backends
    m.set_backend_enabled("new", True)
    second = eng.backup(src, device="pc", backends=["new"])
    assert second.chunks_new == 0           # global dedup: nothing to send
    assert not list((tmp / "new").rglob("*.*"))


def test_replicate_fills_the_new_backend(two_backends):
    eng, m, k, src, tmp, first = two_backends
    tally = eng.replicate("new")
    assert tally["copied"] == first.chunks_new
    assert tally["already"] == 0 and tally["failed"] == 0
    assert tally["bytes"] > 0

    rows = {r["name"]: r for r in m.usage_by_backend()}
    assert rows["new"]["chunk_count"] == rows["old"]["chunk_count"]


def test_the_old_backend_can_then_be_dropped(two_backends):
    """The actual promise: after replicating, the first vendor is no longer
    load-bearing and a restore works without it."""
    eng, m, k, src, tmp, first = two_backends
    eng.replicate("new")

    (tmp / "old" / ".offline").write_text("")      # pretend GitHub is gone
    out = tmp / "out"
    fresh = Engine(m, k, chunk_params=SMALL)
    r = fresh.restore(out, device="pc")
    assert r.ok, r.failed
    assert dirs_equal(src, out)


def test_replicating_twice_copies_nothing_the_second_time(two_backends):
    eng, m, k, src, tmp, first = two_backends
    eng.replicate("new")
    again = eng.replicate("new")
    assert again["copied"] == 0
    assert again["already"] == first.chunks_new


def test_replication_reseals_rather_than_copying_ciphertext(two_backends):
    """Each backend gets its own nonce, so an observer with both cannot tell
    that two blobs are the same chunk by comparing bytes."""
    eng, m, k, src, tmp, first = two_backends
    eng.replicate("new")
    old = {p.name: p.read_bytes() for p in (tmp / "old").rglob("*")
           if p.is_file()}
    new = {p.name: p.read_bytes() for p in (tmp / "new").rglob("*")
           if p.is_file()}
    assert set(old) == set(new)                 # same blob ids
    assert all(old[n] != new[n] for n in old)   # different ciphertext
    assert all(len(old[n]) == len(new[n]) for n in old)


def test_replication_reports_what_it_could_not_copy(two_backends):
    eng, m, k, src, tmp, first = two_backends
    for blob in (tmp / "old").rglob("*"):
        if blob.is_file():
            blob.unlink()                       # source data is gone
    tally = eng.replicate("new")
    assert tally["copied"] == 0
    assert tally["failed"] == first.chunks_new


def test_replicate_rejects_an_unknown_backend(two_backends):
    eng, m, k, src, tmp, first = two_backends
    with pytest.raises(FreeholdError):
        eng.replicate("nowhere")
