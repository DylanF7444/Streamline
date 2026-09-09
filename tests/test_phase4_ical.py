"""Direct iCalendar feed parsing, refresh, and local persistence tests."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

import pytest

from personal_hub.bootstrap import build_hub_container
from personal_hub.calendar.contracts import CalendarQuery
from personal_hub.calendar.ical import (
    ICAL_FEED_SETTINGS_KEY,
    IcalCalendarFeed,
    IcalFeedFetcher,
    IcalFeedManager,
    parse_ical_calendar,
    parse_ical_calendar_items,
)
from personal_hub.config import HubSettings
from personal_hub.feeds.contracts import FeedResponse

ICAL = b"""BEGIN:VCALENDAR\r
VERSION:2.0\r
BEGIN:VEVENT\r
UID:one@example.test\r
DTSTART;TZID=America/Chicago:20260905T090000\r
DTEND;TZID=America/Chicago:20260905T100000\r
SUMMARY:Planning meeting with a folded \r
 subject\r
LOCATION:Room 42\r
URL:https://events.example.test/one\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:all-day@example.test\r
DTSTART;VALUE=DATE:20260906\r
DTEND;VALUE=DATE:20260907\r
SUMMARY:Company holiday\r
END:VEVENT\r
END:VCALENDAR\r
"""


def _feed(feed_id: UUID | None = None) -> IcalCalendarFeed:
    return IcalCalendarFeed(
        feed_id=feed_id or UUID("00000000-0000-0000-0000-000000004201"),
        title="Shared calendar",
        feed_url="https://events.example.test/calendar.ics",
    )


def _query() -> CalendarQuery:
    return CalendarQuery(
        starts_at=datetime(2026, 9, 5, tzinfo=UTC),
        ends_at=datetime(2026, 9, 8, tzinfo=UTC),
        timezone="America/Chicago",
    )


def test_ical_parser_handles_folded_timed_and_all_day_events() -> None:
    events = parse_ical_calendar(ICAL, feed=_feed(), query=_query())

    assert [event.title for event in events] == [
        "Planning meeting with a folded subject",
        "Company holiday",
    ]
    assert events[0].starts_at == datetime(2026, 9, 5, 14, tzinfo=UTC)
    assert events[0].ends_at == datetime(2026, 9, 5, 15, tzinfo=UTC)
    assert events[0].location == "Room 42"
    assert events[0].deep_link == "https://events.example.test/one"
    assert events[1].all_day is True
    assert events[1].starts_at == datetime(2026, 9, 6, 5, tzinfo=UTC)


def test_ical_parser_bounds_recurrence_and_rejects_unsafe_event_links() -> None:
    body = b"""BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:daily@example.test\nDTSTART:20260905T090000Z\nDURATION:PT30M\nRRULE:FREQ=DAILY;COUNT=4\nSUMMARY:Daily\nURL:javascript:alert(1)\nEND:VEVENT\nEND:VCALENDAR\n"""
    events = parse_ical_calendar(body, feed=_feed(), query=_query())

    assert len(events) == 3
    assert [event.starts_at.day for event in events] == [5, 6, 7]
    assert all(event.deep_link == "https://events.example.test/calendar.ics" for event in events)


def test_ical_parser_keeps_zero_length_canvas_events() -> None:
    body = b"""BEGIN:VCALENDAR
BEGIN:VEVENT
UID:canvas-point-in-time@example.test
DTSTART;TZID=America/Chicago:20260905T090000
DTEND;TZID=America/Chicago:20260905T090000
SUMMARY:Canvas schedule item
END:VEVENT
END:VCALENDAR
"""

    events = parse_ical_calendar(body, feed=_feed(), query=_query())
    items = parse_ical_calendar_items(body, fallback_timezone="America/Chicago")

    assert len(events) == 1
    assert events[0].starts_at == datetime(2026, 9, 5, 14, tzinfo=UTC)
    assert events[0].ends_at == datetime(2026, 9, 5, 15, tzinfo=UTC)
    assert [item.title for item in items] == ["Canvas schedule item"]
    assert items[0].due_at == datetime(2026, 9, 5, 14, tzinfo=UTC)


def test_ical_task_items_can_expand_bounded_canvas_recurrence() -> None:
    body = b"""BEGIN:VCALENDAR
BEGIN:VEVENT
UID:canvas-recurring@example.test
DTSTART:20260901T120000Z
DTEND:20260901T120000Z
RRULE:FREQ=DAILY;COUNT=3
SUMMARY:Recurring Canvas item
END:VEVENT
END:VCALENDAR
"""

    items = parse_ical_calendar_items(
        body,
        expand_recurrence=True,
        reference_at=datetime(2026, 9, 2, tzinfo=UTC),
    )

    assert [item.due_at for item in items] == [
        datetime(2026, 9, 1, 12, tzinfo=UTC),
        datetime(2026, 9, 2, 12, tzinfo=UTC),
        datetime(2026, 9, 3, 12, tzinfo=UTC),
    ]
    assert len({item.item_id for item in items}) == 3


def test_ical_task_items_include_every_concrete_event_without_a_view_window() -> None:
    body = b"""BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:old@example.test\nDTSTART;VALUE=DATE:20200102\nSUMMARY:Old assignment\nDESCRIPTION:<b>Read chapter one</b>\nEND:VEVENT\nBEGIN:VEVENT\nUID:future@example.test\nDTSTART:20350102T180000Z\nSUMMARY:Future assignment\nEND:VEVENT\nEND:VCALENDAR\n"""

    items = parse_ical_calendar_items(body, fallback_timezone="America/Chicago")

    assert [item.title for item in items] == ["Old assignment", "Future assignment"]
    assert items[0].due_at == datetime(2020, 1, 2, 6, tzinfo=UTC)
    assert items[0].due_is_all_day is True
    assert items[0].description == "<b>Read chapter one</b>"


class _IcalTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def request(
        self,
        _method: str,
        _url: str,
        *,
        headers: dict[str, str],
        timeout: float,
        max_bytes: int,
    ) -> FeedResponse:
        del timeout, max_bytes
        self.calls.append(dict(headers))
        if len(self.calls) > 1:
            return FeedResponse(
                status_code=304, headers={"ETag": '"calendar-v1"'}, body=b"", url=_url
            )
        return FeedResponse(
            status_code=200,
            headers={"ETag": '"calendar-v1"', "Last-Modified": "Sat, 05 Sep 2026 12:00:00 GMT"},
            body=ICAL,
            url=_url,
        )


@pytest.mark.asyncio
async def test_ical_source_uses_conditional_refresh_and_manager_persists_definition(
    tmp_path: Path,
) -> None:
    settings = HubSettings(data_dir=tmp_path / "hub")
    container = build_hub_container(settings)
    transport = _IcalTransport()
    container.ical_feed_manager.fetcher = IcalFeedFetcher(transport=transport)
    feed_id = UUID("00000000-0000-0000-0000-000000004202")
    try:
        feed = container.controller.create_ical_feed(
            title="Shared calendar",
            feed_url="https://events.example.test/calendar.ics",
            feed_id=feed_id,
        )
        results = await container.controller.refresh_calendars(_query())
        source_id = IcalFeedManager.source_id_for(feed.feed_id)
        assert results[source_id].state == "ready"
        assert results[source_id].snapshot is not None
        assert results[source_id].snapshot.events[0].source.provider_id == "ical"
        await container.controller.refresh_calendars(_query())
        assert transport.calls[1]["If-None-Match"] == '"calendar-v1"'
        selected = container.controller.calendar_today_items(
            day=date(2026, 9, 5),
            timezone="America/Chicago",
            selected_calendar_keys=(f"{source_id}/*",),
        )
        assert [item.title for item in selected] == ["Planning meeting with a folded subject"]
        persisted = container.settings_repository.get(ICAL_FEED_SETTINGS_KEY, [])
        assert isinstance(persisted, list)
        assert persisted[0]["feed"]["feed_id"] == str(feed_id)
    finally:
        container.close()

    reopened = build_hub_container(settings)
    try:
        assert [item.feed_id for item in reopened.controller.list_ical_feeds()] == [feed_id]
        assert IcalFeedManager.source_id_for(feed_id) in reopened.calendar_service.source_ids
        restored_source = reopened.calendar_service.source(IcalFeedManager.source_id_for(feed_id))
        assert restored_source.sync_state.etag == '"calendar-v1"'
        assert restored_source.sync_state.last_success_at is not None
    finally:
        reopened.close()
