"""Agent configuration: a JSON file next to the manifest."""
from __future__ import annotations

import json
import platform
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.engine import DEFAULT_EXCLUDES, DEFAULT_WORKERS
from ..core.errors import ConfigError

CONFIG_NAME = "agent.json"


@dataclass
class Folder:
    path: str
    interval: int = 900                     # seconds between backups
    excludes: List[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    backends: Optional[List[str]] = None    # None = engine's default choice
    redundancy: int = 1
    paused: bool = False

    def resolved(self) -> Path:
        return Path(self.path).expanduser()


@dataclass
class NetworkRules:
    #: Backups run on whatever link is available, cellular included, with no
    #: size or duration limit. Where mobile data is the primary connection, a
    #: Wi-Fi-only default means the agent never runs at all -- which is the
    #: one failure a backup tool must not have. Set these to false if you
    #: would rather wait for an unmetered link.
    allow_cellular: bool = True
    allow_metered: bool = True
    #: What to do when the link type cannot be determined.
    on_unknown: str = "allow"
    #: Shell command printing one of wifi/ethernet/cellular/metered/offline.
    #: An escape hatch for when detection is wrong on a given machine.
    probe_command: Optional[str] = None
    #: Don't re-probe more often than this while a backup is running.
    recheck_seconds: int = 20


@dataclass
class MobileRules:
    """Conditions only the host OS can report, so the platform layer passes
    them in and this decides what to do with them."""
    min_battery_pct: int = 15
    require_charging: bool = False
    #: A background window is short and can be killed; stop cleanly instead.
    budget_seconds: int = 240
    max_bytes_per_sync: int = 0      # 0 = no limit


@dataclass
class AgentConfig:
    device: str = ""
    folders: List[Folder] = field(default_factory=list)
    network: NetworkRules = field(default_factory=NetworkRules)
    mobile: MobileRules = field(default_factory=MobileRules)
    workers: int = DEFAULT_WORKERS
    poll_seconds: int = 60                  # how often the loop wakes up
    log_level: str = "INFO"
    #: Store the manifest in the backends after a successful pass. Without
    #: it, losing this machine means losing the map of what is backed up.
    push_manifest: bool = True

    # -- persistence ------------------------------------------------------
    @staticmethod
    def path_for(home: Path) -> Path:
        return Path(home) / CONFIG_NAME

    @classmethod
    def load(cls, home: Path) -> "AgentConfig":
        p = cls.path_for(home)
        if not p.exists():
            return cls(device=platform.node())
        try:
            doc = json.loads(p.read_text())
        except ValueError as exc:
            raise ConfigError("agent config is not valid JSON: "
                              + str(p)) from exc
        return cls.from_dict(doc)

    @classmethod
    def from_dict(cls, doc: Dict[str, Any]) -> "AgentConfig":
        net = NetworkRules(**(doc.get("network") or {}))
        mob = MobileRules(**(doc.get("mobile") or {}))
        folders = [Folder(**f) for f in (doc.get("folders") or [])]
        known = {"device", "workers", "poll_seconds", "log_level",
                 "push_manifest"}
        rest = {k: v for k, v in doc.items() if k in known}
        cfg = cls(folders=folders, network=net, mobile=mob, **rest)
        if not cfg.device:
            cfg.device = platform.node()
        if cfg.network.on_unknown not in ("allow", "block"):
            raise ConfigError("network.on_unknown must be allow or block")
        return cfg

    def save(self, home: Path) -> Path:
        p = self.path_for(home)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(self), indent=2))
        return p

    # -- folder list ------------------------------------------------------
    def folder(self, path: str) -> Optional[Folder]:
        want = str(Path(path).expanduser().resolve())
        for f in self.folders:
            try:
                if str(f.resolved().resolve()) == want:
                    return f
            except OSError:
                continue
        return None

    def add_folder(self, path: str, **kw) -> Folder:
        resolved = Path(path).expanduser()
        if not resolved.is_dir():
            raise ConfigError("not a directory: " + str(resolved))
        existing = self.folder(path)
        if existing:
            raise ConfigError("already watched: " + existing.path)
        f = Folder(path=str(resolved.resolve()), **kw)
        self.folders.append(f)
        return f

    def remove_folder(self, path: str) -> bool:
        f = self.folder(path)
        if f is None:
            return False
        self.folders.remove(f)
        return True
