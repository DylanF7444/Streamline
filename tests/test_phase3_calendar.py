"""Offline contract and adapter coverage for the Personal Hub calendar core."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from personal_hub.calendar import (
    GOOGLE_CALENDAR_READONLY_SCOPE,
    CalendarDisconnectedError,
    CalendarEvent,
    CalendarInvalidResponseError,
    CalendarPermissionRequiredError,
    CalendarQuery,
    CalendarService,
    CalendarSnapshot,
    CalendarStatus,
    GoogleCalendarAdapter,
)
from personal_hub.contracts import SourceRef


def _query(*calendar_ids: str, limit: int = 20) -> CalendarQuery:
    start = datetime(2026, 8, 31, 9, tzinfo=UTC)
    return CalendarQuery(
        starts_at=start,
        ends_at=start + timedelta(days=7),
        calendar_ids=calendar_ids,
        timezone="America/Chicago",
        limit=limit,
    )


class FakeTokenProvider:
    def __init__(self, token: str = "hub-token") -> None:
        self.token = token
        self.calls: list[tuple[str, ...]] = []

    async def get_token(self, scopes: tuple[str, ...]) -> str:
        self.calls.append(scopes)
        return self.token


class FakeTransport:
    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.calls: list[tuple[str, str, Mapping[str, Any]]] = []

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, Any],
    ) -> object:
        self.calls.append((method, url, params))
        assert method == "GET"
        assert headers["Authorization"] == "Bearer hub-token"
        value = self.handler(url, params)
        if isinstance(value, BaseException):
            raise value
        return value


class FakeSource:
    provider_id = "fake-calendar"
    display_name = "Fake Calendar"

    def __init__(self, source_id: str, callback: Any) -> None:
        self.source_id = source_id
        self.callback = callback
        self.status_state = "ready"
        self.active = 0
        self.max_active = 0

    async def status(self) -> CalendarStatus:
        return CalendarStatus(
            source_id=self.source_id,
            provider_id=self.provider_id,
            state=self.status_state,
            checked_at=datetime.now(UTC),
            safe_message=f"status-{self.status_state}",
        )

    async def list_events(self, query: CalendarQuery) -> CalendarSnapshot:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            value = self.callback(query)
            if asyncio.iscoroutine(value):
                value = await value
            return value
        finally:
            self.active -= 1


def _event(
    event_id: str,
    *,
    calendar_id: str = "primary",
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
    all_day: bool = False,
) -> CalendarEvent:
    starts_at = starts_at or datetime(2026, 8, 31, 14, tzinfo=UTC)
    ends_at = ends_at or starts_at + timedelta(hours=1)
    return CalendarEvent(
        event_id=event_id,
        calendar_id=calendar_id,
        title=event_id.title(),
        starts_at=starts_at,
        ends_at=ends_at,
        all_day=all_day,
        timezone="UTC",
        source=SourceRef(
            provider_id="fake-calendar",
            display_name="Fake Calendar",
            resource_id=event_id,
        ),
    )


def test_calendar_contracts_are_strict_bounded_and_deterministic() -> None:
    with pytest.raises(ValidationError):
        CalendarQuery(
            starts_at=datetime(2026, 1, 1),
            ends_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
    with pytest.raises(ValidationError):
        CalendarQuery(
            starts_at=datetime(2026, 1, 1, tzinfo=UTC),
            ends_at=datetime(2027, 1, 3, tzinfo=UTC),
        )
    with pytest.raises(ValidationError):
        CalendarEvent(
            event_id="one",
            calendar_id="primary",
            title="One",
            starts_at=datetime(2026, 1, 1, tzinfo=UTC),
            ends_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
            timezone="Not/A-Timezone",
            source=SourceRef(provider_id="fake-calendar", display_name="Fake"),
            deep_link="https://user:password@calendar.google.com/calendar/event",
        )
    earlier = _event("earlier", starts_at=datetime(2026, 8, 31, 10, tzinfo=UTC))
    later = _event("later", starts_at=datetime(2026, 8, 31, 12, tzinfo=UTC))
    snapshot = CalendarSnapshot(
        source_id="fake",
        provider_id="fake-calendar",
        collected_at=datetime.now(UTC),
        events=(later, earlier),
        selected_calendar_ids=("primary",),
    )
    ordered = tuple(sorted(snapshot.events, key=lambda item: (item.starts_at, item.event_id)))
    assert [item.event_id for item in ordered] == ["earlier", "later"]
    assert snapshot.calendar_ids == ("primary",)


@pytest.mark.asyncio
async def test_google_maps_timed_all_day_selected_and_paginated_events() -> None:
    token = FakeTokenProvider()

    def handler(url: str, params: Mapping[str, Any]) -> object:
        assert "/calendars/work%40example.com/events" in url
        if "pageToken" not in params:
            return {
                "items": [
                    {
                        "id": "_timed-1",
                        "summary": "Timed planning",
                        "start": {"dateTime": "2026-08-31T10:00:00-05:00"},
                        "end": {"dateTime": "2026-08-31T11:00:00-05:00"},
                        "htmlLink": "https://calendar.google.com/calendar/event?eid=timed-1",
                    }
                ],
                "nextPageToken": "page-2",
            }
        assert params["pageToken"] == "page-2"
        return {
            "items": [
                {
                    "id": "day-1",
                    "summary": "All day",
                    "start": {"date": "2026-09-01"},
                    "end": {"date": "2026-09-02"},
                    "location": "Office",
                }
            ]
        }

    transport = FakeTransport(handler)
    adapter = GoogleCalendarAdapter(token, transport)
    result = await adapter.list_events(_query("work@example.com"))
    assert [event.event_id for event in result.events] == ["_timed-1", "day-1"]
    assert result.events[0].source.provider_id == "google-calendar"
    assert result.events[0].deep_link is not None
    assert result.events[1].all_day is True
    assert result.events[1].timezone == "America/Chicago"
    assert token.calls == [(GOOGLE_CALENDAR_READONLY_SCOPE,)]
    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_google_skips_cancelled_and_deleted_event_resources() -> None:
    token = FakeTokenProvider()

    def handler(_url: str, _params: Mapping[str, Any]) -> object:
        return {
            "items": [
                {
                    "id": "live-event",
                    "status": "confirmed",
                    "summary": "Live event",
                    "start": {"dateTime": "2026-08-31T10:00:00-05:00"},
                    "end": {"dateTime": "2026-08-31T11:00:00-05:00"},
                },
                {
                    "id": "cancelled-event",
                    "status": "cancelled",
                    "summary": "Cancelled event",
                    "start": {"dateTime": "2026-08-31T12:00:00-05:00"},
                    "end": {"dateTime": "2026-08-31T13:00:00-05:00"},
                },
                {
                    "id": "deleted-event",
                    "status": "deleted",
                    "summary": "Deleted event",
                    "start": {"dateTime": "2026-08-31T14:00:00-05:00"},
                    "end": {"dateTime": "2026-08-31T15:00:00-05:00"},
                },
            ]
        }

    adapter = GoogleCalendarAdapter(token, FakeTransport(handler))
    result = await adapter.list_events(_query("primary"))

    assert [event.event_id for event in result.events] == ["live-event"]


@pytest.mark.asyncio
async def test_google_authoritative_empty_refresh_replaces_last_good_snapshot() -> None:
    token = FakeTokenProvider()
    payload: dict[str, object] = {
        "items": [
            {
                "id": "removed-event",
                "status": "confirmed",
                "summary": "Removed after first refresh",
                "start": {"dateTime": "2026-08-31T10:00:00-05:00"},
                "end": {"dateTime": "2026-08-31T11:00:00-05:00"},
            }
        ]
    }
    adapter = GoogleCalendarAdapter(
        token,
        FakeTransport(lambda _url, _params: payload),
    )
    service = CalendarService((adapter,))

    first = await service.refresh(adapter.source_id, _query("primary"))
    assert first.state == "ready"
    assert [event.event_id for event in first.events] == ["removed-event"]

    payload["items"] = []
    second = await service.refresh(adapter.source_id, _query("primary"))

    assert second.state == "empty"
    assert second.events == ()
    last_good = service.last_good_snapshot(adapter.source_id)
    assert last_good is not None
    assert last_good.events == ()


@pytest.mark.asyncio
async def test_google_discovers_unselected_calendars_without_fetching_their_events() -> None:
    token = FakeTokenProvider()

    def handler(url: str, params: Mapping[str, Any]) -> object:
        if url.endswith("/calendarList"):
            assert params["showDeleted"] is False
            return {
                "items": [
                    {"id": "work@example.com", "summary": "Work"},
                    {"id": "school@example.com", "summary": "School"},
                ]
            }
        assert "/calendars/work%40example.com/events" in url
        return {
            "items": [
                {
                    "id": "_canvas_event_1",
                    "summary": "Canvas event",
                    "start": {"date": "2026-09-01"},
                    "end": {"date": "2026-09-02"},
                }
            ]
        }

    transport = FakeTransport(handler)
    adapter = GoogleCalendarAdapter(token, transport, discover_calendars=True)
    result = await adapter.list_events(_query("work@example.com"))

    assert result.selected_calendar_ids == ("work@example.com",)
    assert result.available_calendar_ids == ("school@example.com", "work@example.com")
    assert result.calendar_display_names == {
        "school@example.com": "School",
        "work@example.com": "Work",
    }
    assert result.events[0].event_id == "_canvas_event_1"
    assert [call[1] for call in transport.calls] == [
        "https://www.googleapis.com/calendar/v3/users/me/calendarList",
        "https://www.googleapis.com/calendar/v3/calendars/work%40example.com/events",
    ]


@pytest.mark.asyncio
async def test_google_bounds_calendar_list_and_selection() -> None:
    token = FakeTokenProvider()

    def handler(url: str, params: Mapping[str, Any]) -> object:
        if url.endswith("/calendarList"):
            if "pageToken" not in params:
                return {
                    "items": [{"id": "first@example.com", "summary": "First"}],
                    "nextPageToken": "second",
                }
            return {
                "items": [
                    {"id": "second@example.com", "summary": "Second"},
                    {"id": "third@example.com", "summary": "Third"},
                ]
            }
        assert "/calendars/" in url
        return {"items": []}

    transport = FakeTransport(handler)
    adapter = GoogleCalendarAdapter(token, transport, max_calendars=2)
    result = await adapter.list_events(_query())
    assert result.selected_calendar_ids == ("first@example.com", "second@example.com")
    assert len(transport.calls) == 4
    assert [call[1].split("/calendars/")[-1].split("/")[0] for call in transport.calls[2:]] == [
        "first%40example.com",
        "second%40example.com",
    ]


@pytest.mark.asyncio
async def test_offline_gate_blocks_token_and_transport_calls() -> None:
    token = FakeTokenProvider()
    transport = FakeTransport(lambda _url, _params: {"items": []})
    adapter = GoogleCalendarAdapter(token, transport)
    service = CalendarService((adapter,), offline=True)
    result = await service.refresh(adapter.source_id, _query("primary"))
    assert result.state == "offline"
    assert token.calls == []
    assert transport.calls == []
    status = await service.status(adapter.source_id)
    assert status.state == "offline"
    assert token.calls == []
    assert transport.calls == []


@pytest.mark.asyncio
async def test_last_good_is_stale_but_permission_and_disconnect_remain_distinct() -> None:
    event = _event("saved")
    source = FakeSource(
        "one",
        lambda _query: CalendarSnapshot(
            source_id="one",
            provider_id="fake-calendar",
            collected_at=datetime.now(UTC),
            events=(event,),
        ),
    )
    service = CalendarService((source,))
    first = await service.refresh("one", _query("primary"))
    assert first.state == "ready"

    source.callback = lambda _query: (_ for _ in ()).throw(RuntimeError("private provider body"))
    failed = await service.refresh("one", _query("primary"))
    assert failed.state == "stale"
    assert failed.events == (event,)
    assert "private" not in failed.safe_message

    source.callback = lambda _query: (_ for _ in ()).throw(CalendarPermissionRequiredError())
    permission = await service.refresh("one", _query("primary"))
    assert permission.state == "permission_required"
    source.callback = lambda _query: (_ for _ in ()).throw(CalendarDisconnectedError())
    disconnected = await service.refresh("one", _query("primary"))
    assert disconnected.state == "disconnected"


@pytest.mark.asyncio
async def test_refresh_all_is_partial_and_never_exceeds_two_active_sources() -> None:
    async def successful(query: CalendarQuery) -> CalendarSnapshot:
        await asyncio.sleep(0.01)
        return CalendarSnapshot(
            source_id="placeholder",
            provider_id="fake-calendar",
            collected_at=datetime.now(UTC),
            events=(),
        )

    sources: list[FakeSource] = []
    for index in range(3):
        source = FakeSource(f"source-{index}", successful)
        sources.append(source)

    async def callback(query: CalendarQuery, source: FakeSource) -> CalendarSnapshot:
        await asyncio.sleep(0.01)
        return CalendarSnapshot(
            source_id=source.source_id,
            provider_id=source.provider_id,
            collected_at=datetime.now(UTC),
            events=(),
        )

    for source in sources:
        source.callback = lambda query, current=source: callback(query, current)
    sources[2].callback = lambda _query: (_ for _ in ()).throw(CalendarPermissionRequiredError())
    service = CalendarService(sources, max_concurrency=2)
    values = await service.refresh_all(_query("primary"))
    assert set(values) == {"source-0", "source-1", "source-2"}
    assert values["source-2"].state == "permission_required"
    assert max(source.max_active for source in sources) <= 1


@pytest.mark.asyncio
async def test_timeout_and_cancellation_preserve_async_boundaries() -> None:
    started = asyncio.Event()

    async def slow(_query: CalendarQuery) -> CalendarSnapshot:
        started.set()
        await asyncio.sleep(1)
        raise AssertionError("unreachable")

    source = FakeSource("slow", slow)
    service = CalendarService((source,), timeout_seconds=0.01)
    timed_out = await service.refresh("slow", _query("primary"))
    assert timed_out.state == "error"

    service = CalendarService((source,), timeout_seconds=30)
    task = asyncio.create_task(service.refresh("slow", _query("primary")))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_google_maps_permission_and_rejects_oversized_or_invalid_payloads() -> None:
    token = FakeTokenProvider()

    class Response:
        status_code = 403
        headers = {"content-length": "20"}
        content = b'{"error":"private"}'

        def json(self) -> object:
            return {"error": "private"}

    permission_adapter = GoogleCalendarAdapter(token, FakeTransport(lambda *_: Response()))
    with pytest.raises(CalendarPermissionRequiredError):
        await permission_adapter.list_events(_query("primary"))
    assert "private" not in str(permission_adapter._last_status_message)

    invalid_adapter = GoogleCalendarAdapter(
        token,
        FakeTransport(lambda *_: {"items": {"not": "a-list"}}),
    )
    with pytest.raises(CalendarInvalidResponseError):
        await invalid_adapter.list_events(_query("primary"))

    oversized = "x" * 2_000
    bounded_adapter = GoogleCalendarAdapter(
        token,
        FakeTransport(lambda *_: {"items": [], "oversized": oversized}),
        max_response_bytes=1_024,
    )
    with pytest.raises(CalendarInvalidResponseError):
        await bounded_adapter.list_events(_query("primary"))


def test_google_adapter_is_explicitly_read_only_and_does_not_discover_assistant_secrets() -> None:
    token = FakeTokenProvider()
    transport = FakeTransport(lambda *_: {"items": []})
    with pytest.raises(ValueError):
        GoogleCalendarAdapter(
            token, transport, scopes=("https://www.googleapis.com/auth/calendar",)
        )
    with pytest.raises(ValueError):
        GoogleCalendarAdapter(token, transport, scopes=(GOOGLE_CALENDAR_READONLY_SCOPE, "openid"))
    adapter = GoogleCalendarAdapter(token, transport)
    assert not any(
        hasattr(adapter, name)
        for name in ("create_event", "update_event", "delete_event", "insert_event")
    )
    assert "assistant.storage" not in type(adapter).__module__
