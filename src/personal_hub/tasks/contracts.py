"""Strict, local-only contracts for Personal Hub tasks."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Final, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator

from personal_hub.contracts import HubContract

TaskState = Literal["open", "completed", "archived"]
TaskPriority = Literal["none", "low", "normal", "high"]
RecurrenceFrequency = Literal["daily", "weekly", "monthly", "yearly"]
TaskSortField = Literal["due_at", "created_at", "updated_at", "priority", "title"]

_TAG_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}\Z", re.ASCII)
_MAX_NOTES_LENGTH: Final[int] = 20_000
_MAX_EXTERNAL_URL_LENGTH: Final[int] = 1_024
_MAX_REMINDER_OFFSET_MINUTES: Final[int] = 43_200
_MAX_RECURRENCE_OCCURRENCES: Final[int] = 10_000


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("task timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _timezone(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("task timezone cannot be blank")
    try:
        ZoneInfo(normalized)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"Unknown task time zone: {normalized}") from error
    return normalized


def _external_url(value: str | None) -> str | None:
    """Keep provider navigation links ordinary, bounded HTTP(S) URLs."""

    if value is None:
        return None
    candidate = value.strip()
    if (
        not candidate
        or len(candidate) > _MAX_EXTERNAL_URL_LENGTH
        or any(character in candidate for character in "\r\n\x00\\")
    ):
        raise ValueError("task external URL is invalid")
    try:
        parts = urlsplit(candidate)
    except ValueError as error:
        raise ValueError("task external URL is invalid") from error
    if (
        parts.scheme.casefold() not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
    ):
        raise ValueError("task external URL must use HTTP or HTTPS without credentials")
    return candidate


class Recurrence(HubContract):
    """Bounded calendar recurrence; arbitrary rule expressions are forbidden."""

    frequency: RecurrenceFrequency
    interval: int = Field(default=1, ge=1, le=365)
    weekdays: tuple[int, ...] = Field(default=(), max_length=7)
    day_of_month: int | None = Field(default=None, ge=1, le=31)
    month: int | None = Field(default=None, ge=1, le=12)
    until: datetime | None = None
    max_occurrences: int | None = Field(
        default=None,
        ge=1,
        le=_MAX_RECURRENCE_OCCURRENCES,
    )

    @field_validator("weekdays")
    @classmethod
    def _weekdays_are_monday_zero(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(day < 0 or day > 6 for day in value):
            raise ValueError("recurrence weekdays must use Monday=0 through Sunday=6")
        if len(set(value)) != len(value):
            raise ValueError("recurrence weekdays must be unique")
        return tuple(sorted(value))

    @field_validator("until")
    @classmethod
    def _until_is_aware(cls, value: datetime | None) -> datetime | None:
        return _utc(value)

    @model_validator(mode="after")
    def _pattern_fields_are_supported(self) -> Recurrence:
        if self.frequency == "daily" and (
            self.weekdays or self.day_of_month is not None or self.month is not None
        ):
            raise ValueError("daily recurrence accepts only interval and end limits")
        if self.frequency == "weekly" and (self.day_of_month is not None or self.month is not None):
            raise ValueError("weekly recurrence does not accept month-day fields")
        if self.frequency == "monthly" and (self.weekdays or self.month is not None):
            raise ValueError("monthly recurrence accepts only day_of_month")
        if self.frequency == "yearly" and self.weekdays:
            raise ValueError("yearly recurrence does not accept weekdays")
        # Both bounds are useful, but a year-long unbounded task should not
        # accidentally become an unbounded persisted series.
        if (
            self.until is not None
            and self.max_occurrences is not None
            and self.max_occurrences > _MAX_RECURRENCE_OCCURRENCES
        ):
            raise ValueError("recurrence occurrence bound is too large")
        return self


class HubTask(HubContract):
    """A fully local task record owned by Personal Hub."""

    task_id: UUID = Field(default_factory=uuid4)
    title: str = Field(min_length=1, max_length=240)
    notes: str = Field(default="", max_length=_MAX_NOTES_LENGTH)
    external_url: str | None = Field(default=None, max_length=_MAX_EXTERNAL_URL_LENGTH)
    state: TaskState = "open"
    priority: TaskPriority = "normal"
    due_at: datetime | None = None
    due_is_all_day: bool = False
    timezone: str = "UTC"
    list_id: UUID | None = None
    tags: tuple[str, ...] = Field(default=(), max_length=32)
    recurrence: Recurrence | None = None
    reminder_offsets_minutes: tuple[int, ...] = Field(default=(), max_length=16)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None

    @field_validator("title")
    @classmethod
    def _title_is_single_line(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("task title cannot be blank")
        return normalized

    @field_validator("notes")
    @classmethod
    def _notes_are_bounded(cls, value: str) -> str:
        if len(value) > _MAX_NOTES_LENGTH:
            raise ValueError("task notes exceed the size limit")
        return value

    @field_validator("external_url")
    @classmethod
    def _external_url_is_safe(cls, value: str | None) -> str | None:
        return _external_url(value)

    @field_validator("due_at", "created_at", "updated_at", "completed_at")
    @classmethod
    def _timestamps_are_utc(cls, value: datetime | None) -> datetime | None:
        return _utc(value)

    @field_validator("timezone")
    @classmethod
    def _timezone_is_known(cls, value: str) -> str:
        return _timezone(value)

    @field_validator("tags")
    @classmethod
    def _tags_are_bounded_and_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip().casefold() for item in value)
        if any(_TAG_PATTERN.fullmatch(item) is None for item in normalized):
            raise ValueError("task tags must be simple bounded names")
        if len(set(normalized)) != len(normalized):
            raise ValueError("task tags must be unique")
        return tuple(sorted(normalized))

    @field_validator("reminder_offsets_minutes")
    @classmethod
    def _reminders_are_bounded(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(offset < 0 or offset > _MAX_REMINDER_OFFSET_MINUTES for offset in value):
            raise ValueError("reminder offsets must be between 0 and 43200 minutes")
        if len(set(value)) != len(value):
            raise ValueError("reminder offsets must be unique")
        return tuple(sorted(value))

    @model_validator(mode="after")
    def _task_fields_are_consistent(self) -> HubTask:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        if self.due_is_all_day and self.due_at is None:
            raise ValueError("all-day tasks require a due date")
        if self.recurrence is not None and self.due_at is None:
            raise ValueError("recurring tasks require a due date")
        if self.reminder_offsets_minutes and self.due_at is None:
            raise ValueError("reminders require a due date")
        if self.state == "open" and self.completed_at is not None:
            raise ValueError("open tasks cannot have completed_at")
        if (
            self.state == "completed"
            and self.completed_at is not None
            and self.completed_at < self.created_at
        ):
            raise ValueError("completed_at cannot precede created_at")
        return self


class HubTaskList(HubContract):
    """A named local list used to group tasks."""

    list_id: UUID = Field(default_factory=uuid4)
    name: str = Field(min_length=1, max_length=160)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("name")
    @classmethod
    def _list_name_is_single_line(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("task list name cannot be blank")
        return normalized

    @field_validator("created_at", "updated_at")
    @classmethod
    def _list_timestamps_are_utc(cls, value: datetime) -> datetime:
        normalized = _utc(value)
        assert normalized is not None
        return normalized

    @model_validator(mode="after")
    def _list_timestamps_are_ordered(self) -> HubTaskList:
        if self.updated_at < self.created_at:
            raise ValueError("task list updated_at cannot precede created_at")
        return self


class TaskFilter(HubContract):
    """Strict, serializable task query options."""

    states: tuple[TaskState, ...] = ()
    priorities: tuple[TaskPriority, ...] = ()
    list_id: UUID | None = None
    tags: tuple[str, ...] = ()
    search: str | None = Field(default=None, max_length=240)
    due_before: datetime | None = None
    due_after: datetime | None = None
    due_on: date | None = None
    include_archived: bool = True
    sort_by: TaskSortField = "due_at"
    descending: bool = False
    limit: int = Field(default=100, ge=1, le=10_000)
    offset: int = Field(default=0, ge=0, le=1_000_000)

    @field_validator("tags")
    @classmethod
    def _filter_tags_are_bounded(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip().casefold() for item in value)
        if any(_TAG_PATTERN.fullmatch(item) is None for item in normalized):
            raise ValueError("task filter tags must be simple names")
        if len(set(normalized)) != len(normalized):
            raise ValueError("task filter tags must be unique")
        return tuple(sorted(normalized))

    @field_validator("due_before", "due_after")
    @classmethod
    def _filter_times_are_utc(cls, value: datetime | None) -> datetime | None:
        return _utc(value)

    @model_validator(mode="after")
    def _filter_dates_are_ordered(self) -> TaskFilter:
        if (
            self.due_before is not None
            and self.due_after is not None
            and self.due_before < self.due_after
        ):
            raise ValueError("due_before cannot precede due_after")
        return self


# Concise aliases make the task package easy to compose without duplicating models.
Task = HubTask
TaskList = HubTaskList
RecurrenceRule = Recurrence


__all__ = [
    "HubTask",
    "HubTaskList",
    "Recurrence",
    "RecurrenceFrequency",
    "RecurrenceRule",
    "Task",
    "TaskFilter",
    "TaskList",
    "TaskPriority",
    "TaskSortField",
    "TaskState",
]
