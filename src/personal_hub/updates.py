"""Signed, Hub-specific update verification, staging, apply, and rollback boundaries.

This module performs no network I/O. A release client may download an update through a
separately reviewed HTTPS transport, but the bytes cannot be staged or applied until this
boundary verifies the exact Personal Hub channel, detached Ed25519 signature, size, and
SHA-256 digest. Local Assistant update code and trust roots are intentionally not reused.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from personal_hub.config import HUB_UPDATE_CHANNEL
from personal_hub.storage.database import HubDatabase

UPDATE_PRODUCT_ID: Final[str] = "personal-hub"
MAX_MANIFEST_BYTES: Final[int] = 64 * 1024
MAX_UPDATE_BYTES: Final[int] = 512 * 1024 * 1024
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_KEY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_VERSION = re.compile(
    r"(?P<major>0|[1-9][0-9]*)\.(?P<minor>0|[1-9][0-9]*)\.(?P<patch>0|[1-9][0-9]*)\Z"
)


class HubUpdateError(RuntimeError):
    """Fixed-message update failure that never includes manifest or path content."""


class HubUpdateArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    filename: Literal["PersonalHub.exe"] = "PersonalHub.exe"
    url: str = Field(min_length=1, max_length=2_048)
    size_bytes: int = Field(ge=1, le=MAX_UPDATE_BYTES)
    sha256: str = Field(min_length=64, max_length=64)

    @field_validator("url")
    @classmethod
    def _https_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme.casefold() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError("update artifact URL must be credential-free HTTPS")
        return value

    @field_validator("sha256")
    @classmethod
    def _digest(cls, value: str) -> str:
        normalized = value.casefold()
        if _HEX_DIGEST.fullmatch(normalized) is None:
            raise ValueError("update artifact digest is invalid")
        return normalized


class HubUpdateManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal[1] = 1
    product: Literal["personal-hub"] = UPDATE_PRODUCT_ID
    channel: Literal["personal-hub-stable"] = HUB_UPDATE_CHANNEL
    version: str = Field(min_length=5, max_length=32)
    published_at: datetime
    minimum_schema_version: int = Field(ge=1, le=HubDatabase.CURRENT_SCHEMA_VERSION)
    artifact: HubUpdateArtifact
    key_id: str = Field(min_length=1, max_length=128)
    signature: str = Field(min_length=40, max_length=256)

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        if _VERSION.fullmatch(value) is None:
            raise ValueError("update version must be semantic major.minor.patch")
        return value

    @field_validator("published_at")
    @classmethod
    def _published(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("update timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("key_id")
    @classmethod
    def _key_id(cls, value: str) -> str:
        if _KEY_ID.fullmatch(value) is None:
            raise ValueError("update key identity is invalid")
        return value

    @field_validator("signature")
    @classmethod
    def _signature(cls, value: str) -> str:
        try:
            decoded = base64.b64decode(value, validate=True)
        except (ValueError, TypeError) as error:
            raise ValueError("update signature is invalid") from error
        if len(decoded) != 64:
            raise ValueError("update signature is invalid")
        return value

    @model_validator(mode="after")
    def _schema_is_compatible(self) -> HubUpdateManifest:
        if self.minimum_schema_version > HubDatabase.CURRENT_SCHEMA_VERSION:
            raise ValueError("update requires a newer database schema")
        return self

    def signed_payload(self) -> bytes:
        value = self.model_dump(mode="json", exclude={"signature"})
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = _VERSION.fullmatch(value)
    if match is None:
        raise HubUpdateError("The installed Personal Hub version is invalid.")
    return tuple(int(match.group(name)) for name in ("major", "minor", "patch"))


def parse_and_verify_manifest(
    content: bytes | str,
    trusted_keys: Mapping[str, bytes | Ed25519PublicKey],
    *,
    current_version: str,
) -> HubUpdateManifest:
    """Validate a bounded manifest and its signature before trusting any artifact field."""

    encoded = content.encode() if isinstance(content, str) else content
    if not isinstance(encoded, bytes) or not encoded or len(encoded) > MAX_MANIFEST_BYTES:
        raise HubUpdateError("The Personal Hub update manifest is invalid.")
    try:
        raw = json.loads(encoded)
        manifest = HubUpdateManifest.model_validate(raw)
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise HubUpdateError("The Personal Hub update manifest is invalid.") from error
    if _version_tuple(manifest.version) <= _version_tuple(current_version):
        raise HubUpdateError("The Personal Hub update is not newer than this installation.")
    key = trusted_keys.get(manifest.key_id)
    if key is None:
        raise HubUpdateError("The Personal Hub update signing key is not trusted.")
    try:
        public_key = (
            key if isinstance(key, Ed25519PublicKey) else Ed25519PublicKey.from_public_bytes(key)
        )
        public_key.verify(base64.b64decode(manifest.signature), manifest.signed_payload())
    except (InvalidSignature, TypeError, ValueError) as error:
        raise HubUpdateError("The Personal Hub update signature is invalid.") from error
    return manifest


def verify_staged_artifact(path: Path, artifact: HubUpdateArtifact) -> Path:
    """Verify exact staged executable bytes; never follow a caller-supplied directory tree."""

    selected = path.resolve(strict=True)
    if not selected.is_file() or selected.name != artifact.filename:
        raise HubUpdateError("The staged Personal Hub update is invalid.")
    try:
        size = selected.stat().st_size
        if size != artifact.size_bytes:
            raise HubUpdateError("The staged Personal Hub update is invalid.")
        digest = hashlib.sha256(selected.read_bytes()).hexdigest()
    except OSError as error:
        raise HubUpdateError("The staged Personal Hub update could not be read.") from error
    if digest != artifact.sha256:
        raise HubUpdateError("The staged Personal Hub update is invalid.")
    return selected


def stage_verified_artifact(
    source: Path,
    manifest: HubUpdateManifest,
    staging_directory: Path,
) -> Path:
    """Copy verified bytes into a newly created Hub-owned staging directory."""

    verified = verify_staged_artifact(source, manifest.artifact)
    root = staging_directory.resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    destination_directory = Path(tempfile.mkdtemp(prefix="personal-hub-update-", dir=root))
    destination = destination_directory / "PersonalHub.exe"
    try:
        shutil.copyfile(verified, destination)
        return verify_staged_artifact(destination, manifest.artifact)
    except BaseException:
        shutil.rmtree(destination_directory, ignore_errors=True)
        raise


def apply_staged_update(staged: Path, installed: Path, backup_directory: Path) -> Path:
    """Atomically replace one installed Hub executable and retain a rollback copy."""

    source = staged.resolve(strict=True)
    target = installed.resolve(strict=True)
    if source.name != "PersonalHub.exe" or target.name != "PersonalHub.exe":
        raise HubUpdateError("The Personal Hub update paths are invalid.")
    backup_root = backup_directory.resolve(strict=False)
    backup_root.mkdir(parents=True, exist_ok=True)
    backup = backup_root / "PersonalHub.previous.exe"
    temporary = target.parent / "PersonalHub.update.tmp"
    try:
        shutil.copyfile(target, backup)
        shutil.copyfile(source, temporary)
        os.replace(temporary, target)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        if backup.is_file():
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
                rollback_temp = Path(handle.name)
            try:
                shutil.copyfile(backup, rollback_temp)
                os.replace(rollback_temp, target)
            finally:
                rollback_temp.unlink(missing_ok=True)
        raise HubUpdateError("The Personal Hub update could not be applied safely.") from error
    return backup


def rollback_update(backup: Path, installed: Path) -> None:
    """Atomically restore the exact prior Hub executable retained by apply."""

    source = backup.resolve(strict=True)
    target = installed.resolve(strict=True)
    if source.name != "PersonalHub.previous.exe" or target.name != "PersonalHub.exe":
        raise HubUpdateError("The Personal Hub rollback paths are invalid.")
    temporary = target.parent / "PersonalHub.rollback.tmp"
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, target)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise HubUpdateError("The Personal Hub rollback could not be applied safely.") from error


__all__ = [
    "HubUpdateArtifact",
    "HubUpdateError",
    "HubUpdateManifest",
    "MAX_MANIFEST_BYTES",
    "MAX_UPDATE_BYTES",
    "UPDATE_PRODUCT_ID",
    "apply_staged_update",
    "parse_and_verify_manifest",
    "rollback_update",
    "stage_verified_artifact",
    "verify_staged_artifact",
]
