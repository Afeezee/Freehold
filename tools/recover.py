#!/usr/bin/env python3
"""Standalone Freehold recovery tool.

Deliberately independent of the freehold package: it is one file, reads the
manifest with stdlib sqlite3, and uses `cryptography` only for AES-256-GCM and
HKDF. If Freehold itself ever disappears, this script plus RECOVERY.md is
enough to get the data back.

    python recover.py --manifest manifest.db --key key.json \
                      --blobs ./downloaded_blobs --out ./restored

--blobs is any directory tree containing the encrypted blobs, named by their
blob_id (which is how every driver names them). Dump a GitHub release's assets
or an S3 bucket into a folder and point this at it.
"""
import argparse
import gzip
import hashlib
import hmac
import json
import os
import sqlite3
import sys

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt


def derive(master, info):
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"freehold",
                info=info).derive(master)


def load_key(path, passphrase=None):
    doc = json.load(open(path))
    if not doc.get("wrapped"):
        return bytes.fromhex(doc["master"])
    k = doc["kdf"]
    kek = Scrypt(salt=bytes.fromhex(k["salt"]), length=32, n=k["n"], r=k["r"],
                 p=k["p"]).derive(passphrase.encode())
    return AESGCM(kek).decrypt(bytes.fromhex(doc["nonce"]),
                               bytes.fromhex(doc["ciphertext"]),
                               b"freehold-key")


def index_blobs(root):
    found = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            found.setdefault(name, os.path.join(dirpath, name))
    return found


MANIFEST_LABEL = b"freehold:manifest"
MANIFEST_MAGIC = b"FHM1"


def recover_manifest(blob_path, enc_key, out_db):
    """Decrypt the manifest blob: FHM1 || nonce(12) || GCM ciphertext+tag,
    AAD = b"freehold:manifest", plaintext is a gzipped SQLite file."""
    blob = open(blob_path, "rb").read()
    if not blob.startswith(MANIFEST_MAGIC):
        sys.exit("that file is not a Freehold manifest blob (no FHM1 magic)")
    body = blob[len(MANIFEST_MAGIC):]
    try:
        plain = AESGCM(enc_key).decrypt(body[:12], body[12:], MANIFEST_LABEL)
    except Exception as exc:
        sys.exit("manifest failed to decrypt (wrong key?): " + str(exc))
    with open(out_db, "wb") as fh:
        fh.write(gzip.decompress(plain))
    print("wrote " + out_db)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest")
    ap.add_argument("--key", required=True)
    ap.add_argument("--passphrase")
    ap.add_argument("--blobs", help="directory tree holding the blobs")
    ap.add_argument("--out", help="where to write recovered files")
    ap.add_argument("--manifest-id", action="store_true",
                    help="print the blob id the manifest is stored under")
    ap.add_argument("--manifest-blob",
                    help="decrypt this manifest blob instead of restoring")
    ap.add_argument("--out-db", default="manifest.db",
                    help="where to write the recovered manifest")
    ap.add_argument("--snapshot", type=int,
                    help="snapshot id (default: the newest complete one)")
    ap.add_argument("--device")
    args = ap.parse_args()

    master = load_key(args.key, args.passphrase)
    enc_key, id_key = derive(master, b"freehold:enc"), derive(master,
                                                              b"freehold:id")

    if args.manifest_id:
        print(hmac.new(id_key, MANIFEST_LABEL, hashlib.sha256).hexdigest())
        return
    if args.manifest_blob:
        recover_manifest(args.manifest_blob, enc_key, args.out_db)
        return
    if not (args.manifest and args.blobs and args.out):
        sys.exit("need --manifest, --blobs and --out to restore files "
                 "(or --manifest-blob / --manifest-id)")

    aes = AESGCM(enc_key)
    blobs = index_blobs(args.blobs)

    db = sqlite3.connect(args.manifest)
    db.row_factory = sqlite3.Row
    if args.snapshot:
        sid = args.snapshot
    else:
        q = ("SELECT s.id FROM snapshot s JOIN device d ON d.id=s.device_id "
             "WHERE s.status='complete'")
        a = []
        if args.device:
            q += " AND d.name=?"
            a.append(args.device)
        row = db.execute(q + " ORDER BY s.started_at DESC LIMIT 1", a
                         ).fetchone()
        if row is None:
            sys.exit("no complete snapshot in this manifest")
        sid = row["id"]

    failures = 0
    for f in db.execute("SELECT * FROM file WHERE snapshot_id=? ORDER BY path",
                        (sid,)):
        dest = os.path.join(args.out, *f["path"].split("/"))
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        if f["symlink_target"]:
            continue
        parts, broken = [], None
        for c in db.execute("SELECT chunk_hash FROM file_chunk WHERE "
                            "file_id=? ORDER BY idx", (f["id"],)):
            h = c["chunk_hash"]
            blob_id = hmac.new(id_key, h, hashlib.sha256).hexdigest()
            path = blobs.get(blob_id)
            if path is None:
                broken = "missing blob " + blob_id
                break
            raw = open(path, "rb").read()
            try:
                piece = aes.decrypt(raw[:12], raw[12:], h)
            except Exception as exc:
                broken = "chunk " + h.hex()[:12] + " failed: " + str(exc)
                break
            if hashlib.sha256(piece).digest() != h:
                broken = "chunk " + h.hex()[:12] + " hash mismatch"
                break
            parts.append(piece)
        if broken:
            print("SKIP " + f["path"] + ": " + broken, file=sys.stderr)
            failures += 1
            continue
        data = b"".join(parts)
        if f["content_hash"] and hashlib.sha256(data).digest() != f["content_hash"]:
            print("SKIP " + f["path"] + ": file hash mismatch", file=sys.stderr)
            failures += 1
            continue
        with open(dest, "wb") as out:
            out.write(data)
        print(f["path"])
    sys.exit(2 if failures else 0)


if __name__ == "__main__":
    main()
