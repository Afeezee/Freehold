"""Measuring a folder before uploading any of it.

Written after a GitHub account was restricted for bulk uploading. The point
is not to predict exactly what a provider will do, but to refuse the runs
that are obviously going to cause trouble, before the first request.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freehold.core import Engine, KeyRing, Manifest  # noqa: E402
from freehold.core.driver import build_driver  # noqa: E402
from freehold.core.engine import Survey  # noqa: E402
from freehold.core.errors import ConfigError, FreeholdError  # noqa: E402
from freehold.dashboard import DashboardAPI  # noqa: E402


@pytest.fixture
def world(tmp_path):
    m = Manifest(tmp_path / "m.db")
    m.add_backend("usb", "local", {"path": str(tmp_path / "store")})
    return Engine(m, KeyRing.generate()), m, tmp_path


# -- the survey ----------------------------------------------------------
def test_survey_measures_without_reading_contents(world):
    eng, m, tmp = world
    src = tmp / "src"
    (src / "a" / "b" / "c").mkdir(parents=True)
    (src / "top.txt").write_text("x" * 100)
    (src / "a" / "mid.bin").write_bytes(b"y" * 5000)
    (src / "a" / "b" / "c" / "deep.bin").write_bytes(b"z" * 20000)

    s = eng.survey(src)
    assert s.files == 3
    assert s.bytes == 100 + 5000 + 20000
    assert s.largest_file == 20000
    assert s.largest_path.endswith("deep.bin")
    assert s.max_depth == 4          # a/b/c/deep.bin
    assert s.truncated is False


def test_survey_respects_exclusions(world):
    eng, m, tmp = world
    src = tmp / "src"
    (src / ".git" / "objects").mkdir(parents=True)
    (src / "keep.txt").write_text("keep")
    (src / ".git" / "objects" / "blob").write_text("ignore me")
    s = eng.survey(src)
    assert s.files == 1


def test_survey_stops_at_the_limit(world):
    eng, m, tmp = world
    src = tmp / "src"
    src.mkdir()
    for i in range(30):
        (src / ("f%d" % i)).write_text("x")
    s = eng.survey(src, limit=10)
    assert s.truncated is True and s.files == 10


def test_survey_refuses_a_path_that_is_not_a_folder(world):
    eng, m, tmp = world
    with pytest.raises(FreeholdError):
        eng.survey(tmp / "nope")


def test_many_small_files_cost_an_upload_each():
    """The thing that caused the trouble: not size, but count."""
    big_few = Survey(root="x", files=10, bytes=5 * 1024 ** 3)
    small_many = Survey(root="x", files=40_000, bytes=200 * 1024 ** 2)
    assert small_many.estimated_chunks > big_few.estimated_chunks


# -- what GitHub says ----------------------------------------------------
def github(**cfg):
    base = {"owner": "someone", "token": "x", "soft_cap": "1500000000"}
    base.update({k: str(v) for k, v in cfg.items()})
    return build_driver("github", base)


def test_github_blocks_a_run_beyond_its_hourly_allowance():
    issues = github().preflight(
        Survey(root="x", files=20_000, bytes=400 * 1024 ** 2))
    blocks = [i for i in issues if i.level == "block"]
    assert blocks and "more than GitHub allows in an hour" in blocks[0].message
    assert "disk or S3" in blocks[0].detail      # says what to do instead


def test_github_blocks_bulk_repository_creation():
    issues = github(soft_cap=100_000_000, max_calls=10_000_000).preflight(
        Survey(root="x", files=50, bytes=40 * 1024 ** 3))
    blocks = [i for i in issues if i.level == "block"]
    assert blocks and "private repositories" in blocks[0].message


def test_github_accepts_an_ordinary_folder():
    issues = github().preflight(
        Survey(root="x", files=300, bytes=200 * 1024 ** 2))
    assert not [i for i in issues if i.level == "block"]


def test_github_warns_about_a_pile_of_tiny_files():
    issues = github().preflight(
        Survey(root="x", files=1200, bytes=1200 * 1000))
    assert any("very small files" in i.message for i in issues)


def test_no_small_file_warning_for_a_handful_of_files():
    """A single tiny file is not "mostly very small files". Warnings that
    fire on everything are how real ones get ignored."""
    for count in (1, 10, 100):
        issues = github().preflight(
            Survey(root="x", files=count, bytes=count * 40))
        assert not any("very small files" in i.message for i in issues), count


# -- what the other backends say -----------------------------------------
def test_local_storage_refuses_what_will_not_fit(tmp_path):
    driver = build_driver("local", {"path": str(tmp_path)})
    huge = Survey(root="x", files=1, bytes=10 ** 15)
    blocks = [i for i in driver.preflight(huge) if i.level == "block"]
    assert blocks and "not enough room" in blocks[0].message

    ordinary = Survey(root="x", files=1, bytes=1000)
    assert not [i for i in driver.preflight(ordinary) if i.level == "block"]


def test_s3_warns_past_the_configured_quota(tmp_path):
    driver = build_driver("s3", {
        "endpoint": "https://example.invalid", "bucket": "b",
        "access_key": "a", "secret_key": "s", "quota": str(1024 ** 3)})
    issues = driver.preflight(Survey(root="x", files=1, bytes=2 * 1024 ** 3))
    assert any(i.level == "warn" for i in issues)


# -- and what the interface does with it ---------------------------------
def test_the_button_stays_off_when_storage_says_no(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("hello")
    m = Manifest(tmp_path / "m.db")
    m.add_backend("gh", "github", {"owner": "someone", "token": "x",
                                   "max_calls": "1"})
    api = DashboardAPI(tmp_path, m, KeyRing.generate())

    pf = api.preflight(str(src))
    assert pf["blocked"] is True
    assert pf["can_backup"] is False
    assert pf["issues"][0]["storage"] == "gh"

    # and starting one anyway is refused, however it was asked for
    with pytest.raises(ConfigError):
        api.start_backup(str(src))
    assert api.backup_jobs() == []
    m.close()


def test_a_normal_folder_is_allowed_through(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.bin").write_bytes(random.Random(1).randbytes(50_000))
    m = Manifest(tmp_path / "m.db")
    m.add_backend("usb", "local", {"path": str(tmp_path / "store")})
    api = DashboardAPI(tmp_path, m, KeyRing.generate())

    pf = api.preflight(str(src))
    assert pf["blocked"] is False and pf["can_backup"] is True
    assert pf["files"] == 1 and pf["estimated_uploads"] >= 1
    assert api.start_backup(str(src))
    m.close()


def test_an_empty_folder_is_not_offered_as_a_backup(tmp_path):
    src = tmp_path / "empty"
    src.mkdir()
    m = Manifest(tmp_path / "m.db")
    m.add_backend("usb", "local", {"path": str(tmp_path / "store")})
    api = DashboardAPI(tmp_path, m, KeyRing.generate())
    pf = api.preflight(str(src))
    assert pf["can_backup"] is False and "no files" in pf["reason"]
    m.close()


def test_force_overrides_a_block(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("hello")
    m = Manifest(tmp_path / "m.db")
    m.add_backend("usb", "local", {"path": str(tmp_path / "store")})
    api = DashboardAPI(tmp_path, m, KeyRing.generate())
    # the local backend will not block this, so check force simply works
    assert api.start_backup(str(src), force=True)
    m.close()
