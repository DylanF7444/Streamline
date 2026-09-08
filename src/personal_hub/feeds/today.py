"""Bounded News projection for the Personal Hub Today surface."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC
from uuid import UUID

from personal_hub.contracts import SourceRef
from personal_hub.feeds.contracts import FeedEntryRecord, FeedSubscription
from personal_hub.today import TodayItem


def project_feed_today(
    records: Iterable[FeedEntryRecord],
    subscriptions: Mapping[UUID, FeedSubscription],
    *,
    include_read: bool = False,
    limit: int = 100,
) -> tuple[TodayItem, ...]:
    """Project recent normalized feed entries without retaining raw feed content."""

    if not 1 <= limit <= 1_000:
        raise ValueError("feed Today limit must be between 1 and 1000")
    selected = [
        record
        for record in records
        if include_read or record.state == "unread"
        if record.entry.subscription_id in subscriptions
    ]
    selected.sort(key=_record_sort_key)
    items: list[TodayItem] = []
    for record in selected[:limit]:
        entry = record.entry
        subscription = subscriptions[entry.subscription_id]
        url = str(entry.url)
        feed_url = str(subscription.feed_url)
        navigation_target = url if len(url) <= 512 else f"hub://watchlists/news/{entry.entry_id}"
        items.append(
            TodayItem(
                item_id=f"news:{entry.entry_id}",
                kind="news",
                title=entry.title[:240],
                summary=(entry.summary_text or "")[:1_000] or None,
                starts_at=entry.published_at or entry.first_seen_at,
                due_at=None,
                severity="info",
                source=SourceRef(
                    provider_id="rss",
                    connection_id=f"feed.{entry.subscription_id.hex}",
                    display_name=subscription.title,
                    origin=feed_url if len(feed_url) <= 512 else None,
                ),
                navigation_target=navigation_target,
                sensitive=True,
            )
        )
    return tuple(items)


def _record_sort_key(record: FeedEntryRecord) -> tuple[object, ...]:
    entry = record.entry
    timestamp = entry.published_at or entry.first_seen_at
    return (
        -timestamp.astimezone(UTC).timestamp(),
        entry.title.casefold(),
        entry.entry_id,
    )


__all__ = ["project_feed_today"]
