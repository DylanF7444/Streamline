"""Strict provider-neutral contracts for bounded attention-message reading."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal, Protocol, runtime_checkable
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from personal_hub.contracts import HubContract
from personal_hub.messages.categories import MessageCategory

type MessageProvider = Literal["gmail", "outlook", "slack"]
type MessageState = Literal[
    "ready",
    "empty",
    "stale",
    "offline",
    "permission_required",
    "disconnected",
    "error",
]

_SOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class MessageError(RuntimeError):
    pass


class MessageOfflineError(MessageError):
    pass


class MessagePermissionRequiredError(MessageError):
    pass


class MessageDisconnectedError(MessageError):
    pass


class MessageUnavailableError(MessageError):
    pass


class MessageInvalidResponseError(MessageError):
    pass


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("message timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _line(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split())
    return normalized or None


def _identifier(value: str, field: str, *, maximum: int = 256) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or any(ord(item) < 32 for item in normalized):
        raise ValueError(f"{field} is invalid")
    return normalized


def _link(value: str | None) -> str | None:
    normalized = _line(value)
    if normalized is None:
        return None
    parts = urlsplit(normalized)
    if parts.scheme.casefold() not in {"http", "https"} or not parts.hostname:
        raise ValueError("message links must use HTTP(S)")
    if parts.username is not None or parts.password is not None or len(normalized) > 2_048:
        raise ValueError("message link is invalid")
    return normalized


class MessageQuery(HubContract):
    """Explicit folders, channels, or query and a hard result bound."""

    folders: tuple[str, ...] = Field(default=(), max_length=20)
    channels: tuple[str, ...] = Field(default=(), max_length=50)
    query: str | None = Field(default=None, max_length=500)
    unread_only: bool = True
    important_only: bool = False
    limit: int = Field(default=50, ge=1, le=200)
    received_after: datetime | None = None

    @field_validator("folders", "channels")
    @classmethod
    def _selection(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        result: list[str] = []
        for item in value:
            normalized = _identifier(item, "message selection", maximum=160)
            if normalized not in result:
                result.append(normalized)
        return tuple(result)

    _query = field_validator("query")(_line)

    @field_validator("received_after")
    @classmethod
    def _received_after(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _aware(value)


class MessageSourceConfig(HubContract):
    source_id: str = Field(min_length=1, max_length=128)
    provider: MessageProvider
    account_id: str = Field(min_length=1, max_length=160)
    query: MessageQuery
    refresh_minutes: int = Field(default=10, ge=5, le=1_440)
    retention_days: int = Field(default=7, ge=0, le=30)
    enabled: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("source_id")
    @classmethod
    def _source_id(cls, value: str) -> str:
        if not _SOURCE_ID.fullmatch(value):
            raise ValueError("message source_id must be a stable identifier")
        return value

    @field_validator("account_id")
    @classmethod
    def _account(cls, value: str) -> str:
        return _identifier(value, "account_id", maximum=160)

    @field_validator("created_at", "updated_at")
    @classmethod
    def _timestamps(cls, value: datetime) -> datetime:
        return _aware(value)

    @model_validator(mode="after")
    def _provider_selection(self) -> MessageSourceConfig:
        if self.provider == "gmail" and not (self.query.folders or self.query.query):
            raise ValueError("Gmail requires an explicit folder or query")
        if self.provider == "gmail" and any(
            re.fullmatch(r"[A-Za-z0-9_-]{1,80}", folder) is None for folder in self.query.folders
        ):
            raise ValueError("Gmail folders must be explicit label identifiers")
        if self.provider == "outlook" and not self.query.folders:
            raise ValueError("Outlook requires at least one selected folder")
        if self.provider == "slack" and not (self.query.channels or self.query.query):
            raise ValueError("Slack requires selected channels or a saved query")
        if self.provider != "slack" and self.query.channels:
            raise ValueError("channels are valid only for Slack")
        return self


class MessageSummary(HubContract):
    source_id: str = Field(min_length=1, max_length=128)
    message_id: str = Field(min_length=1, max_length=256)
    thread_id: str | None = Field(default=None, max_length=256)
    provider: MessageProvider
    account_id: str = Field(min_length=1, max_length=160)
    sender: str = Field(min_length=1, max_length=320)
    title: str = Field(min_length=1, max_length=500)
    preview: str | None = Field(default=None, max_length=1_000)
    received_at: datetime
    unread: bool
    importance: Literal["normal", "important"] = "normal"
    category: MessageCategory = "primary"
    deep_link: str | None = Field(default=None, max_length=2_048)
    sensitive: bool = True

    @field_validator("source_id")
    @classmethod
    def _source(cls, value: str) -> str:
        if not _SOURCE_ID.fullmatch(value):
            raise ValueError("message source_id is invalid")
        return value

    @field_validator("message_id", "thread_id")
    @classmethod
    def _ids(cls, value: str | None, info) -> str | None:
        return None if value is None else _identifier(value, info.field_name)

    @field_validator("sender", "title", "preview")
    @classmethod
    def _text(cls, value: str | None) -> str | None:
        return _line(value)

    @field_validator("received_at")
    @classmethod
    def _received(cls, value: datetime) -> datetime:
        return _aware(value)

    _deep_link = field_validator("deep_link")(_link)


class MessageAttentionNotice(HubContract):
    """Ephemeral, count-only attention notice for unread selected messages.

    Message summaries remain in the bounded Messages view.  This contract deliberately
    has no provider content so the desktop layer can show a startup notice without
    turning that notice into a durable Inbox item.
    """

    notice_id: str = Field(min_length=1, max_length=128)
    title: str = Field(default="Attention messages", min_length=1, max_length=120)
    safe_summary: str = Field(min_length=1, max_length=512)
    unread_count: int = Field(ge=1, le=1_000)
    source_count: int = Field(ge=1, le=1_000)
    created_at: datetime
    navigation_target: str = "hub://inbox/messages"
    deduplication_key: str = Field(min_length=1, max_length=256)

    @field_validator("notice_id", "deduplication_key")
    @classmethod
    def _notice_identifiers_are_safe(cls, value: str) -> str:
        return _identifier(value, "message attention notice identifier")

    @field_validator("title", "safe_summary")
    @classmethod
    def _notice_text_is_single_line(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("message attention notice text cannot be blank")
        return normalized

    @field_validator("created_at")
    @classmethod
    def _notice_timestamp_is_utc(cls, value: datetime) -> datetime:
        return _aware(value)


class MessageBody(HubContract):
    source_id: str = Field(min_length=1, max_length=128)
    message_id: str = Field(min_length=1, max_length=256)
    plain_text: str = Field(max_length=100_000)
    links: tuple[str, ...] = Field(default=(), max_length=50)
    fetched_at: datetime
    sensitive: bool = True

    @field_validator("plain_text")
    @classmethod
    def _plain_text(cls, value: str) -> str:
        return "\n".join(line.strip() for line in value.splitlines() if line.strip())

    @field_validator("links")
    @classmethod
    def _links(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        result: list[str] = []
        for item in value:
            link = _link(item)
            if link is not None and link not in result:
                result.append(link)
        return tuple(result)

    @field_validator("fetched_at")
    @classmethod
    def _fetched(cls, value: datetime) -> datetime:
        return _aware(value)


class MessageSnapshot(HubContract):
    source_id: str = Field(min_length=1, max_length=128)
    provider: MessageProvider
    account_id: str = Field(min_length=1, max_length=160)
    collected_at: datetime
    summaries: tuple[MessageSummary, ...] = Field(default=(), max_length=200)

    @field_validator("collected_at")
    @classmethod
    def _collected(cls, value: datetime) -> datetime:
        return _aware(value)


class MessageSyncState(HubContract):
    source_id: str
    state: MessageState = "stale"
    last_checked_at: datetime | None = None
    last_success_at: datetime | None = None
    failure_count: int = Field(default=0, ge=0, le=1_000_000)
    backoff_until: datetime | None = None
    safe_error: str | None = Field(default=None, max_length=512)

    @field_validator("last_checked_at", "last_success_at", "backoff_until")
    @classmethod
    def _timestamps(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _aware(value)


class MessageResult(HubContract):
    source_id: str
    provider: MessageProvider
    account_id: str
    state: MessageState
    checked_at: datetime
    safe_message: str = Field(min_length=1, max_length=512)
    snapshot: MessageSnapshot | None = None
    last_success_at: datetime | None = None

    @field_validator("checked_at", "last_success_at")
    @classmethod
    def _timestamps(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _aware(value)


@runtime_checkable
class MessageSource(Protocol):
    source_id: str
    provider_id: str

    async def list_summaries(self, query: MessageQuery) -> MessageSnapshot: ...

    async def get_body(self, message_id: str) -> MessageBody: ...


__all__ = [
    "MessageBody",
    "MessageAttentionNotice",
    "MessageCategory",
    "MessageDisconnectedError",
    "MessageError",
    "MessageInvalidResponseError",
    "MessageOfflineError",
    "MessagePermissionRequiredError",
    "MessageProvider",
    "MessageQuery",
    "MessageResult",
    "MessageSnapshot",
    "MessageSource",
    "MessageSourceConfig",
    "MessageState",
    "MessageSummary",
    "MessageSyncState",
    "MessageUnavailableError",
]
