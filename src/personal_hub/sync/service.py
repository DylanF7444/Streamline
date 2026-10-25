"""Shared-folder sync transport and deterministic merge logic."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from uuid import uuid4

from personal_hub.storage.repositories import SettingsRepository
from personal_hub.sync.contracts import (
    SYNC_SNAPSHOT_FORMAT,
    SyncRecord,
    SyncResult,
    SyncRole,
    SyncSnapshot,
    SyncStatus,
    SyncTombstone,
)
from personal_hub.sync.crypto import (
    SYNC_BUNDLE_MAX_BYTES,
    SyncBundleError,
    SyncPassphraseError,
    decrypt_provider_relay,
    decrypt_snapshot,
    encrypt_provider_relay,
    encrypt_snapshot,
)
from personal_hub.sync.relay_contracts import (
    RELAY_CATEGORIES,
    ProviderRelaySnapshot,
    RelayCategory,
)

SYNC_ENDPOINT_DIRECTORY: Final[str] = ".personal-hub-sync"
_DEVICE_FILE_RE: Final[re.Pattern[str]] = re.compile(r"device-([a-f0-9]{32})\.sync\Z")
_DEVICE_ID_RE: Final[re.Pattern[str]] = re.compile(r"[a-f0-9]{32}\Z")
_SYNC_ENABLED_KEY: Final[str] = "sync.enabled"
_SYNC_ENDPOINT_KEY: Final[str] = "sync.endpoint"
_SYNC_DEVICE_ID_KEY: Final[str] = "sync.device_id"
_SYNC_PROFILE_ID_KEY: Final[str] = "sync.profile_id"
_SYNC_LAST_SYNC_KEY: Final[str] = "sync.last_sync_at"
_SYNC_LAST_ERROR_KEY: Final[str] = "sync.last_error"
_SYNC_RECORD_COUNT_KEY: Final[str] = "sync.record_count"
_SYNC_REMOTE_DEVICE_COUNT_KEY: Final[str] = "sync.remote_device_count"
_RELAY_ROLE_KEY: Final[str] = "sync.provider_relay_role"
_RELAY_CATEGORIES_KEY: Final[str] = "sync.provider_relay_categories"
_RELAY_LAST_SYNC_KEY: Final[str] = "sync.provider_relay_last_sync_at"
_RELAY_ITEM_COUNT_KEY: Final[str] = "sync.provider_relay_item_count"
_SAFE_ERROR: Final[str] = "Sync could not be completed safely."
_RELAY_FILE_RE: Final[re.Pattern[str]] = re.compile(r"collector-([a-f0-9]{32})\.relay\Z")


class SyncError(RuntimeError):
    """Base class for user-safe synchronization failures."""


class SyncConfigurationError(SyncError, ValueError):
    """Raised when the shared-folder configuration is unusable."""


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("sync timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _canonical_payload(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _profile_ids(snapshots: Iterable[SyncSnapshot]) -> set[str]:
    return {snapshot.profile_id for snapshot in snapshots if snapshot.profile_id is not None}


def merge_snapshots(
    snapshots: Iterable[SyncSnapshot],
    *,
    profile_id: str,
    device_id: str,
    generated_at: datetime | None = None,
) -> tuple[SyncSnapshot, int]:
    """Merge records with UTC last-write-wins and deterministic tie breaking.

    A tombstone wins at an equal timestamp.  When two different payloads have
    the same timestamp, the canonical JSON representation is the deterministic
    tie breaker and the result reports one conflict.
    """

    selected = tuple(snapshots)
    if not selected:
        raise ValueError("at least one sync snapshot is required")
    profiles = _profile_ids(selected)
    if profiles.difference({profile_id}):
        raise SyncError("The selected sync folder belongs to another Personal Hub profile.")

    records: dict[tuple[str, str], tuple[SyncRecord, str]] = {}
    tombstones: dict[tuple[str, str], tuple[SyncTombstone, str]] = {}
    conflicts = 0
    for snapshot in selected:
        source = snapshot.device_id
        for record in snapshot.records:
            key = (record.collection, record.record_id)
            previous = records.get(key)
            if previous is None:
                records[key] = (record, source)
                continue
            old_record, old_source = previous
            if old_record.updated_at == record.updated_at and old_record.payload != record.payload:
                conflicts += 1
            if (
                record.updated_at,
                _canonical_payload(record.payload),
                source,
            ) > (
                old_record.updated_at,
                _canonical_payload(old_record.payload),
                old_source,
            ):
                records[key] = (record, source)
        for tombstone in snapshot.tombstones:
            key = (tombstone.collection, tombstone.record_id)
            previous_tombstone = tombstones.get(key)
            if previous_tombstone is None or (
                tombstone.deleted_at,
                source,
            ) > (
                previous_tombstone[0].deleted_at,
                previous_tombstone[1],
            ):
                tombstones[key] = (tombstone, source)

    for key, (tombstone, _) in tuple(tombstones.items()):
        record = records.get(key)
        if record is not None and record[0].updated_at <= tombstone.deleted_at:
            del records[key]
        elif record is not None:
            del tombstones[key]

    timestamp = _utc(generated_at or datetime.now(UTC))
    merged = SyncSnapshot(
        format=SYNC_SNAPSHOT_FORMAT,
        profile_id=profile_id,
        device_id=device_id,
        generated_at=timestamp,
        records=tuple(
            record
            for record, _source in sorted(
                records.values(),
                key=lambda item: (item[0].collection, item[0].record_id),
            )
        ),
        tombstones=tuple(
            tombstone
            for tombstone, _source in sorted(
                tombstones.values(),
                key=lambda item: (item[0].collection, item[0].record_id),
            )
        ),
    )
    return merged, conflicts


class FileSyncEngine:
    """Read and publish encrypted per-device shards in a shared directory."""

    def __init__(self, endpoint: Path | str, *, device_id: str) -> None:
        self.endpoint = self._normalize_endpoint(endpoint)
        if not _DEVICE_ID_RE.fullmatch(device_id):
            raise SyncConfigurationError("Personal Hub sync device identity is invalid.")
        self.device_id = device_id

    @staticmethod
    def _normalize_endpoint(endpoint: Path | str) -> Path:
        if not isinstance(endpoint, (Path, str)):
            raise SyncConfigurationError("Sync folder is invalid.")
        try:
            raw = Path(endpoint).expanduser()
            if raw.exists() and (raw.is_symlink() or not raw.is_dir()):
                raise SyncConfigurationError("Sync folder must be a directory.")
            selected = raw.resolve(strict=False)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise SyncConfigurationError("Sync folder is invalid.") from error
        if selected.exists() and (not selected.is_dir() or selected.is_symlink()):
            raise SyncConfigurationError("Sync folder must be a directory.")
        return selected

    @property
    def shard_path(self) -> Path:
        return self.endpoint / SYNC_ENDPOINT_DIRECTORY / f"device-{self.device_id}.sync"

    def _directory(self, *, create: bool) -> Path:
        if not self.endpoint.exists():
            if not create:
                return self.endpoint / SYNC_ENDPOINT_DIRECTORY
            try:
                self.endpoint.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                raise SyncConfigurationError("Sync folder could not be opened.") from error
        if not self.endpoint.is_dir() or self.endpoint.is_symlink():
            raise SyncConfigurationError("Sync folder must be a directory.")
        directory = self.endpoint / SYNC_ENDPOINT_DIRECTORY
        if directory.exists() and (not directory.is_dir() or directory.is_symlink()):
            raise SyncConfigurationError("Sync folder metadata is invalid.")
        if create:
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                raise SyncConfigurationError("Sync folder could not be opened.") from error
        return directory

    def _read_shards(self, passphrase: str) -> tuple[SyncSnapshot | None, tuple[SyncSnapshot, ...]]:
        directory = self._directory(create=False)
        if not directory.exists():
            return None, ()
        own: SyncSnapshot | None = None
        other: list[SyncSnapshot] = []
        try:
            candidates = sorted(directory.iterdir(), key=lambda path: path.name)
        except OSError as error:
            raise SyncConfigurationError("Sync folder could not be read.") from error
        for path in candidates:
            match = _DEVICE_FILE_RE.fullmatch(path.name)
            if match is None:
                continue
            if path.is_symlink() or not path.is_file():
                raise SyncBundleError("Sync folder contains an invalid device shard.")
            try:
                bundle = path.read_bytes()
            except OSError as error:
                raise SyncConfigurationError("A sync device shard could not be read.") from error
            if len(bundle) > SYNC_BUNDLE_MAX_BYTES:
                raise SyncBundleError("A sync device shard is too large.")
            snapshot = decrypt_snapshot(bundle, passphrase)
            if snapshot.device_id != match.group(1):
                raise SyncBundleError("A sync device shard identity is invalid.")
            if snapshot.device_id == self.device_id:
                own = snapshot
            else:
                other.append(snapshot)
        return own, tuple(other)

    def _publish(self, snapshot: SyncSnapshot, passphrase: str) -> None:
        directory = self._directory(create=True)
        bundle = encrypt_snapshot(snapshot, passphrase)
        if len(bundle) > SYNC_BUNDLE_MAX_BYTES:
            raise SyncBundleError("Encrypted sync device shard is too large.")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".device-{self.device_id}.",
            suffix=".tmp",
            dir=directory,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(bundle)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.shard_path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def synchronize(
        self,
        local_snapshot: SyncSnapshot,
        passphrase: str,
        *,
        apply: Callable[[SyncSnapshot], None] | None = None,
        now: datetime | None = None,
    ) -> SyncResult:
        """Merge all visible shards, apply the result, then publish this device."""

        if local_snapshot.device_id != self.device_id:
            raise SyncConfigurationError("Local sync snapshot has the wrong device identity.")
        timestamp = _utc(now or datetime.now(UTC))
        previous, other = self._read_shards(passphrase)
        profiles = _profile_ids((*other, *(() if previous is None else (previous,))))
        if local_snapshot.profile_id is not None:
            profiles.add(local_snapshot.profile_id)
        if len(profiles) > 1:
            raise SyncError("The selected sync folder contains multiple Hub profiles.")
        profile_id = next(iter(profiles), uuid4().hex)

        # A newly paired device has freshly persisted default preferences.  Do
        # not let those defaults outrank the established profile's preferences
        # merely because this database was created later in wall-clock time.
        # Local tasks and definitions still participate in the first merge.
        local_records = local_snapshot.records
        if local_snapshot.profile_id is None and profiles:
            local_records = tuple(
                record for record in local_records if record.collection != "settings"
            )

        current_keys = {(record.collection, record.record_id) for record in local_records}
        tombstones = list(local_snapshot.tombstones)
        if previous is not None:
            for record in previous.records:
                key = (record.collection, record.record_id)
                if key not in current_keys and not any(
                    item.collection == record.collection and item.record_id == record.record_id
                    for item in tombstones
                ):
                    tombstones.append(
                        SyncTombstone(
                            collection=record.collection,
                            record_id=record.record_id,
                            deleted_at=timestamp,
                        )
                    )
            tombstones.extend(previous.tombstones)
        candidate = local_snapshot.model_copy(
            update={
                "profile_id": profile_id,
                "device_id": self.device_id,
                "generated_at": timestamp,
                "records": local_records,
                "tombstones": tuple(
                    {(item.collection, item.record_id): item for item in tombstones}.values()
                ),
            }
        )
        merged, conflicts = merge_snapshots(
            (candidate, *other),
            profile_id=profile_id,
            device_id=self.device_id,
            generated_at=timestamp,
        )
        if apply is not None:
            apply(merged)
        self._publish(merged, passphrase)
        return SyncResult(
            profile_id=profile_id,
            completed_at=timestamp,
            remote_device_count=len(other),
            record_count=len(merged.records),
            tombstone_count=len(merged.tombstones),
            conflict_count=conflicts,
        )


class SyncService:
    """Persist local sync configuration and invoke the file transport."""

    def __init__(self, settings_repository: SettingsRepository) -> None:
        self.settings_repository = settings_repository

    def _device_id(self) -> str:
        value = self.settings_repository.get(_SYNC_DEVICE_ID_KEY)
        if isinstance(value, str) and _DEVICE_ID_RE.fullmatch(value):
            return value
        generated = uuid4().hex
        self.settings_repository.set(_SYNC_DEVICE_ID_KEY, generated)
        return generated

    def device_id(self) -> str:
        """Return the stable local device identity used for the shard filename."""

        return self._device_id()

    def endpoint(self) -> Path | None:
        value = self.settings_repository.get(_SYNC_ENDPOINT_KEY)
        if not isinstance(value, str) or not value.strip():
            return None
        return FileSyncEngine._normalize_endpoint(value)

    def configure(self, endpoint: Path | str, *, enabled: bool = True) -> SyncStatus:
        normalized = FileSyncEngine._normalize_endpoint(endpoint)
        try:
            normalized.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise SyncConfigurationError("Sync folder could not be created.") from error
        self.settings_repository.set(_SYNC_ENDPOINT_KEY, os.fspath(normalized))
        self.settings_repository.set(_SYNC_ENABLED_KEY, bool(enabled))
        self.settings_repository.delete(_SYNC_LAST_ERROR_KEY)
        return self.status()

    def disable(self) -> SyncStatus:
        self.settings_repository.set(_SYNC_ENABLED_KEY, False)
        return self.status()

    def provider_relay_role(self) -> SyncRole:
        value = self.settings_repository.get(_RELAY_ROLE_KEY, "local")
        if isinstance(value, str) and value in {"local", "collector", "consumer"}:
            return value
        return "local"

    def set_provider_relay_role(self, role: SyncRole) -> SyncStatus:
        if role not in {"local", "collector", "consumer"}:
            raise SyncConfigurationError("Provider relay device role is invalid.")
        self.settings_repository.set(_RELAY_ROLE_KEY, role)
        self.settings_repository.delete(_SYNC_LAST_ERROR_KEY)
        return self.status()

    def provider_relay_categories(self) -> tuple[RelayCategory, ...]:
        value = self.settings_repository.get(_RELAY_CATEGORIES_KEY)
        if not isinstance(value, list):
            return RELAY_CATEGORIES
        selected = {item for item in value if isinstance(item, str) and item in RELAY_CATEGORIES}
        return tuple(item for item in RELAY_CATEGORIES if item in selected)

    def set_provider_relay_categories(self, categories: Iterable[str]) -> SyncStatus:
        selected = tuple(dict.fromkeys(categories))
        if any(item not in RELAY_CATEGORIES for item in selected):
            raise SyncConfigurationError("Provider relay content selection is invalid.")
        normalized = [item for item in RELAY_CATEGORIES if item in selected]
        self.settings_repository.set(_RELAY_CATEGORIES_KEY, normalized)
        return self.status()

    def publish_provider_relay(
        self,
        snapshot: ProviderRelaySnapshot,
        passphrase: str,
    ) -> ProviderRelaySnapshot:
        endpoint = self.endpoint()
        enabled = self.settings_repository.get(_SYNC_ENABLED_KEY, False) is True
        if not enabled or endpoint is None:
            raise SyncConfigurationError("Configure a shared sync folder before publishing.")
        device_id = self._device_id()
        if snapshot.collector_device_id != device_id:
            raise SyncConfigurationError(
                "Provider relay snapshot has the wrong collector identity."
            )
        directory = self._provider_relay_directory(endpoint, create=True)
        path = directory / f"collector-{device_id}.relay"
        candidates = self._provider_relay_candidates(directory)
        if any(candidate != path for candidate in candidates):
            raise SyncConfigurationError(
                "The shared folder already has a different provider collector."
            )
        bundle = encrypt_provider_relay(snapshot, passphrase)
        if len(bundle) > SYNC_BUNDLE_MAX_BYTES:
            raise SyncBundleError("Encrypted provider relay snapshot is too large.")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".collector-{device_id}.",
            suffix=".tmp",
            dir=directory,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(bundle)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return snapshot

    def read_provider_relay(self, passphrase: str) -> ProviderRelaySnapshot | None:
        endpoint = self.endpoint()
        enabled = self.settings_repository.get(_SYNC_ENABLED_KEY, False) is True
        if not enabled or endpoint is None:
            raise SyncConfigurationError("Configure a shared sync folder before receiving.")
        directory = self._provider_relay_directory(endpoint, create=False)
        candidates = self._provider_relay_candidates(directory)
        if not candidates:
            return None
        if len(candidates) != 1:
            raise SyncConfigurationError("The shared folder has more than one provider collector.")
        path = candidates[0]
        if path.is_symlink() or not path.is_file():
            raise SyncBundleError("Provider relay folder contains an invalid collector shard.")
        try:
            bundle = path.read_bytes()
        except OSError as error:
            raise SyncConfigurationError("Provider relay shard could not be read.") from error
        if len(bundle) > SYNC_BUNDLE_MAX_BYTES:
            raise SyncBundleError("Provider relay shard is too large.")
        snapshot = decrypt_provider_relay(bundle, passphrase)
        match = _RELAY_FILE_RE.fullmatch(path.name)
        if match is None or snapshot.collector_device_id != match.group(1):
            raise SyncBundleError("Provider relay shard identity is invalid.")
        return snapshot

    @staticmethod
    def _provider_relay_directory(endpoint: Path, *, create: bool) -> Path:
        engine = FileSyncEngine(endpoint, device_id="0" * 32)
        return engine._directory(create=create)

    @staticmethod
    def _provider_relay_candidates(directory: Path) -> list[Path]:
        if not directory.exists():
            return []
        try:
            return sorted(
                path
                for path in directory.iterdir()
                if _RELAY_FILE_RE.fullmatch(path.name) is not None
            )
        except OSError as error:
            raise SyncConfigurationError("Provider relay folder could not be listed.") from error

    def record_provider_relay_sync(
        self,
        *,
        at: datetime,
        item_count: int,
    ) -> None:
        timestamp = _utc(at)
        if not 0 <= item_count <= 50_000:
            raise SyncConfigurationError("Provider relay item count is invalid.")
        self.settings_repository.set(_RELAY_LAST_SYNC_KEY, timestamp.isoformat())
        self.settings_repository.set(_RELAY_ITEM_COUNT_KEY, item_count)
        self.settings_repository.delete(_SYNC_LAST_ERROR_KEY)

    def record_provider_relay_error(self, message: str = _SAFE_ERROR) -> None:
        self.settings_repository.set(_SYNC_LAST_ERROR_KEY, " ".join(message.split())[:240])

    def record_error(self, message: str = _SAFE_ERROR) -> None:
        self.settings_repository.set(_SYNC_LAST_ERROR_KEY, " ".join(message.split())[:240])

    def status(self) -> SyncStatus:
        configuration_error = False
        try:
            endpoint = self.endpoint()
        except SyncConfigurationError:
            # Settings are user-editable database values.  A moved folder or
            # a path that has become a file must not make the Settings page
            # itself fail to render.
            endpoint = None
            configuration_error = True
        enabled = self.settings_repository.get(_SYNC_ENABLED_KEY, False) is True
        last_sync_value = self.settings_repository.get(_SYNC_LAST_SYNC_KEY)
        last_sync: datetime | None = None
        if isinstance(last_sync_value, str):
            try:
                last_sync = _utc(datetime.fromisoformat(last_sync_value))
            except ValueError:
                last_sync = None
        last_error = self.settings_repository.get(_SYNC_LAST_ERROR_KEY)
        if not isinstance(last_error, str):
            last_error = None
        profile = self.settings_repository.get(_SYNC_PROFILE_ID_KEY)
        profile_paired = isinstance(profile, str) and bool(profile)
        record_count = self.settings_repository.get(_SYNC_RECORD_COUNT_KEY, 0)
        remote_device_count = self.settings_repository.get(_SYNC_REMOTE_DEVICE_COUNT_KEY, 0)
        if type(record_count) is not int or not 0 <= record_count <= 50_000:
            record_count = 0
        if type(remote_device_count) is not int or not 0 <= remote_device_count <= 1_000:
            remote_device_count = 0
        relay_role = self.provider_relay_role()
        relay_categories = self.provider_relay_categories()
        relay_last_value = self.settings_repository.get(_RELAY_LAST_SYNC_KEY)
        relay_last_sync: datetime | None = None
        if isinstance(relay_last_value, str):
            try:
                relay_last_sync = _utc(datetime.fromisoformat(relay_last_value))
            except ValueError:
                relay_last_sync = None
        relay_item_count = self.settings_repository.get(_RELAY_ITEM_COUNT_KEY, 0)
        if type(relay_item_count) is not int or not 0 <= relay_item_count <= 50_000:
            relay_item_count = 0
        state = "not_configured"
        if enabled and (endpoint is not None or configuration_error):
            state = "error" if last_error or configuration_error else "ready"
        elif not enabled and endpoint is not None:
            state = "disabled"
        return SyncStatus(
            state=state,
            enabled=enabled,
            endpoint=None if endpoint is None else os.fspath(endpoint),
            device_id=self._device_id(),
            profile_paired=profile_paired,
            last_sync_at=last_sync,
            last_error=last_error,
            remote_device_count=remote_device_count,
            record_count=record_count,
            relay_role=relay_role,
            relay_categories=tuple(relay_categories),
            relay_last_sync_at=relay_last_sync,
            relay_item_count=relay_item_count,
        )

    def synchronize(
        self,
        local_snapshot: SyncSnapshot,
        passphrase: str,
        *,
        apply: Callable[[SyncSnapshot], None] | None = None,
        now: datetime | None = None,
    ) -> SyncResult:
        endpoint = self.endpoint()
        enabled = self.settings_repository.get(_SYNC_ENABLED_KEY, False) is True
        if not enabled or endpoint is None:
            raise SyncConfigurationError("Configure a shared sync folder before syncing.")
        engine = FileSyncEngine(endpoint, device_id=self._device_id())
        try:
            result = engine.synchronize(local_snapshot, passphrase, apply=apply, now=now)
        except (SyncError, SyncBundleError, SyncPassphraseError):
            self.record_error()
            raise
        self.settings_repository.set(_SYNC_PROFILE_ID_KEY, result.profile_id)
        self.settings_repository.set(_SYNC_LAST_SYNC_KEY, result.completed_at.isoformat())
        self.settings_repository.set(_SYNC_RECORD_COUNT_KEY, result.record_count)
        self.settings_repository.set(_SYNC_REMOTE_DEVICE_COUNT_KEY, result.remote_device_count)
        self.settings_repository.delete(_SYNC_LAST_ERROR_KEY)
        return result


__all__ = [
    "FileSyncEngine",
    "SYNC_ENDPOINT_DIRECTORY",
    "SyncConfigurationError",
    "SyncError",
    "SyncService",
    "merge_snapshots",
]
