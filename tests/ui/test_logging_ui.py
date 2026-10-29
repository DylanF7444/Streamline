"""Offline end-to-end Logging UI and privacy coverage."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QDate, Qt

from personal_hub.bootstrap import build_hub_container
from personal_hub.config import HubSettings
from personal_hub.logging.contracts import LogFilter, ReflectionEntry
from personal_hub.ui.desktop.application import HubDesktopCoordinator
from personal_hub.ui.desktop.main_window import create_main_window

pytestmark = pytest.mark.ui


@pytest.fixture
def ui(qtbot, qapp, tmp_path):
    container = build_hub_container(HubSettings(data_dir=tmp_path / "hub", offline_mode=True))
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    coordinator = HubDesktopCoordinator(container, window, qapp)
    yield container.controller, window, coordinator
    coordinator.shutdown()
    container.close()


def test_modes_remember_pages_keep_management_and_open_linked_task(ui):
    hub, window, coordinator = ui
    task = hub.create_task(title="Linked task")
    hub.complete_task(str(task.task_id))
    task_list = hub.create_task_list("Mode memory list")
    coordinator.render_task_surfaces()
    assert window.open_workspace_collection("task-list", str(task_list.list_id))
    collection_route = window.active_destination
    window.set_workspace_mode("Logging")
    assert window.workspace_mode == "Logging" and window.active_destination == "Summary"
    assert window.navigation_buttons["Summary"].text() == "Daily overview"
    window.show_destination("Activity")
    window.set_workspace_mode("Tracking")
    assert window.active_destination == collection_route
    window.set_workspace_mode("Logging")
    hub.task_list_repository.delete(task_list.list_id)
    coordinator.render_task_surfaces()
    window.set_workspace_mode("Tracking")
    assert window.workspace_mode == "Tracking" and window.active_destination == "Tasks"
    window.show_destination("Calendar")
    window.set_workspace_mode("Logging")
    assert window.active_destination == "Activity"
    assert window.navigation_buttons["Tasks"].isHidden()
    assert not window.navigation_buttons["Settings"].isHidden()
    view = window.logging_workspace
    view.activity_list.setCurrentRow(1)
    view.open_task()
    assert window.workspace_mode == "Tracking"
    assert window.active_destination == "Tasks"
    window.set_workspace_mode("Logging")
    assert window.active_destination == "Activity"
    window.show_destination("Reflection")
    window.set_workspace_mode("Tracking")
    window.set_workspace_mode("Logging")
    assert window.active_destination == "Reflection"
    preferences = window.workspace_mode_preferences()
    window.show_destination("Home")
    window.restore_workspace_mode(preferences)
    assert window.active_destination == "Reflection"


def test_manual_dialog_task_capture_does_not_complete_and_summary(ui):
    hub, window, coordinator = ui
    task = hub.create_task(title="Open task")
    coordinator.render_task_surfaces()
    window.log_task_progress(str(task.task_id))
    view = window.logging_workspace
    dialog = view.dialog
    dialog.description.setText("Outlined the proposal")
    dialog.duration.setValue(25)
    dialog._save()
    (entry,) = hub.list_activity()
    assert entry.duration_minutes == 25 and entry.task_id == task.task_id
    assert hub.get_task(str(task.task_id)).state == "open"
    assert "25 minutes" in view.summary_text.toPlainText()
    view.activity_list.setCurrentRow(1)
    view.edit_activity()
    view.dialog.description.setText("Reviewed the outline")
    view.dialog._save()
    assert hub.list_activity()[0].description == "Reviewed the outline"
    view.delete_activity()
    assert not hub.list_activity()


def test_reflection_autosave_multiple_entries_search_and_navigation_flush(ui, qtbot):
    hub, window, coordinator = ui
    window.show_destination("Reflection")
    view = window.logging_workspace
    view.new_reflection(day=date(2020, 3, 4))
    view.reflection_title.setText("Remember this")
    view.body.setPlainText("Private reflection paragraphs\n\nMore writing")
    qtbot.waitUntil(lambda: view.saved.text() == "Saved on this device", timeout=2000)
    (entry,) = hub.list_reflections()
    assert entry.display_date == date(2020, 3, 4)
    view.body.setPlainText("Updated before mode switch")
    window.set_workspace_mode("Tracking")
    assert hub.list_reflections()[0].body == "Updated before mode switch"
    window.show_destination("Reflection")
    view.new_reflection(day=date(2020, 3, 4))
    assert len(hub.list_reflections()) == 2
    filters = view.filters["Reflection"]
    filters.period.setCurrentIndex(3)
    view.body.setPlainText("Pending second reflection")
    view.reflection_list.setCurrentRow(1)
    assert view.body.toPlainText() == "Updated before mode switch"
    assert any(entry.body == "Pending second reflection" for entry in hub.list_reflections())
    filters.search.setText("Remember")
    assert view.reflection_list.count() == 1
    view.reflection_list.setCurrentRow(0)
    view.delete_reflection()
    assert len(hub.list_reflections()) == 1
    assert hub.list_activity() == []


def test_mask_and_lock_flush_hide_writing_and_restore_editor(ui):
    hub, window, _ = ui
    window.show_destination("Reflection")
    view = window.logging_workspace
    view.new_reflection()
    view.body.setPlainText("Sensitive draft")
    window.set_privacy_masked(True)
    assert hub.list_reflections()[0].body == "Sensitive draft"
    assert view.body.toPlainText() == ""
    assert view.pages["Reflection"].widget().isHidden()
    window.set_privacy_masked(False)
    assert view.body.toPlainText() == "Sensitive draft"
    view.body.setPlainText("Lock saved this")
    window.set_locked(True)
    assert hub.list_reflections()[0].body == "Lock saved this"
    assert view.body.toPlainText() == ""
    window.set_locked(False)
    assert view.body.toPlainText() == "Lock saved this"


def test_filters_undo_history_empty_state_and_duration_label(ui):
    hub, window, coordinator = ui
    task = hub.create_task(title="Completed title")
    hub.complete_task(str(task.task_id))
    coordinator.render_task_surfaces()
    window.show_destination("Activity")
    view = window.logging_workspace
    assert "Duration was not tracked" in view.summary_text.toPlainText()
    hub.undo_task(str(task.task_id))
    coordinator.render_task_surfaces()
    assert view.activity_list.count() == 0
    assert "No activity" in view.statuses["Activity"].text()
    view.filters["Activity"].undone.setChecked(True)
    assert "Undone" in view.activity_list.item(1).text()
    assert hub.activity_summary(LogFilter())["task_completion"] == 0


def test_narrow_logging_pages_are_scrollable_and_keyboard_accessible(ui, qtbot):
    _, window, _ = ui
    window.resize(720, 460)
    window.show()
    window.activateWindow()
    window.mode_selector.setFocus()
    qtbot.waitUntil(window.mode_selector.hasFocus, timeout=2000)
    qtbot.keyClick(window.mode_selector, Qt.Key.Key_End)
    assert window.workspace_mode == "Logging"
    qtbot.keyClick(window.mode_selector, Qt.Key.Key_Tab)
    assert window.navigation_buttons["Summary"].hasFocus(), window.focusWidget().objectName()
    qtbot.keyClick(window.navigation_buttons["Summary"], Qt.Key.Key_Tab)
    assert window.navigation_buttons["Activity"].hasFocus(), window.focusWidget().objectName()
    qtbot.keyClick(window.navigation_buttons["Activity"], Qt.Key.Key_Tab)
    assert window.navigation_buttons["Reflection"].hasFocus(), window.focusWidget().objectName()
    for name in ("Activity", "Reflection", "Summary"):
        window.show_destination(name)
        page = window.logging_workspace.pages[name]
        assert page.widgetResizable()
        assert page.accessibleName()


def test_refresh_preserves_pending_draft_and_save_failure_blocks_navigation(ui, monkeypatch):
    hub, window, coordinator = ui
    window.show_destination("Reflection")
    view = window.logging_workspace
    view.new_reflection()
    view.body.setPlainText("Pending draft must survive refresh")
    coordinator.render_all()
    assert view.body.toPlainText() == "Pending draft must survive refresh"
    assert view.flush()

    def unavailable(_entry):
        raise OSError("private filesystem detail")

    monkeypatch.setattr(hub, "save_reflection", unavailable)
    view.body.setPlainText("Unsaved writing")
    window.set_workspace_mode("Tracking")
    assert window.active_destination == "Reflection"
    assert view.body.toPlainText() == "Unsaved writing"
    assert "private filesystem" not in view.saved.text()
    quit_requests = []
    window.quit_requested.connect(lambda: quit_requests.append(True))
    window.request_quit()
    assert not quit_requests
    assert not window._quitting
    # Allow fixture shutdown without leaving a failed autosave timer behind.
    monkeypatch.undo()
    assert view.flush()


def test_logging_deletion_preview_and_separate_reflection_sync_choice(ui):
    hub, _window, coordinator = ui
    hub.create_task(title="Keep this task")
    hub.log_progress(description="Old activity", occurred_at=datetime.now(UTC) - timedelta(days=90))
    hub.save_reflection(ReflectionEntry(display_date=date(2020, 1, 1), body="Old reflection"))
    controls = coordinator.logging_coordinator
    assert not controls.reflection_sync.isChecked()
    controls.reflection_sync.setChecked(True)
    assert hub.settings_repository.get("logging.sync_reflections") is True
    controls.days.setValue(30)
    controls.preview()
    assert "1 Activity entries and 1 reflections" in controls.preview_label.text()
    assert len(hub.list_activity()) == len(hub.list_reflections()) == 1
    controls.apply_retention()
    assert hub.list_activity() == hub.list_reflections() == []
    assert len(hub.list_tasks()) == 1
    assert not controls.apply_button.isEnabled()


def test_daily_overview_local_day_dst_snapshots_and_complete_totals(ui):
    hub, window, coordinator = ui
    hub.update_settings(default_timezone="America/Chicago")
    task_list = hub.create_task_list("Writing")
    task = hub.create_task(title="Finished proposal", list_id=task_list.list_id, tags=("draft",))
    # This local day has 25 hours. The final hour still belongs in November 1.
    hub.complete_task(str(task.task_id), completed_at=datetime(2026, 11, 2, 5, 59, tzinfo=UTC))
    hub.update_task(str(task.task_id), title="Later title")
    hub.task_repository.delete(task.task_id)
    undone = hub.create_task(title="Accidental completion")
    hub.complete_task(str(undone.task_id), completed_at=datetime(2026, 11, 1, 7, tzinfo=UTC))
    hub.undo_task(str(undone.task_id))
    for description, when in (
        ("Before the day", datetime(2026, 11, 1, 4, 59, tzinfo=UTC)),
        ("At local midnight", datetime(2026, 11, 1, 5, tzinfo=UTC)),
        ("Following day", datetime(2026, 11, 2, 6, tzinfo=UTC)),
    ):
        hub.log_progress(description=description, occurred_at=when, duration_minutes=15)
    window.show_destination("Summary")
    daily = window.logging_workspace.daily
    daily.select_day(QDate(2026, 11, 1))
    daily.limit = 1
    coordinator.logging_coordinator.query("Summary")
    assert "Tasks completed: 1" in daily.facts.toPlainText()
    assert "Manual progress entries: 1" in daily.facts.toPlainText()
    assert "15 minutes" in daily.facts.toPlainText()
    assert "Finished proposal" in daily.completed.item(0).text()
    assert "Writing · #draft" in daily.completed.item(0).text()
    assert daily.more.isEnabled() and not daily.more.isHidden()
    daily.more.click()
    assert daily.progress.count() == 1
    assert "At local midnight" in daily.progress.item(0).text()
    assert daily.more.isHidden()
    daily.next.click()
    assert daily.day == date(2026, 11, 2)
    assert "Following day" in daily.progress.item(0).text()
    daily.previous.click()
    assert daily.day == date(2026, 11, 1)


def test_daily_reflection_autosaves_browses_and_reuses_existing_records(ui, qtbot):
    hub, window, coordinator = ui
    day = date(2020, 3, 4)
    first = hub.save_reflection(
        ReflectionEntry(display_date=day, title="First", body="First notes")
    )
    second = hub.save_reflection(
        ReflectionEntry(display_date=day, title="Second", body="Second notes")
    )
    window.show_destination("Summary")
    daily = window.logging_workspace.daily
    daily.select_day(QDate(day))
    assert daily.reflection.reflection_id == second.reflection_id
    daily.chooser.setCurrentIndex(daily.chooser.findData(str(first.reflection_id)))
    assert daily.body.toPlainText() == "First notes"
    daily.body.setPlainText("Edited from the daily overview")
    qtbot.waitUntil(lambda: not daily.dirty, timeout=2000)
    assert daily.saved.text() == "Saved on this device"
    assert (
        hub.reflection_repository.get(first.reflection_id).body == "Edited from the daily overview"
    )
    assert len(hub.list_reflections()) == 2
    # Selecting New while a save is pending must leave the chooser and editor consistent.
    daily.body.setPlainText("Flushed before new reflection")
    daily.chooser.setCurrentIndex(0)
    assert daily.reflection is None and daily.chooser.currentData() is None
    daily.title.setText("Third")
    daily.body.setPlainText("A separate reflection for this day")
    daily.next.click()
    assert len(hub.list_reflections()) == 3
    assert daily.body.toPlainText() == ""
    assert "No tasks completed" in daily.completed.item(0).text()
    daily.previous.click()
    assert daily.body.toPlainText() == "A separate reflection for this day"
    cursor = daily.body.textCursor()
    cursor.setPosition(3)
    daily.body.setTextCursor(cursor)
    coordinator.render_all()
    assert daily.body.textCursor().position() == 3
    window.show_destination("Reflection")
    view = window.logging_workspace
    view.filters["Reflection"].period.setCurrentIndex(3)
    matching = [
        index
        for index in range(view.reflection_list.count())
        if "Third" in view.reflection_list.item(index).text()
    ]
    view.reflection_list.setCurrentRow(matching[0])
    view.body.setPlainText("Edited in Reflection")
    window.show_destination("Summary")
    assert daily.body.toPlainText() == "Edited in Reflection"
    assert daily.day == day


def test_daily_draft_failure_blocks_date_navigation_and_survives_lock(ui, monkeypatch):
    hub, window, coordinator = ui
    window.show_destination("Summary")
    daily = window.logging_workspace.daily
    daily.select_day(QDate(2020, 3, 4))

    def unavailable(_entry):
        raise OSError("private filesystem detail")

    monkeypatch.setattr(hub, "save_reflection", unavailable)
    daily.body.setPlainText("Unsaved daily writing")
    coordinator.render_all()
    assert daily.body.toPlainText() == "Unsaved daily writing"
    daily.next.click()
    assert daily.day == daily.date.date().toPython() == date(2020, 3, 4)
    assert not window.show_destination("Tasks")
    assert window.active_destination == "Summary"
    quit_requests = []
    window.quit_requested.connect(lambda: quit_requests.append(True))
    window.request_quit()
    assert not quit_requests
    assert "private filesystem" not in daily.saved.text()
    window.set_locked(True)
    assert daily.body.toPlainText() == ""
    assert daily.completed.count() == daily.progress.count() == daily.chooser.count() == 0
    window.set_locked(False)
    assert daily.body.toPlainText() == "Unsaved daily writing"
    assert daily.dirty
    monkeypatch.undo()
    assert window.logging_workspace.flush()
    (entry,) = hub.list_reflections()
    assert entry.display_date == date(2020, 3, 4)
    assert entry.body == "Unsaved daily writing"
    window.set_privacy_masked(True)
    assert daily.body.toPlainText() == "" and daily.title.text() == ""
    window.set_privacy_masked(False)
    assert daily.body.toPlainText() == entry.body


def test_daily_empty_state_bounds_and_today_navigation(ui):
    hub, window, _ = ui
    window.show_destination("Summary")
    daily = window.logging_workspace.daily
    assert hub.list_reflections() == []
    assert "Tasks completed: 0" in daily.facts.toPlainText()
    assert "Duration was not tracked" in daily.facts.toPlainText()
    assert "No progress logged" in daily.progress.item(0).text()
    daily.select_day(QDate(2020, 3, 4))
    daily.body.setPlainText("x" * 20_001)
    assert not window.logging_workspace.flush()
    assert "20,000" in daily.saved.text()
    assert hub.list_reflections() == []
    daily.body.setPlainText("A bounded reflection")
    daily.today.click()
    assert daily.follow_today
    assert daily.day == datetime.now(ZoneInfo(hub.settings.default_timezone)).date()
    assert hub.list_reflections()[0].display_date == date(2020, 3, 4)


def test_daily_today_rollover_flushes_previous_day_and_preserves_pinned_dates(ui):
    hub, window, coordinator = ui
    window.show_destination("Summary")
    daily = window.logging_workspace.daily
    today = daily.day
    yesterday = today - timedelta(days=1)
    daily.select_day(QDate(yesterday))
    daily.body.setPlainText("Writing just before midnight")
    daily.follow_today = True
    coordinator.render_all()
    assert daily.day == today and daily.follow_today
    assert daily.body.toPlainText() == ""
    (entry,) = hub.list_reflections()
    assert entry.display_date == yesterday and entry.body == "Writing just before midnight"
    daily.select_day(QDate(yesterday))
    coordinator.render_all()
    assert daily.day == yesterday and not daily.follow_today
    assert daily.body.toPlainText() == entry.body


def test_daily_overview_large_text_reflection_is_reachable_without_horizontal_scroll(ui, qtbot):
    _, window, _ = ui
    window.apply_accessibility_preferences(text_scale=1.75, high_contrast=True, reduced_motion=True)
    window.resize(720, 460)
    window.show_destination("Summary")
    window.show()
    window.activateWindow()
    daily = window.logging_workspace.daily
    page = window.logging_workspace.pages["Summary"]
    daily.body.setFocus()
    qtbot.waitUntil(daily.body.hasFocus, timeout=2000)
    page.ensureWidgetVisible(daily.body)
    qtbot.waitUntil(lambda: not daily.body.visibleRegion().isEmpty())
    assert page.horizontalScrollBar().maximum() == 0
    assert daily.body.height() >= 180
    qtbot.keyClick(daily.body, Qt.Key.Key_Tab)
    assert not daily.body.hasFocus()
