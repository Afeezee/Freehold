"""Mobile client: the same engine, adapted to a phone's constraints.

The load-bearing test is the last one — that this reuses the core rather than
reimplementing chunking, dedup or encryption, and that a phone's backup is
restorable by the desktop and vice versa.
"""
from __future__ import annotations

import filecmp
import json
import random
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freehold.agent import network as net  # noqa: E402
from freehold.core import Engine, KeyRing, Manifest  # noqa: E402
from freehold.core.errors import ConfigError  # noqa: E402
from freehold.mobile import Conditions, MobileSession, handle  # noqa: E402
from freehold.mobile import bridge  # noqa: E402

SMALL = {"min_size": 2048, "avg_size": 4096, "max_size": 8192}


def rnd(n: int, seed: int) -> bytes:
    return random.Random(seed).randbytes(n)


@pytest.fixture(autouse=True)
def small_chunks(monkeypatch):
    real = Engine.__init__

    def patched(self, manifest, keyring, redundancy=1, chunk_params=None,
                workers=8):
        real(self, manifest, keyring, redundancy=redundancy,
             chunk_params=chunk_params or SMALL, workers=workers)
    monkeypatch.setattr(Engine, "__init__", patched)
    yield
    bridge.close_all()


def photos(root: Path, n: int = 5, size: int = 40_000) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (root / ("IMG_%04d.jpg" % i)).write_bytes(rnd(size, 900 + i))


@pytest.fixture
def phone(tmp_path):
    home = tmp_path / "phonehome"
    key = KeyRing.generate()
    session = MobileSession.enrol(home, key.master.hex(), "pixel")
    session.m.add_backend("disk", "local", {"path": str(tmp_path / "blobs")})
    cam = tmp_path / "DCIM"
    photos(cam)
    session.add_folder(str(cam))
    return session, cam, home, key, tmp_path


def dirs_equal(a: Path, b: Path) -> bool:
    left = sorted(p.relative_to(a).as_posix() for p in a.rglob("*")
                  if p.is_file())
    right = sorted(p.relative_to(b).as_posix() for p in b.rglob("*")
                   if p.is_file())
    return left == right and all(
        filecmp.cmp(a / r, b / r, shallow=False) for r in left)


# -- enrolment ------------------------------------------------------------
def test_enrol_takes_the_users_key_not_a_new_one(tmp_path):
    key = KeyRing.generate()
    session = MobileSession.enrol(tmp_path / "h", key.master.hex(), "pixel")
    assert session.k.master == key.master
    assert (tmp_path / "h" / "key.json").exists()
    assert session.cfg.device == "pixel"
    session.close()

    reopened = MobileSession.open(tmp_path / "h")
    assert reopened.k.master == key.master
    reopened.close()


def test_enrol_rejects_a_key_that_is_not_a_key(tmp_path):
    with pytest.raises(ConfigError):
        MobileSession.enrol(tmp_path / "h", "not-hex", "pixel")
    with pytest.raises(ConfigError):
        MobileSession.enrol(tmp_path / "h", "ab" * 16, "pixel")  # too short


def test_open_without_a_key_is_refused(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(ConfigError):
        MobileSession.open(tmp_path / "empty")


# -- conditions the phone reports -----------------------------------------
def test_low_battery_defers(phone):
    session, cam, home, key, tmp = phone
    ok, why = session.should_sync(Conditions(network=net.WIFI,
                                             charging=False, battery_pct=5))
    assert not ok and "battery" in why
    ok, _ = session.should_sync(Conditions(network=net.WIFI, charging=True,
                                           battery_pct=5))
    assert ok, "charging overrides the battery floor"


def test_require_charging_is_opt_in(phone):
    session, cam, home, key, tmp = phone
    assert session.cfg.mobile.require_charging is False
    session.cfg.mobile.require_charging = True
    ok, why = session.should_sync(Conditions(network=net.WIFI,
                                             charging=False))
    assert not ok and why == "not charging"


def test_cellular_syncs_by_default(phone):
    """Same decision as the desktop agent: mobile data is a normal link."""
    session, cam, home, key, tmp = phone
    ok, _ = session.should_sync(Conditions(network=net.CELLULAR,
                                           charging=False, battery_pct=80))
    assert ok
    out = session.sync(Conditions(network=net.CELLULAR))
    assert out.state == "complete", out.reason
    assert out.files == 5 and out.uploaded > 0


def test_offline_is_still_refused(phone):
    session, cam, home, key, tmp = phone
    out = session.sync(Conditions(network=net.OFFLINE))
    assert out.state == "skipped" and out.reason == "offline"
    assert out.more_to_do is True


# -- background windows ---------------------------------------------------
def test_a_byte_budget_stops_cleanly_and_resumes(phone):
    session, cam, home, key, tmp = phone
    first = session.sync(Conditions(network=net.WIFI), max_bytes=50_000)
    assert first.state == "partial"
    assert first.more_to_do is True
    assert first.uploaded > 0

    # nothing restorable yet, but the chunks that landed are kept
    assert session.m.snapshots(complete_only=True) == []
    partial_chunks = session.m.totals()["chunks"]
    assert partial_chunks > 0

    second = session.sync(Conditions(network=net.WIFI))
    assert second.state == "complete", second.reason
    assert session.m.snapshots(complete_only=True)

    out = tmp / "restored"
    result = session.restore(
        session.m.latest_snapshot("pixel").id, str(out))
    assert result["ok"], result["failed"]
    assert dirs_equal(cam, out)


def test_a_time_budget_stops_cleanly(phone):
    session, cam, home, key, tmp = phone
    ticks = [1000.0]

    def clock():
        ticks[0] += 0.4          # every check advances the clock
        return ticks[0]

    session.clock = clock
    out = session.sync(Conditions(network=net.WIFI), budget_seconds=1)
    assert out.state == "partial" and out.more_to_do


def test_a_missing_folder_is_reported_not_fatal(phone):
    session, cam, home, key, tmp = phone
    session.add_folder(str(tmp))
    session.cfg.folders[1].path = str(tmp / "no-such-folder")
    out = session.sync(Conditions(network=net.WIFI))
    states = {f.path.split("\\")[-1].split("/")[-1]: f.state
              for f in out.folders}
    assert "done" in states.values() and "skipped" in states.values()


# -- restore from the phone ----------------------------------------------
def test_browse_and_selective_restore(phone):
    session, cam, home, key, tmp = phone
    session.sync(Conditions(network=net.WIFI))
    snap = session.m.latest_snapshot("pixel").id

    tree = session.browse(snap)
    names = [f["name"] for f in tree["files"]]
    assert "IMG_0000.jpg" in names

    out = tmp / "one-photo"
    result = session.restore(snap, str(out), paths=["IMG_0001.jpg"])
    assert result["ok"] and result["restored"] == ["IMG_0001.jpg"]
    assert (out / "IMG_0001.jpg").read_bytes() == (
        cam / "IMG_0001.jpg").read_bytes()


# -- the bridge the native layer calls ------------------------------------
def test_bridge_round_trip(tmp_path):
    key = KeyRing.generate()
    home = tmp_path / "bridged"
    cam = tmp_path / "DCIM"
    photos(cam, n=3)

    reply = json.loads(handle(json.dumps({
        "cmd": "enrol", "home": str(home), "key": key.master.hex(),
        "device": "pixel"})))
    assert reply["ok"] and reply["device"] == "pixel"

    # a backend, added through the same manifest the desktop would use
    session = MobileSession.open(home)
    session.m.add_backend("disk", "local", {"path": str(tmp_path / "blobs")})
    session.close()
    bridge.close_all()

    assert json.loads(handle({"cmd": "add_folder", "home": str(home),
                              "path": str(cam)}))["ok"]

    probe = json.loads(handle({"cmd": "should_sync", "home": str(home),
                               "conditions": {"network": "cellular",
                                              "battery_pct": 90}}))
    assert probe["ok"] and probe["should_sync"] is True

    out = json.loads(handle({"cmd": "sync", "home": str(home),
                             "conditions": {"network": "wifi"}}))
    assert out["ok"] and out["state"] == "complete", out
    assert out["files"] == 3

    snaps = json.loads(handle({"cmd": "snapshots", "home": str(home)}))
    assert snaps["ok"] and snaps["snapshots"]
    sid = snaps["snapshots"][0]["id"]

    tree = json.loads(handle({"cmd": "browse", "home": str(home),
                              "snapshot": sid}))
    assert tree["ok"] and len(tree["files"]) == 3

    dest = tmp_path / "back"
    done = json.loads(handle({"cmd": "restore", "home": str(home),
                              "snapshot": sid, "dest": str(dest)}))
    assert done["ok"] and done["ok"] is True
    assert dirs_equal(cam, dest)


def test_bridge_never_raises_into_the_host():
    assert json.loads(handle("not json"))["ok"] is False
    assert json.loads(handle("[1,2,3]"))["ok"] is False
    assert json.loads(handle({}))["ok"] is False
    assert json.loads(handle({"cmd": "status"}))["ok"] is False  # no home
    bad = json.loads(handle({"cmd": "nope", "home": "/tmp/x"}))
    assert bad["ok"] is False and "unknown cmd" in bad["error"]
    missing = json.loads(handle({"cmd": "status", "home": "/no/such/home"}))
    assert missing["ok"] is False and "key" in missing["error"]


# -- the point: one implementation, shared ---------------------------------
def test_mobile_reimplements_nothing(tmp_path):
    """No second chunker, no second crypto, no second manifest."""
    import inspect
    import re
    from freehold.mobile import bridge as b, session as s
    for mod in (s, b):
        src = inspect.getsource(mod)
        assert "sha256(" not in src.replace("content_hash", "")
        assert "AESGCM" not in src and "chunk_stream" not in src
        assert "CREATE TABLE" not in src
        reached = {a or c for a, c in re.findall(
            r"^from \.\.([\w.]+) import|^import \.\.([\w.]+)", src, re.M)}
        assert reached <= {"core.crypto", "core.engine", "core.errors",
                           "core.manifest", "agent.config", "agent.network",
                           "dashboard.api"}, (mod.__name__, reached)


def test_phone_and_desktop_share_one_manifest_and_key(tmp_path):
    """A photo backed up by the phone restores on the desktop with nothing
    but the shared key — which is the whole reason not to fork the logic."""
    key = KeyRing.generate()
    home = tmp_path / "shared"
    cam = tmp_path / "DCIM"
    photos(cam, n=3, size=50_000)

    session = MobileSession.enrol(home, key.master.hex(), "pixel")
    session.m.add_backend("disk", "local", {"path": str(tmp_path / "blobs")})
    session.add_folder(str(cam))
    assert session.sync(Conditions(network=net.CELLULAR)).state == "complete"
    session.close()

    # the desktop side: plain core, no mobile code involved
    m = Manifest(home / "manifest.db")
    k = KeyRing.load(home / "key.json")
    out = tmp_path / "from-desktop"
    r = Engine(m, k).restore(out, device="pixel")
    assert r.ok, r.failed
    assert dirs_equal(cam, out)

    # and the desktop's own folder restores on the phone's session
    desk = tmp_path / "Desktop"
    desk.mkdir()
    (desk / "report.txt").write_text("quarterly\n" * 500)
    Engine(m, k).backup(desk, device="laptop")
    m.close()

    phone_again = MobileSession.open(home)
    snap = phone_again.m.latest_snapshot("laptop")
    got = phone_again.restore(snap.id, str(tmp_path / "on-phone"))
    assert got["ok"], got["failed"]
    assert (tmp_path / "on-phone" / "report.txt").read_text() == (
        desk / "report.txt").read_text()
    phone_again.close()
