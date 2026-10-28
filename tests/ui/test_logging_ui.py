"""Offline end-to-end Logging UI and privacy coverage."""

from datetime import UTC, date, datetime, timedelta

import pytest
from PySide6.QtCore import Qt

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
    assert window.workspace_mode == "Logging" and window.active_destination == "Activity"
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
