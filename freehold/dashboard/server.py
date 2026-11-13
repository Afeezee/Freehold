"""The dashboard's HTTP layer: stdlib only, bound to localhost.

The manifest holds the map of everything you own, so this is not a page to
expose. It listens on 127.0.0.1, requires a token minted at startup, and
refuses requests whose Origin is not its own — a local page in your browser
should not be able to drive it.
"""
from __future__ import annotations

import json
import mimetypes
import secrets
import sys
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from ..core.errors import FreeholdError
from .api import DashboardAPI

def _static_dir() -> Path:
    """Where the page lives. Inside a PyInstaller bundle that is the
    unpacked temp directory, not next to this source file."""
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        packed = Path(bundled) / "freehold" / "dashboard" / "static"
        if packed.is_dir():
            return packed
    return Path(__file__).parent / "static"


STATIC = _static_dir()
MAX_BODY = 1 << 20


class Handler(BaseHTTPRequestHandler):
    api: DashboardAPI = None  # type: ignore
    token: str = ""
    server_version = "freehold"
    sys_version = ""

    def log_message(self, *a):
        pass

    # -- plumbing ---------------------------------------------------------
    def _json(self, code: int, payload: Any) -> None:
        body = json.dumps(payload, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self) -> bool:
        if not self.token:
            return True
        supplied = (self.headers.get("X-Freehold-Token")
                    or parse_qs(urlparse(self.path).query).get(
                        "token", [""])[0])
        return secrets.compare_digest(str(supplied), self.token)

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True                      # same-origin fetch or curl
        host = self.headers.get("Host", "")
        return origin in ("http://" + host, "https://" + host)

    def _body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise FreeholdError("request body too large")
        raw = self.rfile.read(length)
        try:
            doc = json.loads(raw)
        except ValueError:
            raise FreeholdError("body is not valid JSON") from None
        if not isinstance(doc, dict):
            raise FreeholdError("body must be a JSON object")
        return doc

    def _query(self) -> Dict[str, str]:
        return {k: v[0] for k, v in
                parse_qs(urlparse(self.path).query).items()}

    # -- routing ----------------------------------------------------------
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            if not self._authorised():
                return self._json(403, {"error": "bad or missing token"})
            try:
                return self._json(200, self._get(path, self._query()))
            except FreeholdError as exc:
                return self._json(400, {"error": str(exc)})
            except Exception as exc:
                return self._json(500, {"error": str(exc)})
        return self._static(path)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            return self._json(404, {"error": "not found"})
        if not self._authorised():
            return self._json(403, {"error": "bad or missing token"})
        if not self._origin_ok():
            return self._json(403, {"error": "cross-origin request refused"})
        try:
            code, payload = self._post(path, self._body())
            return self._json(code, payload)
        except FreeholdError as exc:
            return self._json(400, {"error": str(exc)})
        except Exception as exc:
            return self._json(500, {"error": str(exc)})

    def _get(self, path: str, q: Dict[str, str]) -> Any:
        api = self.api
        if path == "/api/overview":
            return api.overview()
        if path == "/api/backends":
            return {"backends": api.backends(live=q.get("live") == "1"),
                    "drivers": api.drivers()}
        if path == "/api/snapshots":
            return {"snapshots": api.snapshots(q.get("device") or None,
                                               q.get("root") or None)}
        if path == "/api/tree":
            return api.tree(int(q.get("snapshot", "0")), q.get("prefix", ""))
        if path == "/api/credentials":
            return {"credentials": api.credentials()}
        if path == "/api/about":
            # AGPL section 13: anyone interacting with this over a network
            # is entitled to the source of the version they are using.
            from .. import __license__, __source__, __version__
            return {"name": "Freehold", "version": __version__,
                    "license": __license__, "source": __source__}
        if path == "/api/health":
            return api.health()
        if path == "/api/settings":
            return api.settings()
        if path == "/api/folders":
            return {"folders": api.watched_folders()}
        if path == "/api/preflight":
            return api.preflight(q.get("path", ""))
        if path == "/api/browse-local":
            return api.local_folders(q.get("path", ""))
        if path == "/api/backups":
            return {"jobs": [asdict(j) for j in api.backup_jobs()]}
        if path == "/api/jobs":
            return {"jobs": [asdict(j) for j in api.jobs()]}
        if path.startswith("/api/jobs/"):
            job = api.job(path.rsplit("/", 1)[1])
            if job is None:
                raise FreeholdError("no such restore job")
            return asdict(job)
        raise FreeholdError("unknown endpoint " + path)

    def _post(self, path: str, body: Dict[str, Any]) -> Tuple[int, Any]:
        api = self.api
        if path == "/api/backends":
            return 201, api.add_backend(str(body.get("name", "")),
                                        str(body.get("driver", "")),
                                        dict(body.get("options") or {}))
        if path == "/api/backends/enable":
            api.set_backend_enabled(str(body["name"]), bool(body["enabled"]))
            return 200, {"ok": True}
        if path == "/api/backends/remove":
            api.remove_backend(str(body["name"]))
            return 200, {"ok": True}
        if path == "/api/backends/check":
            return 200, api.check_backend(str(body["name"]))
        if path == "/api/credentials":
            return 200, api.set_credential(str(body.get("name", "")),
                                           str(body.get("value", "")))
        if path == "/api/credentials/remove":
            return 200, api.remove_credential(str(body["name"]))
        if path == "/api/settings":
            return 200, api.set_setting(str(body["key"]),
                                        str(body.get("value", "")))
        if path == "/api/backup":
            job = api.start_backup(str(body.get("path", "")),
                                   int(body.get("redundancy", 1)),
                                   bool(body.get("force")))
            return 202, asdict(job)
        if path == "/api/folders/forget":
            return 200, api.forget_folder(str(body["path"]))
        if path == "/api/restore":
            job = api.start_restore(int(body["snapshot"]),
                                    str(body.get("dest", "")),
                                    list(body.get("paths") or []),
                                    bool(body.get("overwrite")))
            return 202, asdict(job)
        raise FreeholdError("unknown endpoint " + path)

    # -- static -----------------------------------------------------------
    def _static(self, path: str) -> None:
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (STATIC / name).resolve()
        try:
            target.relative_to(STATIC.resolve())
        except ValueError:
            return self._json(403, {"error": "no"})
        if not target.is_file():
            return self._json(404, {"error": "not found"})
        body = target.read_bytes()
        ctype = mimetypes.guess_type(str(target))[0] or "text/plain"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


class _Server(ThreadingHTTPServer):
    # ThreadingHTTPServer sets this, and on Windows it means a second
    # process can bind a port that is already in use: both then "work" and
    # requests go to whichever the OS picks. For a local app that is a trap -
    # you end up talking to a stale copy without being told. Fail instead.
    allow_reuse_address = False
    daemon_threads = True


class Dashboard:
    def __init__(self, api: DashboardAPI, host: str = "127.0.0.1",
                 port: int = 8731, token: Optional[str] = None):
        self.token = secrets.token_urlsafe(16) if token is None else token
        handler = type("Bound", (Handler,),
                       {"api": api, "token": self.token})
        self.httpd = _Server((host, port), handler)
        self.httpd.daemon_threads = True
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        base = "http://" + str(host) + ":" + str(port) + "/"
        return base + ("?token=" + self.token if self.token else "")

    def start(self) -> None:
        self._thread = threading.Thread(target=self.httpd.serve_forever,
                                        daemon=True)
        self._thread.start()

    def serve_forever(self) -> None:
        self.httpd.serve_forever()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
