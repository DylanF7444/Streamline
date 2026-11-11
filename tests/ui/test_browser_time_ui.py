"""Browser site review, consent, masking and deletion in the actual desktop UI."""

from datetime import UTC, datetime, timedelta

import pytest
from PySide6.QtCore import QDate
from PySide6.QtWidgets import QMessageBox
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
            browser="chrome", domain="example.com", start=start, end=start + timedelta(minutes=10)
        )
    )
    page.start_date.setDate(QDate(2026, 11, 10))
    page.end_date.setDate(QDate(2026, 11, 10))
    panel.refresh()
    assert panel.sites.rowCount() == 1
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
