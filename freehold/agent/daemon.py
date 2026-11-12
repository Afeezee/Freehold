"""The background agent: watch folders, back them up, respect the network.

No GUI, no third-party dependencies. Folders are rescanned on an interval
rather than hooked into filesystem events: the engine already skips unchanged
files on size and mtime without reading them, so a scan is a stat() per file,
and this keeps the agent dependency-free and identical on all three platforms.
A watcher could be added later behind the same interface.
"""
from __future__ import annotations

import logging
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ..core.engine import Engine
from ..core.errors import FreeholdError
from ..core.manifest import Manifest
from .config import AgentConfig, Folder
from .network import NetworkPolicy

LOCK_NAME = "agent.lock"
LOG_NAME = "agent.log"
log = logging.getLogger("freehold.agent")


class Paused(FreeholdError):
    """Raised mid-backup when the network policy stops allowing transfers.

    The snapshot is marked failed, but every chunk already uploaded stays in
    the manifest, so the next run resumes rather than starting over.
    """


@dataclass
class FolderRun:
    path: str
    ok: bool
    skipped: Optional[str] = None
    files: int = 0
    uploaded: int = 0
    seconds: float = 0.0
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


class SingleInstance:
    """A pid lock file, so two agents never fight over one manifest."""

    def __init__(self, home: Path):
        self.path = Path(home) / LOCK_NAME
        self.acquired = False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(str(self.path),
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if self._stale():
                    try:
                        self.path.unlink()
                    except OSError:
                        return False
                    continue
                return False
            with os.fdopen(fd, "w") as fh:
                fh.write(str(os.getpid()))
            self.acquired = True
            return True
        return False

    def _stale(self) -> bool:
        try:
            pid = int(self.path.read_text().strip() or 0)
        except (OSError, ValueError):
            return True
        if pid <= 0:
            return True
        return not _pid_alive(pid)

    def release(self) -> None:
        if self.acquired:
            try:
                self.path.unlink()
            except OSError:
                pass
            self.acquired = False

    def __enter__(self) -> "SingleInstance":
        if not self.acquire():
            raise FreeholdError("another freehold agent is already running "
                                "(" + str(self.path) + ")")
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        out = os.popen('tasklist /FI "PID eq ' + str(pid) + '" /NH').read()
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Agent:
    def __init__(self, home: Path, config: AgentConfig, manifest: Manifest,
                 keyring, policy: Optional[NetworkPolicy] = None,
                 clock: Callable[[], float] = time.time):
        # `clock` is injectable for tests, but it must share a time base with
        # the manifest: due() compares it against snapshot.started_at, which
        # the manifest records with time.time().
        self.home = Path(home)
        self.cfg = config
        self.m = manifest
        self.k = keyring
        self.clock = clock
        self.policy = policy or NetworkPolicy(config.network, clock=clock)
        self.stop_event = threading.Event()
        self._last_run: Dict[str, float] = {}

    # -- scheduling -------------------------------------------------------
    def due(self, folder: Folder) -> bool:
        if folder.paused:
            return False
        last = self._last_run.get(folder.path)
        if last is None:
            snap = self.m.latest_snapshot_for_root(self.cfg.device,
                                                   str(folder.resolved()
                                                       .resolve()))
            last = float(snap.started_at) if snap else 0.0
            self._last_run[folder.path] = last
        return self.clock() - last >= folder.interval

    # -- one pass ---------------------------------------------------------
    def run_once(self, force: bool = False) -> List[FolderRun]:
        results: List[FolderRun] = []
        if not self.cfg.folders:
            log.warning("no folders configured; nothing to do")
            return results

        allowed, why = self.policy.verdict(self.policy.link(force=True))
        if not allowed:
            log.info("holding off: %s", why)
            return [FolderRun(f.path, ok=False, skipped=why)
                    for f in self.cfg.folders if not f.paused]

        log.info("link: %s", why)
        for folder in self.cfg.folders:
            if self.stop_event.is_set():
                break
            if folder.paused:
                results.append(FolderRun(folder.path, ok=False,
                                         skipped="paused in config"))
                continue
            if not force and not self.due(folder):
                continue
            results.append(self._backup(folder))
        if self.cfg.push_manifest and any(r.ok for r in results):
            self._push_manifest()
        return results

    def _push_manifest(self) -> None:
        """Put the manifest where a replacement machine can find it. Without
        this the chunks survive a dead device and the map of them does not."""
        from ..core.driver import build_driver
        from ..core.manifest import ManifestState
        from ..core.portable import push_manifest
        try:
            targets = [(b.name, build_driver(b.driver_id, b.config,
                                             ManifestState(self.m, b.id)))
                       for b in self.m.backends(only_enabled=True)]
            for name, where in push_manifest(self.m, self.k, targets).items():
                if where.startswith("error:"):
                    log.warning("manifest push to %s failed: %s", name,
                                where[7:])
                else:
                    log.info("manifest stored in %s", name)
        except Exception:
            log.exception("could not push the manifest")

    def _backup(self, folder: Folder) -> FolderRun:
        root = folder.resolved()
        if not root.is_dir():
            log.error("skipping %s: not a directory", root)
            return FolderRun(folder.path, ok=False,
                             skipped="not a directory")
        eng = Engine(self.m, self.k, redundancy=folder.redundancy,
                     workers=self.cfg.workers)
        started = self.clock()
        log.info("backing up %s", root)
        try:
            res = eng.backup(root, device=self.cfg.device,
                             excludes=folder.excludes,
                             backends=folder.backends,
                             progress=self._progress())
        except Paused as exc:
            self._last_run[folder.path] = 0.0   # retry as soon as allowed
            log.info("paused during %s: %s", root, exc)
            return FolderRun(folder.path, ok=False, skipped=str(exc),
                             seconds=self.clock() - started)
        except Exception as exc:
            log.exception("backup of %s failed", root)
            return FolderRun(folder.path, ok=False, errors=[str(exc)],
                             seconds=self.clock() - started)
        finally:
            eng.close()

        self._last_run[folder.path] = started
        for w in res.warnings:
            log.warning("%s", w)
        for e in res.errors:
            log.error("%s", e)
        log.info("%s: %d files, %d/%d chunks new, %d bytes in %.1fs",
                 root, res.files, res.chunks_new, res.chunks_seen,
                 res.bytes_uploaded, res.seconds)
        return FolderRun(folder.path, ok=not res.errors, files=res.files,
                         uploaded=res.bytes_uploaded, seconds=res.seconds,
                         errors=list(res.errors),
                         warnings=list(res.warnings))

    def _progress(self):
        """Re-check the link while a backup is running, and stop if it stops
        being allowed. Cheap: the policy caches for recheck_seconds."""
        def progress(_rel, _res):
            if self.stop_event.is_set():
                raise Paused("agent is shutting down")
            allowed, why = self.policy.verdict()
            if not allowed:
                raise Paused(why)
        return progress

    # -- the loop ---------------------------------------------------------
    def run_forever(self) -> None:
        log.info("agent started: device=%s, %d folder(s), workers=%d",
                 self.cfg.device, len(self.cfg.folders), self.cfg.workers)
        self._install_signals()
        while not self.stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                log.exception("unexpected error in agent loop")
            self.stop_event.wait(self.cfg.poll_seconds)
        log.info("agent stopped")

    def _install_signals(self) -> None:
        def handler(_signum, _frame):
            log.info("shutdown requested")
            self.stop_event.set()
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass        # not the main thread, or not supported here


def setup_logging(home: Path, level: str = "INFO",
                  to_stderr: bool = True) -> Path:
    path = Path(home) / LOG_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    handlers: List[logging.Handler] = [logging.FileHandler(str(path),
                                                           encoding="utf-8")]
    if to_stderr:
        handlers.append(logging.StreamHandler())
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger("freehold")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in handlers:
        h.setFormatter(fmt)
        root.addHandler(h)
    return path
