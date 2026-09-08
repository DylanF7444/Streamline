"""Strict, storage-safe contracts for the Personal Hub foundation.

The contracts in this module are intentionally owned by Personal Hub.  They describe
the small amount of durable state needed by the Phase 1 shell and do not expose
assistant commands, transcripts, or provider-specific implementation objects.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Final, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FreshnessState = Literal[
    "ready",
    "refreshing",
    "stale",
    "offline",
    "permission_required",
    "disconnected",
    "empty",
    "error",
]
InboxState = Literal["unread", "read", "dismissed", "archived"]
Severity = Literal["info", "attention", "urgent"]
NotificationChannel = Literal["windows", "inbox", "digest"]
NotificationStatus = Literal["pending", "delivered", "failed", "suppressed"]

_IDENTIFIER_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_RESOURCE_IDENTIFIER_RE: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z0-9_][A-Za-z0-9._:%+@#=-]{0,255}\Z"
)
_METADATA_KEY_RE: Final[re.Pattern[str]] = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_PRIVATE_METADATA_TERMS: Final[frozenset[str]] = frozenset(
    {
        "authorization",
        "body",
        "cookie",
        "content",
        "location",
        "message",
        "notes",
        "password",
        "preview",
        "secret",
        "sender",
        "subject",
        "token",
        "title",
        "url",
    }
)


def _utc(value: datetime) -> datetime:
    """Require a timezone-aware timestamp and normalize it to UTC."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _identifier(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not _IDENTIFIER_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a bounded identifier")
    return normalized


def _resource_identifier(value: str) -> str:
    normalized = value.strip()
    if not _RESOURCE_IDENTIFIER_RE.fullmatch(normalized):
        raise ValueError("source resource identifier must be bounded")
    return normalized


class HubContract(BaseModel):
    """Base model for persisted and provider-bound Hub values."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        validate_default=True,
    )


class SourceRef(HubContract):
    """Stable identity for the provider/resource behind a Hub item."""

    provider_id: str = Field(min_length=1, max_length=64)
    connection_id: str | None = Field(default=None, max_length=128)
    resource_id: str | None = Field(default=None, max_length=256)
    display_name: str = Field(min_length=1, max_length=160)
    origin: str | None = Field(default=None, max_length=512)

    @field_validator("provider_id")
    @classmethod
    def _provider_id_is_bounded(cls, value: str) -> str:
        return _identifier(value, "provider_id")

    @field_validator("connection_id")
    @classmethod
    def _optional_ids_are_bounded(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _identifier(value, "source identifier")

    @field_validator("resource_id")
    @classmethod
    def _resource_id_is_bounded(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _resource_identifier(value)


class Freshness(HubContract):
    """Visible health/freshness state for a source."""

    state: FreshnessState
    collected_at: datetime | None = None
    expires_at: datetime | None = None
    safe_message: str | None = Field(default=None, max_length=512)

    @field_validator("collected_at", "expires_at")
    @classmethod
    def _timestamps_are_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @field_validator("safe_message")
    @classmethod
    def _safe_message_is_single_line(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if not normalized:
            return None
        return normalized

    @model_validator(mode="after")
    def _expiry_follows_collection(self) -> Freshness:
        if (
            self.collected_at is not None
            and self.expires_at is not None
            and self.expires_at < self.collected_at
        ):
            raise ValueError("expires_at cannot precede collected_at")
        return self


class SourceConnection(HubContract):
    """A provider account/connection known to Personal Hub."""

    connection_id: str = Field(default_factory=lambda: str(uuid4()), max_length=128)
    provider_id: str = Field(min_length=1, max_length=64)
    account_label: str = Field(min_length=1, max_length=160)
    capabilities: tuple[str, ...] = Field(default=(), max_length=32)
    granted_scopes: tuple[str, ...] = Field(default=(), max_length=32)
    enabled: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("connection_id")
    @classmethod
    def _connection_id_is_bounded(cls, value: str) -> str:
        return _identifier(value, "connection_id")

    @field_validator("provider_id")
    @classmethod
    def _source_provider_id_is_bounded(cls, value: str) -> str:
        return _identifier(value, "provider_id")

    @field_validator("capabilities", "granted_scopes")
    @classmethod
    def _capabilities_are_bounded(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip() for item in value)
        if any(not item or len(item) > 128 for item in normalized):
            raise ValueError("source capabilities and scopes must be bounded strings")
        if len(set(normalized)) != len(normalized):
            raise ValueError("source capabilities and scopes must be unique")
        return normalized

    @field_validator("created_at", "updated_at")
    @classmethod
    def _source_timestamps_are_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def _source_timestamps_are_ordered(self) -> SourceConnection:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self


class SourceSyncState(HubContract):
    """Last known collection/health state for a provider connection."""

    connection_id: str = Field(max_length=128)
    freshness: Freshness
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    failure_count: int = Field(default=0, ge=0, le=1_000_000)
    backoff_until: datetime | None = None

    @field_validator("connection_id")
    @classmethod
    def _sync_connection_id_is_bounded(cls, value: str) -> str:
        return _identifier(value, "connection_id")

    @field_validator("last_attempt_at", "last_success_at", "backoff_until")
    @classmethod
    def _sync_timestamps_are_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)


class InboxItem(HubContract):
    """A durable, safe-to-display attention item."""

    item_id: str = Field(default_factory=lambda: str(uuid4()), max_length=128)
    kind: Literal[
        "watch_alert",
        "digest",
        "source_failure",
        "connection_warning",
        "message",
        "task_reminder",
        "alert",
        "system",
    ]
    title: str = Field(min_length=1, max_length=240)
    safe_summary: str = Field(min_length=1, max_length=1_000)
    reason: str = Field(min_length=1, max_length=240)
    source: SourceRef | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    severity: Severity = "info"
    navigation_target: str = Field(min_length=1, max_length=512)
    state: InboxState = "unread"
    read_at: datetime | None = None
    dismissed_at: datetime | None = None
    archived_at: datetime | None = None
    deduplication_key: str | None = Field(default=None, max_length=256)
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @field_validator("item_id")
    @classmethod
    def _item_id_is_bounded(cls, value: str) -> str:
        return _identifier(value, "item_id")

    @field_validator("created_at", "read_at", "dismissed_at", "archived_at")
    @classmethod
    def _inbox_timestamps_are_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @field_validator("title", "safe_summary", "reason")
    @classmethod
    def _inbox_text_is_single_line(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("Inbox text cannot be blank")
        return normalized

    @field_validator("deduplication_key")
    @classmethod
    def _inbox_deduplication_key_is_bounded(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        return normalized

    @field_validator("metadata")
    @classmethod
    def _inbox_metadata_is_bounded(
        cls, value: dict[str, str | int | float | bool | None]
    ) -> dict[str, str | int | float | bool | None]:
        if len(value) > 32:
            raise ValueError("Inbox metadata is too large")
        for key, item in value.items():
            if not _METADATA_KEY_RE.fullmatch(key):
                raise ValueError("Inbox metadata keys must be simple names")
            if isinstance(item, str) and len(item) > 256:
                raise ValueError("Inbox metadata values are too large")
        return value

    @model_validator(mode="after")
    def _state_timestamps_are_consistent(self) -> InboxItem:
        if self.state in {"read", "dismissed", "archived"} and self.read_at is None:
            # A state can be restored from an old database without an exact
            # event time; the repository fills this when transitioning state.
            return self
        if self.state == "archived" and self.archived_at is None:
            return self
        return self


class AuditEvent(HubContract):
    """Metadata-only operational audit event.

    The model intentionally has no free-form message/body field.  Metadata is
    restricted to scalar values and rejects names associated with private content
    or credentials before anything can reach SQLite.
    """

    audit_id: str = Field(default_factory=lambda: str(uuid4()), max_length=128)
    event_type: str = Field(min_length=1, max_length=96)
    source_type: str | None = Field(default=None, max_length=64)
    source_id: str | None = Field(default=None, max_length=128)
    outcome: Literal["started", "succeeded", "failed", "skipped"] = "succeeded"
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @field_validator("audit_id")
    @classmethod
    def _audit_id_is_bounded(cls, value: str) -> str:
        return _identifier(value, "audit_id")

    @field_validator("event_type")
    @classmethod
    def _event_type_is_safe(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,95}\Z", normalized):
            raise ValueError("event_type must be a simple operational name")
        return normalized

    @field_validator("source_type", "source_id")
    @classmethod
    def _optional_audit_ids_are_safe(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _identifier(value, "audit source identifier")

    @field_validator("occurred_at")
    @classmethod
    def _audit_timestamp_is_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("metadata")
    @classmethod
    def _audit_metadata_is_safe(
        cls, value: dict[str, str | int | float | bool | None]
    ) -> dict[str, str | int | float | bool | None]:
        if len(value) > 32:
            raise ValueError("audit metadata is too large")
        for key, item in value.items():
            normalized_key = key.casefold()
            if not _METADATA_KEY_RE.fullmatch(key):
                raise ValueError("audit metadata keys must be simple names")
            if any(term in normalized_key for term in _PRIVATE_METADATA_TERMS):
                raise ValueError("audit metadata cannot contain private-content fields")
            if isinstance(item, str) and (
                len(item) > 256 or any(character in item for character in "\r\n")
            ):
                raise ValueError("audit metadata strings must be bounded single lines")
        return value


class NotificationDelivery(HubContract):
    """Durable notification/deduplication state."""

    delivery_id: str = Field(default_factory=lambda: str(uuid4()), max_length=128)
    deduplication_key: str = Field(min_length=1, max_length=256)
    channel: NotificationChannel
    status: NotificationStatus = "pending"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    delivered_at: datetime | None = None
    expires_at: datetime | None = None
    attempts: int = Field(default=0, ge=0, le=1_000_000)
    safe_message: str | None = Field(default=None, max_length=512)

    @field_validator("delivery_id")
    @classmethod
    def _delivery_id_is_bounded(cls, value: str) -> str:
        return _identifier(value, "delivery_id")

    @field_validator("deduplication_key")
    @classmethod
    def _deduplication_key_is_safe(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("deduplication_key cannot be blank")
        return normalized

    @field_validator("created_at", "delivered_at", "expires_at")
    @classmethod
    def _notification_timestamps_are_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @field_validator("safe_message")
    @classmethod
    def _notification_message_is_safe(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        return normalized or None

    @model_validator(mode="after")
    def _notification_dates_are_ordered(self) -> NotificationDelivery:
        if self.expires_at is not None and self.expires_at < self.created_at:
            raise ValueError("expires_at cannot precede created_at")
        if self.delivered_at is not None and self.delivered_at < self.created_at:
            raise ValueError("delivered_at cannot precede created_at")
        return self


# Friendly aliases align names used by the provider/sources layer without creating
# another contract or storage representation.
SourceHealth = SourceSyncState
InboxRecord = InboxItem
AuditRecord = AuditEvent
NotificationRecord = NotificationDelivery
HubSourceConnection = SourceConnection
HubSourceSyncState = SourceSyncState
HubInboxItem = InboxItem
HubAuditEvent = AuditEvent
HubNotificationDelivery = NotificationDelivery

__all__ = [
    "AuditEvent",
    "AuditRecord",
    "Freshness",
    "FreshnessState",
    "HubAuditEvent",
    "HubContract",
    "HubInboxItem",
    "HubNotificationDelivery",
    "HubSourceConnection",
    "HubSourceSyncState",
    "InboxItem",
    "InboxRecord",
    "InboxState",
    "NotificationChannel",
    "NotificationDelivery",
    "NotificationRecord",
    "NotificationStatus",
    "Severity",
    "SourceConnection",
    "SourceHealth",
    "SourceRef",
    "SourceSyncState",
]
