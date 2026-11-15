"""Offline sync protection and actionable failure messages through the desktop UI."""

import pytest

from personal_hub.bootstrap import build_hub_container
from personal_hub.config import HubSettings
from personal_hub.ui.desktop.application import HubDesktopCoordinator
from personal_hub.ui.desktop.main_window import create_main_window

pytestmark = pytest.mark.ui


def test_sync_protection_choice_and_passphrase_errors(qtbot, qapp, tmp_path):
    container = build_hub_container(HubSettings(data_dir=tmp_path / "hub"))
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    coordinator = HubDesktopCoordinator(container, window, qapp)
    try:
        window.show_destination("Settings")
        window.sync_endpoint_input.setText(str(tmp_path / "shared"))
        window.sync_configure_button.click()
        assert window.sync_protection_combo.currentData() is True
        assert window.sync_passphrase_input.isEnabled()
        window.sync_button.click()
        assert "at least 12" in window.sync_status_label.text()
        window.sync_passphrase_input.setText("too short")
        window.sync_button.click()
        assert "at least 12" in window.sync_status_label.text()
        assert "Hub validation" not in window.sync_status_label.text()

        window.sync_protection_combo.setCurrentIndex(1)
        assert not container.controller.sync_status().passphrase_required
        assert not window.sync_passphrase_input.isEnabled()
        assert not window.sync_protection_notice.isHidden()
        window.sync_button.click()
        assert container.controller.sync_status().last_sync_at is not None
        assert container.controller.sync_status().last_error is None
        coordinator.render_all()
        assert window.sync_protection_combo.currentData() is False
        window.sync_protection_combo.setCurrentIndex(0)
        window.sync_passphrase_input.setText("long enough passphrase")
        window.sync_button.click()
        assert "uses no passphrase" in window.sync_status_label.text()
    finally:
        coordinator.shutdown()
        container.close()
