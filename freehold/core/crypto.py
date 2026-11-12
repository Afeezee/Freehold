"""Client-side crypto. Nothing here knows what a backend is.

Chunk blob layout:  nonce(12) || ciphertext || GCM tag(16)
AAD is the plaintext chunk's SHA-256, binding a blob to its manifest entry.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .errors import ConfigError, IntegrityError

NONCE_LEN = 12
TAG_LEN = 16
KEY_FORMAT = "freehold-key-v1"
HKDF_SALT = b"freehold"
INFO_ENC = b"freehold:enc"
INFO_ID = b"freehold:id"
SCRYPT_N, SCRYPT_R, SCRYPT_P = 1 << 15, 8, 1


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def _hkdf(master: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=HKDF_SALT,
                info=info).derive(master)


@dataclass(frozen=True)
class KeyRing:
    master: bytes

    @property
    def enc(self) -> bytes:
        return _hkdf(self.master, INFO_ENC)

    @property
    def idk(self) -> bytes:
        return _hkdf(self.master, INFO_ID)

    @staticmethod
    def generate() -> "KeyRing":
        return KeyRing(secrets.token_bytes(32))

    # -- addressing -------------------------------------------------------
    def blob_id(self, chunk_hash: bytes) -> str:
        """Backend-visible name for a chunk. Blinded so a backend cannot test
        a candidate plaintext against what it stores."""
        return hmac.new(self.idk, chunk_hash, hashlib.sha256).hexdigest()

    # -- chunk encryption -------------------------------------------------
    def seal(self, chunk_hash: bytes, plaintext: bytes) -> bytes:
        nonce = os.urandom(NONCE_LEN)
        return nonce + AESGCM(self.enc).encrypt(nonce, plaintext, chunk_hash)

    def unseal(self, chunk_hash: bytes, blob: bytes) -> bytes:
        if len(blob) < NONCE_LEN + TAG_LEN:
            raise IntegrityError("blob too short")
        try:
            plaintext = AESGCM(self.enc).decrypt(
                blob[:NONCE_LEN], blob[NONCE_LEN:], chunk_hash)
        except Exception as exc:
            raise IntegrityError("chunk failed authentication") from exc
        if sha256(plaintext) != chunk_hash:
            raise IntegrityError("chunk hash mismatch after decryption")
        return plaintext

    # -- key file ---------------------------------------------------------
    def save(self, path: Path, passphrase: Optional[str] = None) -> None:
        if passphrase:
            salt = os.urandom(16)
            kek = Scrypt(salt=salt, length=32, n=SCRYPT_N, r=SCRYPT_R,
                         p=SCRYPT_P).derive(passphrase.encode())
            nonce = os.urandom(NONCE_LEN)
            doc = {
                "format": KEY_FORMAT,
                "wrapped": True,
                "kdf": {"name": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R,
                        "p": SCRYPT_P, "salt": salt.hex()},
                "nonce": nonce.hex(),
                "ciphertext": AESGCM(kek).encrypt(
                    nonce, self.master, b"freehold-key").hex(),
            }
        else:
            doc = {"format": KEY_FORMAT, "wrapped": False,
                   "master": self.master.hex()}
        Path(path).write_text(json.dumps(doc, indent=2))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    @staticmethod
    def load(path: Path, passphrase: Optional[str] = None) -> "KeyRing":
        doc = json.loads(Path(path).read_text())
        if doc.get("format") != KEY_FORMAT:
            raise ConfigError("unknown key file format")
        if not doc.get("wrapped"):
            return KeyRing(bytes.fromhex(doc["master"]))
        if not passphrase:
            raise ConfigError("key file is passphrase-wrapped")
        k = doc["kdf"]
        kek = Scrypt(salt=bytes.fromhex(k["salt"]), length=32, n=k["n"],
                     r=k["r"], p=k["p"]).derive(passphrase.encode())
        try:
            master = AESGCM(kek).decrypt(bytes.fromhex(doc["nonce"]),
                                         bytes.fromhex(doc["ciphertext"]),
                                         b"freehold-key")
        except Exception as exc:
            raise ConfigError("wrong passphrase") from exc
        return KeyRing(master)
