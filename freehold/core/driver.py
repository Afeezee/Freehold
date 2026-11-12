"""The one interface every backend implements."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict, Iterator, List, Optional, Type

from .errors import ConfigError


@dataclass
class BackendStats:
    bytes_stored: int = 0
    chunk_count: int = 0
    quota_bytes: Optional[int] = None  # None = unknown or unlimited
    detail: Dict[str, Any] = field(default_factory=dict)  # driver's own rows


class DriverState:
    """In-memory default for a driver's private bookkeeping. The engine swaps
    in a manifest-backed one so the state survives restarts."""

    def __init__(self, initial: Optional[Dict[str, Any]] = None):
        self._d = dict(initial or {})

    def get(self, key: str, default: Any = None) -> Any:
        return self._d.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._d[key] = value


class StorageDriver(ABC):
    """A dumb encrypted blob store.

    It never sees plaintext, file paths or file names. The core engine talks to
    nothing else, so adding or dropping a backend never touches core logic.

    **Thread safety.** The engine transfers chunks on a thread pool, so `put`
    and `get` may be called concurrently on one driver instance. Guard any
    shared bookkeeping, and do the network call outside that lock so the
    parallelism survives. `state` is backed by the manifest and is already
    safe to call from any thread. `list`, `delete`, `stats` and `health` are
    only ever called from the calling thread.
    """

    driver_id: ClassVar[str]
    config_help: ClassVar[str] = ""

    def __init__(self, config: Dict[str, Any],
                 state: Optional[DriverState] = None):
        self.config = config
        self.state = state if state is not None else DriverState()

    @abstractmethod
    def put(self, blob_id: str, data: bytes) -> str:
        """Store ciphertext under ``blob_id``. Idempotent. Returns an opaque
        location string the core records and later hands back to ``get``."""

    @abstractmethod
    def get(self, location: str) -> bytes:
        """Return the exact bytes stored at ``location``."""

    @abstractmethod
    def list(self) -> Iterator[str]:
        """Yield every location this backend currently holds."""

    @abstractmethod
    def delete(self, location: str) -> bool:
        """Remove a blob. True if it was there."""

    # -- optional ---------------------------------------------------------
    def stats(self) -> BackendStats:
        return BackendStats()

    def drain_warnings(self) -> List[str]:
        """Return and clear anything the driver wants the user to know about
        (approaching a quota, internal housekeeping). Opaque to the core."""
        return []

    def health(self) -> None:
        """Raise BackendUnavailable if this backend cannot serve requests."""

    def close(self) -> None:
        pass


_REGISTRY: Dict[str, Type[StorageDriver]] = {}


def register(cls: Type[StorageDriver]) -> Type[StorageDriver]:
    _REGISTRY[cls.driver_id] = cls
    return cls


def _load_builtin() -> None:
    from .. import drivers  # noqa: F401  (import side effect: registration)


def build_driver(driver_id: str, config: Dict[str, Any],
                 state: Optional[DriverState] = None) -> StorageDriver:
    _load_builtin()
    cls = _REGISTRY.get(driver_id)
    if cls is None:
        raise ConfigError("unknown driver " + repr(driver_id)
                          + "; known: " + ", ".join(sorted(_REGISTRY)))
    return cls(config, state)


def known_drivers() -> Dict[str, Type[StorageDriver]]:
    _load_builtin()
    return dict(_REGISTRY)
