"""Backing up from the dashboard: choosing a folder, running it, repeating."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freehold.core import KeyRing, Manifest  # noqa: E402
from freehold.core.errors import ConfigError  # noqa: E402
from freehold.dashboard import DashboardAPI  # noqa: E402


@pytest.fixture
def api(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    m = Manifest(home / "manifest.db")
    m.add_backend("disk", "local", {"path": str(tmp_path / "blobs")})
    yield DashboardAPI(home, m, KeyRing.generate()), tmp_path
    m.close()


def finish(api, job, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = api.backup_job(job.id)
        if current.state in ("done", "failed"):
            return current
        time.sleep(0.05)
    raise AssertionError("backup did not finish")


def test_browsing_this_computers_folders(api, tmp_path):
    board, tmp = api
    top = board.local_folders()
    assert top["dirs"] == [] and top["shortcuts"]
    assert all("label" in s and "path" in s for s in top["shortcuts"])

    (tmp / "docs" / "inner").mkdir(parents=True)
    (tmp / ".hidden").mkdir()
    (tmp / "a-file.txt").write_text("x")
    here = board.local_folders(str(tmp))
    names = [d["name"] for d in here["dirs"]]
    assert "docs" in names
    assert ".hidden" not in names        # hidden folders stay hidden
    assert "a-file.txt" not in names     # folders only
    assert here["parent"] == str(tmp.parent)


def test_browsing_somewhere_that_is_not_a_folder(api, tmp_path):
    board, tmp = api
    with pytest.raises(ConfigError):
        board.local_folders(str(tmp / "no-such-place"))


def test_backing_up_a_chosen_folder(api, tmp_path):
    board, tmp = api
    src = tmp / "src"
    src.mkdir()
    (src / "a.txt").write_text("hello " * 500)
    (src / "b.txt").write_text("world " * 500)

    job = finish(board, board.start_backup(str(src)))
    assert job.state == "done", job.error or job.errors
    assert job.files == 2 and job.uploaded > 0

    # it shows up as a folder you back up, and in the snapshot list
    folders = board.watched_folders()
    assert [f["path"] for f in folders] == [str(src)]
    assert folders[0]["last_backup"]
    assert board.snapshots()


def test_backing_up_again_sends_nothing_new(api, tmp_path):
    board, tmp = api
    src = tmp / "src"
    src.mkdir()
    (src / "a.txt").write_text("unchanged " * 400)
    finish(board, board.start_backup(str(src)))
    second = finish(board, board.start_backup(str(src)))
    assert second.state == "done"
    assert second.uploaded == 0 and second.new_chunks == 0


def test_the_recovery_copy_is_kept_up_to_date(api, tmp_path):
    """The manifest is pushed after each backup, so a lost machine can be
    recovered without the person having to know that is a thing."""
    from freehold.core.driver import build_driver
    from freehold.core.portable import pull_manifest
    board, tmp = api
    src = tmp / "src"
    src.mkdir()
    (src / "a.txt").write_text("content " * 300)
    finish(board, board.start_backup(str(src)))

    driver = build_driver("local", {"path": str(tmp / "blobs")})
    assert pull_manifest(driver, board.k, tmp / "recovered.db").exists()


def test_refusing_to_back_up_nonsense(api, tmp_path):
    board, tmp = api
    with pytest.raises(ConfigError):
        board.start_backup("")
    with pytest.raises(ConfigError):
        board.start_backup(str(tmp / "not-here"))


def test_refusing_to_back_up_with_nowhere_to_put_it(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    m = Manifest(home / "manifest.db")
    board = DashboardAPI(home, m, KeyRing.generate())
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("x")
    with pytest.raises(ConfigError) as exc:
        board.start_backup(str(src))
    assert "store the backup" in str(exc.value)
    m.close()


def test_forgetting_a_folder_keeps_the_backups(api, tmp_path):
    board, tmp = api
    src = tmp / "src"
    src.mkdir()
    (src / "a.txt").write_text("keep me " * 200)
    finish(board, board.start_backup(str(src)))

    assert board.forget_folder(str(src)) == {"removed": True}
    assert board.watched_folders() == []
    assert board.snapshots()            # the backup itself is untouched
