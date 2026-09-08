from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from personal_hub.ui.desktop.main_window import (
    FeedEntryView,
    FeedSubscriptionView,
    NewsView,
    TodayItemView,
    create_main_window,
)


def _window(qtbot):
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(window.isVisible, timeout=2_000)
    window.show_destination("Watchlists")
    return window


def _subscription() -> FeedSubscriptionView:
    return FeedSubscriptionView(
        subscription_id="feed-tech",
        title="Tech Brief",
        feed_url="https://news.example/tech.xml",
        site_url="https://news.example/",
        include_terms=("security",),
        unread_count=2,
        entry_count=2,
    )


def _entries() -> tuple[FeedEntryView, ...]:
    return (
        FeedEntryView(
            entry_id="entry-1",
            subscription_id="feed-tech",
            title="Security update",
            url="https://news.example/security",
            source_name="Tech Brief",
            author="Reporter",
            published_at=datetime(2026, 8, 31, 12, tzinfo=UTC),
            summary_text="A bounded summary.",
            sensitive=False,
        ),
        FeedEntryView(
            entry_id="entry-2",
            subscription_id="feed-tech",
            title="Old item",
            url="https://news.example/old",
            summary_text="Already read.",
            read=True,
            sensitive=False,
        ),
    )


@pytest.mark.ui
def test_news_surface_renders_subscriptions_entries_and_today_digest(qtbot):
    window = _window(qtbot)
    window.render_news(
        NewsView(
            subscriptions=(_subscription(),),
            entries=_entries(),
            freshness="ready",
            generated_at=datetime(2026, 8, 31, 12, tzinfo=UTC),
        )
    )

    assert window.news_freshness == "ready"
    assert window.news_freshness_label.text() == "Ready"
    assert set(window.news_subscription_checkboxes) == {"feed-tech"}
    assert window.news_entries_list.count() == 1
    assert "Security update" in window.news_entry_rows["entry-1"].accessibleName()
    assert window.news_entry_read_buttons["entry-1"].text() == "Mark read"
    news_section = next(
        section for section in window.today_sections if section.section_id == "news"
    )
    assert len(news_section.items) == 1
    assert all(
        isinstance(item, TodayItemView) and item.kind == "news" for item in news_section.items
    )


@pytest.mark.ui
def test_news_filters_read_state_selection_and_safe_deep_links_are_local_signals(qtbot):
    window = _window(qtbot)
    second = FeedSubscriptionView(
        subscription_id="feed-other",
        title="Other Feed",
        feed_url="https://other.example/feed.xml",
    )
    unsafe = FeedEntryView(
        entry_id="entry-unsafe",
        subscription_id="feed-other",
        title="Unsafe link",
        url="javascript:alert(1)",
        navigation_target="javascript:alert(1)",
    )
    window.render_news(
        {
            "subscriptions": [_subscription(), second],
            "entries": [*_entries(), unsafe],
        }
    )
    changes: list[object] = []
    reads: list[object] = []
    opened: list[FeedEntryView] = []
    window.news_filter_changed.connect(changes.append)
    window.news_entry_read_changed.connect(reads.append)
    window.news_entry_open_requested.connect(opened.append)

    window.set_news_keyword_filters(include_terms="security")
    assert window.news_entries_list.count() == 1
    assert changes[-1]["include_terms"] == ("security",)
    window.set_news_keyword_filters(include_terms=(), exclude_terms="security")
    assert window.news_entries_list.count() == 1  # the other feed has no matching text
    window.set_news_subscription_selection(("feed-tech",))
    assert window.selected_subscription_ids == ("feed-tech",)
    window.set_news_keyword_filters(include_terms=(), exclude_terms=())
    window.set_news_unread_only(True)
    assert window.news_entries_list.count() == 1
    window.news_entry_read_buttons["entry-1"].click()
    assert reads[-1] == {"entry_id": "entry-1", "read": True}
    assert window.news_entries_list.count() == 0
    assert window.open_news_entry("entry-unsafe") is None
    window.set_news_unread_only(False)
    assert window.open_news_entry("entry-1") is not None
    assert opened[-1].entry_id == "entry-1"


@pytest.mark.ui
def test_news_freshness_privacy_and_hostile_content_are_visible_without_rich_text(qtbot):
    window = _window(qtbot)
    entry = FeedEntryView(
        entry_id="private",
        subscription_id="feed-tech",
        title="Private headline",
        url="https://news.example/private",
        summary_text="<script>window.evil = true</script><b>Private summary</b>",
        sensitive=True,
    )
    for state in (
        "ready",
        "empty",
        "stale",
        "offline",
        "permission_required",
        "disconnected",
        "error",
    ):
        window.render_news(
            {
                "subscriptions": [
                    FeedSubscriptionView(
                        subscription_id="feed-tech",
                        title="Tech Brief",
                        feed_url="https://news.example/tech.xml",
                    )
                ],
                "entries": [entry],
                "freshness": state,
            }
        )
        assert window.news_freshness == state
        assert window.news_freshness_label.text()
    window.render_news(
        {
            "subscriptions": [
                FeedSubscriptionView(
                    subscription_id="feed-tech",
                    title="Tech Brief",
                    feed_url="https://news.example/tech.xml",
                )
            ],
            "entries": [entry],
        }
    )
    row_text = " ".join(
        label.text()
        for label in window.news_entry_rows["private"].findChildren(type(window.news_status_label))
    )
    assert "<script>" not in row_text
    assert "window.evil" not in row_text
    assert "Private summary" in row_text
    window.set_privacy_masked(True)
    masked_text = " ".join(
        label.text()
        for label in window.news_entry_rows["private"].findChildren(type(window.news_status_label))
    )
    assert "Private headline" not in masked_text
    assert "Hidden by privacy settings" in masked_text
    controls = [
        button.text().casefold()
        for button in window.watchlists_page.findChildren(type(window.news_refresh_button))
    ]
    assert all(
        not any(word in text for word in ("delete", "write", "send", "reply")) for text in controls
    )


@pytest.mark.ui
def test_subscription_editor_validates_and_emits_create_edit_enable_remove_requests(qtbot):
    window = _window(qtbot)
    window.render_news({"subscriptions": [_subscription()]})
    created: list[dict[str, object]] = []
    updated: list[dict[str, object]] = []
    deleted: list[dict[str, str]] = []
    enabled: list[dict[str, object]] = []
    window.feed_subscription_create_requested.connect(created.append)
    window.feed_subscription_update_requested.connect(updated.append)
    window.feed_subscription_delete_requested.connect(deleted.append)
    window.feed_subscription_enabled_changed.connect(enabled.append)

    window.news_subscription_title_input.setText("Local feed")
    window.news_subscription_url_input.setText("http://feeds.example/local.xml")
    window.news_subscription_interval_input.setText("14")
    assert window.request_create_feed_subscription() is None
    assert "opt-in" in window.news_subscription_editor_status.text()
    window.news_subscription_http_checkbox.setChecked(True)
    assert window.request_create_feed_subscription() is None
    assert "15 minutes" in window.news_subscription_editor_status.text()
    window.news_subscription_interval_input.setText("15")
    payload = window.request_create_feed_subscription()
    assert payload is not None
    assert payload["feed_url"] == "http://feeds.example/local.xml"
    assert payload["refresh_minutes"] == 15
    assert created[-1] == payload

    assert window.edit_feed_subscription("feed-tech") is not None
    window.news_subscription_title_input.setText("Tech updates")
    window.news_subscription_interval_input.setText("20")
    update_payload = window.request_update_feed_subscription()
    assert update_payload is not None
    assert update_payload["subscription_id"] == "feed-tech"
    assert update_payload["title"] == "Tech updates"
    assert updated[-1] == update_payload

    window.news_subscription_enable_checkboxes["feed-tech"].click()
    assert enabled[-1] == {"subscription_id": "feed-tech", "enabled": False}
    window.news_subscription_remove_buttons["feed-tech"].click()
    assert deleted[-1] == {"subscription_id": "feed-tech"}
