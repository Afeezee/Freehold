"""Local-disk blob store.

Not a toy: it is the reference implementation of StorageDriver, the fixture the
core is tested against, and a legitimate backend (external drive, NAS mount).
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterator

from ..core.driver import (BackendStats, Limit, StorageDriver,
                           register)
from ..core.errors import BackendUnavailable, ChunkNotFound, ConfigError


@register
class LocalDriver(StorageDriver):
    driver_id = "local"
    config_help = "path=<directory>  [fail=1 to simulate an outage]"

    def __init__(self, config: Dict[str, Any], state=None):
        super().__init__(config, state)
        if not config.get("path"):
            raise ConfigError("local driver needs a 'path'")
        self.root = Path(config["path"])
        self.root.mkdir(parents=True, exist_ok=True)

    def _fail_if_simulated(self) -> None:
        # Lets the test suite and the CLI take a backend offline on purpose.
        if self.config.get("fail") or (self.root / ".offline").exists():
            raise BackendUnavailable("local backend marked offline: "
                                     + str(self.root))

    def _path(self, location: str) -> Path:
        return self.root / location

    def put(self, blob_id: str, data: bytes) -> str:
        self._fail_if_simulated()
        location = blob_id[:2] + "/" + blob_id
        dest = self._path(location)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and dest.stat().st_size == len(data):
            return location
        fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=".put-")
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, dest)
        return location

    def get(self, location: str) -> bytes:
        self._fail_if_simulated()
        try:
            return self._path(location).read_bytes()
        except FileNotFoundError:
            raise ChunkNotFound(location) from None

    def list(self) -> Iterator[str]:
        self._fail_if_simulated()
        for p in self.root.rglob("*"):
            if p.is_file() and not p.name.startswith("."):
                yield p.relative_to(self.root).as_posix()

    def delete(self, location: str) -> bool:
        self._fail_if_simulated()
        try:
            self._path(location).unlink()
            return True
        except FileNotFoundError:
            return False

    def stats(self) -> BackendStats:
        total = count = 0
        for p in self.root.rglob("*"):
            if p.is_file() and not p.name.startswith("."):
                total += p.stat().st_size
                count += 1
        return BackendStats(bytes_stored=total, chunk_count=count,
                            detail={"path": str(self.root)})

    def preflight(self, survey):
        """Refuse to start a backup the disk cannot hold."""
        import shutil
        issues = []
        try:
            free = shutil.disk_usage(str(self.root)).free
        except OSError:
            return issues
        if survey.bytes > free:
            issues.append(Limit(
                "block",
                "There is not enough room: this folder is %.1f GB and the "
                "drive has %.1f GB free."
                % (survey.bytes / 1024 ** 3, free / 1024 ** 3),
                "Free up space or choose different storage."))
        elif survey.bytes > free * 0.8:
            issues.append(Limit(
                "warn",
                "This would use most of the free space on that drive.", ""))
        return issues

    def health(self) -> None:
        self._fail_if_simulated()
        if not self.root.is_dir():
            raise BackendUnavailable("missing directory " + str(self.root))
