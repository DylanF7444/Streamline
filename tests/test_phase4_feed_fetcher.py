"""Phase 4 RSS/Atom parser, transport-policy, and refresh tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from personal_hub.feeds import (
    FeedFetcher,
    FeedOfflineError,
    FeedParseError,
    FeedParser,
    FeedPolicyError,
    FeedRedirectError,
    FeedRefreshService,
    FeedResponse,
    FeedResponseTooLargeError,
    FeedSSRFError,
    FeedSubscription,
    filter_entries,
    sanitize_html,
    stable_feed_entry_id,
)

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Hub News</title>
    <link>https://news.example.test/</link>
    <item>
      <guid isPermaLink="false">guid-one</guid>
      <title><![CDATA[First <b>release</b>]]></title>
      <link>/article/one#comments</link>
      <description><![CDATA[<p>Personal Hub <img src="https://tracker.invalid/pixel" /> update</p><script>bad()</script>]]></description>
      <pubDate>Mon, 31 Aug 2026 12:00:00 GMT</pubDate>
      <author>author@example.test</author>
    </item>
    <item>
      <guid>guid-one</guid>
      <title>Duplicate</title>
      <link>https://news.example.test/article/duplicate</link>
    </item>
    <item>
      <guid>guid-two</guid>
      <title>Sponsored offer</title>
      <link>https://news.example.test/article/two</link>
    </item>
  </channel>
</rss>
"""

ATOM = b"""<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom updates</title>
  <link rel="alternate" href="https://atom.example.test/" />
  <entry>
    <id>tag:atom.example.test,2026:one</id>
    <title>Atom article</title>
    <link rel="alternate" href="/one" />
    <summary type="html"><![CDATA[<b>Readable</b> summary]]></summary>
    <updated>2026-08-31T13:00:00-05:00</updated>
    <author><name>Atom Author</name></author>
  </entry>
</feed>"""


def _subscription(
    url: str = "https://news.example.test/feed.xml",
    *,
    subscription_id: UUID | None = None,
    include_terms: tuple[str, ...] = (),
    exclude_terms: tuple[str, ...] = (),
) -> FeedSubscription:
    return FeedSubscription(
        subscription_id=subscription_id or UUID("00000000-0000-0000-0000-000000001001"),
        title="News",
        feed_url=url,
        include_terms=include_terms,
        exclude_terms=exclude_terms,
    )


def test_parser_normalizes_rss_and_deduplicates_stable_subscription_scoped_ids() -> None:
    subscription = _subscription(exclude_terms=("sponsored",))
    snapshot = FeedParser(max_items=10).parse(
        RSS,
        subscription,
        collected_at=datetime(2026, 8, 31, 12, tzinfo=UTC),
    )
    assert snapshot.title == "Hub News"
    assert str(snapshot.site_url) == "https://news.example.test/"
    assert len(snapshot.entries) == 2
    assert snapshot.entries[0].title == "First release"
    assert snapshot.entries[0].summary_text == "Personal Hub update"
    assert str(snapshot.entries[0].url) == "https://news.example.test/article/one"
    assert snapshot.entries[0].published_at == datetime(2026, 8, 31, 12, tzinfo=UTC)
    assert snapshot.entries[0].entry_id == stable_feed_entry_id(
        subscription.subscription_id,
        guid="guid-one",
        url="https://news.example.test/article/one",
    )
    other = _subscription(
        subscription_id=UUID("00000000-0000-0000-0000-000000001002"),
    )
    other_snapshot = FeedParser().parse(RSS, other)
    assert snapshot.entries[0].entry_id != other_snapshot.entries[0].entry_id


def test_atom_namespaces_dates_and_relative_links_are_bounded() -> None:
    subscription = _subscription("https://atom.example.test/feed")
    snapshot = FeedParser().parse(ATOM, subscription)
    assert len(snapshot.entries) == 1
    entry = snapshot.entries[0]
    assert str(entry.url) == "https://atom.example.test/one"
    assert entry.author == "Atom Author"
    assert entry.published_at == datetime(2026, 8, 31, 18, tzinfo=UTC)
    assert entry.summary_text == "Readable summary"


def test_hostile_xml_malformed_xml_and_bounds_fail_closed() -> None:
    subscription = _subscription()
    for hostile in (
        b"<!DOCTYPE rss [<!ENTITY xxe SYSTEM 'file:///secret'>]><rss />",
        b"<rss><channel><item><title>broken",
        b"<!DOCTYPE rss PUBLIC '-//RSS//DTD RSS 2.0//EN' 'https://example.test/rss.dtd'><rss />",
    ):
        with pytest.raises(FeedParseError):
            FeedParser().parse(hostile, subscription)
    with pytest.raises(FeedParseError):
        FeedParser(max_response_bytes=1_024).parse(b"x" * 1_025, subscription)
    deep = b"<rss>" + b"<x>" * 70 + b"ok" + b"</x>" * 70 + b"</rss>"
    with pytest.raises(FeedParseError):
        FeedParser().parse(deep, subscription)


def test_html_sanitization_discards_active_content_and_remote_attributes() -> None:
    value = sanitize_html(
        '<p>Hello <a href="javascript:alert(1)">world</a></p>'
        '<img src="https://tracker.invalid/x" alt="tracking">'
        "<script>do_not_show()</script><style>secret</style>"
    )
    assert value == "Hello world"


def test_filter_terms_are_case_insensitive_include_any_exclude_any() -> None:
    subscription = _subscription(include_terms=("PERSONAL HUB",), exclude_terms=("sponsored",))
    snapshot = FeedParser().parse(RSS, subscription)
    entries = filter_entries(snapshot.entries, subscription)
    assert [entry.title for entry in entries] == ["First release"]


class _Transport:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        timeout: float,
        max_bytes: int,
    ) -> object:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "timeout": timeout,
                "max_bytes": max_bytes,
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


@pytest.mark.asyncio
async def test_fetcher_conditional_headers_redirects_and_no_auto_redirect() -> None:
    transport = _Transport(
        [
            FeedResponse(302, {"Location": "/next"}),
            FeedResponse(
                200,
                {"ETag": '"v1"', "Last-Modified": "Mon, 31 Aug 2026 12:00:00 GMT"},
                RSS,
            ),
        ]
    )
    fetcher = FeedFetcher(transport, resolve_host=False)
    subscription = _subscription()
    response = await fetcher.fetch(subscription, etag='"old"', last_modified="yesterday")
    assert response.status_code == 200
    assert len(transport.calls) == 2
    assert transport.calls[0]["method"] == "GET"
    assert transport.calls[0]["headers"]["If-None-Match"] == '"old"'
    assert transport.calls[1]["url"] == "https://news.example.test/next"


@pytest.mark.asyncio
async def test_fetcher_rejects_private_literals_redirect_downgrade_and_oversize() -> None:
    private = _subscription("https://127.0.0.1/feed")
    with pytest.raises(FeedSSRFError):
        await FeedFetcher(_Transport([]), resolve_host=False).fetch(private)

    transport = _Transport([FeedResponse(302, {"Location": "http://news.example.test/feed"})])
    with pytest.raises(FeedRedirectError):
        await FeedFetcher(transport, resolve_host=False).fetch(_subscription())

    transport = _Transport([FeedResponse(200, {}, b"x" * 1_025)])
    with pytest.raises(FeedResponseTooLargeError):
        await FeedFetcher(transport, max_response_bytes=1_024, resolve_host=False).fetch(
            _subscription()
        )


@pytest.mark.asyncio
async def test_fetcher_http_requires_process_and_subscription_opt_in() -> None:
    with pytest.raises(ValidationError):
        _subscription("http://news.example.test/feed")
    subscription = FeedSubscription(
        title="Reviewed HTTP",
        feed_url="http://news.example.test/feed",
        allow_http=True,
        reviewed_origin="http://news.example.test",
    )
    with pytest.raises(FeedPolicyError):
        await FeedFetcher(_Transport([FeedResponse(200, {}, RSS)]), resolve_host=False).fetch(
            subscription
        )
    transport = _Transport([FeedResponse(200, {}, RSS)])
    response = await FeedFetcher(
        transport,
        allow_http=True,
        resolve_host=False,
    ).fetch(subscription)
    assert response.status_code == 200


class _Fetcher:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[UUID, str | None, str | None]] = []

    async def fetch(
        self,
        subscription: FeedSubscription,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> object:
        self.calls.append((subscription.subscription_id, etag, last_modified))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


@pytest.mark.asyncio
async def test_refresh_service_floor_force_conditional_and_restart_seed() -> None:
    subscription = _subscription()
    fetcher = _Fetcher(
        [
            FeedResponse(200, {"ETag": '"v1"'}, RSS),
            FeedResponse(304, {"ETag": '"v1"'}),
        ]
    )
    service = FeedRefreshService(fetcher, jitter_ratio=0.0)
    first = await service.refresh(subscription, now=datetime(2026, 8, 31, 12, tzinfo=UTC))
    assert first.state == "ready"
    not_due = await service.refresh(
        subscription,
        now=datetime(2026, 8, 31, 12, 0, 30, tzinfo=UTC),
    )
    assert not_due.state == "ready"
    assert len(fetcher.calls) == 1
    second = await service.refresh(
        subscription,
        force=True,
        now=datetime(2026, 8, 31, 12, 2, tzinfo=UTC),
    )
    assert second.not_modified is True
    assert fetcher.calls[1][1:] == ('"v1"', None)

    # Reconstruct the in-memory service from persisted sync and cached values;
    # a 304 remains usable after restart and sends validators immediately.
    restarted_fetcher = _Fetcher([FeedResponse(304, {})])
    restarted = FeedRefreshService(restarted_fetcher, jitter_ratio=0.0)
    state = service.sync_state(subscription.subscription_id)
    snapshot = service.last_good_snapshot(subscription.subscription_id)
    assert state is not None and snapshot is not None
    restarted.register(subscription)
    restarted.restore_state(state, snapshot)
    restored = await restarted.refresh(
        subscription,
        force=True,
        now=datetime(2026, 8, 31, 13, tzinfo=UTC),
    )
    assert restored.state == "ready"
    assert restarted_fetcher.calls[0][1:] == ('"v1"', None)


@pytest.mark.asyncio
async def test_refresh_service_backoff_is_per_origin_and_force_does_not_bypass_it() -> None:
    failing = _subscription("https://fail.example.test/feed")
    healthy = _subscription(
        "https://good.example.test/feed",
        subscription_id=UUID("00000000-0000-0000-0000-000000001003"),
    )
    fetcher = _Fetcher([FeedResponse(503, {}), FeedResponse(200, {}, RSS)])
    service = FeedRefreshService(fetcher, jitter_ratio=0.0)
    failed = await service.refresh(failing, now=datetime(2026, 8, 31, 12, tzinfo=UTC))
    assert failed.state == "disconnected"
    blocked = await service.refresh(
        failing,
        force=True,
        now=datetime(2026, 8, 31, 12, 0, 30, tzinfo=UTC),
    )
    assert blocked.state == "error"
    assert len(fetcher.calls) == 1
    good = await service.refresh(healthy, now=datetime(2026, 8, 31, 12, tzinfo=UTC))
    assert good.state == "ready"


@pytest.mark.asyncio
async def test_refresh_service_offline_gate_precedes_fetcher() -> None:
    fetcher = _Fetcher([FeedResponse(200, {}, RSS)])
    service = FeedRefreshService(fetcher, offline=True)
    result = await service.refresh(_subscription())
    assert result.state == "offline"
    assert fetcher.calls == []


@pytest.mark.asyncio
async def test_refresh_all_keeps_failed_feed_independent() -> None:
    first = _subscription("https://one.example.test/feed")
    second = _subscription(
        "https://two.example.test/feed",
        subscription_id=UUID("00000000-0000-0000-0000-000000001004"),
    )
    fetcher = _Fetcher([FeedResponse(503, {}), FeedResponse(200, {}, RSS)])
    service = FeedRefreshService(fetcher, jitter_ratio=0.0)
    results = await service.refresh_all(
        [first, second],
        now=datetime(2026, 8, 31, 12, tzinfo=UTC),
    )
    assert results[first.subscription_id].state == "disconnected"
    assert results[second.subscription_id].state == "ready"


@pytest.mark.asyncio
async def test_fetcher_offline_does_not_validate_dns_or_contact_transport() -> None:
    fetcher = FeedFetcher(_Transport([]), offline=True)
    with pytest.raises(FeedOfflineError):
        await fetcher.fetch(_subscription("https://private.invalid/feed"))
