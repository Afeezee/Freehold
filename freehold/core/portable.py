"""Putting the manifest itself into the backends.

The manifest is the only thing that records file names and chunk order. A
device that dies taking the manifest with it leaves you holding a pile of
authentic, decryptable chunks and no way to know which file any of them
belongs to. So the manifest goes into the same backends, under the same key,
at an address derived from that key:

    blob_id = HMAC-SHA256(K_id, "freehold:manifest")

which means a fresh machine with nothing but the key and the backend
credentials can find it without being told where it is. That is what makes
the lost-device case work.
"""
from __future__ import annotations

import gzip
import sqlite3
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .crypto import NONCE_LEN, KeyRing
from .driver import StorageDriver
from .errors import ConfigError, FreeholdError, IntegrityError
from .manifest import Manifest

MANIFEST_LABEL = b"freehold:manifest"
MAGIC = b"FHM1"


def manifest_blob_id(keyring: KeyRing) -> str:
    """Where the manifest lives in every backend. Derived, never stored."""
    import hashlib
    import hmac
    return hmac.new(keyring.idk, MANIFEST_LABEL, hashlib.sha256).hexdigest()


def export_manifest(manifest: Manifest, keyring: KeyRing) -> bytes:
    """A consistent, compressed, encrypted copy of the manifest file.

    Uses SQLite's own backup API rather than reading the file, so a snapshot
    taken while the database is in use is still coherent.
    """
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "manifest.db"
        target = sqlite3.connect(str(copy))
        try:
            with manifest._lock:
                manifest.db.commit()
                manifest.db.backup(target)
        finally:
            target.close()
        raw = copy.read_bytes()
    body = gzip.compress(raw, 6)
    import os
    nonce = os.urandom(NONCE_LEN)
    sealed = AESGCM(keyring.enc).encrypt(nonce, body, MANIFEST_LABEL)
    return MAGIC + nonce + sealed


def import_manifest(blob: bytes, keyring: KeyRing, dest: Path) -> Path:
    """Decrypt a manifest blob and write it where a Manifest can open it."""
    if not blob.startswith(MAGIC):
        raise IntegrityError("not a Freehold manifest blob")
    payload = blob[len(MAGIC):]
    if len(payload) < NONCE_LEN + 16:
        raise IntegrityError("manifest blob is truncated")
    try:
        body = AESGCM(keyring.enc).decrypt(payload[:NONCE_LEN],
                                           payload[NONCE_LEN:],
                                           MANIFEST_LABEL)
    except Exception as exc:
        raise IntegrityError("manifest failed to decrypt: wrong key?") from exc
    raw = gzip.decompress(body)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)
    return dest


def push_manifest(manifest: Manifest, keyring: KeyRing,
                  drivers: Sequence[Tuple[str, StorageDriver]]
                  ) -> Dict[str, str]:
    """Store the manifest in each given backend. Returns name -> location,
    or name -> "error: ..." for the ones that refused."""
    if not drivers:
        raise FreeholdError("no backends to push the manifest to")
    blob = export_manifest(manifest, keyring)
    blob_id = manifest_blob_id(keyring)
    out: Dict[str, str] = {}
    for name, driver in drivers:
        try:
            out[name] = driver.put(blob_id, blob)
        except Exception as exc:
            out[name] = "error: " + str(exc)
    return out


def pull_manifest(driver: StorageDriver, keyring: KeyRing,
                  dest: Path) -> Path:
    """Find and restore the manifest from one backend, knowing only the key.

    This is the lost-device entry point: no manifest, no config, nothing but
    the key and whatever credentials the driver needs.
    """
    blob_id = manifest_blob_id(keyring)
    location = find_blob(driver, blob_id)
    if location is None:
        raise FreeholdError(
            "no manifest found in this backend. Either it was never pushed "
            "(freehold manifest push), or this is the wrong key.")
    return import_manifest(driver.get(location), keyring, dest)


def find_blob(driver: StorageDriver, blob_id: str) -> Optional[str]:
    """Locate a blob by id without a manifest to look it up in.

    A driver can answer directly if it knows how; otherwise every location it
    lists is checked for the id, which every driver embeds in its locations.
    """
    finder = getattr(driver, "find", None)
    if callable(finder):
        found = finder(blob_id)
        if found:
            return found
    for location in driver.list():
        if blob_id in location:
            return location
    return None
