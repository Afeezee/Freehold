"""An in-memory stand-in for the GitHub REST + LFS surface.

Same method signatures as freehold.drivers.githubapi.GitHubAPI, so the driver
under test is the real one -- only the transport is faked.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from freehold.core.errors import BackendUnavailable, ChunkNotFound

_STORES: Dict[str, "FakeGitHub"] = {}


class FakeGitHub:
    """Shared by name so a driver rebuilt from the manifest sees the same
    server, the way a real account would persist between runs."""

    @classmethod
    def shared(cls, name: str) -> "FakeGitHub":
        return _STORES.setdefault(name, cls())

    @classmethod
    def reset(cls) -> None:
        _STORES.clear()

    def __init__(self) -> None:
        self.repos: Dict[str, dict] = {}
        self.releases: Dict[int, dict] = {}
        self.assets: Dict[int, dict] = {}
        self.files: Dict[str, bytes] = {}
        self.lfs: Dict[str, bytes] = {}
        self.offline = False
        self.lfs_offline = False
        self.calls: List[str] = []
        self._next_id = 1

    # -- test controls ----------------------------------------------------
    def _id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _check(self, what: str) -> None:
        self.calls.append(what)
        if self.offline:
            raise BackendUnavailable("github rate limited; retry after 3600s")

    # -- identity / repos -------------------------------------------------
    def whoami(self) -> str:
        self._check("whoami")
        return "tester"

    def get_repo(self, owner: str, repo: str) -> Optional[dict]:
        self._check("get_repo")
        return self.repos.get(repo)

    def list_repos(self, owner: str, org: bool = False) -> List[dict]:
        self._check("list_repos")
        return [{"name": n, "owner": {"login": owner}}
                for n in sorted(self.repos)]

    def create_repo(self, name: str, private: bool = True,
                    org: Optional[str] = None) -> dict:
        self._check("create_repo")
        if name in self.repos:
            raise BackendUnavailable("github api 422: name already exists")
        self.repos[name] = {"name": name, "private": private}
        return self.repos[name]

    def put_file(self, owner: str, repo: str, path: str, content: bytes,
                 message: str, sha: Optional[str] = None) -> dict:
        self._check("put_file")
        self.files[repo + "/" + path] = content
        return {"content": {"sha": "deadbeef"}}

    def get_file(self, owner: str, repo: str, path: str) -> Optional[bytes]:
        self._check("get_file")
        return self.files.get(repo + "/" + path)

    # -- releases ---------------------------------------------------------
    def ensure_release(self, owner: str, repo: str, tag: str) -> int:
        self._check("ensure_release")
        for rid, rel in self.releases.items():
            if rel["repo"] == repo and rel["tag"] == tag:
                return rid
        rid = self._id()
        self.releases[rid] = {"repo": repo, "tag": tag}
        return rid

    def list_assets(self, owner: str, repo: str, release_id: int) -> List[dict]:
        self._check("list_assets")
        return [a for a in self.assets.values()
                if a["release_id"] == release_id]

    def upload_asset(self, owner: str, repo: str, release_id: int, name: str,
                     data: bytes) -> dict:
        self._check("upload_asset")
        for a in self.assets.values():
            if a["release_id"] == release_id and a["name"] == name:
                return a  # 422 already_exists -> idempotent
        aid = self._id()
        self.assets[aid] = {"id": aid, "name": name, "size": len(data),
                            "release_id": release_id, "repo": repo,
                            "data": data}
        return self.assets[aid]

    def download_asset(self, owner: str, repo: str, asset_id: int) -> bytes:
        self._check("download_asset")
        a = self.assets.get(asset_id)
        if a is None:
            raise ChunkNotFound("asset " + str(asset_id))
        return a["data"]

    def delete_asset(self, owner: str, repo: str, asset_id: int) -> bool:
        self._check("delete_asset")
        return self.assets.pop(asset_id, None) is not None

    # -- lfs --------------------------------------------------------------
    def lfs_upload(self, owner: str, repo: str, oid: str, data: bytes) -> None:
        self._check("lfs_upload")
        if self.lfs_offline:
            raise BackendUnavailable("lfs endpoint down")
        self.lfs[repo + "/" + oid] = data

    def lfs_download(self, owner: str, repo: str, oid: str,
                     size: int) -> bytes:
        self._check("lfs_download")
        if self.lfs_offline:
            raise BackendUnavailable("lfs endpoint down")
        try:
            return self.lfs[repo + "/" + oid]
        except KeyError:
            raise ChunkNotFound(oid) from None
