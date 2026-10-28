"""Phase 7 minimum-size, scaling, contrast, and keyboard accessibility checks."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPushButton, QScrollArea

from personal_hub.ui.desktop.main_window import NAVIGATION_DESTINATIONS, create_main_window


@pytest.mark.ui
def test_minimum_window_and_maximum_supported_text_scale_keep_all_destinations_accessible(
    qtbot,
) -> None:
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    window.resize(*window.MINIMUM_SIZE)
    window.apply_accessibility_preferences(
        text_scale=1.75,
        high_contrast=True,
        reduced_motion=True,
    )
    window.show()
    qtbot.waitUntil(window.isVisible, timeout=2_000)

    assert window.size().width() >= window.MINIMUM_SIZE[0]
    assert window.size().height() >= window.MINIMUM_SIZE[1]
    assert window.text_scale == 1.75
    assert window.high_contrast
    assert window.reduced_motion
    assert set(window.navigation_buttons) == set(NAVIGATION_DESTINATIONS)
    for mode, workspace_pages in (
        ("Tracking", {"Home", "Calendar", "Tasks", "Watchlists", "Inbox"}),
        ("Logging", {"Activity", "Reflection", "Summary"}),
    ):
        window.set_workspace_mode(mode)
        visible_pages = workspace_pages | {"Sources", "Settings"}
        for destination, button in window.navigation_buttons.items():
            assert button.isVisible() == (destination in visible_pages)
            assert button.focusPolicy() == Qt.FocusPolicy.StrongFocus
            assert button.accessibleName() == destination
            assert button.minimumHeight() >= 38

    window.set_workspace_mode("Tracking")
    window.today_button.setFocus()
    qtbot.keyClick(window.today_button, Qt.Key.Key_Tab)
    focused = window.focusWidget()
    assert isinstance(focused, QPushButton)
    assert focused.accessibleName()


@pytest.mark.ui
def test_message_body_and_primary_lists_have_plain_accessible_names(qtbot) -> None:
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    assert window.inbox_list.accessibleName() == "Inbox items"
    assert window.message_list.accessibleName() == "Selected message summaries"
    assert window.message_body_view.accessibleName() == ("Sanitized memory-only message body")
    assert window.message_link_list.accessibleName() == "Safe links in the selected message"
    assert window.message_body_view.isReadOnly()


@pytest.mark.ui
def test_settings_and_watchlists_scroll_without_compressing_text_fields(qtbot) -> None:
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    window.resize(*window.MINIMUM_SIZE)
    window.apply_accessibility_preferences(
        text_scale=1.75,
        high_contrast=False,
        reduced_motion=True,
    )
    window.show()

    for destination, field in (
        ("Watchlists", window.watch_interval_input),
        ("Settings", window.settings_inputs["default_timezone"]),
    ):
        window.show_destination(destination)
        qtbot.wait(25)
        page = window.page_widgets[destination]
        assert isinstance(page, QScrollArea)
        assert field.height() >= field.sizeHint().height()
        assert page.verticalScrollBar().maximum() > 0
