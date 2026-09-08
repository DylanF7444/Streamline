"""Calendar event projection for the Personal Hub Today surface."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, time, timedelta
from hashlib import sha256
from zoneinfo import ZoneInfo

from personal_hub.calendar.contracts import CalendarEvent
from personal_hub.today import TodayItem


def project_calendar_today(
    events: Iterable[CalendarEvent],
    *,
    day: date | None = None,
    timezone: str = "UTC",
    limit: int = 200,
) -> tuple[TodayItem, ...]:
    """Return events overlapping one local calendar day in stable display order."""

    if not 1 <= limit <= 1_000:
        raise ValueError("calendar Today limit must be between 1 and 1000")
    zone = ZoneInfo(timezone)
    selected_day = day or datetime.now(zone).date()
    starts_at = datetime.combine(selected_day, time.min, tzinfo=zone)
    ends_at = starts_at + timedelta(days=1)
    selected = [
        event for event in events if event.starts_at < ends_at and event.ends_at > starts_at
    ]
    selected.sort(
        key=lambda event: (
            event.starts_at,
            0 if event.all_day else 1,
            event.ends_at,
            event.calendar_id,
            event.event_id,
        )
    )
    return tuple(
        TodayItem(
            item_id=_today_id(event),
            kind="event",
            title=event.title,
            starts_at=event.starts_at,
            due_at=None,
            severity="info",
            source=event.source,
            navigation_target=event.deep_link or f"hub://calendar/{event.event_id}",
            sensitive=event.sensitive,
            due_is_all_day=event.all_day,
        )
        for event in selected[:limit]
    )


def _today_id(event: CalendarEvent) -> str:
    value = f"calendar:{event.source.provider_id}:{event.event_id}"
    if len(value) <= 160:
        return value
    digest = sha256(value.encode("utf-8")).hexdigest()[:32]
    return f"calendar:{event.source.provider_id}:{digest}"


__all__ = ["project_calendar_today"]
