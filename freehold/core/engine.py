"""Backup and restore orchestration.

The engine knows about chunks, hashes, keys and the manifest. It does not know
what a backend is beyond StorageDriver. Nothing below this line should ever
need editing to add, swap or remove a storage provider.

Chunk transfers run on a bounded thread pool, because a backup over a network
backend is latency-bound: one HTTP round trip per chunk, serially, is the
difference between 36 KB/s and saturating the link. Chunking, encryption and
every manifest decision stay on the calling thread; only put() and get() are
parallel. Drivers must therefore be safe to call concurrently.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import platform
import stat
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import (Dict, Iterable, List, Optional, Sequence, Set, Tuple)

from . import chunker
from .crypto import KeyRing, sha256
from .driver import StorageDriver, build_driver
from .errors import (BackendUnavailable, ChunkNotFound, FreeholdError,
                     IntegrityError)
from .manifest import Backend, Manifest, ManifestState, Snapshot

DEFAULT_EXCLUDES = (".freehold", ".git/objects", "*.tmp", "~$*",
                    "Thumbs.db", ".DS_Store")
DEFAULT_WORKERS = 8
CACHE_CHUNKS = 64


@dataclass
class BackupResult:
    snapshot_id: int
    files: int = 0
    total_size: int = 0
    chunks_seen: int = 0
    chunks_new: int = 0
    bytes_uploaded: int = 0
    unchanged_files: int = 0
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    seconds: float = 0.0


@dataclass
class RestoreFailure:
    path: str
    reason: str
    chunk: Optional[str] = None
    backends: Sequence[str] = ()


@dataclass
class RestoreResult:
    snapshot_id: int
    restored: List[str] = field(default_factory=list)
    renamed: Dict[str, str] = field(default_factory=dict)
    failed: List[RestoreFailure] = field(default_factory=list)
    bytes_written: int = 0
    #: Backends that were tried and failed. A backend that was never needed
    #: does not appear here: the engine stops at the first location that
    #: answers, so it has no way to know the others are down.
    unreachable_backends: List[str] = field(default_factory=list)
    #: Backends that actually served chunks. "restored entirely from gh" is
    #: a statement you can check.
    served_by: List[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.failed


@dataclass
class _Upload:
    """What a worker thread reports back about one chunk."""
    enc_size: int
    locations: List[Tuple[int, str]] = field(default_factory=list)
    dead: List[Tuple[int, str]] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class _Fetch:
    """What a worker thread reports back about one chunk it went to get."""
    piece: Optional[bytes] = None
    from_backend: Optional[int] = None
    dead: List[Tuple[int, str]] = field(default_factory=list)
    error: Optional[str] = None


class Engine:
    def __init__(self, manifest: Manifest, keyring: KeyRing,
                 redundancy: int = 1,
                 chunk_params: Optional[Dict[str, int]] = None,
                 workers: int = DEFAULT_WORKERS):
        self.m = manifest
        self.k = keyring
        self.redundancy = max(1, redundancy)
        self.chunk_params = chunk_params or {}
        self.workers = max(1, workers)
        self._drivers: Dict[int, StorageDriver] = {}
        self._drv_lock = threading.Lock()
        self._dead: Dict[int, str] = {}
        self._served: Set[int] = set()

    # -- driver plumbing --------------------------------------------------
    def driver(self, backend: Backend) -> StorageDriver:
        d = self._drivers.get(backend.id)
        if d is not None:
            return d
        with self._drv_lock:
            d = self._drivers.get(backend.id)
            if d is None:
                d = build_driver(backend.driver_id, backend.config,
                                 ManifestState(self.m, backend.id))
                self._drivers[backend.id] = d
            return d

    def _write_targets(self, names: Optional[Sequence[str]]) -> List[Backend]:
        all_b = self.m.backends(only_enabled=True)
        if names:
            picked = [b for b in all_b if b.name in set(names)]
            missing = set(names) - {b.name for b in picked}
            if missing:
                raise FreeholdError("no such enabled backend: "
                                    + ", ".join(sorted(missing)))
            return picked
        return all_b[:self.redundancy]

    def close(self) -> None:
        for d in self._drivers.values():
            d.close()
        self._drivers.clear()

    def _drain_warnings(self) -> List[str]:
        out: List[str] = []
        for bid, drv in self._drivers.items():
            b = self.m.backend_by_id(bid)
            name = b.name if b else str(bid)
            for w in drv.drain_warnings():
                out.append(name + ": " + w)
        return out

    def _backend_names(self, ids: Iterable[int]) -> List[str]:
        out = []
        for bid in ids:
            b = self.m.backend_by_id(bid)
            if b:
                out.append(b.name)
        return out

    # -- backup -----------------------------------------------------------
    def backup(self, root: Path, device: Optional[str] = None,
               excludes: Iterable[str] = DEFAULT_EXCLUDES,
               backends: Optional[Sequence[str]] = None,
               progress=None) -> BackupResult:
        root = Path(root).resolve()
        if not root.is_dir():
            raise FreeholdError("not a directory: " + str(root))
        device = device or platform.node()
        targets = self._write_targets(backends)
        if not targets:
            raise FreeholdError("no enabled backends configured")

        t0 = time.time()
        sid = self.m.start_snapshot(device, str(root), platform.system())
        res = BackupResult(snapshot_id=sid)
        ex = list(excludes)
        inflight: List[Tuple[bytes, Future]] = []
        submitted: Set[bytes] = set()
        failed: Dict[bytes, str] = {}
        # Bounds memory: at most this many sealed chunks queued or in transit.
        slots = threading.Semaphore(self.workers * 2)
        try:
            with ThreadPoolExecutor(max_workers=self.workers,
                                    thread_name_prefix="fh-put") as pool:
                ctx = (pool, slots, inflight, submitted, targets)
                for abs_path, rel in _walk(root, ex):
                    try:
                        self._backup_one(sid, device, str(root), abs_path, rel,
                                         res, ctx)
                    except Exception as exc:
                        res.errors.append(rel + ": " + str(exc))
                    self._reap(inflight, res, failed, block=False)
                    if progress:
                        progress(rel, res)
                self._reap(inflight, res, failed, block=True)
            if failed:
                self._discard_unbacked(sid, failed, res)
            self.m.finish_snapshot(sid, "complete", res.files, res.total_size,
                                   res.bytes_uploaded)
        except BaseException:
            self.m.finish_snapshot(sid, "failed", res.files, res.total_size,
                                   res.bytes_uploaded)
            raise
        res.warnings.extend(self._drain_warnings())
        res.seconds = time.time() - t0
        return res

    def _backup_one(self, sid: int, device: str, root: str, abs_path: Path,
                    rel: str, res: BackupResult, ctx) -> None:
        pool, slots, inflight, submitted, targets = ctx
        st = abs_path.lstat()
        if stat.S_ISLNK(st.st_mode):
            self.m.add_file(sid, rel, st.st_mode, 0, st.st_mtime_ns, None,
                            os.readlink(abs_path))
            res.files += 1
            return

        prior = self.m.prior_file(device, root, rel)
        if (prior is not None and prior["size"] == st.st_size
                and prior["mtime_ns"] == st.st_mtime_ns
                and prior["content_hash"] is not None):
            fid = self.m.add_file(sid, rel, st.st_mode, st.st_size,
                                  st.st_mtime_ns, prior["content_hash"])
            n = self.m.copy_file_chunks(prior["id"], fid)
            res.files += 1
            res.unchanged_files += 1
            res.total_size += st.st_size
            res.chunks_seen += n
            self.m.commit()
            return

        whole = hashlib.sha256()
        rows: List[Tuple[int, bytes, int]] = []
        offset = 0
        with open(abs_path, "rb") as fh:
            stream = chunker.chunk_stream(fh, **self.chunk_params)
            for idx, piece in enumerate(stream):
                whole.update(piece)
                h = sha256(piece)
                res.chunks_seen += 1
                if h not in submitted and not self.m.chunk_locations(h):
                    enc = self.k.seal(h, piece)
                    # The chunk row goes in now so file_chunk's foreign key is
                    # satisfied; chunk_location follows when the upload lands.
                    # Dedup keys off chunk_location, so a chunk recorded here
                    # but never stored is simply re-uploaded next time.
                    self.m.record_chunk(h, len(piece), len(enc))
                    submitted.add(h)
                    slots.acquire()
                    fut = pool.submit(self._upload, h, enc, targets)
                    fut.add_done_callback(lambda _f: slots.release())
                    inflight.append((h, fut))
                rows.append((idx, h, offset))
                offset += len(piece)

        fid = self.m.add_file(sid, rel, st.st_mode, st.st_size,
                              st.st_mtime_ns, whole.digest())
        self.m.add_file_chunks(fid, rows)
        res.files += 1
        res.total_size += st.st_size
        self.m.commit()

    def _upload(self, h: bytes, enc: bytes,
                targets: Sequence[Backend]) -> _Upload:
        """Worker thread. Touches no engine state except the driver cache."""
        up = _Upload(enc_size=len(enc))
        blob_id = self.k.blob_id(h)
        for b in targets:
            if b.id in self._dead:
                continue
            try:
                up.locations.append((b.id, self.driver(b).put(blob_id, enc)))
            except BackendUnavailable as exc:
                up.dead.append((b.id, str(exc)))
                up.error = str(exc)
            except Exception as exc:
                up.error = str(exc)
        return up

    def _reap(self, inflight: List[Tuple[bytes, Future]], res: BackupResult,
              failed: Dict[bytes, str], block: bool) -> None:
        """Main thread: fold finished uploads into the manifest."""
        still: List[Tuple[bytes, Future]] = []
        for h, fut in inflight:
            if not block and not fut.done():
                still.append((h, fut))
                continue
            try:
                up = fut.result()
            except Exception as exc:
                failed[h] = str(exc)
                res.errors.append("chunk " + h.hex()[:12] + ": " + str(exc))
                continue
            for bid, why in up.dead:
                if bid not in self._dead:
                    self._dead[bid] = why
                    name = self._backend_names([bid])
                    res.errors.append("backend "
                                      + (name[0] if name else str(bid))
                                      + " unavailable: " + why)
            if up.locations:
                for bid, loc in up.locations:
                    self.m.record_location(h, bid, loc, up.enc_size)
                res.chunks_new += 1
                res.bytes_uploaded += up.enc_size * len(up.locations)
            else:
                failed[h] = up.error or "no backend accepted the chunk"
                res.errors.append("chunk " + h.hex()[:12] + ": " + failed[h])
        inflight[:] = still
        if block:
            self.m.commit()

    def _discard_unbacked(self, sid: int, failed: Dict[bytes, str],
                          res: BackupResult) -> None:
        for h, why in failed.items():
            for path, size in self.m.drop_files_with_chunk(sid, h):
                res.files -= 1
                res.total_size -= size
                res.errors.append(
                    path + ": NOT backed up, chunk " + h.hex()[:12]
                    + " could not be stored (" + why + ")")

    # -- restore ----------------------------------------------------------
    def resolve_snapshot(self, snapshot_id: Optional[int] = None,
                         device: Optional[str] = None,
                         at: Optional[int] = None) -> Snapshot:
        if snapshot_id is not None:
            snap = self.m.snapshot(snapshot_id)
            if snap is None:
                raise FreeholdError("no snapshot " + str(snapshot_id))
            return snap
        snap = self.m.latest_snapshot(device, before=at)
        if snap is None:
            raise FreeholdError("no complete snapshot matches")
        return snap

    def restore(self, dest: Path, snapshot_id: Optional[int] = None,
                device: Optional[str] = None, at: Optional[int] = None,
                paths: Optional[Sequence[str]] = None,
                overwrite: bool = False, progress=None) -> RestoreResult:
        snap = self.resolve_snapshot(snapshot_id, device, at)
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        res = RestoreResult(snapshot_id=snap.id)
        files = self.m.files(snap.id, paths)
        cache: Dict[bytes, bytes] = {}
        order: deque = deque()
        budget = self.workers * 2

        with ThreadPoolExecutor(max_workers=self.workers,
                                thread_name_prefix="fh-get") as pool:
            futures: Dict[bytes, Future] = {}
            tried: Dict[bytes, List[str]] = {}
            window: deque = deque()
            i = 0
            while i < len(files) or window:
                # Always admit at least one file, or an exhausted budget with
                # an empty window would deadlock the drain.
                while i < len(files) and (not window
                                          or (len(window) <= self.workers
                                              and len(futures) < budget)):
                    f = files[i]
                    i += 1
                    plan: List[bytes] = []
                    if f.symlink_target is None:
                        plan = [h for h, _ in self.m.file_chunks(f.id)]
                        for h in plan:
                            if h in futures or h in cache:
                                continue
                            locs = self.m.chunk_locations(h)
                            tried[h] = self._backend_names(
                                b for b, _ in locs)
                            futures[h] = pool.submit(self._fetch, h, locs)
                    window.append((f, plan))
                f, plan = window.popleft()
                self._finish_file(f, plan, futures, tried, cache, order, dest,
                                  overwrite, res, progress)

        res.unreachable_backends = self._backend_names(self._dead)
        res.served_by = sorted(self._backend_names(self._served))
        res.seconds = time.time() - t0
        self.m.commit()
        return res

    def _fetch(self, h: bytes, locations: Sequence[Tuple[int, str]]) -> _Fetch:
        """Worker thread: get a chunk from the first backend that answers."""
        out = _Fetch()
        if not locations:
            out.error = "chunk has no recorded location in any backend"
            return out
        for bid, loc in locations:
            if bid in self._dead:
                continue
            b = self.m.backend_by_id(bid)
            if b is None:
                continue
            try:
                out.piece = self.k.unseal(h, self.driver(b).get(loc))
                out.from_backend = bid
                self._served.add(bid)
                return out
            except BackendUnavailable as exc:
                out.dead.append((bid, str(exc)))
                out.error = str(exc)
            except (ChunkNotFound, IntegrityError) as exc:
                out.error = str(exc)
            except Exception as exc:
                out.error = str(exc)
        if out.error is None:
            out.error = "every backend holding this chunk is unreachable"
        return out

    def _finish_file(self, f, plan: Sequence[bytes],
                     futures: Dict[bytes, Future],
                     tried: Dict[bytes, List[str]],
                     cache: Dict[bytes, bytes], order: deque, dest: Path,
                     overwrite: bool, res: RestoreResult, progress) -> None:
        """Stream one file's chunks to a temp file, hashing as we go, and only
        move it into place once the hash matches what the manifest recorded.

        Chunks are written out as they arrive rather than joined in memory, so
        restoring a 10 GB file does not need 10 GB of RAM.
        """
        target = dest / PurePosixPath(f.path)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            res.failed.append(RestoreFailure(f.path, str(exc)))
            _abandon(plan, futures)
            return
        if f.symlink_target is not None:
            self._place_symlink(f, target, overwrite, res)
            return

        final = target
        if target.exists() and not overwrite:
            final = self._unique(target)
            res.renamed[str(target)] = str(final)

        fresh: Dict[bytes, bytes] = {}
        digest = hashlib.sha256()
        written = 0
        failure: Optional[Tuple[bytes, str]] = None
        tmp: Optional[str] = None
        try:
            fd, tmp = tempfile.mkstemp(dir=str(final.parent), prefix=".fh-")
            with os.fdopen(fd, "wb") as out:
                for h in plan:
                    piece = fresh.get(h)
                    if piece is None:
                        piece = cache.get(h)
                    if piece is None:
                        got = futures.pop(h, None)
                        result = (got.result() if got is not None
                                  else self._fetch(
                                      h, self.m.chunk_locations(h)))
                        for bid, why in result.dead:
                            self._dead.setdefault(bid, why)
                        if result.piece is None:
                            failure = (h, result.error
                                       or "chunk unrecoverable")
                            break
                        if result.from_backend is not None:
                            self.m.mark_verified(h, result.from_backend)
                        piece = result.piece
                        fresh[h] = piece
                    out.write(piece)
                    digest.update(piece)
                    written += len(piece)

            if failure is not None:
                h, why = failure
                res.failed.append(RestoreFailure(f.path, why, h.hex(),
                                                 tried.get(h, ())))
                return
            if f.content_hash and digest.digest() != f.content_hash:
                res.failed.append(RestoreFailure(
                    f.path,
                    "reassembled file does not match its recorded hash"))
                return
            if f.size and written != f.size:
                res.failed.append(RestoreFailure(
                    f.path, "reassembled size " + str(written)
                    + " != recorded " + str(f.size)))
                return
            os.replace(tmp, final)
            tmp = None
            try:
                os.utime(final, ns=(f.mtime_ns, f.mtime_ns))
            except OSError:
                pass
            res.restored.append(f.path)
            res.bytes_written += written
            if progress:
                progress(f.path, final)
        except Exception as exc:
            res.failed.append(RestoreFailure(f.path, str(exc)))
        finally:
            _abandon(plan, futures)
            for h, piece in fresh.items():
                if h not in cache:
                    cache[h] = piece
                    order.append(h)
            while len(order) > CACHE_CHUNKS:
                cache.pop(order.popleft(), None)
            if tmp is not None and os.path.exists(tmp):
                os.unlink(tmp)

    # -- placement --------------------------------------------------------
    def _unique(self, target: Path) -> Path:
        n = 1
        while True:
            cand = target.with_name(
                target.name + ".freehold-restored-" + str(n))
            if not cand.exists():
                return cand
            n += 1

    def _place_symlink(self, f, target: Path, overwrite: bool,
                       res: RestoreResult) -> None:
        final = target
        if target.exists() or target.is_symlink():
            if overwrite:
                target.unlink()
            else:
                final = self._unique(target)
                res.renamed[str(target)] = str(final)
        try:
            os.symlink(f.symlink_target, final)
            res.restored.append(f.path)
        except OSError as exc:
            res.failed.append(RestoreFailure(f.path, "symlink: " + str(exc)))

    # -- replication ------------------------------------------------------
    def replicate(self, target: str, progress=None) -> Dict[str, int]:
        """Copy every chunk the manifest knows about into `target`.

        Dedup is global, so backing up again with a new backend selected
        uploads nothing: the chunks already exist somewhere. That is right for
        backups and wrong for migration, which is what this is for. Adding a
        backend and replicating into it is what makes dropping the old one
        possible -- without this, vendor independence is theoretical.

        Chunks are read from whichever existing backend answers, decrypted to
        verify them, then re-sealed with a fresh nonce for the target. The
        plaintext never leaves this process.
        """
        dest = self.m.backend_by_name(target)
        if dest is None:
            raise FreeholdError("no backend called " + target)
        tally = {"already": 0, "copied": 0, "bytes": 0, "failed": 0}
        rows = self.m.db.execute(
            "SELECT hash FROM chunk ORDER BY created_at").fetchall()
        for row in rows:
            h = row["hash"]
            locations = self.m.chunk_locations(h)
            if any(bid == dest.id for bid, _ in locations):
                tally["already"] += 1
                continue
            sources = [(bid, loc) for bid, loc in locations
                       if bid != dest.id]
            got = self._fetch(h, sources)
            if got.piece is None:
                tally["failed"] += 1
                if progress:
                    progress(h, tally, got.error)
                continue
            enc = self.k.seal(h, got.piece)
            try:
                location = self.driver(dest).put(self.k.blob_id(h), enc)
            except Exception:
                tally["failed"] += 1
                if progress:
                    progress(h, tally, "could not write to " + target)
                continue
            self.m.record_chunk(h, len(got.piece), len(enc))
            self.m.record_location(h, dest.id, location, len(enc))
            tally["copied"] += 1
            tally["bytes"] += len(enc)
            if progress:
                progress(h, tally, None)
        self.m.commit()
        return tally

    # -- verify -----------------------------------------------------------
    def verify(self, snapshot_id: Optional[int] = None,
               device: Optional[str] = None, deep: bool = False) -> List[str]:
        """Check that every chunk a snapshot needs is reachable. With deep,
        actually fetch and authenticate each one."""
        snap = self.resolve_snapshot(snapshot_id, device)
        problems: List[str] = []
        needed: Dict[bytes, str] = {}
        for f in self.m.files(snap.id):
            for h, _ in self.m.file_chunks(f.id):
                needed.setdefault(h, f.path)

        work: List[Tuple[bytes, List[Tuple[int, str]]]] = []
        for h, path in needed.items():
            locs = self.m.chunk_locations(h)
            if not locs:
                problems.append(h.hex()[:12] + " missing (needed by "
                                + path + ")")
            elif deep:
                work.append((h, locs))
        if work:
            with ThreadPoolExecutor(max_workers=self.workers,
                                    thread_name_prefix="fh-vfy") as pool:
                futures = {h: pool.submit(self._fetch, h, locs)
                           for h, locs in work}
                for h, fut in futures.items():
                    got = fut.result()
                    for bid, why in got.dead:
                        self._dead.setdefault(bid, why)
                    if got.piece is None:
                        problems.append(
                            h.hex()[:12] + " unreadable (needed by "
                            + needed[h] + "): " + (got.error or "unknown"))
                    elif got.from_backend is not None:
                        self.m.mark_verified(h, got.from_backend)
        self.m.commit()
        return problems


def _abandon(plan: Sequence[bytes], futures: Dict[bytes, Future]) -> None:
    """Let go of this file's outstanding transfers so the window can advance.
    Any that were already running finish on their own and are discarded; a
    later file needing the same chunk simply asks for it again."""
    for h in plan:
        futures.pop(h, None)


def _walk(root: Path, excludes: Sequence[str]):
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = PurePosixPath(Path(dirpath).relative_to(root).as_posix())
        dirnames[:] = sorted(
            d for d in dirnames
            if not _excluded(str(rel_dir / d).lstrip("."), d, excludes))
        for name in sorted(filenames):
            rel = (Path(dirpath) / name).relative_to(root).as_posix()
            if _excluded(rel, name, excludes):
                continue
            yield Path(dirpath) / name, rel


def _excluded(rel: str, name: str, excludes: Sequence[str]) -> bool:
    for pat in excludes:
        if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel, pat):
            return True
        if rel == pat or rel.startswith(pat.rstrip("/") + "/"):
            return True
    return False
