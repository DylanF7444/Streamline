"""Strict provider-neutral calendar contracts for Personal Hub."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final, Literal, Protocol, runtime_checkable
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from personal_hub.contracts import SourceRef

CalendarState = Literal[
    "ready",
    "empty",
    "stale",
    "offline",
    "permission_required",
    "disconnected",
    "error",
]
CalendarStatusState = CalendarState
_IDENTIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z",
    re.ASCII,
)
_CALENDAR_IDENTIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._:%+@#=-]{0,255}\Z",
    re.ASCII,
)
_EVENT_IDENTIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z0-9_][A-Za-z0-9._:%+@#=-]{0,255}\Z",
    re.ASCII,
)
_SAFE_LINK_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[^\x00-\x20\x7f]+$")
_EVENT_TIMEZONE_MAX_LENGTH: Final[int] = 128
_MAX_EVENT_TITLE_LENGTH: Final[int] = 240
_MAX_EVENT_LOCATION_LENGTH: Final[int] = 512
_MAX_CALENDAR_NAME_LENGTH: Final[int] = 240
_MAX_CALENDAR_EVENTS: Final[int] = 5_000
_MAX_CALENDAR_IDS: Final[int] = 100
_MAX_QUERY_HORIZON: Final[timedelta] = timedelta(days=366)


class CalendarViewState(StrEnum):
    """Stable UI states for calendar source results."""

    READY = "ready"
    EMPTY = "empty"
    STALE = "stale"
    OFFLINE = "offline"
    PERMISSION_REQUIRED = "permission_required"
    DISCONNECTED = "disconnected"
    ERROR = "error"


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("calendar timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _identifier(value: str, field_name: str) -> str:
    normalized = value.strip()
    if _IDENTIFIER_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"{field_name} must be a bounded identifier")
    return normalized


def _calendar_identifier(value: str) -> str:
    normalized = value.strip()
    if _CALENDAR_IDENTIFIER_PATTERN.fullmatch(normalized) is None:
        raise ValueError("calendar_id must be a bounded identifier")
    return normalized


def _event_identifier(value: str) -> str:
    normalized = value.strip()
    if _EVENT_IDENTIFIER_PATTERN.fullmatch(normalized) is None:
        raise ValueError("event_id must be a bounded identifier")
    return normalized


def _timezone(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > _EVENT_TIMEZONE_MAX_LENGTH:
        raise ValueError("calendar timezone must be a bounded IANA name")
    try:
        ZoneInfo(normalized)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"Unknown calendar timezone: {normalized}") from error
    return normalized


def _safe_text(value: str, field_name: str, limit: int) -> str:
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > limit:
        raise ValueError(f"{field_name} must be a bounded non-empty string")
    return normalized


def _safe_link(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if len(normalized) > 1_024 or _SAFE_LINK_PATTERN.fullmatch(normalized) is None:
        raise ValueError("calendar deep link contains unsafe characters")
    parsed = urlsplit(normalized)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("calendar deep links must use HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("calendar deep link must not contain credentials")
    if "\\" in normalized or any(character in normalized for character in "\r\n"):
        raise ValueError("calendar deep link must not contain escapes")
    return normalized


class CalendarContract(BaseModel):
    """Strict base for all provider-neutral calendar values."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        validate_default=True,
    )


class CalendarQuery(CalendarContract):
    """Bounded time window and explicit calendar selection."""

    starts_at: datetime
    ends_at: datetime
    calendar_ids: tuple[str, ...] = Field(default=(), max_length=_MAX_CALENDAR_IDS)
    timezone: str = "UTC"
    limit: int = Field(default=500, ge=1, le=_MAX_CALENDAR_EVENTS)

    @field_validator("starts_at", "ends_at")
    @classmethod
    def _query_times_are_utc(cls, value: datetime) -> datetime:
        normalized = _utc(value)
        assert normalized is not None
        return normalized

    @field_validator("calendar_ids")
    @classmethod
    def _calendar_ids_are_bounded(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_calendar_identifier(item) for item in value)
        if len(set(normalized)) != len(normalized):
            raise ValueError("calendar_ids must be unique")
        return normalized

    @field_validator("timezone")
    @classmethod
    def _query_timezone_is_iana(cls, value: str) -> str:
        return _timezone(value)

    @model_validator(mode="after")
    def _query_window_is_ordered(self) -> CalendarQuery:
        if self.ends_at <= self.starts_at:
            raise ValueError("calendar query ends_at must follow starts_at")
        if self.ends_at - self.starts_at > _MAX_QUERY_HORIZON:
            raise ValueError("calendar query window cannot exceed 366 days")
        return self

    @property
    def selected_calendar_ids(self) -> tuple[str, ...]:
        return self.calendar_ids


class CalendarEvent(CalendarContract):
    """A bounded timed or all-day event from one selected calendar."""

    event_id: str = Field(min_length=1, max_length=256)
    calendar_id: str = Field(min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=_MAX_EVENT_TITLE_LENGTH)
    starts_at: datetime
    ends_at: datetime
    all_day: bool = False
    timezone: str = "UTC"
    source: SourceRef
    deep_link: str | None = Field(default=None, max_length=1_024)
    location: str | None = Field(default=None, max_length=_MAX_EVENT_LOCATION_LENGTH)
    sensitive: bool = True

    @field_validator("event_id")
    @classmethod
    def _event_id_is_stable(cls, value: str) -> str:
        return _event_identifier(value)

    @field_validator("calendar_id")
    @classmethod
    def _calendar_id_is_stable(cls, value: str) -> str:
        return _calendar_identifier(value)

    @field_validator("title")
    @classmethod
    def _title_is_safe(cls, value: str) -> str:
        return _safe_text(value, "event title", _MAX_EVENT_TITLE_LENGTH)

    @field_validator("location")
    @classmethod
    def _location_is_safe(cls, value: str | None) -> str | None:
        return (
            None
            if value is None
            else _safe_text(value, "event location", _MAX_EVENT_LOCATION_LENGTH)
        )

    @field_validator("starts_at", "ends_at")
    @classmethod
    def _event_times_are_utc(cls, value: datetime) -> datetime:
        normalized = _utc(value)
        assert normalized is not None
        return normalized

    @field_validator("timezone")
    @classmethod
    def _event_timezone_is_iana(cls, value: str) -> str:
        return _timezone(value)

    @field_validator("deep_link")
    @classmethod
    def _deep_link_is_safe(cls, value: str | None) -> str | None:
        return _safe_link(value)

    @model_validator(mode="after")
    def _event_window_is_ordered(self) -> CalendarEvent:
        if self.ends_at <= self.starts_at:
            raise ValueError("calendar event ends_at must follow starts_at")
        return self

    @property
    def start(self) -> datetime:
        """Compatibility alias for callers that use the shorter bound name."""

        return self.starts_at

    @property
    def end(self) -> datetime:
        """Compatibility alias for callers that use the shorter bound name."""

        return self.ends_at

    @property
    def is_all_day(self) -> bool:
        return self.all_day

    @property
    def url(self) -> str | None:
        return self.deep_link


class CalendarSnapshot(CalendarContract):
    """In-memory last-good event snapshot; it is never persisted by this core."""

    source_id: str = Field(min_length=1, max_length=128)
    provider_id: str = Field(min_length=1, max_length=128)
    collected_at: datetime
    events: tuple[CalendarEvent, ...] = Field(default=(), max_length=_MAX_CALENDAR_EVENTS)
    selected_calendar_ids: tuple[str, ...] = Field(default=(), max_length=_MAX_CALENDAR_IDS)
    available_calendar_ids: tuple[str, ...] = Field(default=(), max_length=_MAX_CALENDAR_IDS)
    calendar_display_names: dict[str, str] = Field(
        default_factory=dict,
        max_length=_MAX_CALENDAR_IDS,
    )
    is_complete: bool = True

    @field_validator("source_id", "provider_id")
    @classmethod
    def _snapshot_ids_are_stable(cls, value: str, _info: object) -> str:
        return _identifier(value, "calendar snapshot identifier")

    @field_validator("collected_at")
    @classmethod
    def _snapshot_time_is_utc(cls, value: datetime) -> datetime:
        normalized = _utc(value)
        assert normalized is not None
        return normalized

    @field_validator("selected_calendar_ids", "available_calendar_ids")
    @classmethod
    def _snapshot_calendar_ids_are_stable(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_calendar_identifier(item) for item in value)
        if len(set(normalized)) != len(normalized):
            raise ValueError("snapshot calendar_ids must be unique")
        return normalized

    @field_validator("calendar_display_names")
    @classmethod
    def _snapshot_calendar_names_are_safe(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for calendar_id, display_name in value.items():
            normalized_id = _calendar_identifier(calendar_id)
            normalized[normalized_id] = _safe_text(
                display_name,
                "calendar display name",
                _MAX_CALENDAR_NAME_LENGTH,
            )
        return normalized

    @property
    def calendar_ids(self) -> tuple[str, ...]:
        """Alias exposing the calendars represented by this snapshot."""

        return self.selected_calendar_ids


class CalendarStatus(CalendarContract):
    """Safe operational state with no provider response/content fields."""

    source_id: str = Field(min_length=1, max_length=128)
    provider_id: str = Field(min_length=1, max_length=128)
    state: CalendarState
    checked_at: datetime
    item_count: int = Field(default=0, ge=0, le=_MAX_CALENDAR_EVENTS)
    safe_message: str = Field(default="Calendar state is available.", max_length=240)

    @field_validator("source_id", "provider_id")
    @classmethod
    def _status_ids_are_stable(cls, value: str, _info: object) -> str:
        return _identifier(value, "calendar status identifier")

    @field_validator("checked_at")
    @classmethod
    def _status_time_is_utc(cls, value: datetime) -> datetime:
        normalized = _utc(value)
        assert normalized is not None
        return normalized

    @field_validator("safe_message")
    @classmethod
    def _status_message_is_safe(cls, value: str) -> str:
        return _safe_text(value, "calendar status message", 240)


class CalendarResult(CalendarContract):
    """Result of one refresh, optionally carrying a last-good snapshot."""

    source_id: str = Field(min_length=1, max_length=128)
    provider_id: str = Field(min_length=1, max_length=128)
    state: CalendarState
    checked_at: datetime
    snapshot: CalendarSnapshot | None = None
    last_success_at: datetime | None = None
    safe_message: str = Field(max_length=240)

    @field_validator("source_id", "provider_id")
    @classmethod
    def _result_ids_are_stable(cls, value: str, _info: object) -> str:
        return _identifier(value, "calendar result identifier")

    @field_validator("checked_at", "last_success_at")
    @classmethod
    def _result_times_are_utc(cls, value: datetime | None) -> datetime | None:
        return _utc(value)

    @field_validator("safe_message")
    @classmethod
    def _result_message_is_safe(cls, value: str) -> str:
        return _safe_text(value, "calendar result message", 240)

    @property
    def events(self) -> tuple[CalendarEvent, ...]:
        """Return snapshot events without requiring callers to branch on state."""

        return () if self.snapshot is None else self.snapshot.events


class CalendarError(RuntimeError):
    """Base adapter error that is safe to classify without exposing content."""


class CalendarPermissionRequiredError(CalendarError):
    """The account lacks the minimum read permission."""


class CalendarDisconnectedError(CalendarError):
    """The account or source connection is not available."""


class CalendarUnavailableError(CalendarError):
    """The provider is unavailable or rate-limited."""


class CalendarInvalidResponseError(CalendarError):
    """The provider response exceeded bounds or failed schema validation."""


class CalendarOfflineError(CalendarError):
    """The source was deliberately prevented from making external calls."""


@runtime_checkable
class CalendarSource(Protocol):
    """Read-only provider boundary used by :class:`CalendarService`."""

    source_id: str
    provider_id: str
    display_name: str

    async def status(self) -> CalendarStatus: ...

    async def list_events(self, query: CalendarQuery) -> CalendarSnapshot: ...


CalendarProtocol = CalendarSource
HubCalendarSource = CalendarSource
CalendarEventRecord = CalendarEvent
CalendarQueryRequest = CalendarQuery
CalendarSnapshotResult = CalendarSnapshot
CalendarHealth = CalendarStatus


__all__ = [
    "CalendarContract",
    "CalendarDisconnectedError",
    "CalendarError",
    "CalendarEvent",
    "CalendarEventRecord",
    "CalendarHealth",
    "CalendarInvalidResponseError",
    "CalendarOfflineError",
    "CalendarPermissionRequiredError",
    "CalendarProtocol",
    "CalendarQuery",
    "CalendarQueryRequest",
    "CalendarResult",
    "CalendarSnapshot",
    "CalendarSnapshotResult",
    "CalendarSource",
    "CalendarState",
    "CalendarStatus",
    "CalendarStatusState",
    "CalendarUnavailableError",
    "CalendarViewState",
    "HubCalendarSource",
]
