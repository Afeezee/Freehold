"""Everything the dashboard can do, as plain functions over the manifest.

Kept separate from the HTTP layer so it is testable without a socket, and so
the UI has no privileged path: the dashboard can do exactly what the CLI can.
"""
from __future__ import annotations

import os
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


@dataclass
class BackupJob:
    id: str
    path: str
    state: str = "queued"          # queued | running | done | failed
    started_at: float = 0.0
    finished_at: float = 0.0
    current: str = ""
    files: int = 0
    uploaded: int = 0
    new_chunks: int = 0
    dropped: int = 0
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    error: str = ""


def _folder_shortcuts() -> List[Dict[str, str]]:
    """The places an ordinary person actually keeps things."""
    home = Path.home()
    out = []
    for label, p in (("Documents", home / "Documents"),
                     ("Pictures", home / "Pictures"),
                     ("Desktop", home / "Desktop"),
                     ("Downloads", home / "Downloads"),
                     ("Home folder", home)):
        if p.is_dir():
            out.append({"label": label, "path": str(p)})
    if os.name == "nt":
        for letter in "CDEFGH":
            drive = Path(letter + ":/")
            if drive.is_dir():
                out.append({"label": letter + ": drive", "path": str(drive)})
    return out


class DashboardAPI:
    def __init__(self, home: Path, manifest: Manifest, keyring,
                 workers: int = 8):
        self.home = Path(home)
        self.m = manifest
        self.k = keyring
        self.workers = workers
        self._jobs: Dict[str, RestoreJob] = {}
        self._backups: Dict[str, BackupJob] = {}
        self._lock = threading.Lock()

    # -- the key, and small interface preferences -------------------------
    #: Only these may be written through the interface. The manifest's meta
    #: table also holds things the engine depends on.
    UI_SETTINGS = ("ui.key_saved", "ui.onboarded")

    def key_info(self) -> Dict[str, Any]:
        """Where the key is and whether it is protected. Never its contents.

        Losing this file is the one unrecoverable failure, so the interface
        has to be able to point at it and ask whether a copy exists.
        """
        keyfile = self.home / "key.json"
        wrapped = False
        if keyfile.exists():
            try:
                wrapped = '"wrapped": true' in keyfile.read_text()
            except OSError:
                pass
        return {"path": str(keyfile), "exists": keyfile.exists(),
                "passphrase_protected": wrapped,
                "saved_elsewhere": self.m.get_meta("ui.key_saved") == "yes"}

    def settings(self) -> Dict[str, str]:
        return {k: (self.m.get_meta(k) or "") for k in self.UI_SETTINGS}

    def set_setting(self, key: str, value: str) -> Dict[str, str]:
        if key not in self.UI_SETTINGS:
            raise ConfigError("not a setting the interface may change: "
                              + key)
        self.m.set_meta(key, value)
        self.m.commit()
        return {key: value}

    def health(self) -> Dict[str, Any]:
        """One answer to the question people actually have: am I safe?"""
        backends = self.m.backends(only_enabled=True)
        devices = self.devices()
        last = max((d["last_backup"] or 0) for d in devices) if devices else 0
        totals = self.m.totals()
        # A run that stored nothing is recorded as failed. If the most recent
        # attempt failed, say so loudly: an old successful backup does not
        # make today's failure acceptable.
        recent_failures = [s for s in self.m.snapshots(complete_only=False)
                           if s.status == "failed"][:1]
        failing = bool(recent_failures) and (
            not last or recent_failures[0].started_at >= last)

        if failing:
            state = "bad"
            headline = "The last backup did not work"
        elif not backends:
            state, headline = "setup", "Choose where to keep your backups"
        elif not last:
            state, headline = "setup", "Nothing is backed up yet"
        elif time.time() - last > 7 * 24 * 3600:
            state, headline = "stale", "Your last backup is over a week old"
        else:
            state, headline = "ok", "Your files are backed up"
        return {"state": state, "headline": headline, "last_backup": last,
                "protected_bytes": totals["plaintext_bytes"],
                "places": len(backends),
                "mirrored": len(backends) > 1,
                "failing": failing,
                "failed_folder": (recent_failures[0].root if failing else ""),
                "key": self.key_info()}

    # -- choosing a folder to back up -------------------------------------
    def local_folders(self, path: str = "") -> Dict[str, Any]:
        """Browse this machine's folders, so nobody has to type a path.

        A browser cannot hand back a real folder path, so the picking has to
        happen on this side. Safe enough: the dashboard is already bound to
        localhost behind a token, and anything it can read, the backup it is
        about to run could read anyway.
        """
        if not path:
            return {"path": "", "parent": None, "dirs": [],
                    "shortcuts": _folder_shortcuts()}
        here = Path(path).expanduser()
        if not here.is_dir():
            raise ConfigError("not a folder: " + str(here))
        dirs = []
        try:
            for child in sorted(here.iterdir(), key=lambda c: c.name.lower()):
                if child.name.startswith(".") or not child.is_dir():
                    continue
                dirs.append({"name": child.name, "path": str(child)})
        except PermissionError:
            raise ConfigError("no permission to read " + str(here)) from None
        parent = str(here.parent) if here.parent != here else None
        return {"path": str(here), "parent": parent, "dirs": dirs,
                "shortcuts": _folder_shortcuts()}

    # -- measuring before committing --------------------------------------
    def preflight(self, path: str) -> Dict[str, Any]:
        """Measure a folder and ask each storage backend about it.

        This exists because the expensive way to learn a folder is too big
        is to start uploading it: a provider can restrict the account long
        before the backup finishes, and undoing that takes days.
        """
        if not path:
            raise ConfigError("choose a folder first")
        eng = Engine(self.m, self.k, workers=self.workers)
        try:
            survey = eng.survey(Path(path).expanduser())
            limits = eng.check_limits(survey)
        finally:
            eng.close()

        flat = []
        for name, issues in limits.items():
            for issue in issues:
                flat.append({"storage": name, "level": issue.level,
                             "message": issue.message,
                             "detail": issue.detail})
        blocked = any(i["level"] == "block" for i in flat)
        return {
            "path": survey.root,
            "files": survey.files,
            "folders": survey.folders,
            "bytes": survey.bytes,
            "largest_file": survey.largest_file,
            "largest_path": survey.largest_path,
            "max_depth": survey.max_depth,
            "unreadable": survey.unreadable,
            "truncated": survey.truncated,
            "estimated_uploads": survey.estimated_chunks,
            "seconds": round(survey.seconds, 2),
            "issues": flat,
            "blocked": blocked,
            "can_backup": not blocked and survey.files > 0,
            "reason": ("this folder has no files to back up"
                       if survey.files == 0 else ""),
        }

    # -- backing up -------------------------------------------------------
    def start_backup(self, path: str, redundancy: int = 1,
                     force: bool = False) -> BackupJob:
        if not path:
            raise ConfigError("choose a folder first")
        if not Path(path).expanduser().is_dir():
            raise ConfigError("not a folder: " + path)
        if not self.m.backends(only_enabled=True):
            raise ConfigError("add somewhere to store the backup first")
        if not force:
            # Checked here as well as in the interface, so the rule holds
            # however the backup was started.
            check = self.preflight(path)
            if check["blocked"]:
                first = next(i for i in check["issues"]
                             if i["level"] == "block")
                raise ConfigError(first["message"] + " " + first["detail"])
        job = BackupJob(id=uuid.uuid4().hex[:12],
                        path=str(Path(path).expanduser()))
        with self._lock:
            self._backups[job.id] = job
        threading.Thread(target=self._run_backup, args=(job, redundancy),
                         daemon=True).start()
        return job

    def _run_backup(self, job: BackupJob, redundancy: int) -> None:
        job.state = "running"
        job.started_at = time.time()
        eng = Engine(self.m, self.k, redundancy=redundancy,
                     workers=self.workers)

        def progress(rel, res):
            job.current = rel
            job.files = res.files
            job.uploaded = res.bytes_uploaded

        try:
            res = eng.backup(Path(job.path), progress=progress)
            job.files = res.files
            job.uploaded = res.bytes_uploaded
            job.new_chunks = res.chunks_new
            job.dropped = res.dropped_files
            job.errors = list(res.errors)
            job.warnings = list(res.warnings)
            # Neither of these should stop a successful backup being
            # reported as one, but silently swallowing them hides real
            # faults, so they are surfaced as warnings.
            for note in (self._remember_folder(job.path),
                         self._push_manifest()):
                if note:
                    job.warnings.append(note)
            # Set last: anything waiting for "done" must be able to trust
            # that everything this job does has actually happened.
            # A run that dropped files protected less than the person asked
            # for, so it is not a success even if some of it worked.
            job.state = "done" if not (res.errors or res.dropped_files) \
                else "failed"
        except Exception as exc:
            job.state = "failed"
            job.error = str(exc)
        finally:
            eng.close()
            job.current = ""
            job.finished_at = time.time()

    def _remember_folder(self, path: str) -> Optional[str]:
        """Add it to the agent's watch list, so automatic backups cover the
        same folders the person chose here."""
        try:
            from ..agent.config import AgentConfig
            cfg = AgentConfig.load(self.home)
            if cfg.folder(path) is None:
                cfg.add_folder(path)
                cfg.save(self.home)
            return None
        except Exception as exc:
            return "could not add this folder to the automatic list: " \
                   + str(exc)

    def _push_manifest(self) -> Optional[str]:
        """Keep the recovery copy current without anyone having to know it
        exists."""
        try:
            from ..core.portable import push_manifest
            targets = [(b.name, build_driver(b.driver_id, b.config,
                                             ManifestState(self.m, b.id)))
                       for b in self.m.backends(only_enabled=True)]
            if not targets:
                return None
            results = push_manifest(self.m, self.k, targets)
            bad = [n for n, v in results.items() if v.startswith("error:")]
            if bad:
                return ("could not save the recovery copy to: "
                        + ", ".join(bad))
            return None
        except Exception as exc:
            return "could not save the recovery copy: " + str(exc)

    def backup_jobs(self) -> List[BackupJob]:
        return sorted(self._backups.values(), key=lambda j: -j.started_at)

    def backup_job(self, job_id: str) -> Optional[BackupJob]:
        return self._backups.get(job_id)

    def watched_folders(self) -> List[Dict[str, Any]]:
        """Folders the person has backed up before, with when it last
        happened."""
        from ..agent.config import AgentConfig
        try:
            cfg = AgentConfig.load(self.home)
        except Exception:
            return []
        out = []
        for folder in cfg.folders:
            snap = self.m.latest_snapshot_for_root(cfg.device,
                                                   str(folder.resolved()))
            out.append({"path": folder.path,
                        "last_backup": snap.started_at if snap else None,
                        "files": snap.file_count if snap else 0,
                        "size": snap.total_size if snap else 0})
        return out

    def forget_folder(self, path: str) -> Dict[str, bool]:
        from ..agent.config import AgentConfig
        cfg = AgentConfig.load(self.home)
        removed = cfg.remove_folder(path)
        if removed:
            cfg.save(self.home)
        return {"removed": removed}

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
        driver = build_driver(driver_id, dict(options))
        # Prove it actually works before accepting it. Storing a backend
        # that cannot be reached means every later backup fails one chunk at
        # a time, which is a slow and confusing way to find out.
        try:
            driver.health()
        except Exception as exc:
            raise ConfigError(
                "that storage could not be reached, so it has not been "
                "added: " + str(exc)) from None
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
