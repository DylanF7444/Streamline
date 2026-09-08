from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from personal_hub.bootstrap import build_hub_container
from personal_hub.config import HubSettings
from personal_hub.feeds import FeedResponse, FeedUnavailableError

RSS = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>Release feed</title>
  <link>https://news.example.test/</link>
  <item><guid>release-1</guid><title>Personal Hub release</title>
    <link>https://news.example.test/release-1</link>
    <description>Bounded release notes.</description>
    <pubDate>Mon, 31 Aug 2026 12:00:00 GMT</pubDate></item>
</channel></rss>"""


class _FeedTransport:
    def __init__(self, *, not_modified: bool = False) -> None:
        self.not_modified = not_modified
        self.calls: list[tuple[str, str | None, str | None]] = []

    async def fetch(
        self,
        subscription: Any,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FeedResponse:
        self.calls.append((str(subscription.feed_url), etag, last_modified))
        if "failed" in str(subscription.feed_url):
            raise FeedUnavailableError("private origin details")
        if self.not_modified:
            return FeedResponse(status_code=304, headers={"ETag": '"v1"'})
        return FeedResponse(
            status_code=200,
            headers={
                "ETag": '"v1"',
                "Last-Modified": "Mon, 31 Aug 2026 12:00:00 GMT",
            },
            body=RSS,
            url=str(subscription.feed_url),
        )


@pytest.mark.asyncio
async def test_controller_feed_refresh_is_partial_durable_offline_and_restart_conditional(
    tmp_path: Path,
) -> None:
    settings = HubSettings(data_dir=tmp_path / "hub")
    now = datetime(2026, 8, 31, 13, tzinfo=UTC)
    container = build_hub_container(settings)
    transport = _FeedTransport()
    container.feed_service.fetcher = transport
    try:
        healthy = container.controller.create_feed_subscription(
            title="Release feed",
            feed_url="https://news.example.test/feed.xml",
            include_terms=("personal hub",),
            digest_only=True,
        )
        failed = container.controller.create_feed_subscription(
            title="Failed feed",
            feed_url="https://failed.example.test/feed.xml",
        )
        results = await container.controller.refresh_feeds(force=True, now=now)

        assert results[healthy.subscription_id].state == "ready"
        assert results[failed.subscription_id].state == "disconnected"
        assert container.feed_entry_repository.count(subscription_id=healthy.subscription_id) == 1
        records = container.controller.list_feed_entries(state="unread")
        assert [record.entry.title for record in records] == ["Personal Hub release"]
        assert [item.title for item in container.controller.news_today_items()] == [
            "Personal Hub release"
        ]

        record = container.controller.set_feed_entry_read(records[0].entry.entry_id, True, at=now)
        assert record is not None and record.state == "read"
        assert container.controller.news_today_items() == ()
        container.controller.set_feed_entry_read(records[0].entry.entry_id, False, at=now)

        first_digest = container.controller.reconcile_news_digest(now=now)
        second_digest = container.controller.reconcile_news_digest(now=now)
        assert first_digest is not None
        assert first_digest.safe_summary == "1 unread feed item(s) from 1 source(s)."
        assert second_digest is None

        diagnostics = container.controller.diagnostics_json(generated_at=now)
        assert "Personal Hub release" not in diagnostics
        assert "Bounded release notes" not in diagnostics

        before_offline = len(transport.calls)
        container.controller.set_offline_mode(True)
        offline = await container.controller.refresh_feeds(
            (healthy.subscription_id,), force=True, now=now
        )
        assert offline[healthy.subscription_id].state == "offline"
        assert len(transport.calls) == before_offline
    finally:
        container.close()

    restarted = build_hub_container(settings)
    conditional = _FeedTransport(not_modified=True)
    restarted.feed_service.fetcher = conditional
    try:
        restarted.controller.set_offline_mode(False)
        healthy = restarted.controller.list_feed_subscriptions()[1]
        if "failed" in str(healthy.feed_url):
            healthy = restarted.controller.list_feed_subscriptions()[0]
        result = await restarted.controller.refresh_feeds(
            (healthy.subscription_id,), force=True, now=now
        )
        assert result[healthy.subscription_id].not_modified
        assert result[healthy.subscription_id].state == "ready"
        assert conditional.calls[0][1:] == (
            '"v1"',
            "Mon, 31 Aug 2026 12:00:00 GMT",
        )
        assert restarted.feed_entry_repository.count(subscription_id=healthy.subscription_id) == 1
        assert restarted.controller.reconcile_news_digest(now=now) is None
        assert (
            len([item for item in restarted.controller.list_inbox() if item.kind == "digest"]) == 1
        )
    finally:
        restarted.close()


def test_controller_feed_crud_validates_http_and_clears_old_origin_entries(tmp_path: Path) -> None:
    container = build_hub_container(HubSettings(data_dir=tmp_path / "hub"))
    try:
        with pytest.raises(ValueError, match="HTTP feed subscriptions require"):
            container.controller.create_feed_subscription(
                title="Unsafe HTTP",
                feed_url="http://news.example.test/feed.xml",
            )
        subscription = container.controller.create_feed_subscription(
            title="Reviewed HTTP",
            feed_url="http://news.example.test/feed.xml",
            allow_http=True,
        )
        assert subscription.reviewed_origin == "http://news.example.test"
        disabled = container.controller.set_feed_subscription_enabled(
            subscription.subscription_id, False
        )
        assert disabled is not None and not disabled.enabled
        updated = container.controller.update_feed_subscription(
            subscription.subscription_id,
            title="Reviewed feed",
            feed_url="https://news.example.test/feed.xml",
            allow_http=False,
        )
        assert updated is not None
        assert updated.title == "Reviewed feed"
        assert updated.reviewed_origin is None
        assert container.controller.delete_feed_subscription(subscription.subscription_id)
        assert container.controller.list_feed_subscriptions() == ()
    finally:
        container.close()
