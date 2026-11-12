"""GitHub as a blob store.

Primary path: release assets, which are not subject to repo-size guidance and
take files up to the plan's per-asset cap (2 GB on Free/Pro). Fallback path:
Git LFS, for blobs under a threshold, metered against the account-wide free
quota so it stops rather than failing silently.

A pool of private repos sits underneath, with a soft cap per repo. When the
active repo crosses the cap it is sealed and a new one is provisioned. All of
that is internal: the core sees one backend, and every location string names
the repo it lives in, so a restore never has to search across repos.

Raw chunks are never committed into normal git history -- only release assets,
or LFS pointer files.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ..core.driver import BackendStats, StorageDriver, register
from ..core.errors import (BackendUnavailable, ChunkNotFound, ConfigError,
                           QuotaExceeded)
from .githubapi import GitHubAPI

GIB = 1024 ** 3
DEFAULT_SOFT_CAP = 1_500_000_000      # ~1.5 GB of blobs per repo
RELEASE_ASSET_CAP = 2 * GIB           # GitHub per-asset limit on Free/Pro
DEFAULT_LFS_QUOTA = 10 * GIB          # free LFS storage, account-wide
DEFAULT_TAG = "freehold-blobs"
POOL_KEY = "pool"
LOCATION_RE = re.compile(r"^(?P<owner>[^/]+)/(?P<repo>[^#]+)"
                         r"#(?P<kind>[^/]+)/(?P<name>.+)$")


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


@register
class GitHubDriver(StorageDriver):
    driver_id = "github"
    config_help = ("owner=<user-or-org> [org=1] [token_env=FREEHOLD_GITHUB_TOKEN] "
                   "[prefix=freehold-blobs] [soft_cap=1500000000] "
                   "[lfs=1 lfs_max=1048576 lfs_quota=10737418240]")

    def __init__(self, config: Dict[str, Any], state=None, api=None):
        super().__init__(config, state)
        self.owner = config.get("owner")
        if not self.owner:
            raise ConfigError("github driver needs owner=<user-or-org>")
        self.is_org = _as_bool(config.get("org"))
        self.prefix = config.get("prefix") or "freehold-blobs"
        self.tag = config.get("tag") or DEFAULT_TAG
        self.soft_cap = _as_int(config.get("soft_cap"), DEFAULT_SOFT_CAP)
        self.private = _as_bool(config.get("private"), True)
        self.lfs_enabled = _as_bool(config.get("lfs"), False)
        self.lfs_max = _as_int(config.get("lfs_max"), 1024 * 1024)
        self.lfs_quota = _as_int(config.get("lfs_quota"), DEFAULT_LFS_QUOTA)
        self.lfs_warn_at = float(config.get("lfs_warn_at") or 0.8)
        self.warnings: List[str] = []
        self._asset_cache: Dict[str, Dict[str, int]] = {}
        # Guards pool bookkeeping and the asset-id cache. Uploads and
        # downloads happen outside it, so concurrency survives.
        self._lock = threading.RLock()
        self.api = api if api is not None else GitHubAPI(self._token())

    def _token(self) -> str:
        token = self.config.get("token")
        if token:
            return token
        env = self.config.get("token_env") or "FREEHOLD_GITHUB_TOKEN"
        token = os.environ.get(env)
        if not token:
            raise ConfigError(
                "no GitHub token: set " + env + ", or pass token=<pat> "
                "(a PAT with 'repo' scope; token_env keeps it out of the "
                "manifest)")
        return token

    # -- pool state -------------------------------------------------------
    def _pool(self) -> Dict[str, Any]:
        return self.state.get(POOL_KEY, {"repos": [], "lfs_bytes": 0})

    def _save(self, pool: Dict[str, Any]) -> None:
        self.state.set(POOL_KEY, pool)

    def _provision(self, pool: Dict[str, Any]) -> Dict[str, Any]:
        n = len(pool["repos"]) + 1
        while True:
            name = "%s-%03d" % (self.prefix, n)
            if not any(r["name"] == name for r in pool["repos"]):
                if self.api.get_repo(self.owner, name) is None:
                    break
            n += 1
        self.api.create_repo(name, private=self.private,
                             org=self.owner if self.is_org else None)
        if self.lfs_enabled:
            try:
                self.api.put_file(
                    self.owner, name, ".gitattributes",
                    b"lfs/** filter=lfs diff=lfs merge=lfs -text\n",
                    "freehold: route lfs/ through git-lfs")
            except Exception:
                pass  # repo still usable for releases
        release_id = self.api.ensure_release(self.owner, name, self.tag)
        repo = {"name": name, "release_id": release_id, "tag": self.tag,
                "bytes": 0, "chunks": 0, "status": "active",
                "created_at": int(time.time())}
        pool["repos"].append(repo)
        self._save(pool)
        return repo

    def _active(self, pool: Dict[str, Any], need: int) -> Dict[str, Any]:
        """Pick the repo to write to and *reserve* `need` bytes against it
        before the upload starts. Reserving up front is what keeps the soft cap
        honest when several uploads are in flight: crediting afterwards let the
        active repo overshoot by up to workers x chunk_size. Caller holds the
        lock; a failed upload refunds."""
        active = [r for r in pool["repos"] if r["status"] == "active"]
        repo = active[0] if active else None
        if repo is None:
            repo = self._provision(pool)
        elif _as_int(repo.get("bytes"), 0) + need > self.soft_cap:
            repo["status"] = "sealed"
            repo["sealed_at"] = int(time.time())
            self._save(pool)
            self.warnings.append("sealed " + repo["name"] + " at "
                                 + str(repo["bytes"]) + " bytes")
            repo = self._provision(pool)
        repo["bytes"] = _as_int(repo.get("bytes"), 0) + need
        self._save(pool)
        return repo

    def _ensure_pool(self, pool: Dict[str, Any]) -> Dict[str, Any]:
        """Rebuild the repo list from GitHub when local state is empty.

        A replacement machine has the key and the token and nothing else, so
        the pool cannot come from the manifest -- the manifest is the thing
        being recovered. GitHub knows which repos exist; this asks it.
        """
        if pool.get("repos"):
            return pool
        found = []
        for repo in self.api.list_repos(self.owner, self.is_org):
            name = repo.get("name", "")
            if not name.startswith(self.prefix + "-"):
                continue
            try:
                release_id = self.api.ensure_release(self.owner, name,
                                                     self.tag)
            except Exception:
                continue
            found.append({"name": name, "release_id": release_id,
                          "tag": self.tag, "bytes": 0, "chunks": 0,
                          "status": "sealed", "recovered": True})
        if found:
            found.sort(key=lambda r: r["name"])
            found[-1]["status"] = "active"      # newest takes new writes
            pool["repos"] = found
            self._save(pool)
            self.warnings.append("rebuilt the repo pool from GitHub: "
                                 + ", ".join(r["name"] for r in found))
        return pool

    def find(self, blob_id: str) -> Optional[str]:
        """Locate a blob without a manifest, for recovery."""
        with self._lock:
            pool = self._ensure_pool(self._pool())
            repos = list(pool.get("repos") or [])
        for repo in repos:
            assets = self._assets(repo["name"], int(repo["release_id"]),
                                  refresh=True)
            if blob_id in assets:
                return self._loc(repo["name"], repo.get("tag", self.tag),
                                 blob_id)
        if self.lfs_enabled:
            for repo in repos:
                try:
                    if self.api.get_file(self.owner, repo["name"],
                                         "lfs/" + blob_id) is not None:
                        return self._loc(repo["name"], "lfs", blob_id)
                except Exception:
                    continue
        return None

    def _repo_entry(self, pool: Dict[str, Any],
                    name: str) -> Optional[Dict[str, Any]]:
        for r in pool["repos"]:
            if r["name"] == name:
                return r
        return None

    # -- locations --------------------------------------------------------
    def _loc(self, repo: str, kind: str, name: str) -> str:
        return self.owner + "/" + repo + "#" + kind + "/" + name

    def _parse(self, location: str) -> Tuple[str, str, str, str]:
        m = LOCATION_RE.match(location)
        if not m:
            raise ChunkNotFound("unparseable github location: " + location)
        return (m.group("owner"), m.group("repo"), m.group("kind"),
                m.group("name"))

    def _assets(self, repo: str, release_id: int,
                refresh: bool = False) -> Dict[str, int]:
        with self._lock:
            if refresh or repo not in self._asset_cache:
                self._asset_cache[repo] = {
                    a["name"]: int(a["id"])
                    for a in self.api.list_assets(self.owner, repo,
                                                  release_id)}
            return dict(self._asset_cache[repo])

    def _release_id(self, pool: Dict[str, Any], repo: str) -> int:
        entry = self._repo_entry(pool, repo)
        if entry:
            return int(entry["release_id"])
        return self.api.ensure_release(self.owner, repo, self.tag)

    # -- StorageDriver ----------------------------------------------------
    def put(self, blob_id: str, data: bytes) -> str:
        size = len(data)
        if size > RELEASE_ASSET_CAP:
            raise ConfigError("blob of " + str(size) + " bytes exceeds the "
                              "GitHub per-asset cap; lower the chunk size")
        if self.lfs_enabled and size <= self.lfs_max:
            loc = self._put_lfs(blob_id, data)
            if loc:
                return loc
        with self._lock:
            repo = self._active(self._pool(), size)
            name, release_id = repo["name"], int(repo["release_id"])
        try:
            asset = self.api.upload_asset(self.owner, name, release_id,
                                          blob_id, data)
        except BaseException:
            with self._lock:
                self._credit(name, -size, 0)   # give the reservation back
            raise
        with self._lock:
            self._asset_cache.setdefault(name, {})[blob_id] = int(asset["id"])
            self._credit(name, 0, 1)
        return self._loc(name, self.tag, blob_id)

    def _credit(self, repo_name: str, size: int, chunks: int) -> None:
        """Re-read, adjust and save, so concurrent uploads do not clobber each
        other's counters. Caller holds the lock."""
        pool = self._pool()
        entry = self._repo_entry(pool, repo_name)
        if entry is None:
            return
        entry["bytes"] = max(0, _as_int(entry.get("bytes"), 0) + size)
        entry["chunks"] = _as_int(entry.get("chunks"), 0) + chunks
        self._save(pool)

    def _put_lfs(self, blob_id: str, data: bytes) -> Optional[str]:
        size = len(data)
        with self._lock:
            pool = self._pool()
            used = _as_int(pool.get("lfs_bytes"), 0)
            if used + size > self.lfs_quota:
                self.warnings.append(
                    "git-lfs quota reached (" + str(used) + "/"
                    + str(self.lfs_quota)
                    + " bytes); falling back to releases")
                return None
            if used + size > self.lfs_quota * self.lfs_warn_at:
                self.warnings.append("git-lfs at "
                                     + str(int(100 * used / self.lfs_quota))
                                     + "% of its free quota")
            repo = self._active(pool, 0)
            name = repo["name"]
            # Reserve the quota before the upload, so parallel callers see it.
            pool["lfs_bytes"] = used + size
            self._save(pool)
        oid = hashlib.sha256(data).hexdigest()
        try:
            self.api.lfs_upload(self.owner, name, oid, data)
            pointer = ("version https://git-lfs.github.com/spec/v1\noid "
                       "sha256:" + oid + "\nsize " + str(size) + "\n")
            self.api.put_file(self.owner, name, "lfs/" + blob_id,
                              pointer.encode(),
                              "freehold: add " + blob_id[:12])
        except QuotaExceeded:
            self._refund_lfs(size)
            raise
        except BackendUnavailable as exc:
            self._refund_lfs(size)
            self.warnings.append("git-lfs unavailable (" + str(exc)
                                 + "); falling back to releases")
            return None
        with self._lock:
            self._credit(name, 0, 1)
        return self._loc(name, "lfs", blob_id)

    def _refund_lfs(self, size: int) -> None:
        with self._lock:
            pool = self._pool()
            pool["lfs_bytes"] = max(0, _as_int(pool.get("lfs_bytes"), 0)
                                    - size)
            self._save(pool)

    def get(self, location: str) -> bytes:
        _owner, repo, kind, name = self._parse(location)
        if kind == "lfs":
            pointer = self.api.get_file(self.owner, repo, "lfs/" + name)
            if pointer is None:
                raise ChunkNotFound(location)
            oid, size = _parse_pointer(pointer)
            return self.api.lfs_download(self.owner, repo, oid, size)
        pool = self._pool()
        release_id = self._release_id(pool, repo)
        assets = self._assets(repo, release_id)
        asset_id = assets.get(name)
        if asset_id is None:
            assets = self._assets(repo, release_id, refresh=True)
            asset_id = assets.get(name)
        if asset_id is None:
            raise ChunkNotFound(location)
        return self.api.download_asset(self.owner, repo, asset_id)

    def list(self) -> Iterator[str]:
        with self._lock:
            pool = self._ensure_pool(self._pool())
        for repo in pool["repos"]:
            for name in self._assets(repo["name"], int(repo["release_id"]),
                                     refresh=True):
                yield self._loc(repo["name"], repo.get("tag", self.tag), name)

    def delete(self, location: str) -> bool:
        _owner, repo, kind, name = self._parse(location)
        pool = self._pool()
        if kind == "lfs":
            # The pointer goes; the LFS object itself is reclaimed by GitHub.
            return False
        release_id = self._release_id(pool, repo)
        asset_id = self._assets(repo, release_id).get(name)
        if asset_id is None:
            return False
        ok = self.api.delete_asset(self.owner, repo, asset_id)
        self._asset_cache.get(repo, {}).pop(name, None)
        return ok

    # -- reporting --------------------------------------------------------
    def stats(self) -> BackendStats:
        pool = self._pool()
        total = sum(_as_int(r.get("bytes"), 0) for r in pool["repos"])
        chunks = sum(_as_int(r.get("chunks"), 0) for r in pool["repos"])
        return BackendStats(
            bytes_stored=total, chunk_count=chunks, quota_bytes=None,
            detail={"owner": self.owner,
                    "soft_cap": self.soft_cap,
                    "lfs_bytes": _as_int(pool.get("lfs_bytes"), 0),
                    "lfs_quota": self.lfs_quota if self.lfs_enabled else None,
                    "repos": [{"name": r["name"], "status": r["status"],
                               "bytes": r.get("bytes", 0),
                               "chunks": r.get("chunks", 0)}
                              for r in pool["repos"]],
                    "warnings": list(self.warnings)})

    def drain_warnings(self) -> List[str]:
        with self._lock:
            out, self.warnings = self.warnings, []
        return out

    def health(self) -> None:
        self.api.whoami()


def _parse_pointer(pointer: bytes) -> Tuple[str, int]:
    oid, size = "", 0
    for line in pointer.decode("utf-8", "replace").splitlines():
        if line.startswith("oid sha256:"):
            oid = line.split(":", 1)[1].strip()
        elif line.startswith("size "):
            size = _as_int(line.split(" ", 1)[1].strip(), 0)
    if not oid:
        raise ChunkNotFound("malformed lfs pointer")
    return oid, size
