"""Phase 6 message-summary rendering and explicit memory-only body workflow."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from PySide6.QtWidgets import QApplication

from personal_hub.bootstrap import build_hub_container
from personal_hub.config import HubSettings
from personal_hub.messages import (
    MessageBody,
    MessageQuery,
    MessageSnapshot,
    MessageSummary,
)
from personal_hub.ui.desktop.application import HubDesktopCoordinator
from personal_hub.ui.desktop.main_window import (
    MessageBodyView,
    MessageSourceView,
    MessageSummaryView,
    MessagesView,
    create_main_window,
)

NOW = datetime(2026, 9, 1, 14, 30, tzinfo=UTC)


def _summary_view() -> MessageSummaryView:
    return MessageSummaryView(
        source_id="gmail-primary",
        message_id="message-1",
        provider="gmail",
        account="private@example.test",
        sender="Private Sender",
        title="Private subject",
        preview="Private preview",
        received_at=NOW,
        importance="important",
        deep_link="https://mail.google.com/mail/u/0/#all/message-1",
    )


class _MessageSource:
    source_id = "gmail-primary"
    provider_id = "gmail"

    def __init__(self) -> None:
        self.list_calls = 0
        self.body_calls = 0
        self.offline = False

    def set_offline(self, value: bool) -> None:
        self.offline = value

    async def list_summaries(self, _query: MessageQuery) -> MessageSnapshot:
        self.list_calls += 1
        await asyncio.sleep(0.05)
        return MessageSnapshot(
            source_id=self.source_id,
            provider="gmail",
            account_id="private@example.test",
            collected_at=NOW,
            summaries=(
                MessageSummary(
                    source_id=self.source_id,
                    message_id="message-1",
                    provider="gmail",
                    account_id="private@example.test",
                    sender="Private Sender",
                    title="Private subject",
                    preview="Private preview",
                    received_at=NOW,
                    unread=True,
                    importance="important",
                    deep_link="https://mail.google.com/mail/u/0/#all/message-1",
                ),
            ),
        )

    async def get_body(self, message_id: str) -> MessageBody:
        self.body_calls += 1
        await asyncio.sleep(0.05)
        return MessageBody(
            source_id=self.source_id,
            message_id=message_id,
            plain_text="Memory-only private body",
            links=("https://safe.example.test/path",),
            fetched_at=NOW,
        )


@pytest.mark.ui
def test_message_surface_renders_summaries_and_emits_explicit_reads_only(qtbot) -> None:
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    window.show_destination("Inbox")
    source = MessageSourceView(
        source_id="gmail-primary",
        provider="gmail",
        account="private@example.test",
        selection="Folders: INBOX",
        status="ready",
    )
    summary = _summary_view()
    window.render_messages(MessagesView(sources=(source,), summaries=(summary,)))

    assert window.message_list.count() == 1
    assert "Private subject" in window.message_list.item(0).text()
    assert window.message_body is None
    opened: list[object] = []
    links: list[object] = []
    cleared: list[bool] = []
    window.message_open_requested.connect(opened.append)
    window.message_deep_link_requested.connect(links.append)
    window.message_clear_requested.connect(lambda: cleared.append(True))
    window.message_list.setCurrentRow(0)
    window.message_open_button.click()
    assert opened == [summary]
    assert window.message_body is None
    window.message_provider_button.click()
    assert links == [summary.deep_link]
    window.message_clear_button.click()
    assert cleared == [True]

    body = MessageBodyView(
        source_id=summary.source_id,
        message_id=summary.message_id,
        plain_text="Sanitized plain body",
        links=("https://safe.example.test/path",),
        fetched_at=NOW,
    )
    window.render_message_body(body)
    assert window.message_body_view.toPlainText() == "Sanitized plain body"
    assert window.message_link_list.count() == 1
    assert window.message_body_view.document().toHtml().count("<img") == 0

    window.set_privacy_masked(True)
    assert "Private subject" not in window.message_list.item(0).text()
    assert window.message_body_view.toPlainText() == "Hidden by privacy settings"
    window.set_locked(True)
    assert window.message_body is None
    assert window.message_body_view.toPlainText() == ""


@pytest.mark.ui
def test_coordinator_refreshes_summaries_without_bodies_then_clears_body_on_lock(
    qtbot,
    tmp_path: Path,
) -> None:
    container = build_hub_container(HubSettings(data_dir=tmp_path / "hub", default_timezone="UTC"))
    container.controller.create_message_source(
        source_id="gmail-primary",
        provider="gmail",
        account_id="private@example.test",
        query=MessageQuery(folders=("INBOX",)),
        now=NOW,
    )
    source = _MessageSource()
    container.controller.register_message_adapter(source)
    container.lifecycle.start()
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    coordinator = HubDesktopCoordinator(container, window, application)

    coordinator.refresh_messages(force=True)
    qtbot.waitUntil(lambda: coordinator._message_refresh_thread is None, timeout=3_000)
    assert source.list_calls == 1
    assert source.body_calls == 0
    assert window.message_list.count() == 1
    assert window.message_body is None
    messages_section = next(
        section for section in window.today_sections if section.section_id == "inbox"
    )
    assert len(messages_section.items) == 1

    window.message_list.setCurrentRow(0)
    window.message_open_button.click()
    qtbot.waitUntil(lambda: coordinator._message_body_thread is None, timeout=3_000)
    assert source.body_calls == 1
    assert window.message_body_view.toPlainText() == "Memory-only private body"
    assert container.message_service.cached_body("gmail-primary", "message-1") is not None

    container.lifecycle.handle_session_lock()
    assert window.message_body is None
    assert container.message_service.cached_body("gmail-primary", "message-1") is None
    assert source.body_calls == 1

    coordinator.shutdown()
    container.close()
