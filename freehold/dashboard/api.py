"""Everything the dashboard can do, as plain functions over the manifest.

Kept separate from the HTTP layer so it is testable without a socket, and so
the UI has no privileged path: the dashboard can do exactly what the CLI can.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.driver import build_driver, known_drivers
from ..core.engine import Engine
from ..core.errors import ConfigError, FreeholdError
from ..core.manifest import Manifest, ManifestState


@dataclass
class RestoreJob:
    id: str
    snapshot_id: int
    dest: str
    paths: List[str]
    overwrite: bool
    state: str = "queued"          # queued | running | done | failed
    started_at: float = 0.0
    finished_at: float = 0.0
    current: str = ""
    restored: int = 0
    bytes_written: int = 0
    failures: List[Dict[str, Any]] = field(default_factory=list)
    renamed: Dict[str, str] = field(default_factory=dict)
    unreachable: List[str] = field(default_factory=list)
    served_by: List[str] = field(default_factory=list)
    error: str = ""


class DashboardAPI:
    def __init__(self, home: Path, manifest: Manifest, keyring,
                 workers: int = 8):
        self.home = Path(home)
        self.m = manifest
        self.k = keyring
        self.workers = workers
        self._jobs: Dict[str, RestoreJob] = {}
        self._lock = threading.Lock()

    # -- read -------------------------------------------------------------
    def overview(self) -> Dict[str, Any]:
        return {"devices": self.devices(), "backends": self.backends(),
                "totals": self.m.totals(), "folders": self.folders(),
                "generated_at": time.time()}

    def devices(self) -> List[Dict[str, Any]]:
        out = []
        for d in self.m.devices():
            last = self.m.latest_snapshot(d["name"])
            roots = sorted({s.root for s in self.m.snapshots(d["name"])})
            out.append({
                "name": d["name"], "platform": d["platform"] or "",
                "last_backup": last.started_at if last else None,
                "last_snapshot": last.id if last else None,
                "files": last.file_count if last else 0,
                "size": last.total_size if last else 0,
                "roots": roots,
            })
        return out

    def folders(self) -> List[Dict[str, Any]]:
        """Folders under backup, as the manifest knows them. The agent's watch
        list is config; this is what has actually been backed up."""
        seen: Dict[tuple, Dict[str, Any]] = {}
        for s in self.m.snapshots():
            key = (s.device, s.root)
            row = seen.get(key)
            if row is None or s.started_at > row["last_backup"]:
                seen[key] = {"device": s.device, "root": s.root,
                             "last_backup": s.started_at,
                             "snapshot": s.id, "files": s.file_count,
                             "size": s.total_size}
        return sorted(seen.values(), key=lambda r: (r["device"], r["root"]))

    def backends(self, live: bool = False) -> List[Dict[str, Any]]:
        """One row per backend. `live` asks each driver for its own view,
        which for GitHub includes the per-repo breakdown."""
        out = []
        for b in self.m.backends():
            row = {"name": b.name, "driver": b.driver_id,
                   "enabled": b.enabled, "chunks": b.chunk_count,
                   "bytes": b.bytes_stored, "quota": None, "detail": {},
                   "error": ""}
            if live:
                try:
                    drv = build_driver(b.driver_id, b.config,
                                       ManifestState(self.m, b.id))
                    st = drv.stats()
                    row.update(quota=st.quota_bytes, detail=st.detail)
                    if st.chunk_count:
                        row["chunks"] = st.chunk_count
                    if st.bytes_stored:
                        row["bytes"] = st.bytes_stored
                except Exception as exc:
                    row["error"] = str(exc)
            out.append(row)
        return out

    # -- credentials ------------------------------------------------------
    #: Secrets a backend might need, so the page can offer the right boxes
    #: without the dashboard knowing anything vendor-specific itself.
    SECRET_HINTS = {
        "github": [("FREEHOLD_GITHUB_TOKEN",
                    "GitHub token (classic, 'repo' scope)")],
        "s3": [("FREEHOLD_S3_ACCESS_KEY_ID", "Access key ID"),
               ("FREEHOLD_S3_SECRET_ACCESS_KEY", "Secret access key")],
    }

    def credentials(self) -> List[Dict[str, str]]:
        """Names and where each comes from. Never the values."""
        from ..core import credstore
        seen = list(credstore.names(self.home))
        for hints in self.SECRET_HINTS.values():
            for name, _label in hints:
                if name not in seen:
                    seen.append(name)
        return [{"name": n, "source": credstore.sources(n, self.home)}
                for n in sorted(seen)]

    def set_credential(self, name: str, value: str) -> Dict[str, Any]:
        from ..core import credstore
        if not name.strip() or not value.strip():
            raise ConfigError("both a name and a value are needed")
        credstore.set_value(name.strip(), value.strip(), self.home)
        return {"name": name.strip(), "source": "credentials.json"}

    def remove_credential(self, name: str) -> Dict[str, Any]:
        from ..core import credstore
        return {"removed": credstore.unset(name, self.home)}

    def drivers(self) -> List[Dict[str, str]]:
        return [{"id": did, "options": cls.config_help,
                 "secrets": [{"name": n, "label": label}
                             for n, label in self.SECRET_HINTS.get(did, [])]}
                for did, cls in sorted(known_drivers().items())]

    def snapshots(self, device: Optional[str] = None,
                  root: Optional[str] = None) -> List[Dict[str, Any]]:
        rows = []
        for s in self.m.snapshots(device):
            if root and s.root != root:
                continue
            rows.append(asdict(s))
        return rows

    def tree(self, snapshot_id: int, prefix: str = "") -> Dict[str, Any]:
        """One level of the snapshot's tree, for the restore browser."""
        snap = self.m.snapshot(snapshot_id)
        if snap is None:
            raise FreeholdError("no snapshot " + str(snapshot_id))
        prefix = prefix.strip("/")
        base = prefix + "/" if prefix else ""
        dirs: Dict[str, Dict[str, int]] = {}
        files: List[Dict[str, Any]] = []
        for f in self.m.files(snapshot_id):
            if base and not f.path.startswith(base):
                continue
            rest = f.path[len(base):]
            if "/" in rest:
                head = rest.split("/", 1)[0]
                d = dirs.setdefault(head, {"files": 0, "size": 0})
                d["files"] += 1
                d["size"] += f.size
            else:
                files.append({"name": rest, "path": f.path, "size": f.size,
                              "mtime": f.mtime_ns // 1_000_000_000,
                              "symlink": f.symlink_target is not None})
        return {
            "snapshot": asdict(snap), "prefix": prefix,
            "dirs": [{"name": n, "path": base + n, **v}
                     for n, v in sorted(dirs.items())],
            "files": sorted(files, key=lambda r: r["name"]),
        }

    # -- write ------------------------------------------------------------
    def add_backend(self, name: str, driver_id: str,
                    options: Dict[str, str]) -> Dict[str, Any]:
        if not name or not name.strip():
            raise ConfigError("a backend needs a name")
        if self.m.backend_by_name(name):
            raise ConfigError("a backend called " + name + " already exists")
        build_driver(driver_id, dict(options))   # validate before storing
        bid = self.m.add_backend(name, driver_id, dict(options))
        return {"id": bid, "name": name, "driver": driver_id}

    def set_backend_enabled(self, name: str, enabled: bool) -> None:
        if not self.m.backend_by_name(name):
            raise ConfigError("no backend called " + name)
        self.m.set_backend_enabled(name, enabled)

    def remove_backend(self, name: str) -> None:
        b = self.m.backend_by_name(name)
        if b is None:
            raise ConfigError("no backend called " + name)
        if b.chunk_count and len(self.m.backends()) == 1:
            raise ConfigError(
                "refusing to remove the only backend holding "
                + str(b.chunk_count) + " chunks; add another first")
        self.m.remove_backend(name)

    def check_backend(self, name: str) -> Dict[str, Any]:
        b = self.m.backend_by_name(name)
        if b is None:
            raise ConfigError("no backend called " + name)
        drv = build_driver(b.driver_id, b.config, ManifestState(self.m, b.id))
        try:
            drv.health()
            return {"name": name, "ok": True, "message": "reachable"}
        except Exception as exc:
            return {"name": name, "ok": False, "message": str(exc)}

    # -- restores ---------------------------------------------------------
    def start_restore(self, snapshot_id: int, dest: str,
                      paths: Optional[List[str]] = None,
                      overwrite: bool = False) -> RestoreJob:
        if self.m.snapshot(snapshot_id) is None:
            raise FreeholdError("no snapshot " + str(snapshot_id))
        if not dest:
            raise ConfigError("a restore needs a destination folder")
        job = RestoreJob(id=uuid.uuid4().hex[:12], snapshot_id=snapshot_id,
                         dest=str(dest), paths=list(paths or []),
                         overwrite=bool(overwrite))
        with self._lock:
            self._jobs[job.id] = job
        threading.Thread(target=self._run_restore, args=(job,),
                         daemon=True).start()
        return job

    def _run_restore(self, job: RestoreJob) -> None:
        job.state = "running"
        job.started_at = time.time()
        eng = Engine(self.m, self.k, workers=self.workers)

        def progress(path, _final):
            job.current = path
            job.restored += 1

        try:
            res = eng.restore(Path(job.dest), snapshot_id=job.snapshot_id,
                              paths=job.paths or None,
                              overwrite=job.overwrite, progress=progress)
            job.restored = len(res.restored)
            job.bytes_written = res.bytes_written
            job.renamed = dict(res.renamed)
            job.unreachable = list(res.unreachable_backends)
            job.served_by = list(res.served_by)
            job.failures = [{"path": f.path, "reason": f.reason,
                             "chunk": f.chunk,
                             "backends": list(f.backends)}
                            for f in res.failed]
            job.state = "done" if res.ok else "failed"
        except Exception as exc:
            job.state = "failed"
            job.error = str(exc)
        finally:
            eng.close()
            job.current = ""
            job.finished_at = time.time()

    def job(self, job_id: str) -> Optional[RestoreJob]:
        return self._jobs.get(job_id)

    def jobs(self) -> List[RestoreJob]:
        return sorted(self._jobs.values(), key=lambda j: -j.started_at)
