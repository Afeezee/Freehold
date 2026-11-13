"""A backup that stores nothing must never look like a backup.

These exist because of a real incident: a storage location was added with a
placeholder typed in literally ("<name>"), every upload failed, and each run
was recorded as 'complete' with 0 files — indistinguishable from backing up
an empty folder. 8,818 chunks had no storage location and the interface said
everything was fine.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freehold.core import Engine, KeyRing, Manifest  # noqa: E402
from freehold.core.driver import build_driver  # noqa: E402
from freehold.core.errors import ConfigError  # noqa: E402
from freehold.dashboard import DashboardAPI  # noqa: E402

SMALL = {"min_size": 2048, "avg_size": 4096, "max_size": 8192}


def tree(root: Path, n: int = 3) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (root / ("f%d.bin" % i)).write_bytes(
            random.Random(i).randbytes(40_000))


@pytest.fixture
def broken(tmp_path):
    """A manifest whose only storage refuses every write."""
    src = tmp_path / "src"
    tree(src)
    m = Manifest(tmp_path / "m.db")
    store = tmp_path / "store"
    store.mkdir()
    (store / ".offline").write_text("")      # the local driver plays dead
    m.add_backend("broken", "local", {"path": str(store)})
    return Engine(m, KeyRing.generate(), chunk_params=SMALL), m, src, tmp_path


# -- the core guarantee ---------------------------------------------------
def test_a_backup_that_stores_nothing_is_marked_failed(broken):
    eng, m, src, tmp = broken
    res = eng.backup(src, device="pc")

    assert res.failed is True
    assert res.files == 0
    assert res.dropped_files == 3
    assert any("nothing was backed up" in e for e in res.errors)

    snaps = m.snapshots(complete_only=False)
    assert len(snaps) == 1 and snaps[0].status == "failed"
    # and therefore it is not offered as something to restore from
    assert m.snapshots(complete_only=True) == []


def test_a_failed_backup_cannot_be_restored_from(broken):
    eng, m, src, tmp = broken
    eng.backup(src, device="pc")
    from freehold.core.errors import FreeholdError
    with pytest.raises(FreeholdError):
        eng.restore(tmp / "out", device="pc")


def test_the_dashboard_says_the_backup_failed(broken):
    eng, m, src, tmp = broken
    eng.backup(src, device="pc")
    api = DashboardAPI(tmp, m, eng.k)
    health = api.health()
    assert health["state"] == "bad"
    assert health["failing"] is True
    assert "did not work" in health["headline"]
    assert str(src) in health["failed_folder"]


def test_a_partial_failure_keeps_what_did_work(tmp_path):
    """One unstorable file must not discard the rest — but it must be
    counted and reported, not quietly dropped."""
    src = tmp_path / "src"
    tree(src, n=3)
    m = Manifest(tmp_path / "m.db")
    k = KeyRing.generate()
    m.add_backend("disk", "local", {"path": str(tmp_path / "store")})
    eng = Engine(m, k, chunk_params=SMALL)

    from freehold.drivers.local import LocalDriver
    real_put = LocalDriver.put
    doomed = {"hash": None}

    def flaky(self, blob_id, data):
        # fail exactly one chunk, whichever arrives third
        doomed["n"] = doomed.get("n", 0) + 1
        if doomed["n"] == 3:
            raise OSError("disk went away")
        return real_put(self, blob_id, data)

    LocalDriver.put = flaky
    try:
        res = eng.backup(src, device="pc")
    finally:
        LocalDriver.put = real_put

    assert res.failed is False          # some of it genuinely worked
    assert res.dropped_files >= 1
    assert res.files >= 1
    assert m.snapshots(complete_only=True)
    listed = [f.path for f in m.files(res.snapshot_id)]
    assert len(listed) == res.files     # the snapshot lists only what is safe


# -- catching it at the source --------------------------------------------
def test_a_placeholder_account_name_is_refused():
    """The exact input that caused the incident."""
    with pytest.raises(ConfigError) as exc:
        build_driver("github", {"owner": "<Afeezee>", "token": "x"})
    assert "not a GitHub username" in str(exc.value)

    for bad in ("", "  ", "my name", "owner/repo", "-leading", "trailing-",
                "a" * 40):
        with pytest.raises(ConfigError):
            build_driver("github", {"owner": bad, "token": "x"})

    # ordinary names still work
    assert build_driver("github", {"owner": "Afeezee", "token": "x"})
    assert build_driver("github", {"owner": "some-org-7", "token": "x"})


def test_storage_is_verified_before_it_is_accepted(tmp_path):
    m = Manifest(tmp_path / "m.db")
    api = DashboardAPI(tmp_path, m, KeyRing.generate())
    store = tmp_path / "nope"
    store.mkdir()
    (store / ".offline").write_text("")

    with pytest.raises(ConfigError) as exc:
        api.add_backend("dead", "local", {"path": str(store)})
    assert "could not be reached" in str(exc.value)
    assert api.backends() == []          # nothing was stored

    ok = tmp_path / "good"
    assert api.add_backend("live", "local", {"path": str(ok)})
    assert [b["name"] for b in api.backends()] == ["live"]
    m.close()
