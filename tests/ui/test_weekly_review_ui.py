"""Offline weekly review journeys, private drafts and accessible layouts."""

from datetime import UTC, date, datetime

import pytest
from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import QLabel

from personal_hub.bootstrap import build_hub_container
from personal_hub.config import HubSettings
from personal_hub.logging.contracts import ReflectionEntry
from personal_hub.metrics.contracts import (
    Measurement,
    MeasurementBatch,
    MetricDefinition,
    MetricSource,
)
from personal_hub.ui.desktop.application import HubDesktopCoordinator
from personal_hub.ui.desktop.main_window import create_main_window

pytestmark = pytest.mark.ui


@pytest.fixture
def ui(qtbot, qapp, tmp_path):
    container = build_hub_container(HubSettings(data_dir=tmp_path / "hub"))
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    coordinator = HubDesktopCoordinator(container, window, qapp)
    yield container.controller, window, coordinator
    coordinator.shutdown()
    container.close()


def test_week_navigation_accomplishments_metrics_reflections_and_day_drilldown(ui, qtbot):
    hub, window, coordinator = ui
    monday = date(2026, 11, 9)
    hub.log_progress(
        description="Drafted the chapter",
        occurred_at=datetime(2026, 11, 10, 12, tzinfo=UTC),
        duration_minutes=25,
    )
    daily_reflection = hub.save_reflection(
        ReflectionEntry(
            display_date=monday, title="Daily writing", body="Existing daily reflection"
        )
    )
    repo = hub.metrics_repository
    repo.define(MetricDefinition(metric_id="water", name="Water", unit="cups", aggregation="sum"))
    repo.enroll(MetricSource(source_id="manual", name="Manual readings", metric_ids=("water",)))
    repo.ingest(
        MeasurementBatch(
            source_id="manual",
            records=(
                Measurement(
                    record_id="water-1",
                    metric_id="water",
                    revision=1,
                    value=0,
                    unit="cups",
                    start=datetime(2026, 11, 10, 12, tzinfo=UTC),
                ),
            ),
        )
    )
    window.show_destination("Weekly review")
    weekly = window.logging_workspace.weekly
    weekly.select_day(QDate(2026, 11, 13))
    assert window.workspace_mode == "Logging"
    assert weekly.day == monday and not weekly.follow_today
    assert "Nov 09, 2026" in weekly.caption.text()
    assert weekly.day_list.count() == 7
    assert "1" in weekly.facts.toPlainText()
    assert "Drafted the chapter" in weekly.progress.item(0).text()
    assert "Tue, Nov 10" in weekly.progress.item(0).text()
    assert "Entered work duration: 25 minutes" in weekly.time_facts.toPlainText()
    assert "Website foreground time: No recorded usage" in weekly.time_facts.toPlainText()
    assert not weekly.body.toPlainText()  # Never silently edit an existing daily reflection.
    weekly.metric_choice.setCurrentIndex(weekly.metric_choice.findData("water"))
    weekly.metric_enabled.setChecked(True)
    assert hub.settings_repository.get("logging.weekly_metrics") == ["water"]
    assert "0 cups" in weekly.metric_values.item(0).text()
    weekly.body.setPlainText("What moved forward this week")
    qtbot.waitUntil(lambda: weekly.saved.text() == "Saved on this device", timeout=3000)
    saved = weekly.reflection
    assert saved.display_date == monday and "Week of" in saved.title
    assert (
        hub.reflection_repository.get(daily_reflection.reflection_id).body
        == "Existing daily reflection"
    )
    weekly.next.click()
    assert weekly.day == date(2026, 11, 16) and not weekly.body.toPlainText()
    assert "Unavailable" in weekly.metric_values.item(0).text()
    weekly.previous.click()
    assert not weekly.body.toPlainText()
    weekly.chooser.setCurrentIndex(weekly.chooser.findData(str(saved.reflection_id)))
    assert weekly.body.toPlainText() == saved.body
    weekly.body.setPlainText("Saved before opening a day")
    weekly.day_list.setCurrentRow(1)
    weekly.open_day_button.click()
    assert window.active_destination == "Summary"
    assert window.logging_workspace.daily.day == date(2026, 11, 10)
    assert hub.reflection_repository.get(saved.reflection_id).body == "Saved before opening a day"
    window.show_destination("Weekly review")
    assert weekly.day == monday
    window.set_workspace_mode("Tracking")
    window.set_workspace_mode("Logging")
    assert window.active_destination == "Weekly review"
    coordinator.logging_coordinator.refresh()
    assert weekly.metric_enabled.isChecked()


def test_failed_autosave_blocks_navigation_and_masking_recovers_private_draft(ui, monkeypatch):
    hub, window, coordinator = ui
    window.show_destination("Weekly review")
    weekly = window.logging_workspace.weekly
    weekly.select_day(QDate(2026, 11, 13))
    weekly.body.setPlainText("Private unsaved weekly writing")
    cursor = weekly.body.textCursor()
    cursor.setPosition(7)
    weekly.body.setTextCursor(cursor)
    coordinator.logging_coordinator.refresh()
    assert weekly.body.textCursor().position() == 7

    def unavailable(_entry):
        raise OSError("private path must not be displayed")

    monkeypatch.setattr(hub, "save_reflection", unavailable)
    weekly.next.click()
    assert weekly.day == date(2026, 11, 9)
    assert weekly.date.date() == QDate(2026, 11, 9)
    window.show_destination("Home")
    assert window.active_destination == "Weekly review"
    window.set_privacy_masked(True)
    assert not weekly.body.toPlainText() and weekly.day_list.count() == 0
    assert weekly.breakdown.count() == weekly.metric_values.count() == 0
    assert not weekly.time_facts.toPlainText()
    assert window.logging_workspace.pages["Weekly review"].widget().isHidden()
    assert "private path" not in weekly.saved.text()
    window.set_privacy_masked(False)
    assert weekly.body.toPlainText() == "Private unsaved weekly writing"
    monkeypatch.undo()
    assert weekly.flush()
    window.set_locked(True)
    assert not weekly.body.toPlainText()
    window.set_locked(False)
    assert weekly.body.toPlainText() == "Private unsaved weekly writing"
    weekly.body.setPlainText("x" * 20001)
    assert not weekly.flush() and "20,000" in weekly.saved.text()
    weekly.body.setPlainText("Recovered bounded writing")
    assert weekly.flush()
    reflection_id = weekly.reflection.reflection_id
    hub.delete_reflection(str(reflection_id))
    coordinator.logging_coordinator.query("Weekly review")
    assert weekly.reflection is None and weekly.body.toPlainText() == ""


def test_weekly_keyboard_large_text_layout_and_date_limits(ui, qtbot):
    hub, window, _ = ui
    hub.log_progress(
        description="A long accomplishment " * 10,
        occurred_at=datetime(2026, 11, 10, 12, tzinfo=UTC),
    )
    window.apply_accessibility_preferences(text_scale=1.75, high_contrast=True, reduced_motion=True)
    window.resize(720, 460)
    window.show_destination("Weekly review")
    window.show()
    window.activateWindow()
    weekly = window.logging_workspace.weekly
    weekly.select_day(QDate(2026, 11, 13))
    page = window.logging_workspace.pages["Weekly review"]
    weekly.body.setFocus()
    page.ensureWidgetVisible(weekly.body)
    qtbot.waitUntil(weekly.body.hasFocus, timeout=3000)
    qtbot.waitUntil(lambda: not weekly.body.visibleRegion().isEmpty())
    assert page.horizontalScrollBar().maximum() == 0
    assert weekly.body.height() >= 180
    assert weekly.day_list.horizontalScrollBar().maximum() == 0
    assert weekly.progress.horizontalScrollBar().maximum() == 0
    for control in (
        weekly.date,
        weekly.body,
        weekly.metric_choice,
        weekly.metric_enabled,
        weekly.day_list,
    ):
        assert control.accessibleName()
    qtbot.keyClick(weekly.body, Qt.Key.Key_Tab)
    assert not weekly.body.hasFocus()
    weekly.select_day(weekly.date.maximumDate())
    assert not weekly.next.isEnabled()
    weekly.select_day(weekly.date.minimumDate())
    assert not weekly.previous.isEnabled()
    # Untrusted strings are retained as plain text, including in the native accessible representation.
    assert all(
        label.textFormat() == Qt.TextFormat.PlainText
        for label in weekly.time_facts.findChildren(QLabel)
    )


def test_this_week_rollover_timezone_refresh_and_historical_selection(ui, monkeypatch):
    from personal_hub.ui.desktop import logging_coordinator, weekly_review

    hub, window, coordinator = ui
    clock = [datetime(2026, 11, 16, 5, 59, tzinfo=UTC)]
    review_queries = []
    original_review = hub.weekly_review

    def review(*args, **kwargs):
        review_queries.append(True)
        return original_review(*args, **kwargs)

    monkeypatch.setattr(hub, "weekly_review", review)
    coordinator.logging_coordinator.refresh()
    assert (
        not review_queries
    )  # Ordinary task/dashboard refresh does not query hidden weekly history.

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)

    monkeypatch.setattr(logging_coordinator, "datetime", Clock)
    monkeypatch.setattr(weekly_review, "datetime", Clock)
    # DailyOverview.show_today supplies the local date before weekly normalization.
    from personal_hub.ui.desktop import daily_overview

    monkeypatch.setattr(daily_overview, "datetime", Clock)
    hub.update_settings(default_timezone="America/Chicago")
    window.show_destination("Weekly review")
    assert review_queries
    page = window.logging_workspace.weekly
    page.today.click()
    assert page.day == date(2026, 11, 9) and page.follow_today
    clock[0] = datetime(2026, 11, 16, 6, 1, tzinfo=UTC)
    page.refresh_button.click()
    assert page.day == date(2026, 11, 16) and page.follow_today
    page.previous.click()
    coordinator.logging_coordinator.refresh()
    assert page.day == date(2026, 11, 9) and not page.follow_today
    clock[0] = datetime(2026, 11, 16, 5, 59, tzinfo=UTC)
    page.today.click()
    assert page.day == date(2026, 11, 9)
    hub.update_settings(default_timezone="UTC")
    page.refresh_button.click()
    assert page.day == date(2026, 11, 16)
    assert page.timezone == "UTC"
