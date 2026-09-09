from __future__ import annotations

from datetime import datetime, timedelta

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QLabel, QPushButton, QSizePolicy

from personal_hub.ui.desktop.main_window import (
    CalendarEventView,
    CalendarSourceView,
    CalendarView,
    create_main_window,
)


def _window(qtbot):
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(window.isVisible, timeout=2_000)
    window.show_destination("Calendar")
    return window


def _events():
    now = datetime.now().astimezone().replace(second=0, microsecond=0)
    return now, (
        CalendarEventView(
            event_id="event-1",
            title="Planning session",
            starts_at=now,
            ends_at=now + timedelta(hours=1),
            calendar_id="work",
            calendar_name="Work",
            source_name="Google",
            account="work@example.com",
            location="Room 4",
            navigation_target="https://calendar.example/event-1",
            sensitive=False,
        ),
        CalendarEventView(
            event_id="event-2",
            title="Family day",
            starts_at=now.date() + timedelta(days=1),
            all_day=True,
            calendar_id="home",
            calendar_name="Home",
            source_name="Outlook",
            account="home@example.com",
            sensitive=False,
        ),
    )


@pytest.mark.ui
def test_calendar_modes_render_timed_and_all_day_events(qtbot):
    window = _window(qtbot)
    now, events = _events()
    window.render_calendar(
        CalendarView(
            events=events,
            calendars=(
                CalendarSourceView(
                    "work", "Work", source_name="Google", account="work@example.com"
                ),
                CalendarSourceView(
                    "home", "Home", source_name="Outlook", account="home@example.com"
                ),
            ),
            selected_calendar_ids=("work", "home"),
            timezone="America/Chicago",
            freshness="ready",
            cursor_date=now.date(),
        )
    )

    assert window.calendar_freshness == "ready"
    assert window.calendar_freshness_label.text() == "Ready"
    assert window.calendar_timezone_label.text().endswith("America/Chicago")
    assert set(window.calendar_source_checkboxes) == {"work", "home"}
    assert len(window.calendar_month_cells) == 42
    assert any(
        isinstance(button, QPushButton) and "Planning session" in button.text()
        for button in window.calendar_month_view.findChildren(QPushButton)
    )

    assert window.show_calendar_week()
    assert window.calendar_view_stack.currentWidget() is window.calendar_week_view
    assert len(window.calendar_week_columns) == 7
    assert window.show_calendar_agenda()
    assert window.calendar_view_stack.currentWidget() is window.calendar_agenda_view
    assert window.calendar_agenda_view.count() == 2
    assert "All day" in window.calendar_agenda_view.item(1).text()

    old_date = window.current_calendar_date
    window.next_calendar_period()
    assert window.current_calendar_date == old_date + timedelta(days=1)
    window.go_to_calendar_today()
    assert window.current_calendar_date == datetime.now().date()


@pytest.mark.ui
def test_calendar_source_selection_and_event_open_are_injected_signals(qtbot):
    window = _window(qtbot)
    _, events = _events()
    window.render_calendar(
        {
            "events": events,
            "calendars": [
                {
                    "calendar_id": "work",
                    "name": "Work",
                    "source_name": "Google",
                    "account": "work@example.com",
                },
                {
                    "calendar_id": "home",
                    "name": "Home",
                    "source_name": "Outlook",
                    "account": "home@example.com",
                },
            ],
            "selected_calendar_ids": ["work", "home"],
            "freshness": "stale",
            "detail": "Showing the last successful snapshot.",
        }
    )
    selections: list[tuple[str, ...]] = []
    opened: list[CalendarEventView] = []
    window.calendar_selection_changed.connect(selections.append)
    window.calendar_event_open_requested.connect(opened.append)

    window.set_calendar_selection(("home",))
    assert selections[-1] == ("home",)
    assert window.calendar_agenda_view.count() == 1
    assert window.calendar_agenda_view.item(0).data(0x0100) == "event-2"
    window.open_calendar_event("event-2")
    assert opened[-1].event_id == "event-2"
    assert opened[-1].navigation_target == ""

    assert window.calendar_freshness_label.text().startswith("Stale")
    assert "last successful" in window.calendar_status_label.text()
    visible_buttons = [
        button.text().casefold() for button in window.calendar_page.findChildren(QPushButton)
    ]
    assert all(
        not any(word in text for word in ("create", "edit", "delete", "write"))
        for text in visible_buttons
    )


@pytest.mark.ui
def test_calendar_freshness_states_and_privacy_masking_are_visible(qtbot):
    window = _window(qtbot)
    now = datetime.now().astimezone()
    sensitive = CalendarEventView(
        event_id="private",
        title="Private appointment",
        starts_at=now,
        ends_at=now + timedelta(minutes=30),
        calendar_id="private-calendar",
        location="Private location",
        sensitive=True,
    )
    for state in (
        "ready",
        "empty",
        "stale",
        "offline",
        "permission_required",
        "disconnected",
        "error",
    ):
        window.render_calendar({"events": [sensitive], "freshness": state})
        assert window.calendar_freshness == state
        assert window.calendar_freshness_label.text()

    window.set_calendar_mode("agenda")
    assert "Private appointment" in window.calendar_agenda_view.item(0).text()
    window.set_privacy_masked(True)
    masked = window.calendar_agenda_view.item(0).text()
    assert "Private appointment" not in masked
    assert "Private location" not in masked
    assert "Hidden by privacy settings" in masked


@pytest.mark.ui
def test_only_due_tasks_have_completion_controls_in_calendar_views(qtbot):
    window = _window(qtbot)
    now, events = _events()
    due_task = CalendarEventView(
        event_id="task:task-1",
        title="Submit report",
        starts_at=now + timedelta(minutes=30),
        timezone="UTC",
        calendar_name="To-do list",
        source_id="personal-hub-tasks",
        source_name="Personal Hub",
        item_kind="task",
        task_id="task-1",
    )
    window.render_calendar(
        CalendarView(
            events=(events[0], due_task),
            calendars=(CalendarSourceView("work", "Work"),),
            selected_calendar_ids=("work",),
            timezone="UTC",
            cursor_date=now.date(),
        )
    )
    changes: list[dict[str, object]] = []
    window.calendar_task_completion_changed.connect(changes.append)

    month_checks = [
        checkbox
        for checkbox in window.calendar_month_view.findChildren(QCheckBox)
        if checkbox.objectName() == "calendarTaskDoneCheckBox"
    ]
    assert len(month_checks) == 1
    assert "to-do list item" in month_checks[0].accessibleDescription()
    month_checks[0].click()
    assert changes[-1] == {"task_id": "task-1", "completed": True}

    window.show_calendar_agenda()
    provider_item = next(
        window.calendar_agenda_view.item(index)
        for index in range(window.calendar_agenda_view.count())
        if window.calendar_agenda_view.item(index).data(Qt.ItemDataRole.UserRole) == "event-1"
    )
    task_item = next(
        window.calendar_agenda_view.item(index)
        for index in range(window.calendar_agenda_view.count())
        if window.calendar_agenda_view.item(index).data(Qt.ItemDataRole.UserRole) == "task:task-1"
    )
    assert not provider_item.flags() & Qt.ItemFlag.ItemIsUserCheckable
    assert task_item.flags() & Qt.ItemFlag.ItemIsUserCheckable
    task_item.setCheckState(Qt.CheckState.Checked)
    assert changes[-1]["completed"] is True
    assert changes[-1]["task_id"] == "task-1"

    window.render_calendar(
        CalendarView(
            events=(
                CalendarEventView(
                    event_id=due_task.event_id,
                    title=due_task.title,
                    starts_at=due_task.starts_at,
                    item_kind="task",
                    task_id="task-1",
                    is_completed=True,
                ),
            ),
            cursor_date=now.date(),
        )
    )
    window.show_calendar_agenda()
    assert window.calendar_agenda_view.item(0).checkState() == Qt.CheckState.Checked
    assert "Completed" in window.calendar_agenda_view.item(0).text()


@pytest.mark.ui
def test_calendar_event_chips_elide_titles_and_keep_source_colors(qtbot):
    window = _window(qtbot)
    now = datetime.now().astimezone().replace(second=0, microsecond=0)
    long_title = (
        "Discussion Post: Due Week 2 [FA26 LAS 122] · A title that stays compact in the calendar"
    )
    window.resize(1_000, 720)
    window.render_calendar(
        CalendarView(
            events=(
                CalendarEventView(
                    event_id="long-event",
                    title=long_title,
                    starts_at=now,
                    ends_at=now + timedelta(hours=1),
                    calendar_id="work",
                    calendar_name="Work",
                    sensitive=False,
                ),
                CalendarEventView(
                    event_id="home-event",
                    title="Family day",
                    starts_at=now + timedelta(days=1),
                    ends_at=now + timedelta(days=1, hours=1),
                    calendar_id="home",
                    calendar_name="Home",
                    sensitive=False,
                ),
            ),
            calendars=(
                CalendarSourceView("work", "Work"),
                CalendarSourceView("home", "Home"),
            ),
            selected_calendar_ids=("work", "home"),
            cursor_date=now.date(),
        )
    )
    window.show_calendar_week()
    qtbot.waitUntil(
        lambda: all(column.width() > 0 for column in window.calendar_week_columns.values()),
        timeout=2_000,
    )

    buttons = window.calendar_week_view.findChildren(QPushButton, "calendarEventButton")
    long_button = next(button for button in buttons if long_title in button.toolTip())
    assert long_title in long_button.toolTip()
    assert long_title not in long_button.text()
    assert "…" in long_button.text()
    assert long_button.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Ignored

    widths = [column.width() for column in window.calendar_week_columns.values()]
    assert max(widths) - min(widths) <= 12

    source_indicators = window.calendar_sources_panel.findChildren(
        QLabel, "calendarSourceColorIndicator"
    )
    assert len(source_indicators) == 2
    assert len({indicator.property("calendarColor") for indicator in source_indicators}) == 2


@pytest.mark.ui
def test_calendar_item_click_reveals_details_and_native_open_is_explicit(qtbot):
    window = _window(qtbot)
    now, events = _events()
    detailed_event = CalendarEventView(
        event_id=events[0].event_id,
        title=events[0].title,
        starts_at=events[0].starts_at,
        ends_at=events[0].ends_at,
        calendar_id=events[0].calendar_id,
        calendar_name=events[0].calendar_name,
        source_name=events[0].source_name,
        account=events[0].account,
        location=events[0].location,
        description="Bring the project brief and review the open decisions.",
        navigation_target=events[0].navigation_target,
        sensitive=False,
    )
    private_event = CalendarEventView(
        event_id=events[1].event_id,
        title=events[1].title,
        starts_at=events[1].starts_at,
        ends_at=events[1].ends_at,
        all_day=events[1].all_day,
        calendar_id=events[1].calendar_id,
        calendar_name=events[1].calendar_name,
        source_name=events[1].source_name,
        sensitive=True,
    )
    window.render_calendar(
        CalendarView(
            events=(detailed_event, private_event),
            calendars=(
                CalendarSourceView("work", "Work", source_name="Google"),
                CalendarSourceView("home", "Home", source_name="Outlook"),
            ),
            selected_calendar_ids=("work", "home"),
            timezone="America/Chicago",
            cursor_date=now.date(),
        )
    )
    native_requests: list[CalendarEventView] = []
    window.calendar_deep_link_requested.connect(native_requests.append)

    chip = next(
        button
        for button in window.calendar_month_view.findChildren(QPushButton, "calendarEventButton")
        if "Planning session" in button.toolTip()
    )
    chip.click()

    assert window.calendar_event_details_panel.isVisible()
    assert window.selected_calendar_event is not None
    assert window.selected_calendar_event.event_id == "event-1"
    assert window.calendar_event_details_title.text() == "Planning session"
    assert "Room 4" in window.calendar_event_details_location.text()
    assert "Bring the project brief" in window.calendar_event_details_description.text()
    assert native_requests == []
    assert window.calendar_event_native_open_button.isEnabled()

    window.calendar_event_native_open_button.click()
    assert [event.event_id for event in native_requests] == ["event-1"]

    window.open_calendar_event("event-2")
    assert not window.calendar_event_native_open_button.isEnabled()
    assert not window.calendar_event_native_open_button.isVisible()
    window.set_privacy_masked(True)
    assert window.calendar_event_details_title.text() == "Hidden by privacy settings"
    assert not window.calendar_event_native_open_button.isEnabled()
    assert not window.calendar_event_native_open_button.isVisible()


@pytest.mark.ui
def test_canvas_calendar_task_uses_canvas_native_open_action(qtbot):
    window = _window(qtbot)
    now = datetime.now().astimezone().replace(second=0, microsecond=0)
    event = CalendarEventView(
        event_id="task:canvas-1",
        title="Submit assignment",
        starts_at=now,
        calendar_id="personal-hub-tasks",
        calendar_name="Canvas assignments",
        source_id="personal-hub-tasks",
        source_name="Canvas",
        navigation_target="https://canvas.example.edu/courses/10/assignments/101",
        native_platform="canvas",
        sensitive=False,
        item_kind="task",
        task_id="canvas-1",
    )
    window.render_calendar(
        CalendarView(
            events=(event,),
            calendars=(CalendarSourceView("personal-hub-tasks", "Canvas assignments"),),
            selected_calendar_ids=("personal-hub-tasks",),
            timezone="America/Chicago",
            cursor_date=now.date(),
        )
    )
    requests: list[CalendarEventView] = []
    window.calendar_deep_link_requested.connect(requests.append)

    chip = window.calendar_month_view.findChildren(QPushButton, "calendarEventButton")[0]
    chip.click()

    assert window.calendar_event_native_open_button.text() == "Open in Canvas"
    assert window.calendar_event_native_open_button.isVisible()
    assert window.calendar_event_native_open_button.isEnabled()
    window.calendar_event_native_open_button.click()
    assert [item.event_id for item in requests] == ["task:canvas-1"]
