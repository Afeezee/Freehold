"""Deduplication means a second backend does not automatically hold a
second copy.

Found in practice: a folder was backed up to GitHub and local disk at the
same time. 97 of its 115 blocks were new and went to both; the other 18
already existed on disk from an earlier backup, so deduplication skipped
them and GitHub never received them. Restoring from GitHub alone produced
98 of 115 files. Nothing said so.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freehold.core import Engine, KeyRing, Manifest  # noqa: E402

SMALL = {"min_size": 2048, "avg_size": 4096, "max_size": 8192}


def rnd(n, seed):
    return random.Random(seed).randbytes(n)


@pytest.fixture
def two_places(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.bin").write_bytes(rnd(60_000, 1))
    m = Manifest(tmp_path / "m.db")
    k = KeyRing.generate()
    m.add_backend("first", "local", {"path": str(tmp_path / "one")})
    m.add_backend("second", "local", {"path": str(tmp_path / "two")})
    return m, k, src, tmp_path


def test_a_later_backend_is_told_it_has_an_incomplete_copy(two_places):
    m, k, src, tmp = two_places
    # first backup goes only to "first"
    eng = Engine(m, k, chunk_params=SMALL)
    first = eng.backup(src, device="pc", backends=["first"])
    assert first.chunks_new > 0
    assert first.incomplete == []        # nothing was skipped

    # now back up the same folder to both: every block already exists, so
    # nothing is uploaded and "second" ends up holding none of it
    second = eng.backup(src, device="pc", backends=["first", "second"])
    assert second.chunks_new == 0
    assert second.incomplete == ["second"]
    assert any("sync second" in w for w in second.warnings)
    assert any("complete copy" in w for w in second.warnings)


def test_the_warning_is_true_a_restore_really_fails(two_places):
    m, k, src, tmp = two_places
    eng = Engine(m, k, chunk_params=SMALL)
    eng.backup(src, device="pc", backends=["first"])
    eng.backup(src, device="pc", backends=["first", "second"])

    # take the complete copy away
    (tmp / "one" / ".offline").write_text("")
    r = Engine(m, k, chunk_params=SMALL).restore(tmp / "out", device="pc")
    assert not r.ok                      # exactly what the warning predicted

    # sync fills the gap, and then it works
    (tmp / "one" / ".offline").unlink()
    eng2 = Engine(m, k, chunk_params=SMALL)
    tally = eng2.replicate("second")
    assert tally["copied"] > 0

    (tmp / "one" / ".offline").write_text("")
    good = Engine(m, k, chunk_params=SMALL).restore(tmp / "out2", device="pc")
    assert good.ok, good.failed
    assert good.served_by == ["second"]


def test_no_warning_when_every_backend_got_everything(two_places):
    m, k, src, tmp = two_places
    eng = Engine(m, k, chunk_params=SMALL)
    res = eng.backup(src, device="pc", backends=["first", "second"])
    assert res.chunks_new > 0
    assert res.incomplete == []
    assert not any("complete copy" in w for w in res.warnings)
