"""First-use recovery paths and small-window authoring with synthetic local data."""

from datetime import date

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QAccessible
from PySide6.QtWidgets import QApplication, QDialog

from personal_hub.bootstrap import build_hub_container
from personal_hub.config import HubSettings
from personal_hub.ui.desktop.application import HubDesktopCoordinator
from personal_hub.ui.desktop.calendar_item_dialog import CalendarItemDialog
from personal_hub.ui.desktop.main_window import SourceView, create_main_window
from personal_hub.ui.desktop.status_label import StatusLabel

pytestmark = pytest.mark.ui


@pytest.fixture
def window(qtbot):
    window = create_main_window(close_to_tray=False)
    window.set_reduced_motion(True)
    qtbot.addWidget(window)
    window.show()
    window.activateWindow()
    yield window
    window.set_text_scale(1.0)


def test_home_shortcuts_capture_local_task_and_progress_offline(window, qtbot, tmp_path):
    container = build_hub_container(HubSettings(data_dir=tmp_path / "hub", offline_mode=True))
    coordinator = HubDesktopCoordinator(container, window, QApplication.instance())
    try:
        window.home_add_task_button.click()
        assert window.active_destination == "Tasks"
        qtbot.waitUntil(window.task_quick_add_input.hasFocus)
        qtbot.keyClicks(window.task_quick_add_input, "Plan the week")
        qtbot.keyClick(window.task_quick_add_input, Qt.Key.Key_Return)
        assert container.controller.list_tasks()[0].title == "Plan the week"
        window.show_destination("Home")
        window.home_log_progress_button.click()
        assert window.workspace_mode == "Logging"
        dialog = window.logging_workspace.dialog
        dialog._save()
        assert "description" in dialog.status.text()
        assert container.controller.list_activity() == []
        dialog.description.setText("Organized my notes")
        dialog._save()
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert container.controller.list_activity()[0].description == "Organized my notes"
        assert container.controller.list_tasks()[0].state == "open"
    finally:
        coordinator.shutdown()
        container.close()


def test_clear_filters_recovers_results_and_preserves_list_scope_and_sort(window):
    window.set_workspace_task_lists([{"list_id": "work", "name": "Work"}])
    window.render_tasks(
        [
            {"task_id": "one", "title": "Write plan", "list_id": "Work", "list_key": "work"},
            {"task_id": "two", "title": "Buy food"},
        ]
    )
    window.open_workspace_collection("task-list", "work")
    window.set_task_filters(state="completed", due="today", search="missing", sort="title")
    assert window.tasks_list.count() == 0
    assert "State: Completed" in window.task_status_label.text()
    assert window.task_reset_filters_button.isVisible()
    window.task_reset_filters_button.click()
    assert window.tasks_list.count() == 1
    assert window.selected_task() is None
    assert window.task_filters == {"state": "all", "due": "all", "search": "", "sort": "title"}
    assert not window.task_reset_filters_button.isVisible()
    assert "Work" in window.task_status_label.text()


def test_source_guidance_distinguishes_recovery_and_offline_without_requests(window):
    requests = []
    window.source_refresh_requested.connect(requests.append)
    for state, next_step in (
        ("permission_required", "Reconnect"),
        ("disconnected", "Connect / Configure"),
        ("stale", "Refresh"),
        ("error", "Refresh"),
        ("empty", "no items"),
        ("ready", "Ready"),
        ("refreshing", "Refreshing"),
    ):
        window.render_sources([SourceView("platform-google", "Google", status=state)])
        assert next_step in window.source_guidance_label.text()
        assert "permission_required" not in window.sources_list.item(0).text()
    window.set_offline_mode(True)
    assert "Offline mode is on" in window.source_guidance_label.text()
    window.set_offline_mode(False)
    window.render_sources([SourceView("platform-google", "Google", enabled=False)])
    assert "Select Enable" in window.source_guidance_label.text()
    assert requests == []
    window.show_destination("Home")
    window.render_source_failure("platform-google", "Refresh failed safely")
    assert window.attention_notification_open_button.text() == "Review sources"
    window.attention_notification_open_button.click()
    assert window.active_destination == "Sources"
    assert len(window.source_failures) == 1
    assert requests == []


def test_watch_form_discloses_condition_fields_and_keeps_entered_values(window):
    window.show_destination("Watchlists")
    assert window.watch_threshold_input.isHidden()
    assert window.watch_term_input.isHidden()
    window.watch_condition_combo.setCurrentIndex(
        window.watch_condition_combo.findData("price_below")
    )
    assert not window.watch_threshold_input.isHidden()
    window.watch_threshold_input.setText("90")
    window.watch_condition_combo.setCurrentIndex(window.watch_condition_combo.findData("contains"))
    assert window.watch_threshold_input.isHidden()
    assert not window.watch_term_input.isHidden()
    assert window.watch_threshold_input.text() == "90"
    window.watch_kind_combo.setCurrentIndex(window.watch_kind_combo.findData("page_selector"))
    assert not window.watch_selector_input.isHidden()
    assert "minutes" in window.watch_form.labelForField(window.watch_interval_input).text()


def test_minimum_window_large_text_editor_and_logging_controls_are_reachable(window, qtbot):
    window.resize(720, 460)
    window.apply_accessibility_preferences(text_scale=1.75, high_contrast=True, reduced_motion=True)
    window.show_destination("Tasks")
    task = window.add_task("Edit with the keyboard")
    window.open_task_editor(task.task_id)
    assert window.task_content_splitter.orientation() == Qt.Orientation.Vertical
    qtbot.waitUntil(window.task_title_input.hasFocus)
    window.task_title_input.setText("Keyboard edit saved")
    # Tab through the real form so both scroll areas must reveal the focused controls.
    for _ in range(35):
        if window.task_save_button.hasFocus():
            break
        qtbot.keyClick(window.focusWidget(), Qt.Key.Key_Tab)
    assert window.task_save_button.hasFocus()
    qtbot.waitUntil(lambda: window.task_save_button.visibleRegion().isEmpty() is False)
    assert window.task_save_button.visibleRegion().contains(window.task_save_button.rect())
    assert window.task_save_button.height() >= window.task_save_button.sizeHint().height()
    assert window.task_editor_scroll.horizontalScrollBar().maximum() == 0
    qtbot.keyClick(window.task_save_button, Qt.Key.Key_Space)
    assert window.tasks[0].title == "Keyboard edit saved"
    for name in ("Calendar", "Activity", "Reflection", "Summary", "Sources"):
        window.show_destination(name)
        page = window.page_widgets[name]
        qtbot.wait(1)
        assert page.horizontalScrollBar().maximum() == 0, name
    filters = window.logging_workspace.filters["Activity"]
    filters.period.setCurrentIndex(2)
    assert not filters.start.isHidden() and not filters.from_label.isHidden()
    filters.period.setCurrentIndex(0)
    assert filters.start.isHidden() and filters.end.isHidden()
    window.show_destination("Activity")
    window.logging_workspace.add_button.setFocus()
    qtbot.waitUntil(lambda: not window.logging_workspace.add_button.visibleRegion().isEmpty())


def test_calendar_required_title_validation_keeps_dialog_open(window, qtbot):
    dialog = CalendarItemDialog(day=date(2026, 10, 28), timezone="UTC", parent=window)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.accept()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.payload() == {}
    assert "Enter a title" in dialog.status_label.text()


def test_status_outcomes_announce_only_visible_changes(qtbot, monkeypatch):
    messages = []
    monkeypatch.setattr(
        QAccessible, "updateAccessibility", lambda event: messages.append(event.message())
    )
    label = StatusLabel()
    qtbot.addWidget(label)
    label.setText("Hidden outcome")
    assert messages == []
    label.show()
    label.setText("Saved on this device")
    label.setText("Saved on this device")
    assert messages == ["Saved on this device"]
    assert label.textFormat() == Qt.TextFormat.PlainText


def test_settings_loading_does_not_report_unsaved_edits_and_save_outcome_reaches_top(window):
    window.show_destination("Settings")
    window.render_settings({"default_timezone": "America/Chicago", "text_scale": 1.25})
    assert "Unsaved" not in window.settings_status_label.text()
    assert window.settings_save_status_label.isHidden()
    assert "Apply appearance" in window.settings_status_label.text()
    window.settings_inputs["feed_refresh_minutes"].setText("45")
    assert "Unsaved changes" in window.settings_status_label.text()
    window.set_settings_save_status("Settings saved on this device.")
    assert window.settings_status_label.text() == "Settings saved on this device."
    assert window.settings_save_status_label.text() == window.settings_status_label.text()
