"""Core engine tests against the local-disk driver. No network backend yet."""
from __future__ import annotations

import filecmp
import os
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freehold.core import Engine, KeyRing, Manifest  # noqa: E402
from freehold.core.chunker import cut  # noqa: E402
from freehold.core.errors import IntegrityError  # noqa: E402

SMALL = {"min_size": 2048, "avg_size": 8192, "max_size": 32768}


def rnd(n: int, seed: int) -> bytes:
    return random.Random(seed).randbytes(n)


@pytest.fixture
def setup(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "sub").mkdir()
    (src / "a.bin").write_bytes(rnd(300_000, 1))
    (src / "b.txt").write_text("hello freehold\n" * 500)
    (src / "sub" / "c.bin").write_bytes(rnd(150_000, 2))
    (src / "empty").write_bytes(b"")

    m = Manifest(tmp_path / "manifest.db")
    k = KeyRing.generate()
    m.add_backend("disk", "local", {"path": str(tmp_path / "blobs")})
    eng = Engine(m, k, chunk_params=SMALL)
    return eng, m, k, src, tmp_path


def dirs_equal(a: Path, b: Path) -> bool:
    left = sorted(p.relative_to(a).as_posix() for p in a.rglob("*")
                  if p.is_file())
    right = sorted(p.relative_to(b).as_posix() for p in b.rglob("*")
                   if p.is_file())
    if left != right:
        return False
    return all(filecmp.cmp(a / r, b / r, shallow=False) for r in left)


# -- chunker --------------------------------------------------------------
def test_chunking_is_deterministic_and_bounded():
    data = rnd(2_000_000, 7)
    def split(buf):
        out, i = [], 0
        while i < len(buf):
            n = cut(buf[i:], **SMALL)
            out.append(buf[i:i + n])
            i += n
        return out
    parts = split(data)
    assert b"".join(parts) == data
    assert parts == split(data)
    assert len(parts) > 20
    assert all(len(p) <= SMALL["max_size"] for p in parts)


def test_insertion_shifts_only_local_boundaries():
    data = rnd(1_000_000, 8)
    def hashes(buf):
        out, i = [], 0
        while i < len(buf):
            n = cut(buf[i:], **SMALL)
            out.append(buf[i:i + n])
            i += n
        return out
    before = hashes(data)
    after = hashes(data[:400_000] + b"INSERTED" + data[400_000:])
    shared = set(before) & set(after)
    # content-defined chunking must resync: most chunks survive an insertion
    assert len(shared) > 0.8 * len(before)


# -- crypto ---------------------------------------------------------------
def test_blob_id_hides_plaintext_hash():
    k = KeyRing.generate()
    h = bytes(range(32))
    assert k.blob_id(h) != h.hex()
    assert k.blob_id(h) == k.blob_id(h)
    assert KeyRing.generate().blob_id(h) != k.blob_id(h)


def test_tampered_blob_is_rejected():
    k = KeyRing.generate()
    from freehold.core.crypto import sha256
    pt = b"secret" * 100
    h = sha256(pt)
    blob = bytearray(k.seal(h, pt))
    assert k.unseal(h, bytes(blob)) == pt
    blob[-1] ^= 1
    with pytest.raises(IntegrityError):
        k.unseal(h, bytes(blob))


def test_keyfile_roundtrip_with_passphrase(tmp_path):
    k = KeyRing.generate()
    p = tmp_path / "key.json"
    k.save(p, "correct horse")
    assert KeyRing.load(p, "correct horse").master == k.master
    with pytest.raises(Exception):
        KeyRing.load(p, "wrong")


# -- backup / restore -----------------------------------------------------
def test_full_restore_is_byte_identical(setup):
    eng, m, k, src, tmp = setup
    res = eng.backup(src, device="dev1")
    assert res.files == 4 and res.chunks_new > 0
    out = tmp / "restored"
    r = eng.restore(out)
    assert r.ok, r.failed
    assert dirs_equal(src, out)


def test_backend_stores_no_plaintext(setup):
    eng, m, k, src, tmp = setup
    eng.backup(src, device="dev1")
    blobs = b"".join(p.read_bytes() for p in (tmp / "blobs").rglob("*")
                     if p.is_file())
    assert b"hello freehold" not in blobs
    names = [p.name for p in (tmp / "blobs").rglob("*") if p.is_file()]
    assert not any("a.bin" in n or "b.txt" in n for n in names)


def test_incremental_uploads_only_changed_chunks(setup):
    eng, m, k, src, tmp = setup
    first = eng.backup(src, device="dev1")
    big = src / "a.bin"
    data = bytearray(big.read_bytes())
    data[1000] ^= 0xFF
    big.write_bytes(bytes(data))
    os.utime(big, (0, 0))  # force the mtime fast path off
    second = eng.backup(src, device="dev1")
    assert second.chunks_new >= 1
    assert second.chunks_new < first.chunks_new
    assert second.bytes_uploaded < first.bytes_uploaded / 2
    out = tmp / "r2"
    assert eng.restore(out).ok
    assert dirs_equal(src, out)


def test_unchanged_file_fast_path(setup):
    eng, m, k, src, tmp = setup
    eng.backup(src, device="dev1")
    second = eng.backup(src, device="dev1")
    assert second.unchanged_files == 4
    assert second.bytes_uploaded == 0
    assert eng.restore(tmp / "r").ok


def test_point_in_time_and_selective_restore(setup):
    eng, m, k, src, tmp = setup
    s1 = eng.backup(src, device="dev1").snapshot_id
    original = (src / "b.txt").read_text()
    (src / "b.txt").write_text("rewritten\n")
    (src / "a.bin").unlink()
    s2 = eng.backup(src, device="dev1").snapshot_id

    old = tmp / "old"
    r = eng.restore(old, snapshot_id=s1, paths=["b.txt"])
    assert r.ok and r.restored == ["b.txt"]
    assert (old / "b.txt").read_text() == original
    assert not (old / "a.bin").exists()

    new = tmp / "new"
    assert eng.restore(new, snapshot_id=s2).ok
    assert (new / "b.txt").read_text() == "rewritten\n"
    assert not (new / "a.bin").exists()


def test_existing_file_is_not_overwritten_by_default(setup):
    eng, m, k, src, tmp = setup
    eng.backup(src, device="dev1")
    out = tmp / "out"
    out.mkdir()
    (out / "b.txt").write_text("local work in progress\n")
    r = eng.restore(out)
    assert (out / "b.txt").read_text() == "local work in progress\n"
    assert (out / "b.txt.freehold-restored-1").exists()
    assert str(out / "b.txt") in r.renamed

    r2 = eng.restore(out, overwrite=True)
    assert r2.ok
    assert (out / "b.txt").read_text().startswith("hello freehold")


def test_degraded_restore_when_a_backend_is_unreachable(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "only_on_a.bin").write_bytes(rnd(120_000, 11))
    m = Manifest(tmp_path / "m.db")
    k = KeyRing.generate()
    m.add_backend("alpha", "local", {"path": str(tmp_path / "alpha")})
    eng = Engine(m, k, chunk_params=SMALL)
    eng.backup(src, device="dev1")

    (src / "only_on_b.bin").write_bytes(rnd(90_000, 12))
    m.add_backend("beta", "local", {"path": str(tmp_path / "beta")})
    m.set_backend_enabled("alpha", False)
    eng2 = Engine(m, k, chunk_params=SMALL)
    eng2.backup(src, device="dev1")
    m.set_backend_enabled("alpha", True)

    # alpha goes dark: beta-only files must still come back, alpha-only files
    # must be named, and the run must not fail as a whole.
    (tmp_path / "alpha" / ".offline").write_text("")
    eng3 = Engine(m, k, chunk_params=SMALL)
    out = tmp_path / "out"
    r = eng3.restore(out)
    assert not r.ok
    assert r.restored == ["only_on_b.bin"]
    assert [f.path for f in r.failed] == ["only_on_a.bin"]
    assert r.failed[0].chunk
    assert "alpha" in r.failed[0].backends
    assert "alpha" in r.unreachable_backends
    assert (out / "only_on_b.bin").read_bytes() == (
        src / "only_on_b.bin").read_bytes()


def test_corrupt_blob_names_the_chunk_and_backend(setup):
    eng, m, k, src, tmp = setup
    eng.backup(src, device="dev1")
    blobs = [p for p in (tmp / "blobs").rglob("*") if p.is_file()]
    victim = blobs[0]
    victim.write_bytes(b"\x00" * victim.stat().st_size)
    eng2 = Engine(m, k, chunk_params=SMALL)
    r = eng2.restore(tmp / "out")
    assert r.failed
    f = r.failed[0]
    assert f.chunk and "disk" in f.backends


def test_verify_detects_a_deleted_blob(setup):
    eng, m, k, src, tmp = setup
    eng.backup(src, device="dev1")
    assert eng.verify(deep=True) == []
    victim = [p for p in (tmp / "blobs").rglob("*") if p.is_file()][0]
    victim.unlink()
    assert eng.verify(deep=True) != []


def test_restore_reports_which_backends_served_it(tmp_path):
    """A backend that was never needed is not 'unreachable' -- the engine
    stops at the first location that answers and cannot know about the rest.
    What it can report is where the bytes actually came from."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "doc.bin").write_bytes(rnd(200_000, 31))
    m = Manifest(tmp_path / "m.db")
    k = KeyRing.generate()
    m.add_backend("first", "local", {"path": str(tmp_path / "one")})
    m.add_backend("second", "local", {"path": str(tmp_path / "two")})
    Engine(m, k, redundancy=2, chunk_params=SMALL).backup(src, device="pc")

    (tmp_path / "two" / ".offline").write_text("")
    eng = Engine(m, k, chunk_params=SMALL)
    r = eng.restore(tmp_path / "out")
    assert r.ok, r.failed
    assert r.served_by == ["first"]
    assert r.unreachable_backends == []      # never asked, never found out
    assert dirs_equal(src, tmp_path / "out")


def test_mirroring_to_two_backends_survives_losing_one(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "doc.bin").write_bytes(rnd(200_000, 21))
    m = Manifest(tmp_path / "m.db")
    k = KeyRing.generate()
    m.add_backend("one", "local", {"path": str(tmp_path / "one")})
    m.add_backend("two", "local", {"path": str(tmp_path / "two")})
    eng = Engine(m, k, redundancy=2, chunk_params=SMALL)
    eng.backup(src, device="dev1")
    (tmp_path / "one" / ".offline").write_text("")
    eng2 = Engine(m, k, chunk_params=SMALL)
    out = tmp_path / "out"
    r = eng2.restore(out)
    assert r.ok, r.failed
    assert dirs_equal(src, out)


def test_lost_device_restore_from_key_and_manifest_only(tmp_path):
    """Fresh machine: new process state, nothing but the key, the manifest and
    the blob store. (The manifest itself is backed up in step 2.)"""
    src = tmp_path / "src"
    (src / "deep" / "nested").mkdir(parents=True)
    (src / "deep" / "nested" / "x.bin").write_bytes(rnd(400_000, 31))
    (src / "deep" / "note.md").write_text("# notes\n" * 200)
    m = Manifest(tmp_path / "m.db")
    k = KeyRing.generate()
    k.save(tmp_path / "key.json")
    m.add_backend("vault", "local", {"path": str(tmp_path / "vault")})
    Engine(m, k, chunk_params=SMALL).backup(src, device="laptop")
    m.close()

    m2 = Manifest(tmp_path / "m.db")
    k2 = KeyRing.load(tmp_path / "key.json")
    out = tmp_path / "newmachine"
    r = Engine(m2, k2, chunk_params=SMALL).restore(out, device="laptop")
    assert r.ok, r.failed
    assert dirs_equal(src, out)
