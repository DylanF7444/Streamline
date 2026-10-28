from __future__ import annotations

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from PySide6.QtWidgets import QComboBox

from personal_hub.ui.desktop.main_window import SettingsView, TimezoneComboBox, create_main_window


@pytest.mark.ui
def test_timezone_fields_use_searchable_iana_dropdowns(qtbot) -> None:
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)

    task_timezone = window.task_timezone_input
    default_timezone = window.settings_inputs["default_timezone"]
    assert isinstance(task_timezone, TimezoneComboBox)
    assert isinstance(default_timezone, TimezoneComboBox)
    assert isinstance(task_timezone, QComboBox)
    assert isinstance(default_timezone, QComboBox)
    assert task_timezone.isEditable()
    assert default_timezone.isEditable()
    assert task_timezone.findData("America/Chicago") >= 0
    assert default_timezone.findData("America/Chicago") >= 0

    task_timezone.setCurrentText("America/Chicago")
    assert task_timezone.currentData() == "America/Chicago"
    task_timezone.setCurrentText("local")
    assert task_timezone.currentData() == "local"

    window.render_settings(SettingsView(default_timezone="America/Chicago"))
    assert default_timezone.currentData() == "America/Chicago"
