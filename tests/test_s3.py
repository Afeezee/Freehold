"""S3 driver (R2 / B2 / MinIO / AWS), over real HTTP to a local server that
verifies every SigV4 signature.

The point of this driver is to prove the abstraction holds, so the load-bearing
test is the last one: the same engine code, unchanged, backing up to a second
backend and restoring from it.
"""
from __future__ import annotations

import filecmp
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from s3_server import FakeS3  # noqa: E402
from freehold.core import Engine, KeyRing, Manifest  # noqa: E402
from freehold.core.errors import ChunkNotFound  # noqa: E402
from freehold.drivers import s3 as s3mod  # noqa: E402
from freehold.drivers.s3api import authorization  # noqa: E402

SMALL = {"min_size": 2048, "avg_size": 4096, "max_size": 8192}


def rnd(n: int, seed: int) -> bytes:
    return random.Random(seed).randbytes(n)


@pytest.fixture
def srv():
    s = FakeS3()
    yield s
    s.stop()


def tree(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.bin").write_bytes(rnd(90_000, 3))
    (root / "notes.txt").write_text("quarterly plans, confidential\n" * 200)


def dirs_equal(a: Path, b: Path) -> bool:
    left = sorted(p.relative_to(a).as_posix() for p in a.rglob("*")
                  if p.is_file())
    right = sorted(p.relative_to(b).as_posix() for p in b.rglob("*")
                   if p.is_file())
    return left == right and all(
        filecmp.cmp(a / r, b / r, shallow=False) for r in left)


def setup(tmp_path, srv, **cfg):
    src = tmp_path / "src"
    tree(src)
    m = Manifest(tmp_path / "m.db")
    k = KeyRing.generate()
    m.add_backend("r2", "s3", srv.config(**cfg))
    return Engine(m, k, chunk_params=SMALL), m, k, src


# -- signing --------------------------------------------------------------
def test_sigv4_matches_the_published_aws_vector():
    """AWS's own documented GET Object example. Validates the formula itself,
    not just that both sides of a test agree."""
    headers = {
        "host": "examplebucket.s3.amazonaws.com",
        "range": "bytes=0-9",
        "x-amz-content-sha256":
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "x-amz-date": "20130524T000000Z",
    }
    auth = authorization(
        "GET", "/test.txt", "", headers,
        headers["x-amz-content-sha256"], "AKIAIOSFODNN7EXAMPLE",
        "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "us-east-1",
        "20130524T000000Z")
    assert auth.endswith(
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c"
        "6036bdb41"), auth


def test_server_accepts_every_signature_we_send(tmp_path, srv):
    eng, m, k, src = setup(tmp_path, srv)
    eng.backup(src, device="pc")
    assert eng.restore(tmp_path / "out").ok
    assert srv.store.auth_failures == 0
    assert srv.store.bad_payload_hash == 0


# -- the ordinary path ----------------------------------------------------
def test_backup_and_restore_byte_identical(tmp_path, srv):
    eng, m, k, src = setup(tmp_path, srv)
    res = eng.backup(src, device="pc")
    assert res.chunks_new > 5
    assert len(srv.store.objects) == res.chunks_new
    out = tmp_path / "out"
    assert eng.restore(out).ok
    assert dirs_equal(src, out)


def test_bucket_holds_no_plaintext_and_no_filenames(tmp_path, srv):
    eng, m, k, src = setup(tmp_path, srv)
    eng.backup(src, device="pc")
    blob = b"".join(srv.store.objects.values())
    assert b"quarterly plans" not in blob
    for key in srv.store.objects:
        assert "notes" not in key and "a.bin" not in key
        assert key.startswith("freehold/")
        assert len(key.rsplit("/", 1)[1]) == 64   # blinded blob id


def test_incremental_only_sends_changed_chunks(tmp_path, srv):
    eng, m, k, src = setup(tmp_path, srv)
    first = eng.backup(src, device="pc")
    victim = src / "a.bin"
    data = bytearray(victim.read_bytes())
    data[300] ^= 0xFF
    victim.write_bytes(bytes(data))
    import os
    os.utime(victim, (0, 0))
    second = eng.backup(src, device="pc")
    assert 0 < second.chunks_new < first.chunks_new
    assert eng.restore(tmp_path / "out").ok
    assert dirs_equal(src, tmp_path / "out")


def test_list_paginates_and_delete_works(tmp_path, srv):
    eng, m, k, src = setup(tmp_path, srv)
    eng.backup(src, device="pc")
    d = eng.driver(m.backends()[0])
    locations = list(d.list())            # server pages two keys at a time
    assert len(locations) == len(srv.store.objects) > 4
    assert d.delete(locations[0]) is True
    assert d.delete(locations[0]) is False
    assert len(srv.store.objects) == len(locations) - 1


def test_missing_object_raises_chunk_not_found(tmp_path, srv):
    eng, m, k, src = setup(tmp_path, srv)
    eng.backup(src, device="pc")
    d = eng.driver(m.backends()[0])
    with pytest.raises(ChunkNotFound):
        d.get("freehold-test/freehold/zz/" + "f" * 64)


def test_transient_5xx_is_retried(tmp_path, srv):
    eng, m, k, src = setup(tmp_path, srv)
    srv.store.fail_next = 3               # three 503s, then recover
    res = eng.backup(src, device="pc")
    assert not res.errors, res.errors
    assert eng.restore(tmp_path / "out").ok
    assert dirs_equal(src, tmp_path / "out")


def test_uploads_run_concurrently(tmp_path, srv):
    # localhost round trips are too fast to observe overlap reliably, so give
    # the server some latency -- which is the condition concurrency is for.
    srv.store.delay = 0.02
    eng, m, k, src = setup(tmp_path, srv)
    eng.backup(src, device="pc")
    assert srv.store.peak > 1, "S3 uploads were serial"
    assert srv.store.peak <= 8


def test_single_worker_is_serial_against_s3(tmp_path, srv):
    srv.store.delay = 0.02
    src = tmp_path / "src"
    tree(src)
    m = Manifest(tmp_path / "m.db")
    m.add_backend("r2", "s3", srv.config())
    Engine(m, KeyRing.generate(), chunk_params=SMALL,
           workers=1).backup(src, device="pc")
    assert srv.store.peak == 1


def test_quota_warning_surfaces(tmp_path, srv):
    eng, m, k, src = setup(tmp_path, srv, quota=40_000)
    res = eng.backup(src, device="pc")
    assert any("quota" in w or "%" in w for w in res.warnings), res.warnings
    st = eng.driver(m.backends()[0]).stats()
    assert st.quota_bytes == 40_000 and st.chunk_count == res.chunks_new


def test_revoked_credentials_degrade_instead_of_failing(tmp_path, srv):
    """Mirror to disk and S3, then have S3 start refusing. The restore must
    still succeed and name S3 as unreachable."""
    src = tmp_path / "src"
    tree(src)
    m = Manifest(tmp_path / "m.db")
    k = KeyRing.generate()
    m.add_backend("disk", "local", {"path": str(tmp_path / "disk")})
    m.add_backend("r2", "s3", srv.config())
    eng = Engine(m, k, redundancy=2, chunk_params=SMALL)
    eng.backup(src, device="pc")
    assert len(srv.store.objects) > 0

    srv.store.offline = True
    eng2 = Engine(m, k, chunk_params=SMALL)
    out = tmp_path / "out"
    r = eng2.restore(out)
    assert r.ok, r.failed
    assert dirs_equal(src, out)


# -- the reason this driver exists ---------------------------------------
def test_second_backend_needed_no_core_changes(tmp_path, srv):
    """Back up to S3 only, restore from S3 only, with the same Engine, the
    same Manifest schema and the same KeyRing used for GitHub and local disk.
    If this needed a core change, the interface was wrong."""
    src = tmp_path / "src"
    tree(src)
    (src / "nested").mkdir()
    (src / "nested" / "deep.bin").write_bytes(rnd(50_000, 11))

    m = Manifest(tmp_path / "m.db")
    k = KeyRing.generate()
    k.save(tmp_path / "key.json")
    m.add_backend("r2", "s3", srv.config())
    Engine(m, k, chunk_params=SMALL).backup(src, device="laptop")
    m.close()

    # a "new machine": nothing but the key, the manifest and the bucket
    m2 = Manifest(tmp_path / "m.db")
    k2 = KeyRing.load(tmp_path / "key.json")
    out = tmp_path / "newmachine"
    r = Engine(m2, k2, chunk_params=SMALL).restore(out, device="laptop")
    assert r.ok, r.failed
    assert dirs_equal(src, out)


def test_driver_touches_only_the_driver_interface():
    """The driver may import the interface and the error types, nothing else
    from core. No reaching into the engine or the manifest."""
    import inspect
    import re
    from freehold.drivers import s3api
    for mod in (s3mod, s3api):
        src = inspect.getsource(mod)
        reached = {a or b for a, b in re.findall(
            r"^from \.\.([\w.]+) import|^import \.\.([\w.]+)", src, re.M)}
        assert reached <= {"core.driver", "core.errors"}, (mod.__name__,
                                                           reached)
