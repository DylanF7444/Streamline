from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from PySide6.QtWidgets import QWidget

from personal_hub.ui.desktop.main_window import CalendarEventView, CalendarView, create_main_window


@pytest.mark.ui
def test_hourly_calendar_places_due_task_and_exposes_addable_time_slots(qtbot) -> None:
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    window.resize(1_200, 720)
    window.show()
    qtbot.waitUntil(window.isVisible, timeout=2_000)
    window.show_destination("Calendar")

    visible_day = date(2026, 9, 14)
    provider_event = CalendarEventView(
        event_id="event:planning",
        title="Planning session",
        starts_at=datetime(2026, 9, 14, 16, tzinfo=UTC),
        ends_at=datetime(2026, 9, 14, 17, tzinfo=UTC),
        timezone="UTC",
        sensitive=False,
    )
    due_task = CalendarEventView(
        event_id="task:report",
        title="Submit report",
        starts_at=datetime(2026, 9, 14, 20, 15, tzinfo=UTC),
        timezone="America/Chicago",
        item_kind="task",
        task_id="report",
    )
    window.render_calendar(
        CalendarView(
            events=(provider_event, due_task),
            timezone="America/Chicago",
            cursor_date=visible_day,
        )
    )

    assert window.show_calendar_day()
    assert len(window.calendar_day_time_slots) == 24
    assert (
        "provider calendar events are read-only" in window.calendar_day_grid.accessibleDescription()
    )
    assert window.calendar_day_time_slots[15].accessibleName().endswith("15:00")
    slot_requests: list[dict[str, object]] = []
    window.calendar_slot_requested.connect(slot_requests.append)
    window.calendar_day_time_slots[15].click()
    assert slot_requests[-1] == {"date": visible_day, "minutes": 15 * 60}

    day_layout = window.calendar_day_grid.schedule_layout
    assert day_layout is not None
    task_placement = next(item for item in day_layout.timed if item.event_id == due_task.event_id)
    assert task_placement.day == visible_day
    assert task_placement.start_minutes == 15 * 60 + 15

    task_row = next(
        row
        for row in window.calendar_day_grid.findChildren(QWidget, "calendarEventRow")
        if row.property("calendarPlacementKey") == (due_task.event_id, 0)
    )
    expected_y = int(
        window.calendar_day_grid.HEADER_HEIGHT
        + window.calendar_day_grid.ALL_DAY_MINIMUM_HEIGHT
        + task_placement.start_minutes / 60 * window.calendar_day_grid.HOUR_HEIGHT
        + window.calendar_day_grid.EVENT_GAP
    )
    assert abs(task_row.geometry().y() - expected_y) <= 2

    assert window.show_calendar_week()
    assert len(window.calendar_week_columns) == 7
    assert len(window.calendar_time_slots) == 7 * 24
    monday_column = window.calendar_week_columns[visible_day]
    week_placement = next(
        item
        for item in window.calendar_week_grid.schedule_layout.timed
        if item.event_id == due_task.event_id
    )
    week_row = next(
        row
        for row in window.calendar_week_grid.findChildren(QWidget, "calendarEventRow")
        if row.property("calendarPlacementKey") == (due_task.event_id, week_placement.day_index)
    )
    assert monday_column.geometry().x() < week_row.geometry().x()
    assert week_row.geometry().x() < monday_column.geometry().right()
