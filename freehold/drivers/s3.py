"""S3-compatible blob store: Cloudflare R2, Backblaze B2, MinIO, AWS.

This driver exists to prove the abstraction holds. If adding it had required
changing anything in freehold/core, the interface would have been wrong. It
did not: four methods, its own private state, and the engine cannot tell the
difference between this and GitHub.
"""
from __future__ import annotations

import os
import threading
from typing import Any, Dict, Iterator, List, Optional

from ..core import credstore
from ..core.driver import BackendStats, StorageDriver, register
from ..core.errors import ConfigError
from .s3api import S3API

GIB = 1024 ** 3
DEFAULT_QUOTA = 10 * GIB          # R2 and B2 both start at ~10 GB free
USAGE_KEY = "usage"


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@register
class S3Driver(StorageDriver):
    driver_id = "s3"
    config_help = ("endpoint=<url> bucket=<name> [region=auto] "
                   "[prefix=freehold] [quota=10737418240] "
                   "[key_env=FREEHOLD_S3_ACCESS_KEY_ID] "
                   "[secret_env=FREEHOLD_S3_SECRET_ACCESS_KEY]")

    def __init__(self, config: Dict[str, Any], state=None, api=None):
        super().__init__(config, state)
        self.bucket = config.get("bucket")
        if not self.bucket:
            raise ConfigError("s3 driver needs bucket=<name>")
        self.prefix = (config.get("prefix") or "freehold").strip("/")
        self.quota = _as_int(config.get("quota"), DEFAULT_QUOTA)
        self.warn_at = float(config.get("warn_at") or 0.8)
        self.warnings: List[str] = []
        self._lock = threading.RLock()
        self.api = api if api is not None else S3API(
            config.get("endpoint", ""), config.get("region") or "auto",
            self._secret("key_env", "FREEHOLD_S3_ACCESS_KEY_ID",
                         "access_key"),
            self._secret("secret_env", "FREEHOLD_S3_SECRET_ACCESS_KEY",
                         "secret_key"))

    def _secret(self, env_opt: str, env_default: str, inline: str) -> str:
        value = self.config.get(inline)
        if value:
            return value
        env = self.config.get(env_opt) or env_default
        value = credstore.get(env)      # environment, then credentials.json
        if not value:
            raise ConfigError(
                "no S3 credentials. Set the " + env + " environment "
                "variable, or save it with: freehold credentials set " + env)
        return value

    # -- private bookkeeping ---------------------------------------------
    def _usage(self) -> Dict[str, int]:
        return self.state.get(USAGE_KEY, {"bytes": 0, "chunks": 0})

    def _key(self, blob_id: str) -> str:
        return self.prefix + "/" + blob_id[:2] + "/" + blob_id

    def _loc(self, key: str) -> str:
        return self.bucket + "/" + key

    def _split(self, location: str) -> str:
        bucket, _, key = location.partition("/")
        if not key:
            raise ConfigError("unparseable s3 location: " + location)
        return key

    # -- StorageDriver ----------------------------------------------------
    def put(self, blob_id: str, data: bytes) -> str:
        key = self._key(blob_id)
        with self._lock:
            usage = self._usage()
            used = _as_int(usage.get("bytes"), 0)
            if used + len(data) > self.quota:
                self.warnings.append(
                    "quota reached (" + str(used) + "/" + str(self.quota)
                    + " bytes); add a backend or raise quota=")
            elif used + len(data) > self.quota * self.warn_at:
                self.warnings.append(
                    "at " + str(int(100 * used / self.quota))
                    + "% of the configured quota")
        # PUT is idempotent for a content-addressed key: same bytes, same key.
        self.api.put(self.bucket, key, data)
        with self._lock:
            usage = self._usage()
            usage["bytes"] = _as_int(usage.get("bytes"), 0) + len(data)
            usage["chunks"] = _as_int(usage.get("chunks"), 0) + 1
            self.state.set(USAGE_KEY, usage)
        return self._loc(key)

    def get(self, location: str) -> bytes:
        return self.api.get(self.bucket, self._split(location))

    def list(self) -> Iterator[str]:
        for key, _size in self.api.list(self.bucket, self.prefix + "/"):
            yield self._loc(key)

    def delete(self, location: str) -> bool:
        key = self._split(location)
        ok = self.api.delete(self.bucket, key)
        if ok:
            with self._lock:
                usage = self._usage()
                usage["chunks"] = max(0, _as_int(usage.get("chunks"), 0) - 1)
                self.state.set(USAGE_KEY, usage)
        return ok

    def stats(self) -> BackendStats:
        usage = self._usage()
        return BackendStats(
            bytes_stored=_as_int(usage.get("bytes"), 0),
            chunk_count=_as_int(usage.get("chunks"), 0),
            quota_bytes=self.quota,
            detail={"bucket": self.bucket, "prefix": self.prefix,
                    "endpoint": self.api.host, "region": self.api.region,
                    "warnings": list(self.warnings)})

    def drain_warnings(self) -> List[str]:
        with self._lock:
            out, self.warnings = self.warnings, []
        return out

    def health(self) -> None:
        self.api.head_bucket(self.bucket)
