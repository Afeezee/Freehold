"""End-to-end test of the GitHub driver against the real API.

This creates real private repositories under the authenticated account, named
<prefix>-001, -002, ... It writes nothing outside those repos and touches no
existing repository.

    export FREEHOLD_GITHUB_TOKEN=...
    python tools/live_github_test.py                 # run the test
    python tools/live_github_test.py --cleanup       # delete the test repos

The soft cap is deliberately tiny so repo rotation actually happens on a few
megabytes instead of 1.5 GB.
"""
from __future__ import annotations

import argparse
import filecmp
import os
import random
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freehold.core import Engine, KeyRing, Manifest  # noqa: E402
from freehold.core.driver import register  # noqa: E402
from freehold.core.manifest import ManifestState  # noqa: E402
from freehold.drivers.github import GitHubDriver  # noqa: E402
from freehold.drivers.githubapi import GitHubAPI  # noqa: E402

PREFIX = "freehold-live-test"
SOFT_CAP = 1_000_000
CHUNKS = {"min_size": 64 * 1024, "avg_size": 128 * 1024,
          "max_size": 256 * 1024}


def say(step: str, detail: str = "") -> None:
    print(("[%-7s] " % step) + detail, flush=True)


class CountingAPI(GitHubAPI):
    """Wraps the real client to answer one question: how many transfers were
    actually in flight at once? If uploads peak at 8 the pool is working and
    any remaining slowness is the uplink; if they peak at 1 or 2, something is
    serialising them."""

    def __init__(self, token: str):
        super().__init__(token)
        self._l = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._l:
            self.up_live = self.up_peak = self.up_calls = self.up_bytes = 0
            self.dn_live = self.dn_peak = self.dn_calls = self.dn_bytes = 0

    def upload_asset(self, owner, repo, release_id, name, data):
        with self._l:
            self.up_live += 1
            self.up_peak = max(self.up_peak, self.up_live)
            self.up_calls += 1
            self.up_bytes += len(data)
        try:
            return super().upload_asset(owner, repo, release_id, name, data)
        finally:
            with self._l:
                self.up_live -= 1

    def download_asset(self, owner, repo, asset_id):
        with self._l:
            self.dn_live += 1
            self.dn_peak = max(self.dn_peak, self.dn_live)
            self.dn_calls += 1
        try:
            blob = super().download_asset(owner, repo, asset_id)
            with self._l:
                self.dn_bytes += len(blob)
            return blob
        finally:
            with self._l:
                self.dn_live -= 1


_API: list = []


@register
class InstrumentedGitHubDriver(GitHubDriver):
    """The real driver; only the API client is wrapped, so pool rotation and
    locking behave exactly as in production."""
    driver_id = "github-instrumented"

    def __init__(self, config, state=None, api=None):
        super().__init__(config, state, api=api or _API[0])


def rate(nbytes: int, seconds: float) -> str:
    if seconds <= 0:
        return "-"
    return "%.0f KB/s" % (nbytes / seconds / 1024)


def human(n: int) -> str:
    v = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024 or unit == "GB":
            return ("%d %s" % (int(v), unit) if unit == "B"
                    else "%.1f %s" % (v, unit))
        v /= 1024
    return str(n)


def make_tree(root: Path) -> None:
    (root / "photos").mkdir(parents=True)
    (root / "docs").mkdir(parents=True)
    rng = random.Random(1234)
    for i in range(3):
        (root / "photos" / ("img%02d.jpg" % i)).write_bytes(
            rng.randbytes(700_000))
    (root / "docs" / "notes.md").write_text("# notes\n" + "a line\n" * 5000)
    (root / "docs" / "empty.txt").write_bytes(b"")


def dirs_equal(a: Path, b: Path) -> bool:
    left = sorted(p.relative_to(a).as_posix() for p in a.rglob("*")
                  if p.is_file())
    right = sorted(p.relative_to(b).as_posix() for p in b.rglob("*")
                   if p.is_file())
    if left != right:
        print("  file lists differ:", left, "vs", right)
        return False
    bad = [r for r in left if not filecmp.cmp(a / r, b / r, shallow=False)]
    if bad:
        print("  contents differ:", bad)
    return not bad


def token() -> str:
    t = os.environ.get("FREEHOLD_GITHUB_TOKEN")
    if not t:
        sys.exit("set FREEHOLD_GITHUB_TOKEN first")
    return t


def cleanup(owner: str, api: GitHubAPI) -> int:
    doomed = []
    n = 1
    while n < 60:
        name = "%s-%03d" % (PREFIX, n)
        if api.get_repo(owner, name):
            doomed.append(name)
        n += 1
    if not doomed:
        say("cleanup", "nothing to remove")
        return 0
    print("about to permanently delete these repositories:")
    for d in doomed:
        print("   " + owner + "/" + d)
    if os.environ.get("FREEHOLD_LIVE_CONFIRM") != "yes":
        sys.exit("refusing to delete without FREEHOLD_LIVE_CONFIRM=yes")
    for d in doomed:
        ok = api.delete_repo(owner, d)
        say("cleanup", ("deleted " if ok else "missing ") + d)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cleanup", action="store_true")
    ap.add_argument("--keep", action="store_true",
                    help="keep the working directory for inspection")
    ap.add_argument("--workers", type=int, default=8,
                    help="concurrent chunk transfers (1 to serialise)")
    args = ap.parse_args()

    api = CountingAPI(token())
    _API.append(api)
    owner = api.whoami()
    say("auth", "authenticated as " + owner)
    if args.cleanup:
        return cleanup(owner, api)

    work = Path(tempfile.mkdtemp(prefix="freehold-live-"))
    src, out = work / "src", work / "restored"
    failures = []
    try:
        make_tree(src)
        total = sum(p.stat().st_size for p in src.rglob("*") if p.is_file())
        say("source", str(total) + " bytes across "
            + str(sum(1 for p in src.rglob("*") if p.is_file())) + " files")

        m = Manifest(work / "manifest.db")
        k = KeyRing.generate()
        k.save(work / "key.json")
        bid = m.add_backend("gh", "github-instrumented", {
            "owner": owner, "prefix": PREFIX, "soft_cap": str(SOFT_CAP)})
        eng = Engine(m, k, chunk_params=CHUNKS, workers=args.workers)
        say("workers", str(args.workers) + " concurrent transfers")

        api.reset()
        t0 = time.time()
        res = eng.backup(src, device="live-test")
        secs = time.time() - t0
        say("backup", "%d files, %d chunks, %d uploaded, %.1fs"
            % (res.files, res.chunks_seen, res.chunks_new, secs))
        say("up", "%d uploads, %s, %s, peak %d in flight (of %d workers)"
            % (api.up_calls, human(api.up_bytes), rate(api.up_bytes, secs),
               api.up_peak, args.workers))
        if args.workers > 1 and api.up_peak <= 1:
            failures.append("uploads never overlapped: the pool is not "
                            "working")
        for w in res.warnings:
            say("note", w)
        for e in res.errors:
            say("ERROR", e)
            failures.append(e)

        pool = m.get_backend_state(bid, "pool")
        for r in pool["repos"]:
            say("pool", "%-26s %-7s %8d bytes  %3d chunks"
                % (r["name"], r["status"], r["bytes"], r["chunks"]))
        if len(pool["repos"]) < 2:
            failures.append("rotation never triggered")
        if sum(1 for r in pool["repos"] if r["status"] == "active") != 1:
            failures.append("expected exactly one active repo")
        if not any(r["status"] == "sealed" for r in pool["repos"]):
            failures.append("no repo was sealed")

        spread = {r["location"].split("#")[0] for r in
                  m.db.execute("SELECT location FROM chunk_location")}
        say("spread", "chunks live in " + str(len(spread)) + " repos")
        if len(spread) < 2:
            failures.append("chunks did not spread across the pool")

        # second backup: nothing changed, nothing should upload
        again = eng.backup(src, device="live-test")
        say("rerun", "%d unchanged files, %d bytes uploaded"
            % (again.unchanged_files, again.bytes_uploaded))
        if again.bytes_uploaded:
            failures.append("unchanged re-backup uploaded bytes")

        # change one byte in one file
        victim = src / "photos" / "img00.jpg"
        data = bytearray(victim.read_bytes())
        data[500] ^= 0xFF
        victim.write_bytes(bytes(data))
        os.utime(victim, (0, 0))
        inc = eng.backup(src, device="live-test")
        say("incr", "%d of %d chunks re-uploaded (%d bytes)"
            % (inc.chunks_new, inc.chunks_seen, inc.bytes_uploaded))
        if inc.chunks_new >= res.chunks_new:
            failures.append("incremental re-uploaded everything")

        api.reset()
        t0 = time.time()
        problems = eng.verify(deep=True)
        secs = time.time() - t0
        say("verify", "deep verify in %.1fs: %s"
            % (secs, ", ".join(problems) if problems
               else "every chunk readable"))
        say("down", "%d downloads, %s, %s, peak %d in flight (of %d workers)"
            % (api.dn_calls, human(api.dn_bytes), rate(api.dn_bytes, secs),
               api.dn_peak, args.workers))
        failures.extend(problems)

        # fresh manifest handle, as a new process would have
        eng.close()
        m.close()
        m2 = Manifest(work / "manifest.db")
        k2 = KeyRing.load(work / "key.json")
        eng2 = Engine(m2, k2, chunk_params=CHUNKS, workers=args.workers)
        api.reset()
        t0 = time.time()
        r = eng2.restore(out)
        secs = time.time() - t0
        say("restore", "%d files, %s, %.1fs, %s"
            % (len(r.restored), human(r.bytes_written), secs,
               rate(r.bytes_written, secs)))
        say("down", "%d downloads, peak %d in flight (of %d workers)"
            % (api.dn_calls, api.dn_peak, args.workers))
        for f in r.failed:
            say("ERROR", f.path + ": " + f.reason)
            failures.append(f.path)
        if not dirs_equal(src, out):
            failures.append("restored tree is not byte-identical")
        else:
            say("compare", "restored tree is byte-identical to the source")

        drv = GitHubDriver({"owner": owner, "prefix": PREFIX},
                           ManifestState(m2, bid), api=api)
        st = drv.stats()
        say("stats", str(st.bytes_stored) + " bytes, "
            + str(st.chunk_count) + " chunks across "
            + str(len(st.detail["repos"])) + " repos")
    finally:
        if args.keep:
            say("kept", str(work))
        else:
            shutil.rmtree(work, ignore_errors=True)

    print()
    if failures:
        print("FAILED:")
        for f in failures:
            print("  - " + f)
        print("\nrepos are still there; remove them with --cleanup")
        return 1
    print("PASSED -- live GitHub backup, rotation, incremental and restore")
    print("remove the test repos with:  python tools/live_github_test.py "
          "--cleanup")
    return 0


if __name__ == "__main__":
    sys.exit(main())
