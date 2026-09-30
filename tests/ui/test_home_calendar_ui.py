"""Home calendar navigation, selected data, and privacy regressions."""

from datetime import UTC, date, datetime

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import QLabel

from personal_hub.ui.desktop.main_window import (
    CalendarEventView,
    CalendarSourceView,
    CalendarView,
    TodayView,
    create_main_window,
)


def test_home_is_default_and_legacy_today_links_and_tray_open_home(qtbot):
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    assert window.active_destination == "Home"
    assert window.home_button is window.today_button
    assert window.today_title.text() == "Home"
    assert window.tray_controller.today_action.text() == "Open Home"
    window.show_destination("Tasks")
    assert window.show_destination("Today")
    assert window.active_destination == "Home"
    window.show_destination("Inbox")
    window.tray_controller.trigger("today")
    assert window.active_destination == "Home"


def test_mini_calendar_marks_selected_timezone_and_multiday_data_without_titles(qtbot):
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    window.render_today(TodayView(local_date="2026-09-30", timezone="America/Chicago"))
    mini = window.mini_calendar
    window.render_calendar(
        CalendarView(
            timezone="America/Chicago",
            freshness="stale",
            calendars=(CalendarSourceView("work", "Work"), CalendarSourceView("other", "Other")),
            selected_calendar_ids=("work",),
            events=(
                CalendarEventView(
                    "timed",
                    "Private meeting",
                    calendar_id="work",
                    starts_at=datetime(2026, 10, 1, 2, tzinfo=UTC),
                    ends_at=datetime(2026, 10, 1, 3, tzinfo=UTC),
                ),
                CalendarEventView(
                    "all-day",
                    "Private trip",
                    calendar_id="work",
                    all_day=True,
                    starts_at=date(2026, 9, 28),
                    ends_at=date(2026, 10, 1),
                ),
                CalendarEventView(
                    "task",
                    "Due task",
                    calendar_id="work",
                    item_kind="task",
                    starts_at=date(2026, 9, 27),
                    all_day=True,
                ),
                CalendarEventView(
                    "unselected",
                    "Excluded",
                    calendar_id="other",
                    starts_at=date(2026, 9, 26),
                    all_day=True,
                ),
            ),
        )
    )
    assert mini.grid.today == QDate(2026, 9, 30)
    assert mini.grid.activity == {QDate(2026, 9, day) for day in (27, 28, 29, 30)}
    assert "Stale" in mini.status_label.text()
    mini.grid.setSelectedDate(QDate(2026, 9, 30))
    assert "2 events" in mini.activity_label.text()
    window.set_privacy_masked(True)
    assert all("Private" not in label.text() for label in mini.findChildren(QLabel))
    window.set_calendar_selection(())
    assert mini.grid.activity == set()
    window.set_offline_mode(True)
    assert "Offline" in mini.status_label.text()


def test_mini_calendar_month_boundaries_keyboard_open_and_refresh_preserve_state(qtbot):
    window = create_main_window(close_to_tray=False)
    window.set_reduced_motion(True)
    qtbot.addWidget(window)
    window.render_today(TodayView(local_date="2026-12-15"))
    window.show()
    mini = window.mini_calendar
    with qtbot.waitSignal(window.calendar_period_changed):
        mini.next_button.click()
    assert mini.month_label.text() == "January 2027"
    assert window.current_calendar_date == date(2027, 1, 1)
    assert window.active_destination == "Home"
    window.render_today(TodayView(local_date="2026-12-15"))
    assert window.mini_calendar is mini
    assert mini.month_label.text() == "January 2027"
    mini.previous_button.click()
    assert mini.month_label.text() == "December 2026"
    mini.today_button.click()
    assert mini.grid.selectedDate() == QDate(2026, 12, 15)
    mini.grid.setSelectedDate(QDate(2026, 12, 21))
    mini.grid.setFocus()
    qtbot.keyClick(window.focusWidget(), Qt.Key.Key_Return)
    assert window.active_destination == "Calendar"
    assert window.calendar_mode_value == "day"
    assert window.current_calendar_date == date(2026, 12, 21)
    assert window.calendar_period_label.text() == "Monday, December 21, 2026"


@pytest.mark.parametrize("scale", [1.0, 1.75])
def test_mini_calendar_fits_dashboard_at_minimum_window_size(qtbot, scale):
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    window.apply_accessibility_preferences(
        text_scale=scale, high_contrast=True, reduced_motion=True
    )
    window.resize(*window.MINIMUM_SIZE)
    window.show()
    window.dashboard.set_widget_size("mini_calendar", "compact")
    qtbot.wait(25)
    mini = window.mini_calendar
    card = window.dashboard.widget("mini_calendar")
    assert mini.grid.width() >= mini.grid.minimumSizeHint().width()
    assert mini.grid.height() >= mini.grid.minimumSizeHint().height()
    assert mini.geometry().bottom() < card.height()
    assert mini.grid.geometry().bottom() < mini.activity_label.geometry().top()
    assert window.dashboard.remove_widget("mini_calendar")
    assert window.dashboard.add_widget("mini_calendar")
