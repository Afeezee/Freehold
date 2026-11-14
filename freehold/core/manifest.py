"""The manifest: SQLite, and the source of truth for every restore.

Backends are dumb blob stores underneath this file. Nothing here names a
specific vendor -- a backend row is just (driver_id, opaque config).
"""
from __future__ import annotations

import functools
import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SCHEMA_VERSION = "1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backend (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    driver_id   TEXT NOT NULL,
    config_json TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    bytes_stored INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL
);

-- Private scratch space a driver may use for its own bookkeeping, whatever
-- that means for it. The core writes and reads nothing here.
CREATE TABLE IF NOT EXISTS backend_state (
    backend_id INTEGER NOT NULL REFERENCES backend(id) ON DELETE CASCADE,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    PRIMARY KEY (backend_id, key)
);

CREATE TABLE IF NOT EXISTS chunk (
    hash       BLOB PRIMARY KEY,   -- SHA-256 of the PLAINTEXT chunk
    size       INTEGER NOT NULL,
    enc_size   INTEGER NOT NULL,
    refcount   INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS chunk_location (
    chunk_hash  BLOB NOT NULL REFERENCES chunk(hash) ON DELETE CASCADE,
    backend_id  INTEGER NOT NULL REFERENCES backend(id) ON DELETE CASCADE,
    location    TEXT NOT NULL,     -- opaque, driver-defined
    stored_at   INTEGER NOT NULL,
    verified_at INTEGER,
    PRIMARY KEY (chunk_hash, backend_id)
);
CREATE INDEX IF NOT EXISTS idx_loc_backend ON chunk_location(backend_id);

CREATE TABLE IF NOT EXISTS device (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    platform   TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshot (
    id           INTEGER PRIMARY KEY,
    device_id    INTEGER NOT NULL REFERENCES device(id),
    root         TEXT NOT NULL,
    started_at   INTEGER NOT NULL,
    completed_at INTEGER,
    status       TEXT NOT NULL,    -- running | complete | failed
    file_count   INTEGER NOT NULL DEFAULT 0,
    dropped      INTEGER NOT NULL DEFAULT 0,
    total_size   INTEGER NOT NULL DEFAULT 0,
    new_bytes    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_snap_device ON snapshot(device_id, started_at);

CREATE TABLE IF NOT EXISTS file (
    id             INTEGER PRIMARY KEY,
    snapshot_id    INTEGER NOT NULL REFERENCES snapshot(id) ON DELETE CASCADE,
    path           TEXT NOT NULL,  -- relative to snapshot.root, POSIX sep
    mode           INTEGER NOT NULL,
    size           INTEGER NOT NULL,
    mtime_ns       INTEGER NOT NULL,
    content_hash   BLOB,           -- SHA-256 of the whole plaintext file
    symlink_target TEXT,
    UNIQUE (snapshot_id, path)
);

CREATE TABLE IF NOT EXISTS file_chunk (
    file_id    INTEGER NOT NULL REFERENCES file(id) ON DELETE CASCADE,
    idx        INTEGER NOT NULL,
    chunk_hash BLOB NOT NULL REFERENCES chunk(hash),
    offset     INTEGER NOT NULL,
    PRIMARY KEY (file_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_fc_hash ON file_chunk(chunk_hash);
"""


@dataclass
class Backend:
    id: int
    name: str
    driver_id: str
    config: Dict[str, Any]
    enabled: bool
    bytes_stored: int
    chunk_count: int


@dataclass
class Snapshot:
    id: int
    device: str
    root: str
    started_at: int
    completed_at: Optional[int]
    status: str
    file_count: int
    total_size: int
    new_bytes: int
    dropped: int = 0


@dataclass
class FileEntry:
    id: int
    path: str
    mode: int
    size: int
    mtime_ns: int
    content_hash: Optional[bytes]
    symlink_target: Optional[str]


def _synchronized(cls):
    """Serialise every public method on one reentrant lock.

    The engine moves chunks on a thread pool, and a driver's own bookkeeping
    is written from those threads. Manifest calls take microseconds while the
    transfers they bracket take seconds, so one lock costs nothing measurable
    and removes a whole class of race.
    """
    for name, fn in list(vars(cls).items()):
        if not name.startswith("_") and callable(fn):
            setattr(cls, name, _guard(fn))
    return cls


def _guard(fn):
    @functools.wraps(fn)
    def wrapper(self, *a, **kw):
        with self._lock:
            return fn(self, *a, **kw)
    return wrapper


@_synchronized
class Manifest:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # Safe because every access goes through the lock above.
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.executescript(SCHEMA)
        self._migrate()
        if self.get_meta("schema_version") is None:
            self.set_meta("schema_version", SCHEMA_VERSION)
            self.set_meta("manifest_id", uuid.uuid4().hex)
        self.db.commit()

    def _migrate(self) -> None:
        """Add columns that later versions need, without touching data.

        CREATE TABLE IF NOT EXISTS does nothing to a table that already
        exists, so a manifest written by an older version keeps its old
        shape until something like this widens it.
        """
        have = {r["name"] for r in self.db.execute(
            "PRAGMA table_info(snapshot)")}
        if "dropped" not in have:
            # How many files could not be stored. Kept on the snapshot
            # because the run's own report is gone the moment the process
            # ends, and "7 files" reads very differently from "7 files, 5
            # could not be stored".
            self.db.execute("ALTER TABLE snapshot ADD COLUMN dropped "
                            "INTEGER NOT NULL DEFAULT 0")
            self.db.commit()

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    def __enter__(self) -> "Manifest":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- meta -------------------------------------------------------------
    def get_meta(self, key: str) -> Optional[str]:
        row = self.db.execute("SELECT value FROM meta WHERE key=?",
                              (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # -- backends ---------------------------------------------------------
    def add_backend(self, name: str, driver_id: str,
                    config: Dict[str, Any]) -> int:
        cur = self.db.execute(
            "INSERT INTO backend(name,driver_id,config_json,created_at) "
            "VALUES(?,?,?,?)",
            (name, driver_id, json.dumps(config), int(time.time())))
        self.db.commit()
        return int(cur.lastrowid)

    def remove_backend(self, name: str) -> None:
        self.db.execute("DELETE FROM backend WHERE name=?", (name,))
        self.db.commit()

    def set_backend_enabled(self, name: str, enabled: bool) -> None:
        self.db.execute("UPDATE backend SET enabled=? WHERE name=?",
                        (1 if enabled else 0, name))
        self.db.commit()

    def _backend(self, row: sqlite3.Row) -> Backend:
        return Backend(row["id"], row["name"], row["driver_id"],
                       json.loads(row["config_json"]), bool(row["enabled"]),
                       row["bytes_stored"], row["chunk_count"])

    def backends(self, only_enabled: bool = False) -> List[Backend]:
        q = "SELECT * FROM backend"
        if only_enabled:
            q += " WHERE enabled=1"
        return [self._backend(r) for r in self.db.execute(q + " ORDER BY id")]

    def backend_by_name(self, name: str) -> Optional[Backend]:
        row = self.db.execute("SELECT * FROM backend WHERE name=?",
                              (name,)).fetchone()
        return self._backend(row) if row else None

    def backend_by_id(self, bid: int) -> Optional[Backend]:
        row = self.db.execute("SELECT * FROM backend WHERE id=?",
                              (bid,)).fetchone()
        return self._backend(row) if row else None

    def update_backend_config(self, bid: int, config: Dict[str, Any]) -> None:
        self.db.execute("UPDATE backend SET config_json=? WHERE id=?",
                        (json.dumps(config), bid))

    # -- per-backend private state ----------------------------------------
    def get_backend_state(self, bid: int, key: str) -> Optional[Any]:
        row = self.db.execute(
            "SELECT value FROM backend_state WHERE backend_id=? AND key=?",
            (bid, key)).fetchone()
        return json.loads(row["value"]) if row else None

    def set_backend_state(self, bid: int, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT INTO backend_state(backend_id,key,value) VALUES(?,?,?) "
            "ON CONFLICT(backend_id,key) DO UPDATE SET value=excluded.value",
            (bid, key, json.dumps(value)))
        self.db.commit()

    # -- devices ----------------------------------------------------------
    def device_id(self, name: str, platform: str = "") -> int:
        row = self.db.execute("SELECT id FROM device WHERE name=?",
                              (name,)).fetchone()
        if row:
            return int(row["id"])
        cur = self.db.execute(
            "INSERT INTO device(name,platform,created_at) VALUES(?,?,?)",
            (name, platform, int(time.time())))
        return int(cur.lastrowid)

    def devices(self) -> List[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM device ORDER BY name"))

    # -- chunks -----------------------------------------------------------
    def has_chunk(self, chunk_hash: bytes) -> bool:
        return self.db.execute("SELECT 1 FROM chunk WHERE hash=?",
                               (chunk_hash,)).fetchone() is not None

    def chunk_locations(self, chunk_hash: bytes) -> List[Tuple[int, str]]:
        return [(r["backend_id"], r["location"]) for r in self.db.execute(
            "SELECT backend_id, location FROM chunk_location WHERE "
            "chunk_hash=?", (chunk_hash,))]

    def record_chunk(self, chunk_hash: bytes, size: int, enc_size: int) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO chunk(hash,size,enc_size,created_at) "
            "VALUES(?,?,?,?)", (chunk_hash, size, enc_size, int(time.time())))

    def record_location(self, chunk_hash: bytes, backend_id: int,
                        location: str, enc_size: int) -> None:
        existed = self.db.execute(
            "SELECT 1 FROM chunk_location WHERE chunk_hash=? AND backend_id=?",
            (chunk_hash, backend_id)).fetchone()
        self.db.execute(
            "INSERT INTO chunk_location"
            "(chunk_hash,backend_id,location,stored_at) VALUES(?,?,?,?) "
            "ON CONFLICT(chunk_hash,backend_id) DO UPDATE SET "
            "location=excluded.location, stored_at=excluded.stored_at",
            (chunk_hash, backend_id, location, int(time.time())))
        if not existed:
            self.db.execute(
                "UPDATE backend SET bytes_stored=bytes_stored+?, "
                "chunk_count=chunk_count+1 WHERE id=?", (enc_size, backend_id))

    def mark_verified(self, chunk_hash: bytes, backend_id: int) -> None:
        self.db.execute(
            "UPDATE chunk_location SET verified_at=? WHERE chunk_hash=? "
            "AND backend_id=?", (int(time.time()), chunk_hash, backend_id))

    # -- snapshots and files ----------------------------------------------
    def start_snapshot(self, device: str, root: str, platform: str = "") -> int:
        did = self.device_id(device, platform)
        cur = self.db.execute(
            "INSERT INTO snapshot(device_id,root,started_at,status) "
            "VALUES(?,?,?,'running')", (did, root, int(time.time())))
        self.db.commit()
        return int(cur.lastrowid)

    def finish_snapshot(self, sid: int, status: str, file_count: int,
                        total_size: int, new_bytes: int,
                        dropped: int = 0) -> None:
        self.db.execute(
            "UPDATE snapshot SET completed_at=?, status=?, file_count=?, "
            "total_size=?, new_bytes=?, dropped=? WHERE id=?",
            (int(time.time()), status, file_count, total_size, new_bytes,
             dropped, sid))
        self.db.commit()

    def add_file(self, snapshot_id: int, path: str, mode: int, size: int,
                 mtime_ns: int, content_hash: Optional[bytes],
                 symlink_target: Optional[str] = None) -> int:
        cur = self.db.execute(
            "INSERT INTO file(snapshot_id,path,mode,size,mtime_ns,"
            "content_hash,symlink_target) VALUES(?,?,?,?,?,?,?)",
            (snapshot_id, path, mode, size, mtime_ns, content_hash,
             symlink_target))
        return int(cur.lastrowid)

    def add_file_chunks(self, file_id: int,
                        chunks: Sequence[Tuple[int, bytes, int]]) -> None:
        self.db.executemany(
            "INSERT INTO file_chunk(file_id,idx,chunk_hash,offset) "
            "VALUES(?,?,?,?)", [(file_id, i, h, off) for i, h, off in chunks])
        self.db.executemany(
            "UPDATE chunk SET refcount=refcount+1 WHERE hash=?",
            [(h,) for _, h, _ in chunks])

    def _snapshot(self, row: sqlite3.Row) -> Snapshot:
        keys = row.keys()
        return Snapshot(row["id"], row["device"], row["root"],
                        row["started_at"], row["completed_at"], row["status"],
                        row["file_count"], row["total_size"], row["new_bytes"],
                        row["dropped"] if "dropped" in keys else 0)

    _SNAP_Q = ("SELECT s.*, d.name AS device FROM snapshot s "
               "JOIN device d ON d.id = s.device_id")

    def snapshots(self, device: Optional[str] = None,
                  complete_only: bool = True) -> List[Snapshot]:
        q, args = self._SNAP_Q, []
        where = []
        if device:
            where.append("d.name = ?")
            args.append(device)
        if complete_only:
            where.append("s.status = 'complete'")
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY s.started_at DESC, s.id DESC"
        return [self._snapshot(r) for r in self.db.execute(q, args)]

    def snapshot(self, sid: int) -> Optional[Snapshot]:
        row = self.db.execute(self._SNAP_Q + " WHERE s.id=?", (sid,)).fetchone()
        return self._snapshot(row) if row else None

    def latest_snapshot(self, device: Optional[str] = None,
                        before: Optional[int] = None) -> Optional[Snapshot]:
        q, args = self._SNAP_Q + " WHERE s.status='complete'", []
        if device:
            q += " AND d.name=?"
            args.append(device)
        if before is not None:
            q += " AND s.started_at <= ?"
            args.append(before)
        q += " ORDER BY s.started_at DESC, s.id DESC LIMIT 1"
        row = self.db.execute(q, args).fetchone()
        return self._snapshot(row) if row else None

    def latest_snapshot_for_root(self, device: str,
                                 root: str) -> Optional[Snapshot]:
        row = self.db.execute(
            self._SNAP_Q + " WHERE d.name=? AND s.root=? AND "
            "s.status='complete' ORDER BY s.started_at DESC LIMIT 1",
            (device, root)).fetchone()
        return self._snapshot(row) if row else None

    def files(self, snapshot_id: int,
              prefixes: Optional[Iterable[str]] = None) -> List[FileEntry]:
        rows = self.db.execute(
            "SELECT * FROM file WHERE snapshot_id=? ORDER BY path",
            (snapshot_id,))
        out = [FileEntry(r["id"], r["path"], r["mode"], r["size"],
                         r["mtime_ns"], r["content_hash"], r["symlink_target"])
               for r in rows]
        pats = list(prefixes) if prefixes else None
        if not pats:
            return out
        keep = []
        for f in out:
            for p in pats:
                p = p.strip("/")
                if f.path == p or f.path.startswith(p + "/"):
                    keep.append(f)
                    break
        return keep

    def file_chunks(self, file_id: int) -> List[Tuple[bytes, int]]:
        return [(r["chunk_hash"], r["offset"]) for r in self.db.execute(
            "SELECT chunk_hash, offset FROM file_chunk WHERE file_id=? "
            "ORDER BY idx", (file_id,))]

    def drop_files_with_chunk(self, snapshot_id: int,
                              chunk_hash: bytes) -> List[Tuple[str, int]]:
        """Remove files in this snapshot that depend on a chunk which never
        made it to a backend, so a 'complete' snapshot never claims to hold a
        file it cannot restore. Returns the (path, size) pairs dropped."""
        rows = self.db.execute(
            "SELECT DISTINCT f.id, f.path, f.size FROM file f "
            "JOIN file_chunk fc ON fc.file_id = f.id "
            "WHERE f.snapshot_id=? AND fc.chunk_hash=?",
            (snapshot_id, chunk_hash)).fetchall()
        out: List[Tuple[str, int]] = []
        for r in rows:
            refs = [(h,) for h, _ in self.file_chunks(r["id"])]
            self.db.executemany(
                "UPDATE chunk SET refcount=refcount-1 WHERE hash=?", refs)
            self.db.execute("DELETE FROM file WHERE id=?", (r["id"],))
            out.append((r["path"], r["size"]))
        self.db.commit()
        return out

    def unstored_chunks(self) -> int:
        """Chunks the manifest knows about that no backend actually holds.
        Any number above zero means data was recorded but never stored."""
        return self.db.execute(
            "SELECT COUNT(*) n FROM chunk WHERE hash NOT IN "
            "(SELECT chunk_hash FROM chunk_location)").fetchone()["n"]

    def purge_unstored_chunks(self) -> int:
        """Forget chunks that were never stored and nothing references.
        Deduplication keys off chunk_location, so these rows do nothing but
        make a failure look permanent."""
        cur = self.db.execute(
            "DELETE FROM chunk WHERE refcount <= 0 AND hash NOT IN "
            "(SELECT chunk_hash FROM chunk_location)")
        self.db.commit()
        return cur.rowcount or 0

    def unrestorable_snapshots(self) -> List[Tuple[int, str, str, int]]:
        """Snapshots marked complete that reference data no backend holds.
        These are the dangerous ones: they look like backups and are not."""
        rows = self.db.execute(
            "SELECT s.id, d.name AS device, s.root, COUNT(DISTINCT fc.chunk_hash) n "
            "FROM snapshot s "
            "JOIN device d ON d.id = s.device_id "
            "JOIN file f ON f.snapshot_id = s.id "
            "JOIN file_chunk fc ON fc.file_id = f.id "
            "WHERE s.status = 'complete' AND fc.chunk_hash NOT IN "
            "(SELECT chunk_hash FROM chunk_location) "
            "GROUP BY s.id ORDER BY s.id").fetchall()
        return [(r["id"], r["device"], r["root"], r["n"]) for r in rows]

    def mark_snapshot_failed(self, snapshot_id: int) -> None:
        self.db.execute("UPDATE snapshot SET status='failed' WHERE id=?",
                        (snapshot_id,))
        self.db.commit()

    def empty_complete_snapshots(self) -> List[Tuple[int, str, str]]:
        """Complete snapshots holding no files at all."""
        rows = self.db.execute(
            "SELECT s.id, d.name AS device, s.root FROM snapshot s "
            "JOIN device d ON d.id = s.device_id "
            "WHERE s.status='complete' AND s.file_count = 0 "
            "ORDER BY s.id").fetchall()
        return [(r["id"], r["device"], r["root"]) for r in rows]

    def prior_file(self, device: str, root: str,
                   path: str) -> Optional[sqlite3.Row]:
        """Most recent complete record of this path, for the unchanged-file
        fast path (same size + mtime => reuse the previous chunk list)."""
        return self.db.execute(
            "SELECT f.* FROM file f JOIN snapshot s ON s.id=f.snapshot_id "
            "JOIN device d ON d.id=s.device_id "
            "WHERE d.name=? AND s.root=? AND f.path=? AND s.status='complete' "
            "ORDER BY s.started_at DESC LIMIT 1", (device, root, path)
        ).fetchone()

    def backend_is_missing_chunks(self, file_id: int,
                                  backend_id: int) -> bool:
        """Does this backend lack any block this file needs?

        One indexed query per file, so it is cheap enough to ask even on a
        run where nothing changed — which is exactly when a backend can
        quietly be missing data nobody re-uploaded.
        """
        row = self.db.execute(
            "SELECT 1 FROM file_chunk fc WHERE fc.file_id = ? AND NOT EXISTS "
            "(SELECT 1 FROM chunk_location cl WHERE cl.chunk_hash = "
            "fc.chunk_hash AND cl.backend_id = ?) LIMIT 1",
            (file_id, backend_id)).fetchone()
        return row is not None

    def copy_file_chunks(self, src_file_id: int, dst_file_id: int) -> int:
        rows = list(self.db.execute(
            "SELECT idx, chunk_hash, offset FROM file_chunk WHERE file_id=?",
            (src_file_id,)))
        self.db.executemany(
            "INSERT INTO file_chunk(file_id,idx,chunk_hash,offset) "
            "VALUES(?,?,?,?)",
            [(dst_file_id, r["idx"], r["chunk_hash"], r["offset"])
             for r in rows])
        self.db.executemany(
            "UPDATE chunk SET refcount=refcount+1 WHERE hash=?",
            [(r["chunk_hash"],) for r in rows])
        return len(rows)

    # -- reporting --------------------------------------------------------
    def usage_by_backend(self) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.db.execute(
            "SELECT b.name, b.driver_id, b.enabled, b.chunk_count, "
            "b.bytes_stored FROM backend b ORDER BY b.name")]

    def totals(self) -> Dict[str, int]:
        row = self.db.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(size),0) raw, "
            "COALESCE(SUM(enc_size),0) enc FROM chunk").fetchone()
        return {"chunks": row["n"], "plaintext_bytes": row["raw"],
                "encrypted_bytes": row["enc"]}

    def commit(self) -> None:
        self.db.commit()


class ManifestState:
    """A driver's private key/value store, persisted in the manifest.

    The core writes nothing here and reads nothing back; it only hands the
    driver a handle. This is how a driver keeps bookkeeping that must survive
    restarts without the core knowing what any of it means.
    """

    def __init__(self, manifest: "Manifest", backend_id: int):
        self._m = manifest
        self._id = backend_id

    def get(self, key: str, default: Any = None) -> Any:
        value = self._m.get_backend_state(self._id, key)
        return default if value is None else value

    def set(self, key: str, value: Any) -> None:
        self._m.set_backend_state(self._id, key, value)
