"""The desktop agent: scheduling, the Wi-Fi-only default, and what happens
when the link drops out halfway through a backup."""
from __future__ import annotations

import filecmp
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freehold.agent import (Agent, AgentConfig, Link, NetworkPolicy,  # noqa: E402
                            NetworkRules, Paused, SingleInstance)
from freehold.agent import network as net  # noqa: E402
from freehold.core import Engine, KeyRing, Manifest  # noqa: E402
from freehold.core.errors import ConfigError, FreeholdError  # noqa: E402

SMALL = {"min_size": 2048, "avg_size": 4096, "max_size": 8192}


def rnd(n: int, seed: int) -> bytes:
    return random.Random(seed).randbytes(n)


def tree(root: Path, nfiles: int = 4, size: int = 30_000) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for i in range(nfiles):
        (root / ("f%02d.bin" % i)).write_bytes(rnd(size, 300 + i))


def dirs_equal(a: Path, b: Path) -> bool:
    left = sorted(p.relative_to(a).as_posix() for p in a.rglob("*")
                  if p.is_file())
    right = sorted(p.relative_to(b).as_posix() for p in b.rglob("*")
                   if p.is_file())
    return left == right and all(
        filecmp.cmp(a / r, b / r, shallow=False) for r in left)


class Clock:
    """Starts from real wall clock on purpose: the agent compares its clock
    against snapshot timestamps the manifest wrote with time.time(), so the
    two have to share a time base."""

    def __init__(self, t: float = None):
        import time as _t
        self.t = float(_t.time()) if t is None else t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def make(tmp_path, link=net.WIFI, interval=900, **rules):
    src = tmp_path / "src"
    tree(src)
    home = tmp_path / "home"
    home.mkdir()
    m = Manifest(home / "manifest.db")
    k = KeyRing.generate()
    m.add_backend("disk", "local", {"path": str(tmp_path / "blobs")})
    cfg = AgentConfig(device="testpc", network=NetworkRules(**rules))
    cfg.add_folder(str(src), interval=interval)
    clock = Clock()
    links = [Link(link)]
    policy = NetworkPolicy(cfg.network, probe=lambda: links[-1], clock=clock)
    agent = Agent(home, cfg, m, k, policy=policy, clock=clock)
    # the agent builds its own Engines, so steer chunk sizes through it
    agent._chunk_params = SMALL
    return agent, m, k, src, home, clock, links


@pytest.fixture(autouse=True)
def small_chunks(monkeypatch):
    """The agent constructs Engines itself; make them use test chunk sizes."""
    real_init = Engine.__init__

    def patched(self, manifest, keyring, redundancy=1, chunk_params=None,
                workers=8):
        real_init(self, manifest, keyring, redundancy=redundancy,
                  chunk_params=chunk_params or SMALL, workers=workers)
    monkeypatch.setattr(Engine, "__init__", patched)
    yield


# -- config ---------------------------------------------------------------
def test_config_roundtrip(tmp_path):
    cfg = AgentConfig(device="box")
    folder = tmp_path / "docs"
    folder.mkdir()
    cfg.add_folder(str(folder), interval=60)
    cfg.network.allow_cellular = True
    cfg.save(tmp_path)

    back = AgentConfig.load(tmp_path)
    assert back.device == "box"
    assert back.network.allow_cellular is True
    assert len(back.folders) == 1 and back.folders[0].interval == 60
    assert back.folder(str(folder)) is not None


def test_config_rejects_nonsense(tmp_path):
    cfg = AgentConfig()
    with pytest.raises(ConfigError):
        cfg.add_folder(str(tmp_path / "nope"))
    folder = tmp_path / "d"
    folder.mkdir()
    cfg.add_folder(str(folder))
    with pytest.raises(ConfigError):
        cfg.add_folder(str(folder))            # no duplicates
    assert cfg.remove_folder(str(folder)) is True
    assert cfg.remove_folder(str(folder)) is False
    with pytest.raises(ConfigError):
        AgentConfig.from_dict({"network": {"on_unknown": "maybe"}})


def test_missing_config_defaults_to_this_machine(tmp_path):
    cfg = AgentConfig.load(tmp_path)
    assert cfg.device and cfg.folders == []
    # backups run on any link by default, cellular included, unrestricted
    assert cfg.network.allow_cellular is True
    assert cfg.network.allow_metered is True


# -- network policy -------------------------------------------------------
@pytest.mark.parametrize("kind,expected", [
    (net.WIFI, True),
    (net.ETHERNET, True),
    (net.CELLULAR, True),      # no restriction by default
    (net.METERED, True),
    (net.OFFLINE, False),      # the only link we refuse: there isn't one
])
def test_default_policy(kind, expected):
    p = NetworkPolicy(NetworkRules(), probe=lambda: Link(kind))
    assert p.allowed() is expected


def test_cellular_can_still_be_blocked_on_request():
    rules = NetworkRules(allow_cellular=False, allow_metered=False)
    p = NetworkPolicy(rules, probe=lambda: Link(net.CELLULAR, "MTN"))
    ok, why = p.verdict()
    assert not ok and "allow_cellular is false" in why

    metered = NetworkPolicy(rules, probe=lambda: Link(net.METERED, "hotspot"))
    ok, why = metered.verdict()
    assert not ok and "allow_metered is false" in why


def test_unknown_link_is_configurable():
    probe = lambda: Link(net.UNKNOWN, "vpn0")  # noqa: E731
    assert NetworkPolicy(NetworkRules(on_unknown="allow"),
                         probe=probe).allowed() is True
    blocked = NetworkPolicy(NetworkRules(on_unknown="block"), probe=probe)
    ok, why = blocked.verdict()
    assert not ok and "on_unknown is block" in why


def test_link_is_cached_until_recheck_elapses():
    calls = []
    clock = Clock()

    def probe():
        calls.append(1)
        return Link(net.WIFI)

    p = NetworkPolicy(NetworkRules(recheck_seconds=20), probe=probe,
                      clock=clock)
    p.allowed(), p.allowed(), p.allowed()
    assert len(calls) == 1
    clock.advance(21)
    p.allowed()
    assert len(calls) == 2


def test_detection_never_raises(monkeypatch):
    monkeypatch.setattr(net, "_run", lambda *a, **k: "")
    assert net.detect().kind in (net.UNKNOWN, net.ETHERNET, net.WIFI)
    monkeypatch.setattr(net, "_detect_windows",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(net.platform, "system", lambda: "Windows")
    assert net.detect().kind == net.UNKNOWN


def test_probe_command_overrides_detection(monkeypatch):
    monkeypatch.setattr(net, "_run", lambda *a, **k: "cellular")
    assert net.detect("my-script").kind == net.CELLULAR
    monkeypatch.setattr(net, "_run", lambda *a, **k: "gibberish")
    assert net.detect("my-script").kind == net.UNKNOWN


# -- scheduling -----------------------------------------------------------
def test_backs_up_then_waits_for_the_interval(tmp_path):
    agent, m, k, src, home, clock, _links = make(tmp_path, interval=900)
    runs = agent.run_once()
    assert len(runs) == 1 and runs[0].ok and runs[0].files == 4

    assert agent.run_once() == []          # not due yet
    clock.advance(901)
    again = agent.run_once()
    assert len(again) == 1 and again[0].uploaded == 0   # nothing changed


def test_force_ignores_the_interval(tmp_path):
    agent, m, k, src, home, clock, _links = make(tmp_path)
    agent.run_once()
    assert agent.run_once(force=True)[0].ok


def test_due_picks_up_a_previous_run_from_the_manifest(tmp_path):
    agent, m, k, src, home, clock, _links = make(tmp_path, interval=900)
    agent.run_once()
    # a fresh agent, as if the machine had rebooted
    fresh = Agent(home, agent.cfg, m, k, policy=agent.policy, clock=clock)
    assert fresh.due(agent.cfg.folders[0]) is False
    clock.advance(901)
    assert fresh.due(agent.cfg.folders[0]) is True


def test_paused_folder_is_left_alone(tmp_path):
    agent, m, k, src, home, clock, _links = make(tmp_path)
    agent.cfg.folders[0].paused = True
    runs = agent.run_once(force=True)
    assert len(runs) == 1 and not runs[0].ok
    assert runs[0].skipped == "paused in config"
    assert m.snapshots() == []


def test_missing_folder_is_reported_not_fatal(tmp_path):
    agent, m, k, src, home, clock, _links = make(tmp_path)
    agent.cfg.folders[0].path = str(tmp_path / "gone")
    runs = agent.run_once(force=True)
    assert not runs[0].ok and runs[0].skipped == "not a directory"


# -- the wifi-only default in practice ------------------------------------
def test_cellular_backs_up_without_restriction(tmp_path):
    """A large backup over cellular runs to completion: no link check, no
    size cap, no deferral."""
    agent, m, k, src, home, clock, _links = make(tmp_path, link=net.CELLULAR)
    runs = agent.run_once(force=True)
    assert len(runs) == 1 and runs[0].ok, runs[0].errors
    assert runs[0].files == 4 and runs[0].uploaded > 0
    assert m.snapshots(complete_only=True)
    out = tmp_path / "out"
    assert Engine(m, k, chunk_params=SMALL).restore(
        out, device="testpc").ok
    assert dirs_equal(src, out)


def test_cellular_can_be_opted_out_of(tmp_path):
    agent, m, k, src, home, clock, _links = make(
        tmp_path, link=net.CELLULAR, allow_cellular=False,
        allow_metered=False)
    runs = agent.run_once(force=True)
    assert len(runs) == 1 and not runs[0].ok
    assert "cellular" in (runs[0].skipped or "")
    assert m.snapshots(complete_only=False) == []   # nothing even started


def test_offline_is_the_only_default_refusal(tmp_path):
    agent, m, k, src, home, clock, _links = make(tmp_path, link=net.OFFLINE)
    runs = agent.run_once(force=True)
    assert not runs[0].ok and runs[0].skipped == "offline"


# -- the interesting case: the link drops mid-backup ----------------------
def test_losing_the_link_midway_pauses_and_resumes(tmp_path):
    agent, m, k, src, home, clock, links = make(tmp_path)
    agent.policy.rules.recheck_seconds = 0      # re-probe on every file

    calls = {"n": 0}

    def probe():
        calls["n"] += 1
        # connected for the first couple of checks, then the link drops
        return Link(net.WIFI) if calls["n"] <= 2 else Link(net.OFFLINE)

    agent.policy._probe = probe
    runs = agent.run_once(force=True)
    assert len(runs) == 1 and not runs[0].ok
    assert "offline" in (runs[0].skipped or "")

    # the interrupted snapshot must not look restorable
    assert m.snapshots(complete_only=True) == []
    aborted = m.snapshots(complete_only=False)
    assert len(aborted) == 1 and aborted[0].status == "failed"
    uploaded_before = m.totals()["chunks"]
    assert uploaded_before > 0, "some chunks should have made it"

    # link back: the next pass finishes, reusing what already uploaded
    agent.policy._probe = lambda: Link(net.WIFI)
    second = agent.run_once(force=True)
    assert second[0].ok, second[0].errors
    assert m.snapshots(complete_only=True)

    out = tmp_path / "out"
    r = Engine(m, k, chunk_params=SMALL).restore(out, device="testpc")
    assert r.ok, r.failed
    assert dirs_equal(src, out)


def test_shutdown_request_stops_mid_backup(tmp_path):
    agent, m, k, src, home, clock, _links = make(tmp_path)
    original = agent._progress()

    def stopping(rel, res):
        agent.stop_event.set()
        original(rel, res)

    agent._progress = lambda: stopping
    runs = agent.run_once(force=True)
    assert not runs[0].ok and "shutting down" in (runs[0].skipped or "")


# -- single instance ------------------------------------------------------
def test_only_one_agent_at_a_time(tmp_path):
    with SingleInstance(tmp_path) as first:
        assert first.acquired
        with pytest.raises(FreeholdError):
            with SingleInstance(tmp_path):
                pass
    # released on exit, so a later run gets it
    with SingleInstance(tmp_path) as third:
        assert third.acquired


def test_stale_lock_from_a_dead_process_is_reclaimed(tmp_path):
    (tmp_path / "agent.lock").write_text("999999999")
    with SingleInstance(tmp_path) as s:
        assert s.acquired


# -- service definitions --------------------------------------------------
@pytest.mark.parametrize("system,needle", [
    ("Windows", "schtasks"),
    ("Linux", "[Service]"),
    ("Darwin", "launchctl"),
    ("Haiku", "Run it yourself"),
])
def test_service_definition_per_platform(tmp_path, system, needle):
    from freehold.agent import service
    text = service.definition(tmp_path, system)
    assert needle in text
    assert str(tmp_path) in text


def test_install_warns_about_a_wrapped_key(tmp_path):
    from freehold.agent import service
    KeyRing.generate().save(tmp_path / "key.json", "a passphrase")
    assert "passphrase-wrapped" in service.note(tmp_path)
    KeyRing.generate().save(tmp_path / "key.json")
    assert service.note(tmp_path) == ""
