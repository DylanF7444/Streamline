from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from personal_hub.ui.desktop.import_dialog import ImportDialog
from personal_hub.ui.desktop.main_window import (
    CalendarEventView,
    CalendarImportView,
    CalendarSourceView,
    CalendarView,
    create_main_window,
)


@pytest.mark.ui
def test_import_dialog_exposes_csv_template_and_text_list_payload(qtbot):
    dialog = ImportDialog(default_target="calendar")
    qtbot.addWidget(dialog)

    assert dialog.target == "calendar"
    assert dialog.source_format == "csv"
    assert dialog.save_template_button.text() == "Save calendar CSV template"

    dialog.format_combo.setCurrentIndex(1)
    dialog.name_input.setText("Family events")
    dialog.content_input.setPlainText("2026-09-14 09:00 | School meeting")
    assert dialog.calendar_defaults_frame.isVisible() is False
    dialog.show()
    assert dialog.calendar_defaults_frame.isVisible()
    dialog.accept()

    payload = dialog.payload()
    assert payload is not None
    assert payload["target"] == "calendar"
    assert payload["source_format"] == "text"
    assert payload["import_name"] == "Family events"
    assert payload["content"] == "2026-09-14 09:00 | School meeting"


@pytest.mark.ui
def test_import_controls_emit_destination_requests(qtbot):
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    task_requests: list[bool] = []
    calendar_requests: list[bool] = []
    window.task_import_requested.connect(lambda: task_requests.append(True))
    window.calendar_import_requested.connect(lambda: calendar_requests.append(True))

    window.task_import_button.click()
    window.calendar_local_import_button.click()

    assert task_requests == [True]
    assert calendar_requests == [True]


@pytest.mark.ui
def test_local_calendar_import_is_visible_and_removable(qtbot):
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    starts = datetime(2026, 9, 14, 10, tzinfo=UTC)
    window.render_calendar(
        CalendarView(
            events=(
                CalendarEventView(
                    event_id="local:event",
                    title="School meeting",
                    starts_at=starts,
                    ends_at=starts + timedelta(hours=1),
                    calendar_id="personal-hub-import/abc",
                    calendar_name="Family events",
                    source_id="personal-hub-import",
                    source_name="Local import",
                    sensitive=True,
                ),
            ),
            calendars=(
                CalendarSourceView(
                    "personal-hub-import/abc",
                    "Family events",
                    source_id="personal-hub-import",
                    source_name="Local import",
                    sensitive=True,
                ),
            ),
            selected_calendar_ids=("personal-hub-import/abc",),
            local_imports=(CalendarImportView("abc", "Family events", "text", 1),),
            cursor_date=starts.date(),
        )
    )
    window.calendar_settings_button.setChecked(True)

    assert window.calendar_local_imports_layout.count() == 1
    remove_button = next(
        button
        for button in window.calendar_local_imports_panel.findChildren(
            type(window.task_import_button)
        )
        if button.objectName() == "calendarLocalImportRemoveButton"
    )
    removed: list[str] = []
    window.calendar_import_delete_requested.connect(removed.append)
    remove_button.click()
    assert removed == ["abc"]
    assert "Family events" in window.calendar_source_checkboxes["personal-hub-import/abc"].text()
