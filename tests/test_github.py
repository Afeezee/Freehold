"""GitHub driver: release assets, repo pool rotation, LFS fallback.

The driver under test is the real one. Only the HTTP transport is faked, so
pool rotation, location handling and quota logic are exercised for real.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fake_github import FakeGitHub  # noqa: E402
from freehold.core import Engine, KeyRing, Manifest  # noqa: E402
from freehold.core.driver import register  # noqa: E402
from freehold.core.errors import ConfigError  # noqa: E402
from freehold.drivers import github as gh  # noqa: E402

SMALL = {"min_size": 2048, "avg_size": 8192, "max_size": 32768}


@register
class _TestGitHubDriver(gh.GitHubDriver):
    """Same driver, transport swapped. config['store'] names the fake server
    so a driver rebuilt from the manifest reconnects to the same one."""
    driver_id = "github-test"

    def __init__(self, config, state=None, api=None):
        super().__init__(config, state,
                         api=api or FakeGitHub.shared(config.get("store", "s")))


def rnd(n: int, seed: int) -> bytes:
    return random.Random(seed).randbytes(n)


@pytest.fixture(autouse=True)
def clean():
    FakeGitHub.reset()
    yield
    FakeGitHub.reset()


def make(tmp_path, src_bytes=400_000, **cfg):
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "a.bin").write_bytes(rnd(src_bytes, 5))
    (src / "note.txt").write_text("plans for the weekend\n" * 50)
    m = Manifest(tmp_path / "m.db")
    k = KeyRing.generate()
    config = {"owner": "tester", "store": "s", "prefix": "fh"}
    config.update({key: str(v) for key, v in cfg.items()})
    m.add_backend("gh", "github-test", config)
    return Engine(m, k, chunk_params=SMALL), m, k, src


def dirs_equal(a: Path, b: Path) -> bool:
    import filecmp
    left = sorted(p.relative_to(a).as_posix() for p in a.rglob("*")
                  if p.is_file())
    right = sorted(p.relative_to(b).as_posix() for p in b.rglob("*")
                   if p.is_file())
    return left == right and all(
        filecmp.cmp(a / r, b / r, shallow=False) for r in left)


# -- the plain path -------------------------------------------------------
def test_backup_and_restore_through_releases(tmp_path):
    eng, m, k, src = make(tmp_path)
    res = eng.backup(src, device="pc")
    assert res.chunks_new > 0
    server = FakeGitHub.shared("s")
    assert len(server.assets) == res.chunks_new
    out = tmp_path / "out"
    assert eng.restore(out).ok
    assert dirs_equal(src, out)


def test_nothing_recognisable_reaches_github(tmp_path):
    eng, m, k, src = make(tmp_path)
    eng.backup(src, device="pc")
    server = FakeGitHub.shared("s")
    blobs = b"".join(a["data"] for a in server.assets.values())
    assert b"plans for the weekend" not in blobs
    names = [a["name"] for a in server.assets.values()]
    assert all(len(n) == 64 and "." not in n for n in names)  # blinded ids


def test_locations_name_their_repo(tmp_path):
    eng, m, k, src = make(tmp_path)
    eng.backup(src, device="pc")
    rows = m.db.execute("SELECT location FROM chunk_location").fetchall()
    assert rows
    for r in rows:
        owner, rest = r["location"].split("/", 1)
        assert owner == "tester" and "#" in rest


# -- repo pool rotation ---------------------------------------------------
def test_rotation_seals_and_provisions(tmp_path):
    eng, m, k, src = make(tmp_path, src_bytes=600_000, soft_cap=100_000)
    res = eng.backup(src, device="pc")
    assert any("sealed" in w for w in res.warnings), res.warnings
    server = FakeGitHub.shared("s")
    assert len(server.repos) > 3, server.repos
    pool = m.get_backend_state(1, "pool")
    sealed = [r for r in pool["repos"] if r["status"] == "sealed"]
    active = [r for r in pool["repos"] if r["status"] == "active"]
    assert len(active) == 1 and sealed
    # reservation happens before the upload, so the cap holds exactly even
    # with several transfers in flight -- no workers x chunk_size slack
    assert all(r["bytes"] <= 100_000 for r in pool["repos"]), pool["repos"]
    assert sorted(r["name"] for r in pool["repos"])[0] == "fh-001"


def test_restore_spans_sealed_repos_without_searching(tmp_path):
    eng, m, k, src = make(tmp_path, src_bytes=600_000, soft_cap=100_000)
    eng.backup(src, device="pc")
    repos = {r["location"].split("#")[0]
             for r in m.db.execute("SELECT location FROM chunk_location")}
    assert len(repos) > 2  # chunks really are spread across the pool
    server = FakeGitHub.shared("s")
    server.calls.clear()
    out = tmp_path / "out"
    assert eng.restore(out).ok
    assert dirs_equal(src, out)
    # one listing per repo touched, not a scan of the whole pool per chunk
    assert server.calls.count("list_assets") <= len(server.repos)


def test_pool_survives_a_new_process(tmp_path):
    eng, m, k, src = make(tmp_path, src_bytes=300_000, soft_cap=80_000)
    eng.backup(src, device="pc")
    before = len(FakeGitHub.shared("s").repos)
    m.close()

    m2 = Manifest(tmp_path / "m.db")
    eng2 = Engine(m2, k, chunk_params=SMALL)
    (src / "b.bin").write_bytes(rnd(40_000, 6))
    eng2.backup(src, device="pc")
    pool = m2.get_backend_state(1, "pool")
    assert len(pool["repos"]) >= before          # reused, not restarted
    assert sum(1 for r in pool["repos"] if r["status"] == "active") == 1
    assert eng2.restore(tmp_path / "out").ok


def test_put_is_idempotent(tmp_path):
    eng, m, k, src = make(tmp_path)
    d = eng.driver(m.backends()[0])
    loc1 = d.put("a" * 64, b"ciphertext")
    loc2 = d.put("a" * 64, b"ciphertext")
    assert loc1 == loc2
    assert len(FakeGitHub.shared("s").assets) == 1
    assert d.get(loc1) == b"ciphertext"


def test_oversized_blob_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(gh, "RELEASE_ASSET_CAP", 1000)
    eng, m, k, src = make(tmp_path)
    d = eng.driver(m.backends()[0])
    with pytest.raises(ConfigError):
        d.put("b" * 64, b"x" * 1001)


# -- lfs fallback ---------------------------------------------------------
def test_small_blobs_go_to_lfs_when_enabled(tmp_path):
    eng, m, k, src = make(tmp_path, lfs=1, lfs_max=10 ** 9)
    eng.backup(src, device="pc")
    server = FakeGitHub.shared("s")
    assert server.lfs and not server.assets
    assert all(loc["location"].split("#")[1].startswith("lfs/")
               for loc in m.db.execute("SELECT location FROM chunk_location"))
    assert eng.restore(tmp_path / "out").ok
    assert dirs_equal(src, tmp_path / "out")


def test_lfs_quota_stops_before_it_fails_silently(tmp_path):
    eng, m, k, src = make(tmp_path, lfs=1, lfs_max=10 ** 9, lfs_quota=50_000)
    res = eng.backup(src, device="pc")
    server = FakeGitHub.shared("s")
    assert server.lfs and server.assets       # started in LFS, spilled over
    pool = m.get_backend_state(1, "pool")
    assert pool["lfs_bytes"] <= 50_000
    assert any("quota" in w for w in res.warnings), res.warnings
    assert eng.restore(tmp_path / "out").ok   # mixed locations still restore
    assert dirs_equal(src, tmp_path / "out")


def test_lfs_outage_falls_back_to_releases(tmp_path):
    eng, m, k, src = make(tmp_path, lfs=1, lfs_max=10 ** 9)
    FakeGitHub.shared("s").lfs_offline = True
    eng.backup(src, device="pc")
    server = FakeGitHub.shared("s")
    assert server.assets and not server.lfs
    assert eng.restore(tmp_path / "out").ok


# -- degradation ----------------------------------------------------------
def test_rate_limited_github_degrades_rather_than_failing(tmp_path):
    eng, m, k, src = make(tmp_path)
    m.add_backend("disk", "local", {"path": str(tmp_path / "disk")})
    eng2 = Engine(m, k, redundancy=2, chunk_params=SMALL)
    eng2.backup(src, device="pc")

    FakeGitHub.shared("s").offline = True
    eng3 = Engine(m, k, chunk_params=SMALL)
    out = tmp_path / "out"
    r = eng3.restore(out)
    assert r.ok, r.failed            # the mirror carried it
    assert "gh" in r.unreachable_backends
    assert dirs_equal(src, out)


def test_github_only_outage_names_what_is_stranded(tmp_path):
    eng, m, k, src = make(tmp_path)
    eng.backup(src, device="pc")
    FakeGitHub.shared("s").offline = True
    eng2 = Engine(m, k, chunk_params=SMALL)
    r = eng2.restore(tmp_path / "out")
    assert not r.ok
    assert r.failed[0].chunk and "gh" in r.failed[0].backends
    assert "gh" in r.unreachable_backends


# -- the point of the exercise -------------------------------------------
def test_core_engine_has_no_github_knowledge():
    import inspect
    import re
    from freehold.core import chunker, crypto, driver, engine, manifest
    for mod in (engine, manifest, driver, crypto, chunker):
        text = inspect.getsource(mod).lower()
        # "releases" not "release": the engine legitimately calls
        # Semaphore.release(), which is a threading term, not a vendor one.
        for word in ("github", "releases", "repo", "repos", "lfs", "r2", "s3",
                     "bucket", "asset", "assets"):
            assert not re.search(r"\b" + word + r"\b", text), \
                mod.__name__ + " mentions " + word
