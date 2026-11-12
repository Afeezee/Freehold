"""The scenario the whole project exists for.

A machine dies. All that survives is the encryption key and the backend
credentials. Everything else -- the manifest, the config, Freehold's own
state -- went with it. Can the files come back?
"""
from __future__ import annotations

import filecmp
import random
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fake_github import FakeGitHub  # noqa: E402
from freehold.core import Engine, KeyRing, Manifest  # noqa: E402
from freehold.core.driver import build_driver  # noqa: E402
from freehold.core.errors import FreeholdError, IntegrityError  # noqa: E402
from freehold.core.manifest import ManifestState  # noqa: E402
from freehold.core.portable import (export_manifest, import_manifest,  # noqa: E402
                                    manifest_blob_id, pull_manifest,
                                    push_manifest)

SMALL = {"min_size": 2048, "avg_size": 4096, "max_size": 8192}


def rnd(n: int, seed: int) -> bytes:
    return random.Random(seed).randbytes(n)


@pytest.fixture(autouse=True)
def clean():
    FakeGitHub.reset()
    yield
    FakeGitHub.reset()


def tree(root: Path) -> None:
    (root / "docs").mkdir(parents=True)
    (root / "media").mkdir()
    (root / "docs" / "thesis.txt").write_text("chapter one\n" * 2000)
    (root / "docs" / "refs.bib").write_text("@book{x}\n" * 200)
    (root / "media" / "clip.bin").write_bytes(rnd(120_000, 77))


def dirs_equal(a: Path, b: Path) -> bool:
    left = sorted(p.relative_to(a).as_posix() for p in a.rglob("*")
                  if p.is_file())
    right = sorted(p.relative_to(b).as_posix() for p in b.rglob("*")
                   if p.is_file())
    return left == right and all(
        filecmp.cmp(a / r, b / r, shallow=False) for r in left)


def drivers_for(m, names=None):
    return [(b.name, build_driver(b.driver_id, b.config,
                                  ManifestState(m, b.id)))
            for b in m.backends(only_enabled=True)
            if names is None or b.name in names]


# -- the manifest blob ----------------------------------------------------
def test_manifest_address_comes_from_the_key(tmp_path):
    a, b = KeyRing.generate(), KeyRing.generate()
    assert manifest_blob_id(a) == manifest_blob_id(a)
    assert manifest_blob_id(a) != manifest_blob_id(b)
    assert len(manifest_blob_id(a)) == 64


def test_manifest_blob_is_encrypted_and_round_trips(tmp_path):
    m = Manifest(tmp_path / "m.db")
    m.add_backend("disk", "local", {"path": str(tmp_path / "blobs")})
    m.start_snapshot("pc", str(tmp_path / "src"))
    k = KeyRing.generate()

    blob = export_manifest(m, k)
    assert b"SQLite format" not in blob          # not sitting there in clear
    assert b"disk" not in blob

    out = import_manifest(blob, k, tmp_path / "recovered.db")
    recovered = Manifest(out)
    assert [b.name for b in recovered.backends()] == ["disk"]
    recovered.close()
    m.close()


def test_the_wrong_key_cannot_read_the_manifest(tmp_path):
    m = Manifest(tmp_path / "m.db")
    blob = export_manifest(m, KeyRing.generate())
    m.close()
    with pytest.raises(IntegrityError):
        import_manifest(blob, KeyRing.generate(), tmp_path / "no.db")
    with pytest.raises(IntegrityError):
        import_manifest(b"garbage", KeyRing.generate(), tmp_path / "no.db")


# -- the real thing, on local disk ---------------------------------------
def test_lost_device_restore_from_key_and_credentials_only(tmp_path):
    src = tmp_path / "src"
    tree(src)
    vault = tmp_path / "vault"           # survives; stands in for a backend

    old_home = tmp_path / "old-laptop"
    old_home.mkdir()
    key = KeyRing.generate()
    key.save(old_home / "key.json")
    m = Manifest(old_home / "manifest.db")
    m.add_backend("vault", "local", {"path": str(vault)})
    Engine(m, key, chunk_params=SMALL).backup(src, device="old-laptop")
    push_manifest(m, key, drivers_for(m))
    m.close()

    # the laptop is gone: home, manifest, config, everything
    shutil.rmtree(old_home)
    assert not old_home.exists()

    # a replacement machine. The user has their key file and knows the
    # backend. Nothing else was carried across.
    new_home = tmp_path / "new-laptop"
    new_home.mkdir()
    carried_key = KeyRing(key.master)
    driver = build_driver("local", {"path": str(vault)})
    pull_manifest(driver, carried_key, new_home / "manifest.db")

    m2 = Manifest(new_home / "manifest.db")
    out = tmp_path / "restored"
    result = Engine(m2, carried_key, chunk_params=SMALL).restore(
        out, device="old-laptop")
    assert result.ok, result.failed
    assert dirs_equal(src, out)
    m2.close()


def test_pull_says_so_when_nothing_was_pushed(tmp_path):
    driver = build_driver("local", {"path": str(tmp_path / "empty")})
    with pytest.raises(FreeholdError) as exc:
        pull_manifest(driver, KeyRing.generate(), tmp_path / "x.db")
    assert "no manifest found" in str(exc.value)


# -- the full MVP scenario: GitHub, rotation, one backend dark ------------
def test_lost_device_with_github_rotation_and_a_dead_backend(tmp_path):
    """Everything at once: a GitHub pool that rotated across repos, a second
    backend, the device gone, and one of the two backends unreachable during
    the recovery."""
    import test_github  # noqa: F401  (registers github-test)

    src = tmp_path / "src"
    tree(src)
    mirror = tmp_path / "mirror"

    old_home = tmp_path / "old"
    old_home.mkdir()
    key = KeyRing.generate()
    key.save(old_home / "key.json")
    m = Manifest(old_home / "manifest.db")
    gh_cfg = {"owner": "tester", "store": "lost", "prefix": "fh",
              "soft_cap": "60000"}
    m.add_backend("gh", "github-test", gh_cfg)
    m.add_backend("mirror", "local", {"path": str(mirror)})
    eng = Engine(m, key, redundancy=2, chunk_params=SMALL)
    eng.backup(src, device="old")

    pool = m.get_backend_state(1, "pool")
    assert len(pool["repos"]) > 1, "the pool should have rotated"
    push_manifest(m, key, drivers_for(m))
    m.close()
    shutil.rmtree(old_home)

    # replacement machine: key + GitHub credentials. It has never seen the
    # repo pool, so the driver has to rediscover it from GitHub.
    new_home = tmp_path / "new"
    new_home.mkdir()
    carried = KeyRing(key.master)
    fresh_driver = build_driver("github-test", gh_cfg)
    pull_manifest(fresh_driver, carried, new_home / "manifest.db")

    m2 = Manifest(new_home / "manifest.db")
    assert {b.name for b in m2.backends()} == {"gh", "mirror"}

    # now kill the mirror: the restore must come entirely from GitHub
    shutil.rmtree(mirror)
    (tmp_path / "mirror").mkdir()
    (tmp_path / "mirror" / ".offline").write_text("")

    out = tmp_path / "restored"
    result = Engine(m2, carried, chunk_params=SMALL).restore(out,
                                                             device="old")
    assert result.ok, result.failed
    assert dirs_equal(src, out)
    m2.close()


def test_recovered_pool_keeps_one_active_repo(tmp_path):
    """After rediscovery the driver must still know where to write next."""
    import test_github  # noqa: F401
    src = tmp_path / "src"
    tree(src)
    home = tmp_path / "h"
    home.mkdir()
    key = KeyRing.generate()
    m = Manifest(home / "manifest.db")
    cfg = {"owner": "tester", "store": "pool", "prefix": "fh",
           "soft_cap": "60000"}
    m.add_backend("gh", "github-test", cfg)
    Engine(m, key, chunk_params=SMALL).backup(src, device="pc")
    push_manifest(m, key, drivers_for(m))
    m.close()

    fresh = Manifest(tmp_path / "fresh.db")
    bid = fresh.add_backend("gh", "github-test", cfg)
    driver = build_driver("github-test", cfg, ManifestState(fresh, bid))
    assert driver.find(manifest_blob_id(key))
    rebuilt = fresh.get_backend_state(bid, "pool")
    assert len(rebuilt["repos"]) > 1
    assert sum(1 for r in rebuilt["repos"] if r["status"] == "active") == 1
    fresh.close()


def test_push_reports_a_backend_that_refused(tmp_path):
    src = tmp_path / "src"
    tree(src)
    home = tmp_path / "h"
    home.mkdir()
    key = KeyRing.generate()
    m = Manifest(home / "manifest.db")
    m.add_backend("ok", "local", {"path": str(tmp_path / "ok")})
    m.add_backend("bad", "local", {"path": str(tmp_path / "bad"),
                                   "fail": "1"})
    Engine(m, key, chunk_params=SMALL).backup(src, device="pc",
                                              backends=["ok"])
    results = push_manifest(m, key, drivers_for(m))
    assert not results["ok"].startswith("error:")
    assert results["bad"].startswith("error:")
    m.close()


# -- the agent does this on its own --------------------------------------
def test_agent_pushes_the_manifest_after_a_pass(tmp_path):
    from freehold.agent import Agent, AgentConfig, Link, NetworkPolicy
    from freehold.agent import network as net

    src = tmp_path / "src"
    tree(src)
    home = tmp_path / "home"
    home.mkdir()
    key = KeyRing.generate()
    m = Manifest(home / "manifest.db")
    m.add_backend("disk", "local", {"path": str(tmp_path / "blobs")})
    cfg = AgentConfig(device="pc")
    cfg.add_folder(str(src))
    assert cfg.push_manifest is True        # on by default
    policy = NetworkPolicy(cfg.network, probe=lambda: Link(net.WIFI))
    agent = Agent(home, cfg, m, key, policy=policy)
    agent.cfg.workers = 4

    assert agent.run_once(force=True)[0].ok
    driver = build_driver("local", {"path": str(tmp_path / "blobs")})
    assert pull_manifest(driver, key, tmp_path / "pulled.db")
    m.close()
