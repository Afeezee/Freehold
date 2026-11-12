"""The mobile client's view of Freehold.

There is no second implementation of anything here. Chunking, deduplication,
encryption and the manifest are the same `freehold.core` used by the CLI and
the desktop agent; the folder list and network policy are the same
`freehold.agent` types. This module only adapts that to how a phone works:

  * No daemon. The OS gives you a short background window and may kill you,
    so `sync()` takes a time and byte budget and stops cleanly when it runs
    out. Interrupted backups resume, because chunks are content-addressed.
  * The platform knows things Python cannot see — whether the handset is
    charging, the battery level, the real link type — so those are passed in
    rather than guessed at.
  * Every call returns plain data, so a Kotlin or Swift layer can render it
    without reaching into Python objects.
"""
from __future__ import annotations

import platform
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..agent.config import AgentConfig, MobileRules
from ..agent.network import Link, NetworkPolicy
from ..core.crypto import KeyRing
from ..core.engine import Engine
from ..core.errors import ConfigError, FreeholdError
from ..core.manifest import Manifest


class BudgetReached(FreeholdError):
    """The background window ran out. Not an error: what was uploaded stays
    uploaded, and the next sync picks up where this one stopped."""


@dataclass
class Conditions:
    """What the host OS reports. Defaults are the permissive case."""
    network: str = "unknown"      # wifi | ethernet | cellular | metered | offline
    charging: bool = True
    battery_pct: int = 100
    detail: str = ""

    def link(self) -> Link:
        return Link(self.network, self.detail)


@dataclass
class FolderOutcome:
    path: str
    state: str                    # done | partial | skipped | failed
    files: int = 0
    uploaded: int = 0
    reason: str = ""
    errors: List[str] = field(default_factory=list)


@dataclass
class SyncOutcome:
    state: str                    # complete | partial | skipped | failed
    reason: str = ""
    folders: List[FolderOutcome] = field(default_factory=list)
    files: int = 0
    uploaded: int = 0
    seconds: float = 0.0
    more_to_do: bool = False      # ask the OS for another window

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


class MobileSession:
    """One handle the platform layer holds for the life of the app."""

    def __init__(self, home: Path, config: AgentConfig, manifest: Manifest,
                 keyring: KeyRing, clock=time.time):
        self.home = Path(home)
        self.cfg = config
        self.m = manifest
        self.k = keyring
        self.clock = clock

    # -- lifecycle --------------------------------------------------------
    @classmethod
    def open(cls, home: Path, passphrase: Optional[str] = None,
             device: Optional[str] = None) -> "MobileSession":
        home = Path(home)
        keyfile = home / "key.json"
        if not keyfile.exists():
            raise ConfigError("no key at " + str(keyfile)
                              + "; import or create one first")
        cfg = AgentConfig.load(home)
        if device:
            cfg.device = device
        elif not cfg.device:
            cfg.device = platform.node() or "mobile"
        return cls(home, cfg, Manifest(home / "manifest.db"),
                   KeyRing.load(keyfile, passphrase))

    @classmethod
    def enrol(cls, home: Path, key_hex: str,
              device: str) -> "MobileSession":
        """Bring a phone into an existing Freehold with the user's key.

        The key arrives from the desktop out of band — a QR code or a typed
        string — and never from a backend, because the backends are the thing
        it protects.
        """
        home = Path(home)
        home.mkdir(parents=True, exist_ok=True)
        try:
            raw = bytes.fromhex(key_hex.strip().replace(" ", ""))
        except ValueError:
            raise ConfigError("that is not a Freehold key: expected 64 hex "
                              "characters") from None
        if len(raw) != 32:
            raise ConfigError("a Freehold key is 32 bytes (64 hex chars), "
                              "got " + str(len(raw)))
        ring = KeyRing(raw)
        ring.save(home / "key.json")
        cfg = AgentConfig(device=device)
        cfg.save(home)
        return cls(home, cfg, Manifest(home / "manifest.db"), ring)

    def close(self) -> None:
        self.m.close()

    # -- configuration ----------------------------------------------------
    def add_folder(self, path: str, interval: int = 3600) -> Dict[str, Any]:
        folder = self.cfg.add_folder(path, interval=interval)
        self.cfg.save(self.home)
        return asdict(folder)

    def remove_folder(self, path: str) -> bool:
        ok = self.cfg.remove_folder(path)
        if ok:
            self.cfg.save(self.home)
        return ok

    def add_backend(self, name: str, driver: str,
                    options: Optional[Dict[str, Any]] = None
                    ) -> Dict[str, Any]:
        """Point the phone at somewhere to store blocks. Usually the same
        backend the desktop already uses -- the manifest is shared, so both
        devices write into one pool and deduplicate against each other."""
        from ..core.driver import build_driver
        if not name.strip():
            raise ConfigError("a backend needs a name")
        if self.m.backend_by_name(name):
            raise ConfigError("a backend called " + name + " already exists")
        config = {str(k): str(v) for k, v in (options or {}).items()}
        build_driver(driver, config)        # fail now, not at first backup
        bid = self.m.add_backend(name, driver, config)
        return {"id": bid, "name": name, "driver": driver}

    def set_credential(self, name: str, value: str) -> Dict[str, Any]:
        """Store a backend token in the app's private directory. There is no
        environment to read on a phone."""
        from ..core import credstore
        if not name.strip() or not value.strip():
            raise ConfigError("both a name and a value are needed")
        credstore.set_value(name.strip(), value.strip(), self.home)
        return {"name": name.strip(), "stored": True}

    def credentials(self) -> List[Dict[str, str]]:
        from ..core import credstore
        return [{"name": n, "source": credstore.sources(n, self.home)}
                for n in credstore.names(self.home)]

    def status(self) -> Dict[str, Any]:
        last = self.m.latest_snapshot(self.cfg.device)
        return {
            "device": self.cfg.device,
            "folders": [asdict(f) for f in self.cfg.folders],
            "backends": [{"name": b["name"], "driver": b["driver_id"],
                          "enabled": bool(b["enabled"]),
                          "chunks": b["chunk_count"],
                          "bytes": b["bytes_stored"]}
                         for b in self.m.usage_by_backend()],
            "last_backup": last.started_at if last else None,
            "totals": self.m.totals(),
            "rules": asdict(self.cfg.mobile),
        }

    # -- the decision to run ----------------------------------------------
    def should_sync(self, conditions: Conditions) -> tuple:
        rules: MobileRules = self.cfg.mobile
        policy = NetworkPolicy(self.cfg.network,
                               probe=lambda: conditions.link(),
                               clock=self.clock)
        allowed, why = policy.verdict(conditions.link())
        if not allowed:
            return False, why
        if rules.require_charging and not conditions.charging:
            return False, "not charging"
        if (not conditions.charging
                and conditions.battery_pct < rules.min_battery_pct):
            return False, ("battery at " + str(conditions.battery_pct)
                           + "%, below " + str(rules.min_battery_pct) + "%")
        return True, why

    # -- the one call the platform makes in a background window -----------
    def sync(self, conditions: Optional[Conditions] = None,
             budget_seconds: Optional[int] = None,
             max_bytes: Optional[int] = None,
             force: bool = False) -> SyncOutcome:
        conditions = conditions or Conditions()
        started = self.clock()
        allowed, why = self.should_sync(conditions)
        if not allowed:
            return SyncOutcome(state="skipped", reason=why, more_to_do=True)

        rules = self.cfg.mobile
        budget = (rules.budget_seconds if budget_seconds is None
                  else budget_seconds)
        cap = (rules.max_bytes_per_sync if max_bytes is None else max_bytes)
        deadline = started + budget if budget else None

        out = SyncOutcome(state="complete", reason=why)
        for folder in self.cfg.folders:
            if folder.paused:
                out.folders.append(FolderOutcome(folder.path, "skipped",
                                                 reason="paused"))
                continue
            if deadline and self.clock() >= deadline:
                out.state = "partial"
                out.more_to_do = True
                out.folders.append(FolderOutcome(folder.path, "skipped",
                                                 reason="out of time"))
                continue
            result = self._sync_folder(folder, deadline, cap, out)
            out.folders.append(result)
            if result.state == "partial":
                out.state = "partial"
                out.more_to_do = True

        out.seconds = self.clock() - started
        if any(f.state == "failed" for f in out.folders) and out.state != \
                "partial":
            out.state = "failed"
        return out

    def _sync_folder(self, folder, deadline, cap, out: SyncOutcome
                     ) -> FolderOutcome:
        root = folder.resolved()
        if not root.is_dir():
            return FolderOutcome(folder.path, "skipped",
                                 reason="folder not present on this device")
        eng = Engine(self.m, self.k, redundancy=folder.redundancy,
                     workers=self.cfg.workers)
        spent_before = out.uploaded
        # Track progress as it happens: when the budget cuts a folder short
        # there is no result object, and reporting zero would understate what
        # the sync actually cost the user in data.
        seen = {"bytes": 0, "files": 0}

        def progress(_rel, res):
            seen["bytes"] = res.bytes_uploaded
            seen["files"] = res.files
            if deadline and self.clock() >= deadline:
                raise BudgetReached("background window ended")
            if cap and (spent_before + res.bytes_uploaded) >= cap:
                raise BudgetReached("reached the per-sync byte limit")

        try:
            res = eng.backup(root, device=self.cfg.device,
                             excludes=folder.excludes,
                             backends=folder.backends, progress=progress)
        except BudgetReached as exc:
            out.files += seen["files"]
            out.uploaded += seen["bytes"]
            return FolderOutcome(folder.path, "partial", files=seen["files"],
                                 uploaded=seen["bytes"], reason=str(exc))
        except Exception as exc:
            out.uploaded += seen["bytes"]
            return FolderOutcome(folder.path, "failed",
                                 uploaded=seen["bytes"], reason=str(exc))
        finally:
            eng.close()

        out.files += res.files
        out.uploaded += res.bytes_uploaded
        return FolderOutcome(folder.path, "done", files=res.files,
                             uploaded=res.bytes_uploaded,
                             errors=list(res.errors))

    # -- restore ----------------------------------------------------------
    def snapshots(self, device: Optional[str] = None) -> List[Dict[str, Any]]:
        return [asdict(s) for s in self.m.snapshots(device)]

    def browse(self, snapshot_id: int, prefix: str = "") -> Dict[str, Any]:
        from ..dashboard.api import DashboardAPI
        return DashboardAPI(self.home, self.m, self.k).tree(snapshot_id,
                                                            prefix)

    def restore(self, snapshot_id: int, dest: str,
                paths: Optional[List[str]] = None,
                overwrite: bool = False) -> Dict[str, Any]:
        eng = Engine(self.m, self.k, workers=self.cfg.workers)
        try:
            res = eng.restore(Path(dest), snapshot_id=snapshot_id,
                              paths=paths or None, overwrite=overwrite)
        finally:
            eng.close()
        return {
            "ok": res.ok, "restored": res.restored,
            "bytes": res.bytes_written, "renamed": res.renamed,
            "unreachable": res.unreachable_backends,
            "failed": [{"path": f.path, "reason": f.reason,
                        "chunk": f.chunk, "backends": list(f.backends)}
                       for f in res.failed],
        }
