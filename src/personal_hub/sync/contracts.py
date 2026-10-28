"""Strict contracts for encrypted Personal Hub sync state.

Sync is intentionally a file-based transport.  These contracts describe only
Hub-owned records and never contain credentials, provider bodies, or opaque
serialized Python values.
"""

from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime
from typing import Final, Literal

from pydantic import Field, field_validator, model_validator

from personal_hub.contracts import HubContract

SyncCollection = Literal[
    "settings",
    "task_lists",
    "tasks",
    "calendar_imports",
    "calendar_events",
    "feed_subscriptions",
    "watches",
    "activity",
    "reflections",
]
SyncState = Literal["disabled", "not_configured", "ready", "error"]
SyncRole = Literal["local", "collector", "consumer"]
SyncRelayCategory = Literal["calendar", "messages", "feeds", "watches"]
type JSONScalar = str | int | float | bool | None
type JSONValue = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]

SYNC_SNAPSHOT_FORMAT: Final[str] = "personal-hub-sync-snapshot-v1"
_IDENTIFIER_RE: Final[re.Pattern[str]] = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,255}\Z")
_MAX_RECORD_JSON_BYTES: Final[int] = 256 * 1024
_MAX_SNAPSHOT_JSON_BYTES: Final[int] = 16 * 1024 * 1024


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("sync timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _validate_json_value(value: object, *, depth: int = 0) -> None:
    if depth > 8:
        raise ValueError("sync JSON is nested too deeply")
    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, str) and len(value) > 20_000:
            raise ValueError("sync JSON strings are too large")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("sync JSON numbers must be finite")
        return
    if isinstance(value, list):
        if len(value) > 10_000:
            raise ValueError("sync JSON lists are too large")
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 1_000:
            raise ValueError("sync JSON objects are too large")
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 256:
                raise ValueError("sync JSON keys are invalid")
            _validate_json_value(item, depth=depth + 1)
        return
    raise TypeError("sync payloads must contain JSON-compatible values")


def _bounded_identifier(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not _IDENTIFIER_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} is not a valid sync identifier")
    return normalized


class SyncRecord(HubContract):
    """One validated Hub-owned record in a device snapshot."""

    collection: SyncCollection
    record_id: str = Field(min_length=1, max_length=256)
    updated_at: datetime
    payload: dict[str, JSONValue]

    @field_validator("record_id")
    @classmethod
    def _record_id_is_safe(cls, value: str) -> str:
        return _bounded_identifier(value, "record_id")

    @field_validator("updated_at")
    @classmethod
    def _timestamp_is_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("payload")
    @classmethod
    def _payload_is_bounded(cls, value: dict[str, JSONValue]) -> dict[str, JSONValue]:
        _validate_json_value(value)
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > _MAX_RECORD_JSON_BYTES:
            raise ValueError("sync record payload is too large")
        return value


class SyncTombstone(HubContract):
    """A durable deletion marker that prevents deleted records reappearing."""

    collection: SyncCollection
    record_id: str = Field(min_length=1, max_length=256)
    deleted_at: datetime

    @field_validator("record_id")
    @classmethod
    def _record_id_is_safe(cls, value: str) -> str:
        return _bounded_identifier(value, "tombstone record_id")

    @field_validator("deleted_at")
    @classmethod
    def _timestamp_is_utc(cls, value: datetime) -> datetime:
        return _utc(value)


class SyncSnapshot(HubContract):
    """The plaintext payload encrypted inside one per-device sync shard."""

    format: Literal["personal-hub-sync-snapshot-v1"] = SYNC_SNAPSHOT_FORMAT
    profile_id: str | None = Field(default=None, max_length=128)
    device_id: str = Field(min_length=1, max_length=128)
    generated_at: datetime
    records: tuple[SyncRecord, ...] = Field(default=(), max_length=50_000)
    tombstones: tuple[SyncTombstone, ...] = Field(default=(), max_length=50_000)

    @field_validator("profile_id", "device_id")
    @classmethod
    def _ids_are_safe(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return _bounded_identifier(value, "sync profile/device id")

    @field_validator("generated_at")
    @classmethod
    def _generated_at_is_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def _record_identities_are_unique(self) -> SyncSnapshot:
        record_keys = [(item.collection, item.record_id) for item in self.records]
        tombstone_keys = [(item.collection, item.record_id) for item in self.tombstones]
        if len(record_keys) != len(set(record_keys)):
            raise ValueError("sync snapshots cannot contain duplicate records")
        if len(tombstone_keys) != len(set(tombstone_keys)):
            raise ValueError("sync snapshots cannot contain duplicate tombstones")
        encoded = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(encoded.encode("utf-8")) > _MAX_SNAPSHOT_JSON_BYTES:
            raise ValueError("sync snapshot is too large")
        return self


class SyncStatus(HubContract):
    """Safe status suitable for the Settings page."""

    state: SyncState = "not_configured"
    enabled: bool = False
    endpoint: str | None = Field(default=None, max_length=1_024)
    device_id: str = Field(min_length=1, max_length=128)
    profile_paired: bool = False
    last_sync_at: datetime | None = None
    last_error: str | None = Field(default=None, max_length=240)
    remote_device_count: int = Field(default=0, ge=0, le=1_000)
    record_count: int = Field(default=0, ge=0, le=50_000)
    relay_role: SyncRole = "local"
    relay_categories: tuple[SyncRelayCategory, ...] = (
        "calendar",
        "messages",
        "feeds",
        "watches",
    )
    relay_last_sync_at: datetime | None = None
    relay_item_count: int = Field(default=0, ge=0, le=50_000)

    @field_validator("device_id")
    @classmethod
    def _device_id_is_safe(cls, value: str) -> str:
        return _bounded_identifier(value, "device_id")

    @field_validator("last_sync_at")
    @classmethod
    def _last_sync_is_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @field_validator("relay_last_sync_at")
    @classmethod
    def _last_relay_sync_is_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @field_validator("relay_categories")
    @classmethod
    def _relay_categories_are_unique(
        cls,
        value: tuple[SyncRelayCategory, ...],
    ) -> tuple[SyncRelayCategory, ...]:
        if len(value) != len(set(value)):
            raise ValueError("provider relay categories must be unique")
        return value

    @field_validator("last_error")
    @classmethod
    def _error_is_safe(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        return normalized[:240] or None


class SyncResult(HubContract):
    """Bounded result of one successful merge and publication."""

    profile_id: str = Field(min_length=1, max_length=128)
    completed_at: datetime
    remote_device_count: int = Field(default=0, ge=0, le=1_000)
    record_count: int = Field(default=0, ge=0, le=50_000)
    tombstone_count: int = Field(default=0, ge=0, le=50_000)
    conflict_count: int = Field(default=0, ge=0, le=50_000)
    relay_item_count: int = Field(default=0, ge=0, le=50_000)

    @field_validator("completed_at")
    @classmethod
    def _completed_at_is_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("profile_id")
    @classmethod
    def _profile_id_is_safe(cls, value: str) -> str:
        return _bounded_identifier(value, "profile_id")


__all__ = [
    "JSONValue",
    "SYNC_SNAPSHOT_FORMAT",
    "SyncCollection",
    "SyncRecord",
    "SyncResult",
    "SyncRole",
    "SyncRelayCategory",
    "SyncSnapshot",
    "SyncState",
    "SyncStatus",
    "SyncTombstone",
]
