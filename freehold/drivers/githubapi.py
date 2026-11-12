"""A small GitHub REST + Git LFS client, using only the standard library.

Split out from the driver so the driver's pool logic can be tested against a
fake with the same surface, without touching the network.
"""
from __future__ import annotations

import base64
import json
import random
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from ..core.errors import BackendUnavailable, ChunkNotFound, ConfigError

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
LFS_MEDIA = "application/vnd.git-lfs+json"
USER_AGENT = "freehold/0.1"
RETRY_STATUS = (500, 502, 503, 504)


class GitHubAPI:
    def __init__(self, token: str, timeout: int = 60, max_retries: int = 4):
        if not token:
            raise ConfigError("github driver needs a token")
        self.token = token
        self.timeout = timeout
        self.max_retries = max_retries

    # -- transport --------------------------------------------------------
    def _raw(self, method: str, url: str, *, data: Optional[bytes] = None,
             headers: Optional[Dict[str, str]] = None,
             auth: str = "bearer") -> tuple:
        h = {"User-Agent": USER_AGENT,
             "X-GitHub-Api-Version": "2022-11-28"}
        if auth == "bearer":
            h["Authorization"] = "Bearer " + self.token
        elif auth == "basic":
            cred = base64.b64encode(("x-access-token:" + self.token).encode())
            h["Authorization"] = "Basic " + cred.decode()
        h.update(headers or {})
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        attempt = 0
        while True:
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return r.status, r.read(), dict(r.headers)
            except urllib.error.HTTPError as exc:
                body = exc.read()
                hdrs = dict(exc.headers or {})
                if exc.code in (403, 429) and _is_rate_limited(exc.code, hdrs):
                    raise BackendUnavailable(
                        "github rate limited; retry after "
                        + _retry_hint(hdrs)) from None
                if exc.code in RETRY_STATUS and attempt < self.max_retries:
                    attempt += 1
                    time.sleep(min(30, 2 ** attempt) + random.random())
                    continue
                return exc.code, body, hdrs
            except urllib.error.URLError as exc:
                if attempt < self.max_retries:
                    attempt += 1
                    time.sleep(min(30, 2 ** attempt) + random.random())
                    continue
                raise BackendUnavailable("github unreachable: "
                                         + str(exc.reason)) from None

    def _json(self, method: str, path: str, body: Any = None,
              accept: str = "application/vnd.github+json") -> Any:
        url = path if path.startswith("http") else API + path
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": accept}
        if data:
            headers["Content-Type"] = "application/json"
        status, payload, _ = self._raw(method, url, data=data, headers=headers)
        if status == 404:
            return None
        if status >= 400:
            raise _api_error(status, payload)
        return json.loads(payload) if payload else {}

    # -- identity ---------------------------------------------------------
    def whoami(self) -> str:
        me = self._json("GET", "/user")
        if not me:
            raise BackendUnavailable("github token rejected")
        return me["login"]

    # -- repos ------------------------------------------------------------
    def get_repo(self, owner: str, repo: str) -> Optional[dict]:
        return self._json("GET", "/repos/" + owner + "/" + repo)

    def create_repo(self, name: str, private: bool = True,
                    org: Optional[str] = None) -> dict:
        body = {"name": name, "private": private, "auto_init": True,
                "description": "Freehold encrypted blob store"}
        path = "/orgs/" + org + "/repos" if org else "/user/repos"
        return self._json("POST", path, body)

    def list_repos(self, owner: str, org: bool = False) -> List[dict]:
        """Repos this token can see for `owner`, private included.

        Needed to rebuild the pool on a machine that has never seen it: the
        manifest is gone, so the repo list has to come from GitHub itself.
        """
        out: List[dict] = []
        page = 1
        while page < 20:
            if org:
                path = ("/orgs/" + owner + "/repos?type=all&per_page=100"
                        "&page=" + str(page))
            else:
                path = ("/user/repos?affiliation=owner&per_page=100&page="
                        + str(page))
            batch = self._json("GET", path)
            if not batch:
                break
            out.extend(r for r in batch
                       if (r.get("owner") or {}).get("login") == owner
                       or org)
            if len(batch) < 100:
                break
            page += 1
        return out

    def delete_repo(self, owner: str, repo: str) -> bool:
        """Only used by the live test's cleanup. Irreversible."""
        status, payload, _ = self._raw(
            "DELETE", API + "/repos/" + owner + "/" + repo)
        if status == 404:
            return False
        if status >= 400:
            raise _api_error(status, payload)
        return True

    def put_file(self, owner: str, repo: str, path: str, content: bytes,
                 message: str, sha: Optional[str] = None) -> dict:
        body = {"message": message,
                "content": base64.b64encode(content).decode()}
        if sha:
            body["sha"] = sha
        return self._json("PUT", "/repos/" + owner + "/" + repo
                          + "/contents/" + path, body)

    def get_file(self, owner: str, repo: str, path: str) -> Optional[bytes]:
        url = API + "/repos/" + owner + "/" + repo + "/contents/" + path
        status, payload, _ = self._raw(
            "GET", url, headers={"Accept": "application/vnd.github.raw"})
        if status == 404:
            return None
        if status >= 400:
            raise _api_error(status, payload)
        return payload

    # -- releases ---------------------------------------------------------
    def ensure_release(self, owner: str, repo: str, tag: str) -> int:
        rel = self._json("GET", "/repos/" + owner + "/" + repo
                         + "/releases/tags/" + tag)
        if rel:
            return int(rel["id"])
        rel = self._json("POST", "/repos/" + owner + "/" + repo + "/releases",
                         {"tag_name": tag, "name": tag,
                          "body": "Freehold encrypted chunks. Do not edit.",
                          "draft": False, "prerelease": False})
        return int(rel["id"])

    def list_assets(self, owner: str, repo: str, release_id: int) -> List[dict]:
        out: List[dict] = []
        page = 1
        while True:
            batch = self._json(
                "GET", "/repos/" + owner + "/" + repo + "/releases/"
                + str(release_id) + "/assets?per_page=100&page=" + str(page))
            if not batch:
                return out
            out.extend(batch)
            if len(batch) < 100:
                return out
            page += 1

    def upload_asset(self, owner: str, repo: str, release_id: int, name: str,
                     data: bytes) -> dict:
        url = (UPLOADS + "/repos/" + owner + "/" + repo + "/releases/"
               + str(release_id) + "/assets?name=" + name)
        status, payload, _ = self._raw(
            "POST", url, data=data,
            headers={"Content-Type": "application/octet-stream",
                     "Accept": "application/vnd.github+json"})
        if status == 422:
            # asset with that name already exists: idempotent success
            for a in self.list_assets(owner, repo, release_id):
                if a["name"] == name:
                    return a
        if status >= 400:
            raise _api_error(status, payload)
        return json.loads(payload)

    def download_asset(self, owner: str, repo: str, asset_id: int) -> bytes:
        url = (API + "/repos/" + owner + "/" + repo + "/releases/assets/"
               + str(asset_id))
        status, payload, _ = self._raw(
            "GET", url, headers={"Accept": "application/octet-stream"})
        if status == 404:
            raise ChunkNotFound("asset " + str(asset_id))
        if status >= 400:
            raise _api_error(status, payload)
        return payload

    def delete_asset(self, owner: str, repo: str, asset_id: int) -> bool:
        url = (API + "/repos/" + owner + "/" + repo + "/releases/assets/"
               + str(asset_id))
        status, payload, _ = self._raw("DELETE", url)
        if status == 404:
            return False
        if status >= 400:
            raise _api_error(status, payload)
        return True

    # -- git lfs ----------------------------------------------------------
    def _lfs_batch(self, owner: str, repo: str, operation: str, oid: str,
                   size: int) -> dict:
        url = ("https://github.com/" + owner + "/" + repo
               + ".git/info/lfs/objects/batch")
        body = json.dumps({"operation": operation, "transfers": ["basic"],
                           "objects": [{"oid": oid, "size": size}]}).encode()
        status, payload, _ = self._raw(
            "POST", url, data=body, auth="basic",
            headers={"Accept": LFS_MEDIA, "Content-Type": LFS_MEDIA})
        if status >= 400:
            raise _api_error(status, payload)
        objects = json.loads(payload).get("objects") or []
        if not objects:
            raise BackendUnavailable("lfs batch returned no objects")
        obj = objects[0]
        if "error" in obj:
            raise _api_error(obj["error"].get("code", 500),
                             json.dumps(obj["error"]).encode())
        return obj

    def lfs_upload(self, owner: str, repo: str, oid: str, data: bytes) -> None:
        obj = self._lfs_batch(owner, repo, "upload", oid, len(data))
        action = (obj.get("actions") or {}).get("upload")
        if not action:
            return  # object already present
        status, payload, _ = self._raw(
            "PUT", action["href"], data=data,
            headers=dict(action.get("header") or {}), auth="none")
        if status >= 400:
            raise _api_error(status, payload)
        verify = (obj.get("actions") or {}).get("verify")
        if verify:
            self._raw("POST", verify["href"],
                      data=json.dumps({"oid": oid, "size": len(data)}).encode(),
                      headers=dict(verify.get("header") or {}), auth="basic")

    def lfs_download(self, owner: str, repo: str, oid: str,
                     size: int) -> bytes:
        obj = self._lfs_batch(owner, repo, "download", oid, size)
        action = (obj.get("actions") or {}).get("download")
        if not action:
            raise ChunkNotFound("lfs object " + oid)
        status, payload, _ = self._raw(
            "GET", action["href"], headers=dict(action.get("header") or {}),
            auth="none")
        if status == 404:
            raise ChunkNotFound("lfs object " + oid)
        if status >= 400:
            raise _api_error(status, payload)
        return payload


def _is_rate_limited(code: int, headers: Dict[str, str]) -> bool:
    if code == 429:
        return True
    lower = {k.lower(): v for k, v in headers.items()}
    return (lower.get("x-ratelimit-remaining") == "0"
            or "retry-after" in lower)


def _retry_hint(headers: Dict[str, str]) -> str:
    lower = {k.lower(): v for k, v in headers.items()}
    if "retry-after" in lower:
        return lower["retry-after"] + "s"
    reset = lower.get("x-ratelimit-reset")
    if reset:
        try:
            return str(max(0, int(reset) - int(time.time()))) + "s"
        except ValueError:
            pass
    return "unknown"


def _api_error(status: int, payload: bytes) -> Exception:
    try:
        msg = json.loads(payload).get("message", "")
    except Exception:
        msg = payload[:200].decode("utf-8", "replace")
    text = "github api " + str(status) + ": " + str(msg)
    if status in (401, 403):
        return BackendUnavailable(text)
    if status == 404:
        return ChunkNotFound(text)
    if status in RETRY_STATUS:
        return BackendUnavailable(text)
    return BackendUnavailable(text)
