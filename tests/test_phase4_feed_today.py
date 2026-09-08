from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from personal_hub.feeds.contracts import FeedEntry, FeedEntryRecord, FeedSubscription
from personal_hub.feeds.today import project_feed_today


def test_feed_today_projects_only_selected_unread_entries_in_stable_order() -> None:
    subscription = FeedSubscription(
        subscription_id=UUID("00000000-0000-0000-0000-000000000401"),
        title="Release feed",
        feed_url="https://news.example.test/feed.xml",
    )
    observed = datetime(2026, 8, 31, 12, tzinfo=UTC)
    newer = FeedEntry(
        entry_id="feed-new",
        subscription_id=subscription.subscription_id,
        title="New release",
        url="https://news.example.test/new",
        summary_text="A normalized summary",
        published_at=observed + timedelta(hours=1),
        first_seen_at=observed,
    )
    older = FeedEntry(
        entry_id="feed-old",
        subscription_id=subscription.subscription_id,
        title="Old release",
        url="https://news.example.test/old",
        published_at=observed,
        first_seen_at=observed,
    )

    projected = project_feed_today(
        (
            FeedEntryRecord(entry=older, state="read", read_at=observed),
            FeedEntryRecord(entry=newer),
        ),
        {subscription.subscription_id: subscription},
    )

    assert [item.title for item in projected] == ["New release"]
    assert projected[0].kind == "news"
    assert projected[0].source.provider_id == "rss"
    assert projected[0].source.display_name == "Release feed"
    assert projected[0].navigation_target == "https://news.example.test/new"
    assert projected[0].sensitive


def test_feed_today_is_bounded_and_uses_local_navigation_for_oversized_links() -> None:
    subscription = FeedSubscription(
        subscription_id=UUID("00000000-0000-0000-0000-000000000402"),
        title="Long links",
        feed_url="https://news.example.test/feed.xml",
    )
    entry = FeedEntry(
        entry_id="feed-long",
        subscription_id=subscription.subscription_id,
        title="Long link",
        url="https://news.example.test/article?value=" + "x" * 700,
        first_seen_at=datetime(2026, 8, 31, 12, tzinfo=UTC),
    )

    item = project_feed_today(
        (FeedEntryRecord(entry=entry),),
        {subscription.subscription_id: subscription},
        limit=1,
    )[0]

    assert item.navigation_target == "hub://watchlists/news/feed-long"
