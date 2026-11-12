"""The whole thing, live, end to end.

Walks every MVP success criterion against the real GitHub API, finishing with
the scenario the project exists for: the machine is gone, and all that is left
is the encryption key and the backend credentials.

    export FREEHOLD_GITHUB_TOKEN=...            # classic PAT, repo scope
    python tools/live_e2e_test.py
    python tools/live_e2e_test.py --cleanup     # remove the test repos

If FREEHOLD_S3_ENDPOINT, FREEHOLD_S3_BUCKET and the S3 credentials are set,
the second backend is a real S3-compatible store; otherwise it is a local
directory. The script says which it used.
"""
from __future__ import annotations

import argparse
import filecmp
import os
import random
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freehold.core import Engine, KeyRing, Manifest  # noqa: E402
from freehold.core.driver import build_driver  # noqa: E402
from freehold.core.manifest import ManifestState  # noqa: E402
from freehold.core.portable import pull_manifest, push_manifest  # noqa: E402
from freehold.dashboard import DashboardAPI  # noqa: E402
from freehold.drivers.githubapi import GitHubAPI  # noqa: E402

PREFIX = "freehold-e2e"
SOFT_CAP = 900_000
CHUNKS = {"min_size": 64 * 1024, "avg_size": 128 * 1024,
          "max_size": 256 * 1024}

PASS, FAIL = [], []


def say(step: str, detail: str = "") -> None:
    print(("[%-9s] " % step) + detail, flush=True)


def check(name: str, ok: bool, detail: str = "") -> bool:
    (PASS if ok else FAIL).append(name)
    say("ok" if ok else "FAIL", name + (" -- " + detail if detail else ""))
    return ok


def human(n) -> str:
    v = float(n or 0)
    for u in ("B", "KB", "MB", "GB"):
        if v < 1024 or u == "GB":
            return ("%d %s" % (int(v), u) if u == "B" else "%.1f %s" % (v, u))
        v /= 1024
    return str(n)


def make_tree(root: Path) -> None:
    (root / "docs").mkdir(parents=True)
    (root / "media" / "2026").mkdir(parents=True)
    rng = random.Random(4242)
    (root / "docs" / "thesis.txt").write_text("chapter one\n" * 40000)
    (root / "docs" / "refs.bib").write_text("@book{key}\n" * 2000)
    (root / "docs" / "empty.txt").write_bytes(b"")
    for i in range(2):
        (root / "media" / "2026" / ("IMG_%03d.jpg" % i)).write_bytes(
            rng.randbytes(800_000))


def dirs_equal(a: Path, b: Path) -> bool:
    left = sorted(p.relative_to(a).as_posix() for p in a.rglob("*")
                  if p.is_file())
    right = sorted(p.relative_to(b).as_posix() for p in b.rglob("*")
                   if p.is_file())
    if left != right:
        say("diff", "file lists differ: " + str(set(left) ^ set(right)))
        return False
    bad = [r for r in left if not filecmp.cmp(a / r, b / r, shallow=False)]
    if bad:
        say("diff", "contents differ: " + str(bad))
    return not bad


def token() -> str:
    t = os.environ.get("FREEHOLD_GITHUB_TOKEN")
    if not t:
        sys.exit("set FREEHOLD_GITHUB_TOKEN first")
    return t


def second_backend(work: Path):
    """Real S3 if the environment provides one, else a local directory."""
    endpoint = os.environ.get("FREEHOLD_S3_ENDPOINT")
    bucket = os.environ.get("FREEHOLD_S3_BUCKET")
    if endpoint and bucket and os.environ.get("FREEHOLD_S3_ACCESS_KEY_ID"):
        return "s3", {"endpoint": endpoint, "bucket": bucket,
                      "region": os.environ.get("FREEHOLD_S3_REGION", "auto"),
                      "prefix": "freehold-e2e"}, "real S3 at " + endpoint
    path = work / "mirror"
    return "local", {"path": str(path)}, "local directory (no S3 "\
        "credentials in the environment)"


def cleanup(owner: str, api: GitHubAPI) -> int:
    doomed = [n for n in ("%s-%03d" % (PREFIX, i) for i in range(1, 40))
              if api.get_repo(owner, n)]
    if not doomed:
        say("cleanup", "nothing to remove")
        return 0
    print("about to permanently delete:")
    for d in doomed:
        print("   " + owner + "/" + d)
    if os.environ.get("FREEHOLD_LIVE_CONFIRM") != "yes":
        sys.exit("refusing to delete without FREEHOLD_LIVE_CONFIRM=yes")
    for d in doomed:
        say("cleanup", ("deleted " if api.delete_repo(owner, d)
                        else "missing ") + d)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cleanup", action="store_true")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    api = GitHubAPI(token())
    owner = api.whoami()
    say("auth", "authenticated as " + owner)
    if args.cleanup:
        return cleanup(owner, api)

    work = Path(tempfile.mkdtemp(prefix="freehold-e2e-"))
    src = work / "src"
    home = work / "home"
    home.mkdir(parents=True)
    try:
        make_tree(src)
        total = sum(p.stat().st_size for p in src.rglob("*") if p.is_file())
        say("source", human(total) + " across "
            + str(sum(1 for p in src.rglob("*") if p.is_file())) + " files")

        drv2, cfg2, where2 = second_backend(work)
        say("backends", "github (pool, cap " + human(SOFT_CAP)
            + ") + " + drv2 + ": " + where2)

        key = KeyRing.generate()
        key.save(home / "key.json")
        m = Manifest(home / "manifest.db")
        gh_cfg = {"owner": owner, "prefix": PREFIX, "soft_cap": str(SOFT_CAP)}
        m.add_backend("gh", "github", gh_cfg)
        m.add_backend("mirror", drv2, cfg2)

        # 1. back up to both backends at once
        eng = Engine(m, key, redundancy=2, chunk_params=CHUNKS,
                     workers=args.workers)
        t0 = time.time()
        first = eng.backup(src, device="workstation")
        say("backup", "%d files, %d chunks, %s to each backend, %.1fs"
            % (first.files, first.chunks_new, human(first.bytes_uploaded / 2),
               time.time() - t0))
        for w in first.warnings:
            say("note", w)
        check("backup completed without errors", not first.errors,
              "; ".join(first.errors[:2]))

        # 2. the GitHub pool rotated
        pool = m.get_backend_state(1, "pool")
        for r in pool["repos"]:
            say("pool", "%-24s %-7s %9s %3d chunks"
                % (r["name"], r["status"], human(r["bytes"]), r["chunks"]))
        check("repo pool rotated", len(pool["repos"]) > 1,
              str(len(pool["repos"])) + " repo(s)")
        check("exactly one active repo",
              sum(1 for r in pool["repos"] if r["status"] == "active") == 1)
        check("soft cap respected",
              all(r["bytes"] <= SOFT_CAP for r in pool["repos"]))
        spread = {r["location"].split("#")[0] for r in
                  m.db.execute("SELECT location FROM chunk_location "
                               "WHERE backend_id=1")}
        check("chunks spread across the pool", len(spread) > 1,
              str(len(spread)) + " repos hold chunks")

        # 3. the second driver took the same data with no core changes
        rows = {r["name"]: r for r in m.usage_by_backend()}
        check("both backends hold every chunk",
              rows["gh"]["chunk_count"] == rows["mirror"]["chunk_count"]
              == first.chunks_new,
              str(rows["gh"]["chunk_count"]) + " vs "
              + str(rows["mirror"]["chunk_count"]))

        # 4. an edit re-uploads only the changed chunks
        victim = src / "media" / "2026" / "IMG_000.jpg"
        data = bytearray(victim.read_bytes())
        data[1234] ^= 0xFF
        victim.write_bytes(bytes(data))
        os.utime(victim, (0, 0))
        inc = eng.backup(src, device="workstation")
        say("incremental", "%d of %d chunks re-uploaded (%s)"
            % (inc.chunks_new, inc.chunks_seen, human(inc.bytes_uploaded)))
        check("incremental sent only changed chunks",
              0 < inc.chunks_new < first.chunks_new / 2)

        # 5. the dashboard sees both backends and can restore a point in time
        board = DashboardAPI(home, m, key, workers=args.workers)
        over = board.overview()
        names = {b["name"] for b in over["backends"]}
        check("dashboard lists both backends", names == {"gh", "mirror"},
              str(sorted(names)))
        live = {b["name"]: b for b in board.backends(live=True)}
        check("dashboard shows the GitHub repo breakdown",
              bool(live["gh"]["detail"].get("repos")))
        check("dashboard shows usage per backend",
              live["gh"]["bytes"] > 0 and live["mirror"]["bytes"] > 0,
              human(live["gh"]["bytes"]) + " / "
              + human(live["mirror"]["bytes"]))

        snaps = board.snapshots("workstation")
        check("two snapshots recorded", len(snaps) == 2)
        oldest = snaps[-1]["id"]
        pit = work / "point-in-time"
        job = board.start_restore(oldest, str(pit))
        while board.job(job.id).state in ("queued", "running"):
            time.sleep(0.2)
        job = board.job(job.id)
        say("dashboard", "point-in-time restore: %s, %d files, %s"
            % (job.state, job.restored, human(job.bytes_written)))
        check("dashboard point-in-time restore completed",
              job.state == "done", str(job.failures[:1]))
        restored_old = (pit / "media" / "2026" / "IMG_000.jpg").read_bytes()
        check("point-in-time gave the pre-edit file",
              restored_old != bytes(data) and len(restored_old) == len(data))

        # 6. push the manifest so a replacement machine can find it
        targets = [(b.name, build_driver(b.driver_id, b.config,
                                         ManifestState(m, b.id)))
                   for b in m.backends(only_enabled=True)]
        pushed = push_manifest(m, key, targets)
        for name, where in sorted(pushed.items()):
            say("manifest", name + " -> " + where)
        check("manifest stored in both backends",
              all(not v.startswith("error:") for v in pushed.values()))

        # 7. deep verify: every chunk really is readable from a backend
        problems = eng.verify(deep=True)
        check("deep verify found every chunk readable", not problems,
              "; ".join(problems[:2]))
        eng.close()
        m.close()

        # ---- the lost device -------------------------------------------
        say("disaster", "destroying the machine: manifest, config, "
                        "everything except the key")
        carried_key = KeyRing(key.master)       # the only thing carried out
        shutil.rmtree(home)
        check("old machine is gone", not home.exists())

        if drv2 == "local":
            (work / "mirror" / ".offline").write_text("")
            say("disaster", "and the mirror backend is unreachable")
        else:
            cfg2 = dict(cfg2, access_key="revoked", secret_key="revoked")
            say("disaster", "and the mirror's credentials no longer work")

        new_home = work / "replacement"
        new_home.mkdir()
        carried_key.save(new_home / "key.json")

        # nothing but the key and the GitHub credentials
        fresh_driver = build_driver("github", gh_cfg)
        t0 = time.time()
        pull_manifest(fresh_driver, carried_key, new_home / "manifest.db")
        say("recovery", "manifest recovered from GitHub in %.1fs "
                        "(repo pool rediscovered via the API)"
            % (time.time() - t0))
        check("manifest recovered with only the key and credentials",
              (new_home / "manifest.db").exists())

        m2 = Manifest(new_home / "manifest.db")
        if drv2 != "local":
            bad = m2.backend_by_name("mirror")
            m2.update_backend_config(bad.id, cfg2)
            m2.commit()
        check("recovered manifest knows both backends",
              {b.name for b in m2.backends()} == {"gh", "mirror"})
        recovered_snaps = m2.snapshots("workstation")
        check("recovered manifest has both snapshots",
              len(recovered_snaps) == 2)

        out = work / "restored"
        t0 = time.time()
        result = Engine(m2, carried_key, chunk_params=CHUNKS,
                        workers=args.workers).restore(out,
                                                      device="workstation")
        say("recovery", "%d files, %s, %.1fs"
            % (len(result.restored), human(result.bytes_written),
               time.time() - t0))
        for f in result.failed:
            say("FAIL", f.path + ": " + f.reason)
        check("restore succeeded with one backend dead", result.ok)
        # The engine stops at the first location that answers, so a backend
        # it never needed is never discovered to be down. What matters is
        # that every byte came from the surviving one.
        check("every chunk came from GitHub alone",
              result.served_by == ["gh"], "served by "
              + str(result.served_by))
        mirror = build_driver(drv2, cfg2)
        try:
            mirror.health()
            dead = False
        except Exception:
            dead = True
        check("the mirror really was unreachable throughout", dead)
        check("restored tree is byte-identical to the original",
              dirs_equal(src, out))
        m2.close()
    finally:
        if args.keep:
            say("kept", str(work))
        else:
            shutil.rmtree(work, ignore_errors=True)

    print()
    print("%d checks passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        for name in FAIL:
            print("  FAILED: " + name)
        print("\nrepos left in place for inspection; remove with --cleanup")
        return 1
    print("\nALL CHECKS PASSED")
    print("remove the test repos with:  python tools/live_e2e_test.py "
          "--cleanup")
    return 0


if __name__ == "__main__":
    sys.exit(main())
