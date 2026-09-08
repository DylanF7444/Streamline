"""Typed, local-only contracts for the Personal Hub Today projection."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from personal_hub.contracts import SourceRef


class TodaySection(StrEnum):
    """Stable order used by the Today surface and deterministic digests."""

    OVERDUE = "overdue"
    TODAY = "today"
    UPCOMING = "upcoming"
    UNSCHEDULED = "unscheduled"


class TodayItem(BaseModel):
    """A bounded task projection item suitable for a replaceable desktop UI."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
    )

    item_id: str = Field(min_length=1, max_length=160)
    kind: Literal["event", "task", "message", "alert", "news", "source_problem"] = "task"
    title: str = Field(min_length=1, max_length=240)
    summary: str | None = Field(default=None, max_length=1_000)
    starts_at: datetime | None = None
    due_at: datetime | None = None
    severity: str = Field(default="info", pattern=r"^(?:info|attention|urgent)$")
    source: SourceRef
    navigation_target: str = Field(min_length=1, max_length=512)
    sensitive: bool = False
    due_is_all_day: bool = False
    is_completed: bool = False
    completed_at: datetime | None = None

    @field_validator("starts_at", "due_at", "completed_at")
    @classmethod
    def _timestamps_are_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Today timestamps must be timezone-aware")
        return value


class TodayProjection(BaseModel):
    """Ordered, bounded Today sections projected from local task records."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
    )

    generated_at: datetime
    timezone: str = Field(min_length=1, max_length=128)
    overdue: tuple[TodayItem, ...] = Field(default_factory=tuple, max_length=1_000)
    today: tuple[TodayItem, ...] = Field(default_factory=tuple, max_length=1_000)
    upcoming: tuple[TodayItem, ...] = Field(default_factory=tuple, max_length=1_000)
    unscheduled: tuple[TodayItem, ...] = Field(default_factory=tuple, max_length=1_000)

    @field_validator("generated_at")
    @classmethod
    def _generated_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Today generated_at must be timezone-aware")
        return value

    @property
    def sections(self) -> tuple[tuple[TodaySection, tuple[TodayItem, ...]], ...]:
        """Return sections in the documented stable display order."""

        return (
            (TodaySection.OVERDUE, self.overdue),
            (TodaySection.TODAY, self.today),
            (TodaySection.UPCOMING, self.upcoming),
            (TodaySection.UNSCHEDULED, self.unscheduled),
        )

    @property
    def items(self) -> tuple[TodayItem, ...]:
        return tuple(item for _, section in self.sections for item in section)

    @property
    def all_items(self) -> tuple[TodayItem, ...]:
        return self.items

    @property
    def item_count(self) -> int:
        return len(self.items)

    def section(self, name: TodaySection | str) -> tuple[TodayItem, ...]:
        selected = TodaySection(name)
        return dict(self.sections)[selected]


@runtime_checkable
class TaskLike(Protocol):
    """Structural view of the HubTask fields required by Today/reminders."""

    task_id: UUID | str
    title: str
    state: str
    priority: str
    due_at: datetime | date | None
    due_is_all_day: bool
    timezone: str
    reminder_offsets_minutes: tuple[int, ...]
    created_at: datetime
    updated_at: datetime


@runtime_checkable
class TaskRepositoryProtocol(Protocol):
    """Minimal repository boundary consumed by Today projection services."""

    def list(self, *args: object, **kwargs: object) -> object: ...


__all__ = [
    "TaskLike",
    "TaskRepositoryProtocol",
    "TodayItem",
    "TodayProjection",
    "TodaySection",
]
