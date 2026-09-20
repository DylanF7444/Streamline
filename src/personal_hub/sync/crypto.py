"""Passphrase encryption for Personal Hub sync shards."""

from __future__ import annotations

import base64
import json
import os
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from personal_hub.sync.contracts import SyncSnapshot

SYNC_BUNDLE_FORMAT: Final[str] = "personal-hub-sync-bundle-v1"
SYNC_BUNDLE_MAX_BYTES: Final[int] = 20 * 1024 * 1024
MIN_PASSPHRASE_LENGTH: Final[int] = 12
_AAD: Final[bytes] = SYNC_BUNDLE_FORMAT.encode("ascii")
_SALT_BYTES: Final[int] = 16
_NONCE_BYTES: Final[int] = 12
_KEY_BYTES: Final[int] = 32
_SCRYPT_N: Final[int] = 2**14
_SCRYPT_R: Final[int] = 8
_SCRYPT_P: Final[int] = 1


class SyncBundleError(ValueError):
    """Raised when a sync bundle is malformed, too large, or cannot decrypt."""


class SyncPassphraseError(SyncBundleError):
    """Raised for an unusable passphrase or an authentication failure."""


def _require_passphrase(passphrase: str) -> bytes:
    if not isinstance(passphrase, str):
        raise SyncPassphraseError("Sync passphrase is required.")
    if len(passphrase) < MIN_PASSPHRASE_LENGTH:
        raise SyncPassphraseError(
            f"Sync passphrase must contain at least {MIN_PASSPHRASE_LENGTH} characters."
        )
    try:
        encoded = passphrase.encode("utf-8")
    except UnicodeEncodeError as error:
        raise SyncPassphraseError("Sync passphrase is invalid.") from error
    if len(encoded) > 4_096:
        raise SyncPassphraseError("Sync passphrase is too long.")
    return encoded


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    return Scrypt(
        salt=salt,
        length=_KEY_BYTES,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
    ).derive(_require_passphrase(passphrase))


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def _decode(value: object, *, name: str, length: int | None = None) -> bytes:
    if not isinstance(value, str) or not value or len(value) > SYNC_BUNDLE_MAX_BYTES:
        raise SyncBundleError("Sync bundle encoding is invalid.")
    try:
        decoded = base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)
    except (ValueError, UnicodeEncodeError) as error:
        raise SyncBundleError("Sync bundle encoding is invalid.") from error
    if length is not None and len(decoded) != length:
        raise SyncBundleError(f"Sync bundle {name} has an invalid length.")
    return decoded


def _plaintext(snapshot: SyncSnapshot) -> bytes:
    payload = json.dumps(
        snapshot.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > SYNC_BUNDLE_MAX_BYTES:
        raise SyncBundleError("Sync snapshot is too large to publish.")
    return payload


def encrypt_snapshot(snapshot: SyncSnapshot, passphrase: str) -> bytes:
    """Return an authenticated encrypted bundle with no plaintext user data."""

    if not isinstance(snapshot, SyncSnapshot):
        raise TypeError("encrypt_snapshot expects a SyncSnapshot")
    salt = os.urandom(_SALT_BYTES)
    nonce = os.urandom(_NONCE_BYTES)
    key = _derive_key(passphrase, salt)
    ciphertext = AESGCM(key).encrypt(nonce, _plaintext(snapshot), _AAD)
    envelope = {
        "format": SYNC_BUNDLE_FORMAT,
        "kdf": "scrypt",
        "cipher": "aes-256-gcm",
        "salt": _encode(salt),
        "nonce": _encode(nonce),
        "ciphertext": _encode(ciphertext),
    }
    encoded = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("ascii")
    if len(encoded) > SYNC_BUNDLE_MAX_BYTES:
        raise SyncBundleError("Encrypted sync bundle is too large.")
    return encoded


def decrypt_snapshot(bundle: bytes, passphrase: str) -> SyncSnapshot:
    """Authenticate and decode one encrypted sync bundle."""

    if not isinstance(bundle, bytes) or not 1 <= len(bundle) <= SYNC_BUNDLE_MAX_BYTES:
        raise SyncBundleError("Sync bundle size is invalid.")
    try:
        envelope = json.loads(bundle.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SyncBundleError("Sync bundle is not valid.") from error
    if not isinstance(envelope, dict) or set(envelope) != {
        "format",
        "kdf",
        "cipher",
        "salt",
        "nonce",
        "ciphertext",
    }:
        raise SyncBundleError("Sync bundle envelope is invalid.")
    if (
        envelope.get("format") != SYNC_BUNDLE_FORMAT
        or envelope.get("kdf") != "scrypt"
        or envelope.get("cipher") != "aes-256-gcm"
    ):
        raise SyncBundleError("Sync bundle version is not supported.")
    salt = _decode(envelope["salt"], name="salt", length=_SALT_BYTES)
    nonce = _decode(envelope["nonce"], name="nonce", length=_NONCE_BYTES)
    ciphertext = _decode(envelope["ciphertext"], name="ciphertext")
    try:
        key = _derive_key(passphrase, salt)
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, _AAD)
    except (InvalidTag, SyncPassphraseError) as error:
        raise SyncPassphraseError(
            "Sync passphrase was rejected or the bundle was changed."
        ) from error
    if len(plaintext) > SYNC_BUNDLE_MAX_BYTES:
        raise SyncBundleError("Decrypted sync snapshot is too large.")
    try:
        return SyncSnapshot.model_validate_json(plaintext)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise SyncBundleError("Decrypted sync snapshot is invalid.") from error


__all__ = [
    "MIN_PASSPHRASE_LENGTH",
    "SYNC_BUNDLE_FORMAT",
    "SYNC_BUNDLE_MAX_BYTES",
    "SyncBundleError",
    "SyncPassphraseError",
    "decrypt_snapshot",
    "encrypt_snapshot",
]
