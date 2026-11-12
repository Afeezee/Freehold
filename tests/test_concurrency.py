"""Concurrency: that it happens, that it stays bounded, and that a chunk which
fails to store never leaves a snapshot claiming a file it cannot restore."""
from __future__ import annotations

import random
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fake_github import FakeGitHub  # noqa: E402
from freehold.core import Engine, KeyRing, Manifest  # noqa: E402
from freehold.core.crypto import sha256  # noqa: E402
from freehold.core.driver import BackendStats, StorageDriver, register  # noqa: E402

SMALL = {"min_size": 2048, "avg_size": 4096, "max_size": 8192}


@register
class CountingDriver(StorageDriver):
    """Shared class state, so every Engine rebuild sees the same server and the
    test can watch how many calls are in flight at once."""
    driver_id = "counting"

    blobs: Dict[str, bytes] = {}
    fail_blobs: set = set()
    delay = 0.01
    live = 0
    peak = 0
    puts = 0
    lock = threading.Lock()

    @classmethod
    def reset(cls, delay: float = 0.01) -> None:
        cls.blobs = {}
        cls.fail_blobs = set()
        cls.delay = delay
        cls.live = cls.peak = cls.puts = 0

    def _enter(self) -> None:
        with CountingDriver.lock:
            CountingDriver.live += 1
            CountingDriver.peak = max(CountingDriver.peak,
                                      CountingDriver.live)

    def _exit(self) -> None:
        with CountingDriver.lock:
            CountingDriver.live -= 1

    def put(self, blob_id: str, data: bytes) -> str:
        self._enter()
        try:
            time.sleep(CountingDriver.delay)
            if blob_id in CountingDriver.fail_blobs:
                raise OSError("disk on fire")
            CountingDriver.blobs[blob_id] = data
            with CountingDriver.lock:
                CountingDriver.puts += 1
            return "blob/" + blob_id
        finally:
            self._exit()

    def get(self, location: str) -> bytes:
        self._enter()
        try:
            time.sleep(CountingDriver.delay)
            return CountingDriver.blobs[location.split("/", 1)[1]]
        finally:
            self._exit()

    def list(self) -> Iterator[str]:
        return iter("blob/" + b for b in CountingDriver.blobs)

    def delete(self, location: str) -> bool:
        return CountingDriver.blobs.pop(location.split("/", 1)[1],
                                        None) is not None

    def stats(self) -> BackendStats:
        return BackendStats(
            bytes_stored=sum(len(v) for v in CountingDriver.blobs.values()),
            chunk_count=len(CountingDriver.blobs))


def rnd(n: int, seed: int) -> bytes:
    return random.Random(seed).randbytes(n)


@pytest.fixture(autouse=True)
def clean():
    CountingDriver.reset()
    FakeGitHub.reset()
    yield
    CountingDriver.reset()
    FakeGitHub.reset()


def build(tmp_path, nfiles=4, size=60_000, workers=4):
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    for i in range(nfiles):
        (src / ("f%02d.bin" % i)).write_bytes(rnd(size, 100 + i))
    m = Manifest(tmp_path / "m.db")
    k = KeyRing.generate()
    m.add_backend("cnt", "counting", {})
    return Engine(m, k, chunk_params=SMALL, workers=workers), m, k, src


def dirs_equal(a: Path, b: Path) -> bool:
    import filecmp
    left = sorted(p.relative_to(a).as_posix() for p in a.rglob("*")
                  if p.is_file())
    right = sorted(p.relative_to(b).as_posix() for p in b.rglob("*")
                   if p.is_file())
    return left == right and all(
        filecmp.cmp(a / r, b / r, shallow=False) for r in left)


# -- it actually runs in parallel ----------------------------------------
def test_uploads_run_concurrently(tmp_path):
    eng, m, k, src = build(tmp_path, workers=4)
    res = eng.backup(src, device="pc")
    assert res.chunks_new > 8
    assert CountingDriver.peak > 1, "uploads were serial"
    assert CountingDriver.peak <= 4, "exceeded the worker limit"


def test_downloads_run_concurrently(tmp_path):
    eng, m, k, src = build(tmp_path, workers=4)
    eng.backup(src, device="pc")
    CountingDriver.peak = 0
    r = eng.restore(tmp_path / "out")
    assert r.ok, r.failed
    assert CountingDriver.peak > 1, "downloads were serial"
    assert CountingDriver.peak <= 4
    assert dirs_equal(src, tmp_path / "out")


def test_concurrency_is_a_real_speedup(tmp_path):
    """Same work, 1 worker vs 8. Latency-bound by construction, so the wall
    clock has to drop. Generous margin to stay honest on a loaded machine."""
    CountingDriver.reset(delay=0.03)
    serial_engine, m1, _, src = build(tmp_path, nfiles=3, workers=1)
    t0 = time.time()
    serial = serial_engine.backup(src, device="a")
    serial_secs = time.time() - t0

    CountingDriver.reset(delay=0.03)
    m2 = Manifest(tmp_path / "m2.db")
    m2.add_backend("cnt", "counting", {})
    par = Engine(m2, KeyRing.generate(), chunk_params=SMALL, workers=8)
    t0 = time.time()
    parallel = par.backup(src, device="b")
    par_secs = time.time() - t0

    assert serial.chunks_new == parallel.chunks_new
    assert par_secs < serial_secs * 0.6, (serial_secs, par_secs)


def test_single_worker_is_strictly_serial(tmp_path):
    eng, m, k, src = build(tmp_path, workers=1)
    eng.backup(src, device="pc")
    assert CountingDriver.peak == 1


def test_in_flight_work_stays_bounded(tmp_path):
    """The queue is bounded, so a backup of many chunks cannot pile every
    sealed chunk into memory at once."""
    eng, m, k, src = build(tmp_path, nfiles=6, size=120_000, workers=2)
    res = eng.backup(src, device="pc")
    assert res.chunks_new > 30
    assert CountingDriver.peak <= 2


# -- dedup under concurrency ---------------------------------------------
def test_identical_chunks_upload_once(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    payload = rnd(50_000, 7)
    for i in range(4):
        (src / ("copy%d.bin" % i)).write_bytes(payload)
    m = Manifest(tmp_path / "m.db")
    m.add_backend("cnt", "counting", {})
    eng = Engine(m, KeyRing.generate(), chunk_params=SMALL, workers=8)
    res = eng.backup(src, device="pc")
    assert res.files == 4
    # four identical files, one file's worth of chunks actually uploaded
    assert CountingDriver.puts == res.chunks_new
    assert res.chunks_new * 4 == res.chunks_seen
    assert eng.restore(tmp_path / "out").ok
    assert dirs_equal(src, tmp_path / "out")


# -- honesty when a chunk cannot be stored --------------------------------
def test_a_file_whose_chunk_fails_is_dropped_from_the_snapshot(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    doomed = b"this file will not make it\n" * 20      # one chunk, < min_size
    (src / "doomed.txt").write_bytes(doomed)
    (src / "fine.bin").write_bytes(rnd(40_000, 9))
    m = Manifest(tmp_path / "m.db")
    k = KeyRing.generate()
    m.add_backend("cnt", "counting", {})
    CountingDriver.fail_blobs = {k.blob_id(sha256(doomed))}

    eng = Engine(m, k, chunk_params=SMALL, workers=4)
    res = eng.backup(src, device="pc")

    assert res.files == 1, "the unstorable file should not be counted"
    assert any("doomed.txt" in e and "NOT backed up" in e
               for e in res.errors), res.errors
    listed = [f.path for f in m.files(res.snapshot_id)]
    assert listed == ["fine.bin"]

    # the snapshot is honest, so restoring it succeeds completely
    r = eng.restore(tmp_path / "out")
    assert r.ok, r.failed
    assert not (tmp_path / "out" / "doomed.txt").exists()
    assert (tmp_path / "out" / "fine.bin").read_bytes() == (
        src / "fine.bin").read_bytes()


def test_a_failed_chunk_is_retried_on_the_next_backup(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    doomed = b"retry me\n" * 50
    (src / "doomed.txt").write_bytes(doomed)
    m = Manifest(tmp_path / "m.db")
    k = KeyRing.generate()
    m.add_backend("cnt", "counting", {})
    CountingDriver.fail_blobs = {k.blob_id(sha256(doomed))}
    eng = Engine(m, k, chunk_params=SMALL, workers=2)
    assert eng.backup(src, device="pc").files == 0

    CountingDriver.fail_blobs = set()
    second = eng.backup(src, device="pc")
    assert second.files == 1 and second.chunks_new == 1
    r = eng.restore(tmp_path / "out")
    assert r.ok, r.failed
    assert (tmp_path / "out" / "doomed.txt").read_bytes() == doomed


# -- driver bookkeeping survives parallel writers -------------------------
def test_github_pool_counters_are_not_lost_under_concurrency(tmp_path):
    import test_github  # noqa: F401  (registers the github-test driver)
    src = tmp_path / "src"
    src.mkdir()
    for i in range(4):
        (src / ("f%d.bin" % i)).write_bytes(rnd(90_000, 200 + i))
    m = Manifest(tmp_path / "m.db")
    bid = m.add_backend("gh", "github-test", {
        "owner": "tester", "store": "conc", "prefix": "fh",
        "soft_cap": "120000"})
    eng = Engine(m, KeyRing.generate(), chunk_params=SMALL, workers=8)
    res = eng.backup(src, device="pc")

    pool = m.get_backend_state(bid, "pool")
    pool_bytes = sum(r["bytes"] for r in pool["repos"])
    pool_chunks = sum(r["chunks"] for r in pool["repos"])
    row = [r for r in m.usage_by_backend() if r["name"] == "gh"][0]
    # the driver's own tally and the manifest's must agree exactly
    assert pool_chunks == res.chunks_new == row["chunk_count"]
    assert pool_bytes == row["bytes_stored"]
    assert len(pool["repos"]) > 1 and sum(
        1 for r in pool["repos"] if r["status"] == "active") == 1
    # the soft cap is not overshot by in-flight uploads
    assert all(r["bytes"] <= 120_000 for r in pool["repos"]), pool["repos"]
    assert eng.restore(tmp_path / "out").ok
    assert dirs_equal(src, tmp_path / "out")


# -- restores no longer hold a whole file in memory -----------------------
def test_restore_streams_chunks_to_disk(tmp_path, monkeypatch):
    """A file with far more chunks than the chunk cache must still restore,
    which only works if chunks are written out as they arrive."""
    import freehold.core.engine as engine_mod
    monkeypatch.setattr(engine_mod, "CACHE_CHUNKS", 4)
    src = tmp_path / "src"
    src.mkdir()
    (src / "big.bin").write_bytes(rnd(400_000, 42))
    m = Manifest(tmp_path / "m.db")
    m.add_backend("cnt", "counting", {})
    eng = Engine(m, KeyRing.generate(), chunk_params=SMALL, workers=4)
    res = eng.backup(src, device="pc")
    assert res.chunks_seen > 20
    r = eng.restore(tmp_path / "out")
    assert r.ok, r.failed
    assert (tmp_path / "out" / "big.bin").read_bytes() == (
        src / "big.bin").read_bytes()


def test_no_partial_file_is_left_when_a_chunk_is_unrecoverable(tmp_path):
    eng, m, k, src = build(tmp_path, nfiles=2, size=80_000, workers=4)
    eng.backup(src, device="pc")
    CountingDriver.blobs.clear()          # every blob vanishes
    out = tmp_path / "out"
    r = eng.restore(out)
    assert not r.ok
    assert len(r.failed) == 2
    # nothing half-written, and no temp files left behind
    assert not any(p.is_file() for p in out.rglob("*")), list(out.rglob("*"))
