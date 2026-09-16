"""Timezone-aware hourly calendar layout helpers.

The provider calendar remains read-only.  This module only turns already validated
calendar items (including local due-dated tasks) into bounded display placements for
an hour-by-hour day or week surface.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MINUTES_PER_DAY = 24 * 60
DEFAULT_TIMED_DURATION_MINUTES = 30
MAX_SCHEDULE_DAYS = 7
MAX_OVERLAP_COLUMNS = 8


@dataclass(frozen=True, slots=True)
class HourlySchedulePlacement:
    """One timed item clipped to one visible local calendar day."""

    event_id: str
    day: date
    day_index: int
    start_minutes: float
    end_minutes: float
    column: int = 0
    column_count: int = 1
    clipped_start: bool = False
    clipped_end: bool = False

    @property
    def duration_minutes(self) -> float:
        return max(1.0, self.end_minutes - self.start_minutes)


@dataclass(frozen=True, slots=True)
class AllDaySchedulePlacement:
    """One all-day item clipped to one visible local calendar day."""

    event_id: str
    day: date
    day_index: int
    clipped_start: bool = False
    clipped_end: bool = False


@dataclass(frozen=True, slots=True)
class HourlyScheduleLayout:
    """Bounded placements for a one-day or seven-day hourly surface."""

    start_date: date
    day_count: int
    timezone: str
    timed: tuple[HourlySchedulePlacement, ...] = ()
    all_day: tuple[AllDaySchedulePlacement, ...] = ()

    @property
    def end_date(self) -> date:
        return self.start_date + timedelta(days=self.day_count - 1)


def build_hourly_schedule_layout(
    events: Iterable[object],
    *,
    start_date: date,
    day_count: int,
    timezone: str,
    max_events: int = 1_000,
) -> HourlyScheduleLayout:
    """Create stable day/all-day/timed placements for visible calendar days.

    Timestamps are displayed in ``timezone``.  All-day values retain the item's
    declared timezone when one is available, which prevents an all-day event from
    moving to the adjacent day during UTC conversion.  Timed items without an end
    receive a short visual duration so due-time tasks remain visible in the correct
    hour instead of becoming zero-height chips.
    """

    if not isinstance(start_date, date) or isinstance(start_date, datetime):
        raise TypeError("start_date must be a calendar date")
    if not isinstance(day_count, int) or isinstance(day_count, bool):
        raise TypeError("day_count must be an integer")
    if not 1 <= day_count <= MAX_SCHEDULE_DAYS:
        raise ValueError(f"day_count must be between 1 and {MAX_SCHEDULE_DAYS}")
    if not isinstance(max_events, int) or isinstance(max_events, bool) or max_events < 1:
        raise ValueError("max_events must be a positive integer")
    display_zone = _zone(timezone)
    visible_end = start_date + timedelta(days=day_count)
    timed_by_day: list[list[tuple[int, str, float, float, bool, bool]]] = [
        [] for _ in range(day_count)
    ]
    all_day: list[AllDaySchedulePlacement] = []

    for sequence, event in enumerate(events):
        if sequence >= max_events:
            break
        event_id = _event_id(event, sequence)
        if _is_all_day(event):
            interval = _all_day_interval(event, display_zone)
            if interval is None:
                continue
            event_start, event_end = interval
            first = max(start_date, event_start)
            last = min(visible_end, event_end)
            current = first
            while current < last:
                index = (current - start_date).days
                all_day.append(
                    AllDaySchedulePlacement(
                        event_id=event_id,
                        day=current,
                        day_index=index,
                        clipped_start=current == start_date and event_start < start_date,
                        clipped_end=current == visible_end - timedelta(days=1)
                        and event_end > visible_end,
                    )
                )
                current += timedelta(days=1)
            continue

        interval = _timed_interval(event, display_zone)
        if interval is None:
            continue
        event_start, event_end = interval
        for day_index in range(day_count):
            current = start_date + timedelta(days=day_index)
            day_start = datetime.combine(current, time.min, tzinfo=display_zone)
            next_day = current + timedelta(days=1)
            day_end = datetime.combine(next_day, time.min, tzinfo=display_zone)
            if event_start >= day_end or event_end <= day_start:
                continue
            clipped_start = event_start < day_start
            clipped_end = event_end > day_end
            visible_start = max(event_start, day_start)
            visible_finish = min(event_end, day_end)
            start_minutes = _wall_minutes(visible_start, current, clipped_start)
            end_minutes = _wall_minutes(visible_finish, current, clipped_end, end=True)
            if end_minutes <= start_minutes:
                end_minutes = min(
                    float(MINUTES_PER_DAY), start_minutes + DEFAULT_TIMED_DURATION_MINUTES
                )
            index = day_index
            timed_by_day[index].append(
                (
                    sequence,
                    event_id,
                    start_minutes,
                    end_minutes,
                    clipped_start,
                    clipped_end,
                )
            )
    placements: list[HourlySchedulePlacement] = []
    for day_index, entries in enumerate(timed_by_day):
        placements.extend(_place_day_entries(entries, start_date, day_index))
    placements.sort(
        key=lambda item: (item.day_index, item.start_minutes, item.column, item.event_id)
    )
    all_day.sort(key=lambda item: (item.day_index, item.event_id))
    return HourlyScheduleLayout(
        start_date=start_date,
        day_count=day_count,
        timezone=_zone_name(display_zone),
        timed=tuple(placements),
        all_day=tuple(all_day),
    )


def event_local_date(
    event: object,
    *,
    timezone: str,
) -> date | None:
    """Return the display-day for an event's start, preserving all-day semantics."""

    display_zone = _zone(timezone)
    if _is_all_day(event):
        interval = _all_day_interval(event, display_zone)
        return None if interval is None else interval[0]
    value = _datetime_value(_value(event, "starts_at", _value(event, "start_at", None)))
    if value is None:
        return None
    source_zone = _event_zone(event, display_zone)
    return _as_aware(value, source_zone).astimezone(display_zone).date()


def _place_day_entries(
    entries: list[tuple[int, str, float, float, bool, bool]],
    start_date: date,
    day_index: int,
) -> list[HourlySchedulePlacement]:
    if not entries:
        return []
    ordered = sorted(entries, key=lambda value: (value[2], value[3], value[0], value[1]))
    groups: list[list[tuple[int, str, float, float, bool, bool]]] = []
    current_group: list[tuple[int, str, float, float, bool, bool]] = []
    group_end = -1.0
    for entry in ordered:
        if current_group and entry[2] >= group_end:
            groups.append(current_group)
            current_group = []
            group_end = -1.0
        current_group.append(entry)
        group_end = max(group_end, entry[3])
    if current_group:
        groups.append(current_group)

    placements: list[HourlySchedulePlacement] = []
    for group in groups:
        column_ends: list[float] = []
        assigned: list[tuple[tuple[int, str, float, float, bool, bool], int]] = []
        for entry in group:
            selected_column = next(
                (index for index, column_end in enumerate(column_ends) if column_end <= entry[2]),
                None,
            )
            if selected_column is None:
                selected_column = len(column_ends)
                column_ends.append(entry[3])
            else:
                column_ends[selected_column] = entry[3]
            assigned.append((entry, min(selected_column, MAX_OVERLAP_COLUMNS - 1)))
        column_count = min(max(1, len(column_ends)), MAX_OVERLAP_COLUMNS)
        for entry, column in assigned:
            _sequence, event_id, start, end, clipped_start, clipped_end = entry
            placements.append(
                HourlySchedulePlacement(
                    event_id=event_id,
                    day=start_date + timedelta(days=day_index),
                    day_index=day_index,
                    start_minutes=start,
                    end_minutes=end,
                    column=column,
                    column_count=column_count,
                    clipped_start=clipped_start,
                    clipped_end=clipped_end,
                )
            )
    return placements


def _all_day_interval(event: object, display_zone: tzinfo) -> tuple[date, date] | None:
    start_value = _value(event, "starts_at", _value(event, "start_at", None))
    end_value = _value(event, "ends_at", _value(event, "end_at", None))
    start = _calendar_date(start_value, _event_zone(event, display_zone))
    if start is None:
        return None
    end = _calendar_date(end_value, _event_zone(event, display_zone))
    if end is None or end <= start:
        end = start + timedelta(days=1)
    return start, end


def _timed_interval(event: object, display_zone: tzinfo) -> tuple[datetime, datetime] | None:
    start_value = _datetime_value(_value(event, "starts_at", _value(event, "start_at", None)))
    if start_value is None:
        return None
    start = _as_aware(start_value, _event_zone(event, display_zone)).astimezone(display_zone)
    end_value = _datetime_value(_value(event, "ends_at", _value(event, "end_at", None)))
    end = (
        _as_aware(end_value, _event_zone(event, display_zone)).astimezone(display_zone)
        if end_value is not None
        else start + timedelta(minutes=DEFAULT_TIMED_DURATION_MINUTES)
    )
    if end <= start:
        end = start + timedelta(minutes=DEFAULT_TIMED_DURATION_MINUTES)
    return start, end


def _wall_minutes(
    value: datetime,
    day: date,
    clipped: bool,
    *,
    end: bool = False,
) -> float:
    if clipped:
        return 0.0 if not end else float(MINUTES_PER_DAY)
    local = value.replace(tzinfo=None)
    result = (local - datetime.combine(day, time.min)).total_seconds() / 60
    return max(0.0, min(float(MINUTES_PER_DAY), result))


def _event_id(event: object, sequence: int) -> str:
    value = _value(event, "event_id", _value(event, "id", None))
    normalized = str(value).strip() if value is not None else ""
    return normalized[:256] if normalized else f"calendar-item-{sequence}"


def _is_all_day(event: object) -> bool:
    return bool(_value(event, "all_day", _value(event, "is_all_day", False)))


def _event_zone(event: object, fallback: tzinfo) -> tzinfo:
    value = _value(event, "timezone", None)
    if value is None or value == "" or (isinstance(value, str) and value.casefold() == "local"):
        return fallback
    try:
        return _zone(str(value))
    except (TypeError, ValueError):
        return fallback


def _calendar_date(value: object, zone: tzinfo) -> date | None:
    if isinstance(value, datetime):
        return _as_aware(value, zone).astimezone(zone).date()
    if isinstance(value, date):
        return value
    parsed = _datetime_value(value)
    if parsed is not None:
        return _as_aware(parsed, zone).astimezone(zone).date()
    return None


def _datetime_value(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if normalized.endswith(("Z", "z")):
        normalized = normalized[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        try:
            return datetime.combine(date.fromisoformat(normalized), time.min)
        except ValueError:
            return None


def _as_aware(value: datetime, zone: tzinfo) -> datetime:
    return (
        value
        if value.tzinfo is not None and value.utcoffset() is not None
        else value.replace(tzinfo=zone)
    )


def _value(event: object, name: str, default: Any) -> Any:
    if isinstance(event, Mapping):
        return event.get(name, default)
    return getattr(event, name, default)


def _zone(value: str) -> ZoneInfo:
    normalized = str(value).strip()
    if normalized.casefold() == "local":
        local = datetime.now().astimezone().tzinfo
        key = getattr(local, "key", None)
        if isinstance(key, str):
            normalized = key
        else:
            return ZoneInfo("UTC")
    try:
        return ZoneInfo(normalized)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError(f"Unknown calendar display timezone: {normalized}") from error


def _zone_name(zone: tzinfo) -> str:
    key = getattr(zone, "key", None)
    return key if isinstance(key, str) else "UTC"


__all__ = [
    "AllDaySchedulePlacement",
    "DEFAULT_TIMED_DURATION_MINUTES",
    "HourlyScheduleLayout",
    "HourlySchedulePlacement",
    "MAX_OVERLAP_COLUMNS",
    "MAX_SCHEDULE_DAYS",
    "MINUTES_PER_DAY",
    "build_hourly_schedule_layout",
    "event_local_date",
]
