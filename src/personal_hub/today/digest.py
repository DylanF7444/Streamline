"""Deterministic daily digest foundation over the Today projection."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from datetime import date as Date

from pydantic import BaseModel, ConfigDict, Field, field_validator

from personal_hub.today.contracts import TaskLike, TodayItem, TodayProjection
from personal_hub.today.projection import TodayProjector


class DigestSection(BaseModel):
    """One bounded, ordered section in a daily task digest."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
    )

    section_id: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=80)
    items: tuple[TodayItem, ...] = Field(default_factory=tuple, max_length=1_000)

    @property
    def item_count(self) -> int:
        return len(self.items)


class DailyDigest(BaseModel):
    """Stable local digest envelope with no provider or AI dependency."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
    )

    digest_id: str = Field(min_length=1, max_length=192)
    digest_date: Date
    timezone: str = Field(min_length=1, max_length=128)
    generated_at: datetime
    sections: tuple[DigestSection, ...] = Field(max_length=4)
    item_count: int = Field(ge=0, le=4_000)

    @field_validator("generated_at")
    @classmethod
    def _generated_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("digest generated_at must be timezone-aware")
        return value

    def to_text(self) -> str:
        """Render a deterministic plain-text digest for local preview or copy."""

        lines = [f"Daily digest · {self.digest_date.isoformat()} ({self.timezone})"]
        for section in self.sections:
            lines.append(f"{section.title} ({section.item_count})")
            for item in section.items:
                lines.append(f"- {item.title}")
        return "\n".join(lines)

    @property
    def date(self) -> Date:
        return self.digest_date

    @property
    def items(self) -> tuple[TodayItem, ...]:
        return tuple(item for section in self.sections for item in section.items)

    render = to_text


class DailyDigestBuilder:
    """Build task sections in stable order from a Today projection or task iterable."""

    _SECTION_TITLES = {
        "overdue": "Overdue tasks",
        "today": "Due today",
        "upcoming": "Upcoming tasks",
        "unscheduled": "Unscheduled tasks",
    }

    def __init__(
        self,
        projector: TodayProjector | None = None,
        *,
        timezone: str = "UTC",
        horizon_days: int = 7,
        section_limit: int = 100,
        include_empty_sections: bool = False,
    ) -> None:
        self.projector = projector or TodayProjector(
            timezone=timezone,
            horizon_days=horizon_days,
            section_limit=section_limit,
        )
        self.include_empty_sections = include_empty_sections

    def build(
        self,
        tasks_or_projection: Iterable[TaskLike] | TodayProjection,
        *,
        now: datetime | None = None,
        timezone: str | None = None,
        horizon_days: int | None = None,
        limit: int | None = None,
    ) -> DailyDigest:
        if isinstance(tasks_or_projection, TodayProjection):
            projection = tasks_or_projection
            current = projection.generated_at if now is None else now
            if now is not None and current != projection.generated_at:
                raise ValueError("now cannot differ from a supplied TodayProjection generated_at")
        else:
            current = now or datetime.now(UTC)
            projection = self.projector.project(
                tasks_or_projection,
                now=current,
                timezone=timezone,
                horizon_days=horizon_days,
                limit=limit,
            )
        return self._from_projection(projection, generated_at=current)

    build_digest = build
    build_daily_digest = build

    def _from_projection(
        self,
        projection: TodayProjection,
        *,
        generated_at: datetime,
    ) -> DailyDigest:
        sections: list[DigestSection] = []
        for section_name, items in (
            ("overdue", projection.overdue),
            ("today", projection.today),
            ("upcoming", projection.upcoming),
            ("unscheduled", projection.unscheduled),
        ):
            if items or self.include_empty_sections:
                sections.append(
                    DigestSection(
                        section_id=section_name,
                        title=self._SECTION_TITLES[section_name],
                        items=items,
                    )
                )
        digest_date = projection.generated_at.astimezone(_projection_zone(projection)).date()
        return DailyDigest(
            digest_id=f"daily:{projection.timezone}:{digest_date.isoformat()}",
            digest_date=digest_date,
            timezone=projection.timezone,
            generated_at=generated_at,
            sections=tuple(sections),
            item_count=sum(section.item_count for section in sections),
        )


class DailyDigestService(DailyDigestBuilder):
    """Named service alias for desktop/scheduler composition."""


DigestBuilder = DailyDigestBuilder
DigestService = DailyDigestService


def build_daily_digest(
    tasks: Iterable[TaskLike],
    *,
    now: datetime | None = None,
    timezone: str = "UTC",
    horizon_days: int = 7,
    limit: int = 100,
) -> DailyDigest:
    return DailyDigestBuilder(
        timezone=timezone,
        horizon_days=horizon_days,
        section_limit=limit,
    ).build(tasks, now=now)


def _projection_zone(projection: TodayProjection):
    # The projector validates this value before constructing TodayProjection.  Importing
    # ZoneInfo here keeps the contract model free of timezone implementation details.
    from zoneinfo import ZoneInfo

    return ZoneInfo(projection.timezone)


__all__ = [
    "DailyDigest",
    "DailyDigestBuilder",
    "DailyDigestService",
    "DigestBuilder",
    "DigestSection",
    "DigestService",
    "build_daily_digest",
]
