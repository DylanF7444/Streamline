"""Bounded plain-text contracts; Logging never participates in provider refreshes."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator

from personal_hub.contracts import HubContract
from personal_hub.contracts import _utc as contract_utc


def _utc(value: datetime | None) -> datetime | None:
    return contract_utc(value) if value is not None else None


class ActivityEntry(HubContract):
    entry_id: UUID = Field(default_factory=uuid4)
    kind: Literal["task_completion", "manual"] = "manual"
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    description: str = Field(min_length=1, max_length=240)
    notes: str = Field(default="", max_length=20_000)
    duration_minutes: int | None = Field(default=None, ge=1, le=525_600)
    task_id: UUID | None = None
    occurrence_key: str | None = Field(default=None, max_length=256)
    title_snapshot: str | None = Field(default=None, max_length=240)
    list_id: UUID | None = None
    list_name: str | None = Field(default=None, max_length=160)
    tags: tuple[str, ...] = Field(default=(), max_length=32)
    undone_at: datetime | None = None

    _times = field_validator("occurred_at", "created_at", "updated_at", "undone_at")(_utc)

    @field_validator("tags")
    @classmethod
    def _tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        tags = tuple(tag.strip().casefold() for tag in value)
        if len(set(tags)) != len(tags) or any(
            re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", tag) is None for tag in tags
        ):
            raise ValueError("tags must be unique simple names")
        return tuple(sorted(tags))

    @field_validator("description")
    @classmethod
    def _description(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("describe the work or result")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> ActivityEntry:
        if self.kind == "task_completion":
            if self.task_id is None or not self.title_snapshot:
                raise ValueError("completion requires task identity and title snapshot")
            if self.description != self.title_snapshot or self.duration_minutes is not None:
                raise ValueError("completion facts are immutable; duration is manual input only")
        elif self.undone_at is not None or self.title_snapshot is not None:
            raise ValueError("manual entries cannot be completion corrections")
        if self.updated_at < self.created_at:
            raise ValueError("update cannot precede creation")
        return self


class ReflectionEntry(HubContract):
    reflection_id: UUID = Field(default_factory=uuid4)
    display_date: date = Field(default_factory=date.today)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    title: str = Field(default="", max_length=240)
    body: str = Field(default="", max_length=20_000)
    task_id: UUID | None = None
    activity_ids: tuple[UUID, ...] = Field(default=(), max_length=100)

    _times = field_validator("created_at", "updated_at")(_utc)

    @model_validator(mode="after")
    def _consistent(self) -> ReflectionEntry:
        if self.updated_at < self.created_at:
            raise ValueError("update cannot precede creation")
        return self


class LogFilter(HubContract):
    start: date | None = None
    end: date | None = None
    timezone: str = "UTC"
    search: str = Field(default="", max_length=240)
    kind: Literal["task_completion", "manual"] | None = None
    list_id: UUID | None = None
    tag: str | None = Field(default=None, max_length=32)
    include_undone: bool = False
    limit: int = Field(default=200, ge=1, le=10_000)
    offset: int = Field(default=0, ge=0, le=1_000_000)

    @model_validator(mode="after")
    def _consistent(self) -> LogFilter:
        ZoneInfo(self.timezone)
        if self.start and self.end and self.end < self.start:
            raise ValueError("end date must follow start date")
        return self
