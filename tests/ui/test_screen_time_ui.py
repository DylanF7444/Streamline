"""Screen time controls, privacy, date review and saved local preferences."""

from datetime import UTC, datetime, timedelta

import pytest
from PySide6.QtCore import QDate
from PySide6.QtWidgets import QMessageBox

from personal_hub.bootstrap import build_hub_container
from personal_hub.config import HubSettings
from personal_hub.screen_time.collector import Observation
from personal_hub.screen_time.contracts import ScreenTimeSession
from personal_hub.ui.desktop.application import HubDesktopCoordinator
from personal_hub.ui.desktop.main_window import create_main_window

pytestmark = pytest.mark.ui


@pytest.fixture
def ui(qtbot, qapp, tmp_path):
    container = build_hub_container(HubSettings(data_dir=tmp_path / "hub"))
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    coordinator = HubDesktopCoordinator(container, window, qapp)
    view = window.screen_time_page
    view.observer.sample = lambda: Observation("editor.exe")
    view.observer.supported = True
    yield container.controller, window, view
    coordinator.shutdown()
    container.close()


def test_navigation_consent_settings_rules_and_mask(ui):
    hub, window, view = ui
    window.show_destination("Screen time")
    assert window.workspace_mode == "Logging"
    assert not view.recorder.running
    view.toggle.click()
    assert view.recorder.running
    view.tray_action.trigger()
    assert not view.recorder.running
    view.exclusions.setText("SECRET.exe")
    view.card.setChecked(True)
    view.save.click()
    assert view.recorder.settings.excluded_apps == ("secret.exe",)
    view.rule_app.setText("editor.exe")
    view.rule_name.setText("My editor")
    view.rule_category.setText("Writing")
    view.rule_save.click()
    assert (
        hub.settings_repository.get("screen_time.settings")["rules"]["editor.exe"]["category"]
        == "Writing"
    )
    window.set_privacy_masked(True)
    assert view.apps.rowCount() == 0
    assert not view.daily_label.text()
    assert not view.exclusions.text()
    window.set_privacy_masked(False)
    assert view.exclusions.text() == "secret.exe"


def test_review_delete_and_restore_pause(ui, tmp_path, monkeypatch):
    hub, window, view = ui
    start = datetime(2026, 11, 9, 12, tzinfo=UTC)
    hub.screen_time_repository.save(
        ScreenTimeSession(app="editor.exe", start=start, end=start + timedelta(minutes=10))
    )
    view.start_date.setDate(QDate(2026, 11, 9))
    view.end_date.setDate(QDate(2026, 11, 9))
    view.refresh()
    assert "10m" in view.total.text()
    assert view.apps.rowCount() == 1
    backup = hub.backup_database(tmp_path / "backup.db")
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    view.delete.click()
    assert not hub.screen_time_repository.records()
    view.recorder.running = True
    hub.restore_database(backup)
    assert not view.recorder.running


def test_lock_blocks_native_sampling_and_failure_pauses(ui, monkeypatch):
    hub, window, view = ui
    view.recorder.running = True
    window.set_locked(True)

    def fail_sample():
        raise AssertionError("Locked windows must not sample foreground apps")

    monkeypatch.setattr(view.observer, "sample", fail_sample)
    view.tick()
    assert view.recorder.running
    window.set_locked(False)

    def unavailable():
        raise OSError("private details that must not reach the UI")

    monkeypatch.setattr(view.observer, "sample", unavailable)
    view.tick()
    assert not view.recorder.running
    assert "private details" not in view.status.text()
    window.set_locked(True)
    view.toggle_recording()
    assert not view.recorder.running
