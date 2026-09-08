"""Calendar arithmetic for the bounded Hub recurrence contract."""

from __future__ import annotations

import calendar
from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from personal_hub.tasks.contracts import Recurrence


def next_due_at(
    due_at: datetime,
    recurrence: Recurrence,
    *,
    timezone: str = "UTC",
    occurrence_number: int = 1,
    anchor_due_at: datetime | None = None,
) -> datetime | None:
    """Return the next due instant, preserving local wall-clock semantics.

    ``occurrence_number`` is one-based and counts the already-completed
    occurrence.  This makes ``max_occurrences`` deterministic across process
    restarts and lets the repository derive a stable next occurrence key.
    ``None`` means the recurrence has reached one of its explicit bounds.
    """

    if not isinstance(due_at, datetime) or due_at.tzinfo is None or due_at.utcoffset() is None:
        raise ValueError("recurrence due_at must be timezone-aware")
    if anchor_due_at is not None and (
        not isinstance(anchor_due_at, datetime)
        or anchor_due_at.tzinfo is None
        or anchor_due_at.utcoffset() is None
    ):
        raise ValueError("recurrence anchor_due_at must be timezone-aware")
    if not isinstance(recurrence, Recurrence):
        raise TypeError("recurrence must be a Recurrence contract")
    if type(occurrence_number) is not int or occurrence_number < 1:
        raise ValueError("occurrence_number must be a positive integer")
    if recurrence.max_occurrences is not None and occurrence_number >= recurrence.max_occurrences:
        return None

    zone = ZoneInfo(timezone)
    local_start = due_at.astimezone(zone).replace(tzinfo=None)
    local_anchor = (
        local_start
        if anchor_due_at is None
        else anchor_due_at.astimezone(zone).replace(tzinfo=None)
    )
    if recurrence.frequency == "daily":
        local_candidate = local_start + timedelta(days=recurrence.interval)
    elif recurrence.frequency == "weekly":
        local_candidate = _next_weekly(local_start, recurrence)
    elif recurrence.frequency == "monthly":
        local_candidate = _next_monthly(local_start, recurrence, anchor_day=local_anchor.day)
    else:
        local_candidate = _next_yearly(
            local_start,
            recurrence,
            anchor_month=local_anchor.month,
            anchor_day=local_anchor.day,
        )

    candidate = _from_local(local_candidate, zone)
    if recurrence.until is not None and candidate > recurrence.until:
        return None
    return candidate


def _next_weekly(local_start: datetime, recurrence: Recurrence) -> datetime:
    if not recurrence.weekdays:
        return local_start + timedelta(weeks=recurrence.interval)

    current_date = local_start.date()
    week_start = current_date - timedelta(days=current_date.weekday())
    for weekday in recurrence.weekdays:
        candidate_date = week_start + timedelta(days=weekday)
        if candidate_date > current_date:
            return local_start.replace(
                year=candidate_date.year,
                month=candidate_date.month,
                day=candidate_date.day,
            )

    candidate_week = week_start + timedelta(weeks=recurrence.interval)
    candidate_date = candidate_week + timedelta(days=recurrence.weekdays[0])
    return local_start.replace(
        year=candidate_date.year,
        month=candidate_date.month,
        day=candidate_date.day,
    )


def _next_monthly(
    local_start: datetime,
    recurrence: Recurrence,
    *,
    anchor_day: int,
) -> datetime:
    month_index = local_start.year * 12 + local_start.month - 1 + recurrence.interval
    year, month_index = divmod(month_index, 12)
    month = month_index + 1
    day = recurrence.day_of_month or anchor_day
    day = min(day, calendar.monthrange(year, month)[1])
    return local_start.replace(year=year, month=month, day=day)


def _next_yearly(
    local_start: datetime,
    recurrence: Recurrence,
    *,
    anchor_month: int,
    anchor_day: int,
) -> datetime:
    year = local_start.year + recurrence.interval
    month = recurrence.month or anchor_month
    day = recurrence.day_of_month or anchor_day
    day = min(day, calendar.monthrange(year, month)[1])
    return local_start.replace(year=year, month=month, day=day)


def _from_local(value: datetime, zone: ZoneInfo) -> datetime:
    """Convert a local wall time to UTC while keeping the zone's fold policy."""

    return value.replace(tzinfo=zone).astimezone(UTC)


def occurrence_key(series_id: UUID | str, occurrence_number: int) -> str:
    """Build the stable database key used to deduplicate a series occurrence."""

    if isinstance(series_id, UUID):
        series_value = str(series_id)
    elif type(series_id) is str and series_id.strip():
        series_value = series_id.strip()
    else:
        raise ValueError("series_id must be a non-empty identifier")
    if type(occurrence_number) is not int or occurrence_number < 1:
        raise ValueError("occurrence_number must be positive")
    return f"{series_value}:{occurrence_number}"


def next_occurrence(
    due_at: datetime,
    recurrence: Recurrence,
    *,
    timezone: str = "UTC",
    occurrence_number: int = 1,
    anchor_due_at: datetime | None = None,
) -> datetime | None:
    """Compatibility name for :func:`next_due_at`."""

    return next_due_at(
        due_at,
        recurrence,
        timezone=timezone,
        occurrence_number=occurrence_number,
        anchor_due_at=anchor_due_at,
    )


calculate_next_due = next_due_at
advance_recurrence = next_due_at


__all__ = [
    "Recurrence",
    "advance_recurrence",
    "calculate_next_due",
    "next_due_at",
    "next_occurrence",
    "occurrence_key",
]
