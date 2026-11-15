"""Offline orientation, message recovery, and compact Inbox regression coverage."""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QScrollArea

from personal_hub.ui.desktop.main_window import (
    MessageSourceView,
    MessageSummaryView,
    MessagesView,
    SourceView,
    create_main_window,
)

pytestmark = pytest.mark.ui


@pytest.fixture
def window(qtbot):
    window = create_main_window(close_to_tray=False)
    window.set_reduced_motion(True)
    qtbot.addWidget(window)
    window.show()
    yield window
    window.set_text_scale(1.0)


def test_visible_page_and_mode_guidance_preserves_remembered_routes(window):
    for destination in ("Home", "Calendar", "Tasks", "Watchlists", "Inbox", "Sources", "Settings"):
        window.show_destination(destination)
        descriptions = [
            label
            for label in window.page_widgets[destination].findChildren(QLabel)
            if label.accessibleName() == f"{destination} page description"
        ]
        assert len(descriptions) == 1
        assert descriptions[0].isVisible() and descriptions[0].text()
        assert descriptions[0].wordWrap()
        assert descriptions[0].textFormat() == Qt.TextFormat.PlainText
    window.show_destination("Tasks")
    window.set_workspace_mode("Logging")
    assert "reflect" in window.mode_description_label.text()
    window.show_destination("Activity")
    description = window.page_widgets["Activity"].findChild(QLabel, "pageSubtitle")
    assert description.isVisible() and "automatically" in description.text()
    window.set_workspace_mode("Tracking")
    assert window.active_destination == "Tasks"
    assert "attention" in window.mode_description_label.text()
    window.set_workspace_mode("Logging")
    assert window.active_destination == "Activity"


def test_message_health_recovery_and_category_empty_state_do_not_fetch(window):
    window.show_destination("Inbox")
    window.inbox_mode_tabs.setCurrentIndex(1)
    requests = []
    window.message_open_requested.connect(requests.append)
    window.source_refresh_requested.connect(requests.append)
    window.render_messages(MessagesView())
    assert "No message sources configured" in window.message_status_label.text()
    assert window.message_sources_button.isVisible()
    window.message_sources_button.click()
    assert window.active_destination == "Sources"

    source = MessageSourceView("mail", "gmail", status="ready")
    summary = MessageSummaryView(
        "mail", "one", "gmail", "example.test", "Example", "A saved summary", category="updates"
    )
    for state, explanation in (
        ("stale", "out of date"),
        ("offline", "Offline"),
        ("permission_required", "Reconnect"),
        ("disconnected", "reconnect"),
        ("error", "retry"),
        ("paused", "paused"),
        ("refreshing", "Refreshing"),
    ):
        window.render_messages(
            MessagesView(sources=(source,), summaries=(summary,), freshness=state)
        )
        assert explanation in window.message_status_label.text()
        assert "permission_required" not in window.message_status_label.text()
        assert "Choose another category" in window.message_status_label.text()
    window.render_messages(MessagesView(sources=(source,), summaries=(summary,), freshness="ready"))
    assert window.message_sources_button.isHidden()
    window.set_message_category("updates")
    assert "1 summary in Updates" in window.message_status_label.text()
    assert window.message_list.count() == 1
    window.set_network_refresh_paused(True)
    assert "paused" in window.message_status_label.text()
    window.set_network_refresh_paused(False)
    assert "Up to date" in window.message_status_label.text()
    window.set_privacy_masked(True)
    assert "A saved summary" not in window.message_list.item(0).text()
    window.set_locked(True)
    assert not window.message_sources_button.isEnabled()
    assert requests == []


def test_sources_summary_counts_actionable_problems_without_flagging_unused_platforms(window):
    window.render_sources(
        [
            SourceView("platform-google", "Google", status="disconnected", enabled=False),
            SourceView("mail", "Mail", status="permission_required"),
            SourceView("calendar", "Calendar", status="stale"),
            SourceView("disabled", "Disabled", status="error", enabled=False),
        ]
    )
    assert window.sources_status_label.text().startswith("2 sources need attention.")
    window.sources_list.setCurrentRow(2)
    window.render_sources(window.sources)
    assert window.sources_list.currentItem().data(Qt.ItemDataRole.UserRole) == "calendar"
    window.render_sources(
        [SourceView("platform-google", "Google", status="disconnected", enabled=False)]
    )
    assert "No source problems reported" in window.sources_status_label.text()
    assert "optional" in window.sources_status_label.text()
    assert "Connect / Configure" in window.source_guidance_label.text()
    window.render_sources([SourceView("platform-google", "Google", status="disconnected")])
    assert window.sources_status_label.text().startswith("1 source needs attention.")


def test_compact_inbox_keeps_message_actions_reachable_by_keyboard(window, qtbot):
    window.resize(720, 460)
    window.apply_accessibility_preferences(text_scale=1.75, high_contrast=True, reduced_motion=True)
    window.show_destination("Inbox")
    window.inbox_mode_tabs.setCurrentIndex(1)
    window.render_messages(MessagesView())
    qtbot.wait(10)
    page = window.page_widgets["Inbox"]
    assert isinstance(page, QScrollArea)
    assert window.message_content_splitter.orientation() == Qt.Orientation.Vertical
    assert page.horizontalScrollBar().maximum() == 0
    window.message_category_selector.setFocus()
    qtbot.keyClick(window.message_category_selector, Qt.Key.Key_Backtab)
    qtbot.waitUntil(window.message_sources_button.hasFocus)
    qtbot.waitUntil(
        lambda: window.message_sources_button.visibleRegion().contains(
            window.message_sources_button.rect()
        )
    )
    assert (
        window.message_sources_button.height() >= window.message_sources_button.sizeHint().height()
    )
    qtbot.keyClick(window.message_sources_button, Qt.Key.Key_Space)
    assert window.active_destination == "Sources"
    window.show_destination("Inbox")
    window.render_messages(
        MessagesView(
            sources=(MessageSourceView("mail", "gmail", status="ready"),),
            summaries=(
                MessageSummaryView(
                    "mail",
                    "one",
                    "gmail",
                    "example.test",
                    "Example",
                    "Saved",
                    deep_link="https://example.test/message",
                ),
            ),
        )
    )
    window.message_list.setCurrentRow(0)
    window.message_category_selector.setFocus()
    for target in (
        window.message_open_button,
        window.message_provider_button,
        window.message_clear_button,
    ):
        for _ in range(8):
            if target.hasFocus():
                break
            qtbot.keyClick(window.focusWidget(), Qt.Key.Key_Tab)
        assert target.hasFocus()
        qtbot.waitUntil(lambda control=target: control.visibleRegion().contains(control.rect()))
    assert page.horizontalScrollBar().maximum() == 0
    window.inbox_mode_tabs.setCurrentIndex(0)
    assert page.horizontalScrollBar().maximum() == 0
    window.resize(1200, 900)
    qtbot.wait(10)
    assert window.message_content_splitter.orientation() == Qt.Orientation.Vertical
    window.set_text_scale(1.0)
    assert window.message_content_splitter.orientation() == Qt.Orientation.Horizontal


def test_top_settings_save_emits_existing_payload_and_shows_feedback(window):
    window.show_destination("Settings")
    saved = []
    window.settings_save_requested.connect(saved.append)
    window.settings_inputs["feed_refresh_minutes"].setText("45")
    window.settings_top_save_button.click()
    assert len(saved) == 1 and saved[0]["feed_refresh_minutes"] == "45"
    window.set_settings_save_status("Settings saved on this device.")
    assert window.settings_status_label.text() == "Settings saved on this device."
