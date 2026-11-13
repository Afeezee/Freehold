"""Dashboard: the API over the manifest, and the HTTP layer that guards it."""
from __future__ import annotations

import filecmp
import json
import random
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freehold.core import Engine, KeyRing, Manifest  # noqa: E402
from freehold.core.errors import ConfigError, FreeholdError  # noqa: E402
from freehold.dashboard import Dashboard, DashboardAPI  # noqa: E402

SMALL = {"min_size": 2048, "avg_size": 4096, "max_size": 8192}


def rnd(n: int, seed: int) -> bytes:
    return random.Random(seed).randbytes(n)


@pytest.fixture
def world(tmp_path):
    src = tmp_path / "src"
    (src / "docs").mkdir(parents=True)
    (src / "photos").mkdir()
    (src / "docs" / "plan.txt").write_text("the plan\n" * 400)
    (src / "docs" / "notes.md").write_text("notes\n" * 100)
    (src / "photos" / "shot.bin").write_bytes(rnd(60_000, 4))
    home = tmp_path / "home"
    home.mkdir()
    m = Manifest(home / "manifest.db")
    k = KeyRing.generate()
    m.add_backend("disk", "local", {"path": str(tmp_path / "blobs")})
    eng = Engine(m, k, chunk_params=SMALL)
    first = eng.backup(src, device="laptop")
    time.sleep(1.1)                      # distinct snapshot timestamps
    (src / "docs" / "plan.txt").write_text("the revised plan\n" * 400)
    second = eng.backup(src, device="laptop")
    api = DashboardAPI(home, m, k)
    return api, m, k, src, home, first, second


def dirs_equal(a: Path, b: Path) -> bool:
    left = sorted(p.relative_to(a).as_posix() for p in a.rglob("*")
                  if p.is_file())
    right = sorted(p.relative_to(b).as_posix() for p in b.rglob("*")
                   if p.is_file())
    return left == right and all(
        filecmp.cmp(a / r, b / r, shallow=False) for r in left)


def wait_for(job, api, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = api.job(job.id)
        if current.state in ("done", "failed"):
            return current
        time.sleep(0.05)
    raise AssertionError("restore job did not finish")


# -- what the dashboard shows ---------------------------------------------
def test_overview_reports_devices_folders_and_usage(world):
    api, m, k, src, home, first, second = world
    data = api.overview()

    assert [d["name"] for d in data["devices"]] == ["laptop"]
    dev = data["devices"][0]
    assert dev["last_snapshot"] == second.snapshot_id
    assert dev["files"] == 3 and dev["size"] > 0
    assert dev["roots"] == [str(src.resolve())]

    assert len(data["folders"]) == 1
    assert data["folders"][0]["snapshot"] == second.snapshot_id

    assert data["totals"]["chunks"] > 0
    assert [b["name"] for b in data["backends"]] == ["disk"]


def test_backend_rows_include_the_drivers_own_view(world):
    api, m, k, src, home, _f, _s = world
    rows = api.backends(live=True)
    assert rows[0]["driver"] == "local"
    assert rows[0]["chunks"] > 0
    assert "path" in rows[0]["detail"]
    assert not rows[0]["error"]


def test_github_appears_as_one_backend_among_others(world):
    """The dashboard must not special-case GitHub. It is a row like any
    other, with its per-repo breakdown carried in the driver's own detail."""
    import test_github  # noqa: F401  (registers github-test)
    api, m, k, src, home, _f, _s = world
    api.add_backend("gh", "github-test",
                    {"owner": "tester", "store": "dash", "prefix": "fh"})
    eng = Engine(m, k, chunk_params=SMALL)
    # dedup is global, so a plain backup would upload nothing to a new
    # backend: replication is what actually populates it
    tally = eng.replicate("gh")
    assert tally["copied"] > 0 and tally["failed"] == 0

    rows = {r["name"]: r for r in api.backends(live=True)}
    assert set(rows) == {"disk", "gh"}
    assert rows["gh"]["driver"] == "github-test"
    assert [r["name"] for r in rows["gh"]["detail"]["repos"]]
    assert "repos" not in rows["disk"]["detail"]     # not a shared concept


def test_drivers_are_listed_for_the_add_form(world):
    api, m, k, src, home, _f, _s = world
    listed = {d["id"]: d["options"] for d in api.drivers()}
    # other test modules register their own drivers into the same global
    # registry, so assert about the real ones rather than everything present
    assert {"local", "github", "s3"} <= set(listed)
    for shipped in ("local", "github", "s3"):
        assert listed[shipped], shipped + " has no option help for the form"


# -- the restore browser --------------------------------------------------
def test_tree_walks_one_level_at_a_time(world):
    api, m, k, src, home, _f, second = world
    top = api.tree(second.snapshot_id)
    assert [d["name"] for d in top["dirs"]] == ["docs", "photos"]
    assert top["files"] == []
    assert top["dirs"][0]["files"] == 2

    docs = api.tree(second.snapshot_id, "docs")
    assert [f["name"] for f in docs["files"]] == ["notes.md", "plan.txt"]
    assert docs["files"][0]["path"] == "docs/notes.md"
    assert docs["prefix"] == "docs"


def test_tree_rejects_an_unknown_snapshot(world):
    api, m, k, src, home, _f, _s = world
    with pytest.raises(FreeholdError):
        api.tree(99999)


def test_point_in_time_restore_of_a_whole_snapshot(world):
    api, m, k, src, home, first, second = world
    out = home / "old"
    job = wait_for(api.start_restore(first.snapshot_id, str(out)), api)
    assert job.state == "done", job.failures
    assert job.restored == 3
    # the earlier snapshot, so the pre-edit text
    assert (out / "docs" / "plan.txt").read_text().startswith("the plan")


def test_selective_restore_takes_only_what_was_picked(world):
    api, m, k, src, home, _f, second = world
    out = home / "just-docs"
    job = wait_for(api.start_restore(second.snapshot_id, str(out),
                                     paths=["docs/notes.md"]), api)
    assert job.state == "done", job.failures
    assert job.restored == 1
    assert (out / "docs" / "notes.md").exists()
    assert not (out / "photos").exists()


def test_restoring_a_folder_prefix(world):
    api, m, k, src, home, _f, second = world
    out = home / "photos-only"
    job = wait_for(api.start_restore(second.snapshot_id, str(out),
                                     paths=["photos"]), api)
    assert job.state == "done"
    assert (out / "photos" / "shot.bin").read_bytes() == (
        src / "photos" / "shot.bin").read_bytes()


def test_restore_keeps_existing_files_unless_told_otherwise(world):
    api, m, k, src, home, _f, second = world
    out = home / "conflict"
    (out / "docs").mkdir(parents=True)
    (out / "docs" / "notes.md").write_text("mine\n")

    job = wait_for(api.start_restore(second.snapshot_id, str(out),
                                     paths=["docs/notes.md"]), api)
    assert job.state == "done"
    assert (out / "docs" / "notes.md").read_text() == "mine\n"
    assert job.renamed

    job2 = wait_for(api.start_restore(second.snapshot_id, str(out),
                                      paths=["docs/notes.md"],
                                      overwrite=True), api)
    assert job2.state == "done" and not job2.renamed
    assert (out / "docs" / "notes.md").read_text().startswith("notes")


def test_failed_restore_is_reported_with_chunk_and_backend(world, tmp_path):
    api, m, k, src, home, _f, second = world
    for blob in (tmp_path / "blobs").rglob("*"):
        if blob.is_file():
            blob.unlink()
    job = wait_for(api.start_restore(second.snapshot_id,
                                     str(home / "doomed")), api)
    assert job.state == "failed"
    assert job.failures and job.failures[0]["chunk"]
    assert "disk" in job.failures[0]["backends"]


def test_restore_needs_a_destination(world):
    api, m, k, src, home, _f, second = world
    with pytest.raises(ConfigError):
        api.start_restore(second.snapshot_id, "")


# -- adding and removing backends ----------------------------------------
def test_add_and_remove_a_backend(world, tmp_path):
    api, m, k, src, home, _f, _s = world
    api.add_backend("second", "local", {"path": str(tmp_path / "other")})
    assert {b["name"] for b in api.backends()} == {"disk", "second"}

    api.set_backend_enabled("second", False)
    assert [b for b in api.backends() if b["name"] == "second"
            ][0]["enabled"] is False

    api.remove_backend("second")
    assert {b["name"] for b in api.backends()} == {"disk"}


def test_bad_backend_config_is_refused_before_it_is_stored(world):
    api, m, k, src, home, _f, _s = world
    with pytest.raises(ConfigError):
        api.add_backend("broken", "local", {})          # no path
    with pytest.raises(ConfigError):
        api.add_backend("nope", "no-such-driver", {})
    with pytest.raises(ConfigError):
        api.add_backend("disk", "local", {"path": "x"})  # duplicate name
    assert {b["name"] for b in api.backends()} == {"disk"}


def test_will_not_remove_the_only_backend_holding_chunks(world):
    api, m, k, src, home, _f, _s = world
    with pytest.raises(ConfigError) as exc:
        api.remove_backend("disk")
    assert "only backend" in str(exc.value)


def test_check_reports_an_unreachable_backend(world, tmp_path):
    api, m, k, src, home, _f, _s = world
    assert api.check_backend("disk")["ok"] is True
    (tmp_path / "blobs" / ".offline").write_text("")
    result = api.check_backend("disk")
    assert result["ok"] is False and "offline" in result["message"]


# -- the HTTP layer -------------------------------------------------------
@pytest.fixture
def served(world):
    api = world[0]
    board = Dashboard(api, port=0)
    board.start()
    yield board, api
    board.stop()


def fetch(board, path, body=None, token=None, headers=None):
    url = board.url.split("?")[0].rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=
                                 "POST" if data else "GET")
    tok = board.token if token is None else token
    if tok:
        req.add_header("X-Freehold-Token", tok)
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    # A freshly bound loopback server can refuse the first connection on a
    # busy machine; retry briefly rather than letting it flake.
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")
        except (urllib.error.URLError, ConnectionError):
            if attempt == 3:
                raise
            time.sleep(0.25)


def test_serves_the_page_and_the_api(served):
    board, api = served
    status, doc = fetch(board, "/api/overview")
    assert status == 200 and doc["devices"][0]["name"] == "laptop"

    url = board.url.split("?")[0]
    with urllib.request.urlopen(url, timeout=10) as r:
        page = r.read().decode()
    assert "<title>Freehold</title>" in page and "app.js" in r.headers.get(
        "Content-Type", "text/html") is False or "Freehold" in page


def test_token_is_required(served):
    board, api = served
    assert fetch(board, "/api/overview", token="")[0] == 403
    assert fetch(board, "/api/overview", token="wrong")[0] == 403
    assert fetch(board, "/api/overview")[0] == 200


def test_cross_origin_writes_are_refused(served):
    board, api = served
    status, doc = fetch(board, "/api/backends/check", {"name": "disk"},
                        headers={"Origin": "http://evil.example"})
    assert status == 403 and "cross-origin" in doc["error"]


def test_static_serving_will_not_escape_its_folder(served):
    board, api = served
    url = board.url.split("?")[0].rstrip("/") + "/../../cli.py"
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read().decode("utf-8", "replace")
        assert "def main" not in body
    except urllib.error.HTTPError as exc:
        assert exc.code in (403, 404)


def test_restore_over_http_end_to_end(served, tmp_path):
    board, api = served
    status, snaps = fetch(board, "/api/snapshots?device=laptop")
    assert status == 200
    newest = snaps["snapshots"][0]

    out = tmp_path / "http-restore"
    status, job = fetch(board, "/api/restore",
                        {"snapshot": newest["id"], "dest": str(out)})
    assert status == 202
    final = wait_for(api.job(job["id"]), api)
    assert final.state == "done", final.failures

    status, doc = fetch(board, "/api/jobs/" + job["id"])
    assert status == 200 and doc["state"] == "done"
    assert (out / "docs" / "plan.txt").exists()


def test_unknown_endpoint_is_a_clean_error(served):
    board, api = served
    status, doc = fetch(board, "/api/nope")
    assert status == 400 and "unknown endpoint" in doc["error"]
