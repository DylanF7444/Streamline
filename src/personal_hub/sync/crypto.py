"""Passphrase encryption and explicit unencrypted Personal Hub sync bundles."""

from __future__ import annotations

import base64
import json
import os
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from personal_hub.sync.contracts import SyncSnapshot
from personal_hub.sync.relay_contracts import (
    PROVIDER_RELAY_MAX_BYTES,
    ProviderRelaySnapshot,
)

SYNC_BUNDLE_FORMAT: Final[str] = "personal-hub-sync-bundle-v1"
PROVIDER_RELAY_BUNDLE_FORMAT: Final[str] = "personal-hub-provider-relay-bundle-v1"
SYNC_BUNDLE_MAX_BYTES: Final[int] = 20 * 1024 * 1024
MIN_PASSPHRASE_LENGTH: Final[int] = 12
_PLAIN_SYNC_FORMAT: Final[str] = "personal-hub-sync-unencrypted-v1"
_PLAIN_RELAY_FORMAT: Final[str] = "personal-hub-provider-relay-unencrypted-v1"
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


def validate_sync_passphrase(passphrase: str | None) -> None:
    """Validate before applying data; None explicitly selects unencrypted sync."""
    if passphrase is not None:
        _require_passphrase(passphrase)


def _unencrypted_bundle(payload: bytes, format_tag: str) -> bytes:
    encoded = json.dumps(
        {"format": format_tag, "payload": json.loads(payload)},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    if len(encoded) > SYNC_BUNDLE_MAX_BYTES:
        raise SyncBundleError("Unencrypted sync bundle is too large.")
    return encoded


def _unencrypted_payload(envelope: object, format_tag: str) -> bytes:
    if not isinstance(envelope, dict) or envelope.get("format") != format_tag:
        raise SyncBundleError(
            "Sync protection does not match this folder. Use the same passphrase setting "
            "on every device, or choose a new shared folder to change protection."
        )
    if set(envelope) != {"format", "payload"} or not isinstance(envelope["payload"], dict):
        raise SyncBundleError("Unencrypted sync bundle envelope is invalid.")
    return json.dumps(envelope["payload"], ensure_ascii=True).encode("ascii")


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


def encrypt_snapshot(snapshot: SyncSnapshot, passphrase: str | None) -> bytes:
    """Encrypt a snapshot, or explicitly encode without encryption for None."""

    if not isinstance(snapshot, SyncSnapshot):
        raise TypeError("encrypt_snapshot expects a SyncSnapshot")
    if passphrase is None:
        return _unencrypted_bundle(_plaintext(snapshot), _PLAIN_SYNC_FORMAT)
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


def decrypt_snapshot(bundle: bytes, passphrase: str | None) -> SyncSnapshot:
    """Decode the selected protection mode; never silently downgrade encryption."""

    if not isinstance(bundle, bytes) or not 1 <= len(bundle) <= SYNC_BUNDLE_MAX_BYTES:
        raise SyncBundleError("Sync bundle size is invalid.")
    try:
        envelope = json.loads(bundle.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SyncBundleError("Sync bundle is not valid.") from error
    if passphrase is None:
        payload = _unencrypted_payload(envelope, _PLAIN_SYNC_FORMAT)
        try:
            return SyncSnapshot.model_validate_json(payload)
        except (TypeError, ValueError) as error:
            raise SyncBundleError("Unencrypted sync snapshot is invalid.") from error
    if isinstance(envelope, dict) and envelope.get("format") == _PLAIN_SYNC_FORMAT:
        raise SyncBundleError(
            "This folder uses no passphrase. Select No passphrase on every device, "
            "or choose a new shared folder to enable encryption."
        )
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


def encrypt_provider_relay(snapshot: ProviderRelaySnapshot, passphrase: str | None) -> bytes:
    """Encode a validated provider relay with a distinct format for each mode."""
    if not isinstance(snapshot, ProviderRelaySnapshot):
        raise TypeError("encrypt_provider_relay expects a ProviderRelaySnapshot")
    payload = json.dumps(
        snapshot.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > min(PROVIDER_RELAY_MAX_BYTES, SYNC_BUNDLE_MAX_BYTES):
        raise SyncBundleError("Provider relay snapshot is too large to publish.")
    if passphrase is None:
        return _unencrypted_bundle(payload, _PLAIN_RELAY_FORMAT)
    salt = os.urandom(_SALT_BYTES)
    nonce = os.urandom(_NONCE_BYTES)
    aad = PROVIDER_RELAY_BUNDLE_FORMAT.encode("ascii")
    key = _derive_key(passphrase, salt)
    ciphertext = AESGCM(key).encrypt(nonce, payload, aad)
    envelope = {
        "format": PROVIDER_RELAY_BUNDLE_FORMAT,
        "kdf": "scrypt",
        "cipher": "aes-256-gcm",
        "salt": _encode(salt),
        "nonce": _encode(nonce),
        "ciphertext": _encode(ciphertext),
    }
    encoded = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("ascii")
    if len(encoded) > SYNC_BUNDLE_MAX_BYTES:
        raise SyncBundleError("Encrypted provider relay snapshot is too large.")
    return encoded


def decrypt_provider_relay(bundle: bytes, passphrase: str | None) -> ProviderRelaySnapshot:
    """Decode one provider relay envelope in the explicitly selected mode."""
    if not isinstance(bundle, bytes) or not 1 <= len(bundle) <= SYNC_BUNDLE_MAX_BYTES:
        raise SyncBundleError("Provider relay bundle size is invalid.")
    try:
        envelope = json.loads(bundle.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SyncBundleError("Provider relay bundle is not valid.") from error
    if passphrase is None:
        payload = _unencrypted_payload(envelope, _PLAIN_RELAY_FORMAT)
        if len(payload) > min(PROVIDER_RELAY_MAX_BYTES, SYNC_BUNDLE_MAX_BYTES):
            raise SyncBundleError("Unencrypted provider relay snapshot is too large.")
        try:
            return ProviderRelaySnapshot.model_validate_json(payload)
        except (TypeError, ValueError) as error:
            raise SyncBundleError("Unencrypted provider relay snapshot is invalid.") from error
    if isinstance(envelope, dict) and envelope.get("format") == _PLAIN_RELAY_FORMAT:
        raise SyncBundleError(
            "This provider relay uses no passphrase. Select No passphrase on every device."
        )
    if not isinstance(envelope, dict) or set(envelope) != {
        "format",
        "kdf",
        "cipher",
        "salt",
        "nonce",
        "ciphertext",
    }:
        raise SyncBundleError("Provider relay bundle envelope is invalid.")
    if (
        envelope.get("format") != PROVIDER_RELAY_BUNDLE_FORMAT
        or envelope.get("kdf") != "scrypt"
        or envelope.get("cipher") != "aes-256-gcm"
    ):
        raise SyncBundleError("Provider relay bundle version is not supported.")
    salt = _decode(envelope["salt"], name="salt", length=_SALT_BYTES)
    nonce = _decode(envelope["nonce"], name="nonce", length=_NONCE_BYTES)
    ciphertext = _decode(envelope["ciphertext"], name="ciphertext")
    try:
        key = _derive_key(passphrase, salt)
        plaintext = AESGCM(key).decrypt(
            nonce,
            ciphertext,
            PROVIDER_RELAY_BUNDLE_FORMAT.encode("ascii"),
        )
    except (InvalidTag, SyncPassphraseError) as error:
        raise SyncPassphraseError(
            "Sync passphrase was rejected or the provider relay bundle was changed."
        ) from error
    if len(plaintext) > min(PROVIDER_RELAY_MAX_BYTES, SYNC_BUNDLE_MAX_BYTES):
        raise SyncBundleError("Decrypted provider relay snapshot is too large.")
    try:
        return ProviderRelaySnapshot.model_validate_json(plaintext)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise SyncBundleError("Decrypted provider relay snapshot is invalid.") from error


__all__ = [
    "MIN_PASSPHRASE_LENGTH",
    "PROVIDER_RELAY_BUNDLE_FORMAT",
    "SYNC_BUNDLE_FORMAT",
    "SYNC_BUNDLE_MAX_BYTES",
    "SyncBundleError",
    "SyncPassphraseError",
    "decrypt_snapshot",
    "decrypt_provider_relay",
    "encrypt_snapshot",
    "encrypt_provider_relay",
]
