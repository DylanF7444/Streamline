from __future__ import annotations

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from PySide6.QtWidgets import QDialog

from personal_hub.integrations.platforms import PLATFORM_DEFINITIONS
from personal_hub.ui.desktop.connection_dialog import ExternalPlatformDialog
from personal_hub.ui.desktop.main_window import SourceView, create_main_window


@pytest.mark.ui
def test_unconfigured_platform_exposes_setup_without_enabling_refresh(qtbot) -> None:
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    window.render_sources(
        (
            SourceView(
                source_id="platform-google",
                display_name="Google",
                status="disconnected",
                detail="Choose Connect / Configure to set up this platform.",
                enabled=False,
            ),
        )
    )
    window.sources_list.setCurrentRow(0)

    assert window.source_configure_button.isEnabled()
    assert not window.source_refresh_button.isEnabled()


@pytest.mark.ui
def test_google_setup_dialog_returns_read_only_fields_and_clears_secret_control(qtbot) -> None:
    definition = next(item for item in PLATFORM_DEFINITIONS if item.platform_id == "google")
    dialog = ExternalPlatformDialog(definition)
    qtbot.addWidget(dialog)
    assert dialog.client_id_input is not None
    assert dialog.client_secret_input is not None
    dialog.client_id_input.setText("desktop-client.apps.googleusercontent.com")
    dialog.client_secret_input.setText("temporary-secret")

    dialog.accept()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.connection_payload() == {
        "account_label": "Google",
        "enabled": True,
        "calendar_sync_enabled": False,
        "client_id": "desktop-client.apps.googleusercontent.com",
        "capabilities": ("calendar", "messages"),
        "client_secret": "temporary-secret",
    }
    assert dialog.client_secret_input.text() == ""


@pytest.mark.ui
def test_canvas_setup_dialog_collects_and_clears_private_ical_url(qtbot) -> None:
    definition = next(item for item in PLATFORM_DEFINITIONS if item.platform_id == "canvas")
    dialog = ExternalPlatformDialog(definition)
    qtbot.addWidget(dialog)
    assert dialog.canvas_feed_input is not None
    dialog.account_input.setText("School Canvas")
    dialog.canvas_feed_input.setText("https://canvas.example.edu/feeds/calendars/private-token.ics")

    dialog.accept()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.connection_payload() == {
        "account_label": "School Canvas",
        "enabled": True,
        "capabilities": ("tasks", "calendar"),
        "feed_url": "https://canvas.example.edu/feeds/calendars/private-token.ics",
    }
    assert dialog.canvas_feed_input.text() == ""
