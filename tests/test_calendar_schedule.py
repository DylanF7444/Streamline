from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from personal_hub.calendar import build_hourly_schedule_layout, event_local_date


def test_hourly_layout_places_due_time_in_display_timezone_and_assigns_overlap_columns() -> None:
    chicago = ZoneInfo("America/Chicago")
    visible_day = date(2026, 9, 14)
    layout = build_hourly_schedule_layout(
        (
            {
                "event_id": "task:morning",
                "starts_at": datetime(2026, 9, 14, 14, tzinfo=UTC),
                "timezone": "America/Chicago",
            },
            {
                "event_id": "appointment",
                "starts_at": datetime(2026, 9, 14, 9, 30, tzinfo=chicago),
                "ends_at": datetime(2026, 9, 14, 11, tzinfo=chicago),
                "timezone": "America/Chicago",
            },
            {
                "event_id": "overlap",
                "starts_at": datetime(2026, 9, 14, 10, tzinfo=chicago),
                "ends_at": datetime(2026, 9, 14, 12, tzinfo=chicago),
                "timezone": "America/Chicago",
            },
        ),
        start_date=visible_day,
        day_count=1,
        timezone="America/Chicago",
    )

    task = next(item for item in layout.timed if item.event_id == "task:morning")
    assert task.start_minutes == 9 * 60
    assert task.end_minutes == 9 * 60 + 30

    appointment = next(item for item in layout.timed if item.event_id == "appointment")
    overlap = next(item for item in layout.timed if item.event_id == "overlap")
    assert appointment.column_count == overlap.column_count == 2
    assert appointment.column != overlap.column


def test_hourly_layout_clips_cross_midnight_events_without_phantom_next_day_chip() -> None:
    chicago = ZoneInfo("America/Chicago")
    start = datetime(2026, 9, 14, 23, 30, tzinfo=chicago)
    layout = build_hourly_schedule_layout(
        (
            {
                "event_id": "cross-midnight",
                "starts_at": start,
                "ends_at": start + timedelta(hours=2),
                "timezone": "America/Chicago",
            },
            {
                "event_id": "ends-at-midnight",
                "starts_at": start,
                "ends_at": datetime(2026, 9, 15, tzinfo=chicago),
                "timezone": "America/Chicago",
            },
        ),
        start_date=date(2026, 9, 14),
        day_count=2,
        timezone="America/Chicago",
    )

    cross_midnight = [item for item in layout.timed if item.event_id == "cross-midnight"]
    assert [(item.day_index, item.start_minutes, item.end_minutes) for item in cross_midnight] == [
        (0, 23.5 * 60, 24 * 60),
        (1, 0, 1.5 * 60),
    ]
    assert cross_midnight[0].clipped_end is True
    assert cross_midnight[1].clipped_start is True

    midnight_boundary = [item for item in layout.timed if item.event_id == "ends-at-midnight"]
    assert len(midnight_boundary) == 1
    assert midnight_boundary[0].day_index == 0
    assert midnight_boundary[0].end_minutes == 24 * 60


def test_hourly_layout_keeps_all_day_dates_and_local_start_dates_distinct() -> None:
    layout = build_hourly_schedule_layout(
        (
            {
                "event_id": "all-day",
                "starts_at": date(2026, 9, 15),
                "ends_at": date(2026, 9, 17),
                "all_day": True,
                "timezone": "America/Chicago",
            },
        ),
        start_date=date(2026, 9, 14),
        day_count=3,
        timezone="UTC",
    )

    assert [(item.event_id, item.day) for item in layout.all_day] == [
        ("all-day", date(2026, 9, 15)),
        ("all-day", date(2026, 9, 16)),
    ]
    assert event_local_date(
        {
            "starts_at": datetime(2026, 9, 15, 0, 30, tzinfo=UTC),
            "timezone": "UTC",
        },
        timezone="America/Chicago",
    ) == date(2026, 9, 14)
    assert event_local_date(
        {
            "starts_at": date(2026, 9, 15),
            "all_day": True,
            "timezone": "America/Chicago",
        },
        timezone="America/Chicago",
    ) == date(2026, 9, 15)
