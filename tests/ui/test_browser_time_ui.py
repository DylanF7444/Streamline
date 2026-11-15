"""Browser site review, consent, masking and deletion in the actual desktop UI."""

from datetime import UTC, datetime, timedelta

import pytest
from PySide6.QtCore import QDate
from PySide6.QtWidgets import QComboBox, QDialog, QFileDialog, QInputDialog, QMessageBox
from test_screen_time_ui import ui as screen_ui  # noqa: F401

from personal_hub.browser_time.contracts import BrowserTimeSession

pytestmark = pytest.mark.ui


def test_browser_consent_review_mask_lock_and_delete(screen_ui, monkeypatch, qtbot):  # noqa: F811
    hub, window, page = screen_ui
    panel = page.browser
    assert not panel.runtime.running
    panel.toggle.click()
    assert panel.runtime.running
    panel.runtime.tick(page.observer.sample())
    window.set_locked(True)
    assert not panel.runtime.running
    assert not (panel.runtime.root / "lease.json").exists()
    window.set_locked(False)
    panel.toggle.click()
    generation = panel.runtime.generation
    panel.exclusions.setText("private.example.com")
    panel.save.click()
    assert panel.settings.excluded_domains == ("private.example.com",)
    assert panel.runtime.running
    assert panel.runtime.generation != generation
    start = datetime(2026, 11, 10, 12, tzinfo=UTC)
    hub.browser_time_repository.save(
        BrowserTimeSession(
            browser="brave", domain="example.com", start=start, end=start + timedelta(minutes=10)
        )
    )
    page.start_date.setDate(QDate(2026, 11, 10))
    page.end_date.setDate(QDate(2026, 11, 10))
    panel.refresh()
    assert panel.sites.rowCount() == 1
    assert panel.sessions.item(0, 2).text() == "brave"
    assert "10m" in panel.total.text()
    panel.domain.setText("example.com")
    panel.alias.setText("My site")
    panel.category.setText("Reading")
    panel.rule_save.click()
    assert panel.sites.item(0, 0).text() == "My site"
    window.set_privacy_masked(True)
    assert panel.sites.rowCount() == 0
    assert not panel.total.text()
    assert not panel.exclusions.text()
    assert not panel.toggle.isEnabled()
    window.set_privacy_masked(False)
    assert panel.exclusions.text() == "private.example.com"
    assert panel.sites.rowCount() == 1
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    panel.delete.click()
    assert not hub.browser_time_repository.records()

    window.resize(720, 460)
    window.show_destination("Screen time")
    window.show()
    qtbot.wait(100)
    assert page.horizontalScrollBar().maximum() == 0


def test_brave_setup_choice_and_extension_export(screen_ui, monkeypatch, tmp_path):  # noqa: F811
    from personal_hub.ui.desktop import browser_time_panel

    hub, window, page = screen_ui
    registered = []
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a: str(tmp_path))
    monkeypatch.setattr(QMessageBox, "information", lambda *a: None)
    monkeypatch.setattr(QInputDialog, "getText", lambda *a: ("a" * 32, True))

    def choose_brave(dialog):
        combo = dialog.findChild(QComboBox)
        assert combo.findText("brave") >= 0
        combo.setCurrentIndex(combo.findText("brave"))
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QDialog, "exec", choose_brave)
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        lambda *a: (str(tmp_path / "PersonalHubBrowserHost.exe"), ""),
    )
    monkeypatch.setattr(browser_time_panel, "register", lambda *a: registered.append(a))
    page.browser.setup_extension()
    assert registered[0][0] == "brave"
    assert registered[0][1] == "a" * 32
    assert registered[0][3] == hub.settings.data_dir
    assert (tmp_path / "PersonalHubBrowserTime" / "manifest.json").is_file()
