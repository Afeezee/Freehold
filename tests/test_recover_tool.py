"""The promise in RECOVERY.md, checked by running the actual script.

tools/recover.py must be able to rebuild everything with no part of the
freehold package involved. It is run as a subprocess in isolated mode so an
accidental import could not silently pass.
"""
from __future__ import annotations

import filecmp
import random
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freehold.core import Engine, KeyRing, Manifest  # noqa: E402
from freehold.core.driver import build_driver  # noqa: E402
from freehold.core.manifest import ManifestState  # noqa: E402
from freehold.core.portable import push_manifest  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "recover.py"
SMALL = {"min_size": 2048, "avg_size": 4096, "max_size": 8192}


def rnd(n: int, seed: int) -> bytes:
    return random.Random(seed).randbytes(n)


def run(*args) -> str:
    """Isolated mode: no site dir, no cwd on sys.path, no env tricks."""
    out = subprocess.run([sys.executable, "-I", str(TOOL), *args],
                         capture_output=True, text=True, cwd=str(ROOT))
    if out.returncode != 0:
        raise AssertionError("recover.py failed:\n" + out.stdout + out.stderr)
    return out.stdout


@pytest.fixture
def backed_up(tmp_path):
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "notes.txt").write_text("independent recovery\n" * 400)
    (src / "sub" / "data.bin").write_bytes(rnd(90_000, 5))
    home = tmp_path / "home"
    home.mkdir()
    key = KeyRing.generate()
    key.save(home / "key.json")
    m = Manifest(home / "manifest.db")
    m.add_backend("vault", "local", {"path": str(tmp_path / "vault")})
    Engine(m, key, chunk_params=SMALL).backup(src, device="oldpc")
    push_manifest(m, key, [(b.name, build_driver(b.driver_id, b.config,
                                                 ManifestState(m, b.id)))
                           for b in m.backends()])
    m.close()
    return src, home, tmp_path


def dirs_equal(a: Path, b: Path) -> bool:
    left = sorted(p.relative_to(a).as_posix() for p in a.rglob("*")
                  if p.is_file())
    right = sorted(p.relative_to(b).as_posix() for p in b.rglob("*")
                   if p.is_file())
    return left == right and all(
        filecmp.cmp(a / r, b / r, shallow=False) for r in left)


def test_recovers_files_with_the_manifest_in_hand(backed_up):
    src, home, tmp = backed_up
    run("--manifest", str(home / "manifest.db"), "--key",
        str(home / "key.json"), "--blobs", str(tmp / "vault"),
        "--out", str(tmp / "out"))
    assert dirs_equal(src, tmp / "out")


def test_recovers_the_manifest_itself_then_the_files(backed_up):
    """The lost-device path with nothing but the key and a pile of blobs."""
    src, home, tmp = backed_up
    blob_id = run("--key", str(home / "key.json"), "--manifest-id").strip()
    assert len(blob_id) == 64

    matches = [p for p in (tmp / "vault").rglob("*") if p.name == blob_id]
    assert matches, "the manifest blob should be findable by its derived id"

    recovered = tmp / "recovered.db"
    run("--key", str(home / "key.json"), "--manifest-blob", str(matches[0]),
        "--out-db", str(recovered))
    assert recovered.exists()
    assert recovered.read_bytes()[:15] == b"SQLite format 3"

    run("--manifest", str(recovered), "--key", str(home / "key.json"),
        "--blobs", str(tmp / "vault"), "--out", str(tmp / "out2"))
    assert dirs_equal(src, tmp / "out2")


def test_the_wrong_key_recovers_nothing(backed_up, tmp_path):
    src, home, tmp = backed_up
    other = tmp / "other-key.json"
    KeyRing.generate().save(other)
    blob_id = run("--key", str(other), "--manifest-id").strip()
    # a different key derives a different address, so it cannot even look
    assert not [p for p in (tmp / "vault").rglob("*") if p.name == blob_id]

    real_id = run("--key", str(home / "key.json"), "--manifest-id").strip()
    blob = [p for p in (tmp / "vault").rglob("*") if p.name == real_id][0]
    result = subprocess.run(
        [sys.executable, "-I", str(TOOL), "--key", str(other),
         "--manifest-blob", str(blob), "--out-db", str(tmp / "no.db")],
        capture_output=True, text=True)
    assert result.returncode != 0
    assert "failed to decrypt" in (result.stdout + result.stderr)


def test_it_says_what_it_needs(backed_up):
    src, home, tmp = backed_up
    result = subprocess.run(
        [sys.executable, "-I", str(TOOL), "--key", str(home / "key.json")],
        capture_output=True, text=True)
    assert result.returncode != 0
    assert "--manifest" in (result.stdout + result.stderr)
