"""Calendar editor time validation and persistence through the desktop controls."""

from datetime import UTC, date, datetime

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from PySide6.QtCore import QTime
from PySide6.QtWidgets import QApplication, QDialog

from personal_hub.bootstrap import build_hub_container
from personal_hub.config import HubSettings
from personal_hub.integrations.platforms import PLATFORM_DEFINITIONS
from personal_hub.ui.desktop.application import HubDesktopCoordinator
from personal_hub.ui.desktop.calendar_item_dialog import CalendarItemDialog
from personal_hub.ui.desktop.connection_dialog import ExternalPlatformDialog
from personal_hub.ui.desktop.main_window import create_main_window


def test_editor_defaults_local_and_supports_all_day_exclusive_end(qtbot):
    dialog = CalendarItemDialog(
        day=date(2026, 10, 12),
        timezone="America/Chicago",
        google_calendars=(("primary", "Primary"),),
    )
    qtbot.addWidget(dialog)
    assert dialog.sync_combo.currentData() is None
    dialog.title_input.setText("Day off")
    dialog.all_day_checkbox.setChecked(True)
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted
    payload = dialog.payload()
    assert payload["starts_at"] == datetime(2026, 10, 12, 5, tzinfo=UTC)
    assert payload["ends_at"] == datetime(2026, 10, 13, 5, tzinfo=UTC)
    assert payload["google_calendar_id"] is None


@pytest.mark.parametrize(
    "day,clock", [(date(2026, 3, 8), QTime(2, 30)), (date(2026, 11, 1), QTime(1, 30))]
)
def test_editor_rejects_nonexistent_or_ambiguous_clock_times(qtbot, day, clock):
    dialog = CalendarItemDialog(day=day, timezone="America/Chicago")
    qtbot.addWidget(dialog)
    dialog.title_input.setText("Appointment")
    dialog.start_input.setTime(clock)
    dialog.accept()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert "unambiguous" in dialog.status_label.text()


def test_calendar_add_edit_remove_persists_offline(qtbot, tmp_path, monkeypatch):
    container = build_hub_container(HubSettings(data_dir=tmp_path / "hub", offline_mode=True))
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    coordinator = HubDesktopCoordinator(container, window, QApplication.instance())
    window._calendar_cursor = date(2026, 10, 12)

    def accept_editor(dialog):
        dialog.title_input.setText("Edited appointment" if dialog.current else "Appointment")
        dialog.accept()
        return dialog.result()

    monkeypatch.setattr(CalendarItemDialog, "exec", accept_editor)
    try:
        window.calendar_add_item_button.click()
        events = container.controller.list_local_calendar_events()
        assert len(events) == 1
        assert events[0].title == "Appointment"
        view = next(value for value in window._calendar_events if value.title == "Appointment")
        window.open_calendar_event(view)
        window.calendar_item_edit_button.click()
        assert (
            container.controller.local_calendar.get(events[0].event_id).title
            == "Edited appointment"
        )
        view = next(
            value for value in window._calendar_events if value.title == "Edited appointment"
        )
        window.open_calendar_event(view)
        window.calendar_item_delete_button.click()
        assert container.controller.list_local_calendar_events() == []
    finally:
        coordinator.shutdown()
        container.close()


def test_google_sync_is_explicit_in_connection_and_item_dialogs(qtbot):
    definition = next(value for value in PLATFORM_DEFINITIONS if value.platform_id == "google")
    connection = ExternalPlatformDialog(definition)
    qtbot.addWidget(connection)
    assert not connection.calendar_sync_checkbox.isChecked()
    connection.client_id_input.setText("desktop-client")
    connection.calendar_sync_checkbox.setChecked(True)
    connection.accept()
    assert connection.connection_payload()["calendar_sync_enabled"] is True
    editor = CalendarItemDialog(
        day=date(2026, 10, 12), timezone="UTC", google_calendars=(("work@example.com", "Work"),)
    )
    qtbot.addWidget(editor)
    editor.title_input.setText("Planning")
    editor.sync_combo.setCurrentIndex(1)
    editor.accept()
    assert editor.payload()["google_calendar_id"] == "work@example.com"
