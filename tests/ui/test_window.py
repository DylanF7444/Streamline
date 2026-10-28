from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QAbstractButton, QApplication, QLabel, QMainWindow, QTabBar, QWidget

EXPECTED_NAVIGATION = (
    "Home",
    "Calendar",
    "Tasks",
    "Watchlists",
    "Inbox",
    "Sources",
    "Settings",
    "Activity",
    "Reflection",
    "Summary",
)


def _hub_container(tmp_path: Path):
    from personal_hub.bootstrap import build_hub_container
    from personal_hub.config import HubSettings

    return build_hub_container(
        HubSettings(data_dir=tmp_path / "hub", database_name="personal_hub.db")
    )


def _window_factory():
    candidates = (
        ("personal_hub.ui", "create_main_window"),
        ("personal_hub.ui.desktop", "create_main_window"),
        ("personal_hub.ui.desktop.app", "create_main_window"),
        ("personal_hub.ui.desktop.window", "create_main_window"),
        ("personal_hub.app", "create_main_window"),
    )
    for module_name, attribute_name in candidates:
        module = importlib.import_module(module_name)
        factory = getattr(module, attribute_name, None)
        if callable(factory):
            return factory
    raise AssertionError("Phase 0 must expose create_main_window for the Hub UI")


def _make_window(factory: Any, container: Any):
    parameters = inspect.signature(factory).parameters
    required = [
        parameter
        for parameter in parameters.values()
        if parameter.default is inspect.Parameter.empty
        and parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    if required:
        return factory(container)
    return factory()


def _visible_texts(window: QWidget) -> set[str]:
    widgets = [window, *window.findChildren(QWidget)]
    values: set[str] = set()
    for widget in widgets:
        text = getattr(widget, "text", None)
        if callable(text):
            value = str(text()).strip()
            if value:
                values.add(value)
        accessible_name = widget.accessibleName().strip()
        if accessible_name:
            values.add(accessible_name)
    for tab_bar in window.findChildren(QTabBar):
        values.update(
            tab_bar.tabText(index).strip()
            for index in range(tab_bar.count())
            if tab_bar.tabText(index).strip()
        )
    return values


class _TopLevelShowRecorder(QObject):
    """Capture widgets that Qt tries to show as standalone windows."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.widgets: list[QWidget] = []

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            isinstance(watched, QWidget)
            and watched.isWindow()
            and event.type() in {QEvent.Type.Show, QEvent.Type.ShowToParent}
        ):
            self.widgets.append(watched)
        return False


@pytest.mark.ui
def test_window_construction_and_refresh_do_not_show_unparented_today_widgets(qtbot):
    application = QApplication.instance()
    assert application is not None
    recorder = _TopLevelShowRecorder(application)
    application.installEventFilter(recorder)
    try:
        window = _make_window(_window_factory(), None)
        window.render_today_sections(
            [
                {
                    "section_id": "refresh-check",
                    "title": "Refresh check",
                    "body": "The Today section was refreshed.",
                    "visible": True,
                }
            ]
        )
    finally:
        application.removeEventFilter(recorder)

    qtbot.addWidget(window)
    assert recorder.widgets == []


@pytest.mark.ui
def test_minimal_hub_window_has_personal_hub_title_and_primary_navigation(qtbot, tmp_path):
    container = _hub_container(tmp_path)
    window = _make_window(_window_factory(), container)
    assert isinstance(window, QMainWindow)
    qtbot.addWidget(window)

    window.show()
    qtbot.waitUntil(window.isVisible, timeout=2000)

    assert window.windowTitle() == "Personal Hub"
    texts = _visible_texts(window)
    assert set(EXPECTED_NAVIGATION).issubset(texts)
    assert window.navigation_caption.text() == "NAVIGATION"
    assert window.today_subtitle.text() == "Schedule, tasks, alerts, news, and source status."
    assert not window.findChildren(QLabel, "brandSubtitle")
    assert "A little clarity for every day." not in texts
    assert "YOUR PERSONAL SPACE" not in texts
    assert "Stored on this device" not in texts
    assert any(
        isinstance(widget, (QAbstractButton, QLabel)) and widget.text().strip() == "Home"
        for widget in [window, *window.findChildren(QWidget)]
        if hasattr(widget, "text") and callable(widget.text)
    )

    forbidden_ui_text = {
        "assistant",
        "transcript",
        "prompt",
        "microphone",
        "command palette",
    }
    lowered_texts = {text.casefold() for text in texts}
    assert lowered_texts.isdisjoint(forbidden_ui_text)

    close = getattr(container, "close", None)
    if callable(close):
        close()


@pytest.mark.ui
def test_phase1_navigation_is_enabled_accessible_and_responsive(qtbot):
    window = _make_window(_window_factory(), None)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(window.isVisible, timeout=2000)

    assert window.pages.count() == len(EXPECTED_NAVIGATION)
    assert window.active_destination == "Home"
    assert window.navigation_buttons["Home"].isChecked()
    assert all(button.isEnabled() for button in window.navigation_buttons.values())
    assert all(
        button.accessibleName() == destination and button.accessibleDescription()
        for destination, button in window.navigation_buttons.items()
    )

    assert window.show_destination("Inbox")
    assert window.active_destination == "Inbox"
    assert window.pages.currentWidget() is window.page_widgets["Inbox"]
    assert window.inbox_button.isChecked()

    window.resize(760, 500)
    qtbot.waitUntil(lambda: window.compact_navigation)
    assert window.navigation.minimumWidth() < 190
    window.resize(1000, 700)
    qtbot.waitUntil(lambda: not window.compact_navigation)


@pytest.mark.ui
def test_inbox_sources_settings_and_diagnostics_render_plain_view_data(qtbot):
    from personal_hub.ui.desktop.main_window import (
        DiagnosticsView,
        InboxItemView,
        SettingsView,
        SourceFailureView,
        SourceView,
    )

    window = _make_window(_window_factory(), None)
    qtbot.addWidget(window)

    window.render_inbox(
        [
            InboxItemView(
                item_id="alert-1",
                title="Price changed",
                summary="The tracked item changed.",
                source="Watchlist",
                timestamp="2026-08-31T10:00:00Z",
                sensitive=False,
            ),
            {
                "item_id": "message-1",
                "title": "Private subject",
                "summary": "Private preview",
                "source": "Mail",
                "sensitive": True,
            },
        ]
    )
    assert window.inbox_list.count() == 2
    assert "Price changed" in window.inbox_list.item(0).text()
    assert window.inbox_list.item(0).data(0x0100) == "alert-1"

    window.render_sources(
        [
            SourceView(
                source_id="calendar",
                display_name="Calendar",
                status="ready",
                detail="Last refresh succeeded",
            )
        ]
    )
    assert window.sources_list.count() == 1
    assert "Calendar" in window.sources_list.item(0).text()
    refreshed: list[str] = []
    reconnected: list[str] = []
    enabled_changes: list[object] = []
    disconnected: list[str] = []
    window.source_refresh_requested.connect(refreshed.append)
    window.source_reconnect_requested.connect(reconnected.append)
    window.source_enabled_changed.connect(enabled_changes.append)
    window.source_disconnect_requested.connect(disconnected.append)
    window.source_refresh_button.click()
    window.source_reconnect_button.click()
    window.source_enable_button.click()
    window.source_disconnect_button.click()
    assert refreshed == ["calendar"]
    assert reconnected == ["calendar"]
    assert enabled_changes == [("calendar", False)]
    assert disconnected == ["calendar"]

    window.render_sources(
        [SourceView(source_id="calendar", display_name="Calendar", enabled=False)]
    )
    assert window.source_enable_button.text() == "Enable"
    assert not window.source_refresh_button.isEnabled()

    window.render_settings(
        SettingsView(
            offline_mode=True,
            privacy_masking=True,
            close_to_tray=False,
            text_scale=1.25,
            high_contrast=True,
            reduced_motion=True,
            network_refresh_paused=True,
        )
    )
    assert window.settings_values["offline_mode"] == "On"
    assert window.settings_values["network_refresh_paused"] == "Paused"
    assert window.offline_mode is True
    assert window.close_to_tray is False
    assert window.text_scale == 1.25
    assert window.high_contrast is True
    assert window.reduced_motion is True
    exports: list[bool] = []
    window.export_requested.connect(lambda: exports.append(True))
    window.settings_export_button.click()
    assert exports == [True]

    diagnostics = DiagnosticsView(
        status="degraded",
        detail="One source needs attention",
        entries=("Database integrity: ok",),
        source_failures=(SourceFailureView("calendar", "Timed out", "Calendar"),),
        generated_at="2026-08-31T10:01:00Z",
    )
    window.render_diagnostics(diagnostics)
    assert "degraded" in window.diagnostics_status_label.text()
    assert window.diagnostics_list.count() == 2
    assert "Timed out" in window.diagnostics_list.item(1).text()


@pytest.mark.ui
def test_privacy_masking_lock_and_nonblocking_source_failure_render(qtbot):
    from personal_hub.ui.desktop.main_window import InboxItemView

    window = _make_window(_window_factory(), None)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(window.isVisible, timeout=2000)

    window.render_inbox(
        [
            InboxItemView(
                item_id="private-1",
                title="Sensitive subject",
                summary="Sensitive body",
                source="Private source",
                sensitive=True,
            )
        ]
    )
    assert "Sensitive subject" in window.inbox_list.item(0).text()
    window.set_privacy_masked(True)
    assert "Sensitive subject" not in window.inbox_list.item(0).text()
    assert "Hidden by privacy settings" in window.inbox_list.item(0).text()

    window.set_locked(True)
    assert window.locked is True
    assert window.privacy_overlay.isVisible()
    assert "locked" in window.status_label.text()
    window.set_locked(False)
    assert not window.privacy_overlay.isVisible()

    with qtbot.waitSignal(window.source_failure_received, timeout=1000):
        window.post_source_failure(
            "calendar",
            "Timed out while refreshing",
            last_success_at="2026-08-31T09:00:00Z",
        )
    assert window.source_failure_panel.isVisible()
    assert "Timed out while refreshing" in window.source_failure_body.text()
    window.clear_source_failures("calendar")
    assert not window.source_failure_panel.isVisible()


@pytest.mark.ui
def test_tray_actions_emit_requests_and_close_to_tray_is_reversible(qtbot):
    window = _make_window(_window_factory(), None)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(window.isVisible, timeout=2000)

    assert {
        "open",
        "today",
        "inbox",
        "refresh",
        "pause",
        "offline",
        "quit",
    } <= set(window.tray_controller.actions)
    assert window.tray_controller.open_action.text() == "Open Hub"
    assert window.tray_controller.refresh_action.text() == "Refresh enabled sources"

    calls: list[str] = []
    window.refresh_requested.connect(lambda: calls.append("refresh"))
    window.tray_controller.trigger("refresh")
    assert calls == ["refresh"]

    window.tray_controller.trigger("pause")
    assert window.network_refresh_paused is True
    assert window.tray_controller.pause_action.text() == "Resume network refresh"
    window.tray_controller.trigger("offline")
    assert window.offline_mode is True
    window.tray_controller.trigger("today")
    assert window.active_destination == "Home"

    window.set_close_to_tray(True)
    window.close()
    qtbot.waitUntil(lambda: not window.isVisible(), timeout=1000)
    assert window._quitting is False
    window.show_from_tray()
    qtbot.waitUntil(window.isVisible, timeout=1000)
    window.request_quit()
    assert window._quitting is True
