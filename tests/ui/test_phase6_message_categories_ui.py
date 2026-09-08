"""Gmail-style category tabs for the Personal Hub message surface."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from personal_hub.ui.desktop.main_window import (
    MessageSummaryView,
    MessagesView,
    create_main_window,
)

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


def _summary(message_id: str, category: str) -> MessageSummaryView:
    return MessageSummaryView(
        source_id="gmail-primary",
        message_id=message_id,
        provider="gmail",
        account="private@example.test",
        sender=f"Sender {message_id}",
        title=f"{category.title()} subject",
        preview="Preview",
        received_at=NOW,
        category=category,
        deep_link=f"https://mail.google.com/mail/u/0/#all/{message_id}",
    )


@pytest.mark.ui
def test_message_category_tabs_filter_locally_and_keep_spam_out_of_primary(qtbot) -> None:
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    window.show_destination("Inbox")
    view = MessagesView(
        summaries=tuple(
            _summary(category, category)
            for category in ("primary", "promotions", "social", "updates", "spam")
        )
    )
    window.render_messages(view)

    assert window.message_category == "primary"
    assert window.message_list.count() == 1
    assert "Primary subject" in window.message_list.item(0).text()
    assert set(window.message_category_buttons) == {
        "primary",
        "promotions",
        "social",
        "updates",
        "spam",
    }
    assert window.message_category_buttons["primary"].isChecked()
    assert "5" not in window.message_category_status_label.text()

    changed: list[str] = []
    window.message_category_changed.connect(changed.append)
    window.message_category_buttons["promotions"].click()
    assert window.message_category == "promotions"
    assert window.message_list.count() == 1
    assert "Promotions subject" in window.message_list.item(0).text()
    assert changed == ["promotions"]

    window.message_category_buttons["spam"].click()
    assert window.message_list.count() == 1
    assert "Spam subject" in window.message_list.item(0).text()
    assert window.message_category_status_label.text().startswith("Spam: 1")
