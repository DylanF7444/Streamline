from __future__ import annotations

import ast
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from personal_hub.calendar.contracts import CalendarQuery, CalendarUnavailableError
from personal_hub.calendar.outlook import (
    OUTLOOK_CALENDAR_READ_SCOPE,
    OutlookCalendarSource,
)

NOW = datetime(2026, 11, 1, 6, 0, tzinfo=UTC)


def _query(*, limit: int = 20, calendars: tuple[str, ...] = ("work",)) -> CalendarQuery:
    return CalendarQuery(
        starts_at=NOW,
        ends_at=NOW + timedelta(days=3),
        calendar_ids=calendars,
        timezone="America/Chicago",
        limit=limit,
    )


def _event(
    event_id: str,
    start: str,
    end: str,
    *,
    subject: str = "Event",
    timezone: str = "UTC",
    all_day: bool = False,
    link: str | None = "https://outlook.office.com/calendar/item?id=event",
) -> dict[str, Any]:
    start_value = {"dateTime": start, "timeZone": timezone}
    end_value = {"dateTime": end, "timeZone": timezone}
    return {
        "id": event_id,
        "subject": subject,
        "start": start_value,
        "end": end_value,
        "isAllDay": all_day,
        "webLink": link,
        "location": {"displayName": " Room\n 1 "},
    }


class FakeTokenProvider:
    def __init__(self, token: str | None = "access-token") -> None:
        self.token = token
        self.scopes: list[tuple[str, ...]] = []
        self.calls = 0
        self.started = asyncio.Event()
        self.delay = 0.0
        self.error: BaseException | None = None

    async def access_token(self, *, scopes: tuple[str, ...]) -> str | None:
        self.calls += 1
        self.scopes.append(scopes)
        self.started.set()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.token


class FakeTransport:
    def __init__(self, responses: list[Any] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []
        self.started = asyncio.Event()
        self.delay = 0.0
        self.error: BaseException | None = None

    async def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        access_token: str | None = None,
    ) -> Any:
        self.calls.append(
            {
                "method": method,
                "path": path,
                "query": dict(query or {}),
                "access_token": access_token,
            }
        )
        self.started.set()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.responses.pop(0) if self.responses else {"value": []}


def _source(
    transport: FakeTransport,
    token_provider: FakeTokenProvider,
    **kwargs: Any,
) -> OutlookCalendarSource:
    return OutlookCalendarSource(transport, token_provider, **kwargs)


@pytest.mark.asyncio
async def test_outlook_maps_selected_calendars_pages_times_and_safe_links():
    transport = FakeTransport(
        [
            {
                "value": [
                    _event(
                        "timed-late",
                        "2026-11-02T09:00:00Z",
                        "2026-11-02T10:00:00Z",
                        subject="Late",
                    ),
                    _event(
                        "all-day",
                        "2026-11-01T00:00:00.0000000",
                        "2026-11-02T00:00:00.0000000",
                        subject="  All-day\nplanning ",
                        timezone="Central Standard Time",
                        all_day=True,
                        link="https://outlook.office.com/calendar/item?id=all#private-fragment",
                    ),
                ],
                "@odata.nextLink": (
                    "https://graph.microsoft.com/v1.0/me/calendars/work/calendarView"
                    "?$skiptoken=page-two"
                ),
            },
            {
                "value": [
                    _event(
                        "timed-early",
                        "2026-11-01T07:00:00Z",
                        "2026-11-01T08:00:00Z",
                        subject="Early",
                        link="http://unsafe.example/event",
                    )
                ]
            },
            {
                "value": [
                    _event(
                        "home-event",
                        "2026-11-03T12:00:00Z",
                        "2026-11-03T13:00:00Z",
                        subject="Home",
                    )
                ]
            },
        ]
    )
    token_provider = FakeTokenProvider()
    source = _source(transport, token_provider, max_events_per_page=2)

    snapshot = await source.list_events(_query(calendars=("work", "home")))

    assert [event.event_id for event in snapshot.events] == [
        "all-day",
        "timed-early",
        "timed-late",
        "home-event",
    ]
    assert snapshot.selected_calendar_ids == ("work", "home")
    assert snapshot.events[0].all_day is True
    assert snapshot.events[0].timezone == "America/Chicago"
    assert snapshot.events[0].starts_at == datetime(2026, 11, 1, 5, tzinfo=UTC)
    assert snapshot.events[0].deep_link == "https://outlook.office.com/calendar/item?id=all"
    assert snapshot.events[1].location == "Room 1"
    assert snapshot.events[1].deep_link is None
    assert token_provider.scopes == [(OUTLOOK_CALENDAR_READ_SCOPE,)]
    assert all(call["method"] == "GET" for call in transport.calls)
    assert [call["path"] for call in transport.calls] == [
        "/me/calendars/work/calendarView",
        "/me/calendars/work/calendarView",
        "/me/calendars/home/calendarView",
    ]
    assert transport.calls[1]["query"]["$skiptoken"] == "page-two"
    assert all(call["access_token"] == "access-token" for call in transport.calls)
    assert all(call["query"]["startDateTime"] == "2026-11-01T06:00:00Z" for call in transport.calls)


@pytest.mark.asyncio
async def test_outlook_offline_gate_blocks_token_and_transport_before_connection():
    transport = FakeTransport()
    token_provider = FakeTokenProvider()
    source = _source(transport, token_provider, offline=True)

    status = await source.status(_query())
    result = await source.refresh(_query())

    assert status.state == "offline"
    assert result.state == "offline"
    assert token_provider.calls == 0
    assert transport.calls == []


@pytest.mark.asyncio
async def test_outlook_states_are_safe_and_last_good_failure_is_stale():
    disconnected = _source(FakeTransport(), FakeTokenProvider(token=None))
    disconnected_result = await disconnected.refresh(_query())
    assert disconnected_result.state == "disconnected"
    assert "access-token" not in disconnected_result.safe_message

    permission_token = FakeTokenProvider()
    permission_token.error = PermissionError("secret-account-token")
    permission = _source(FakeTransport(), permission_token)
    permission_result = await permission.refresh(_query())
    assert permission_result.state == "permission_required"
    assert "secret-account-token" not in permission_result.safe_message

    transport = FakeTransport(
        [
            {
                "value": [
                    _event(
                        "one",
                        "2026-11-01T08:00:00Z",
                        "2026-11-01T09:00:00Z",
                    )
                ]
            }
        ]
    )
    source = _source(transport, FakeTokenProvider())
    ready = await source.refresh(_query())
    assert ready.state == "ready"
    transport.error = TimeoutError("private response body")
    stale = await source.refresh(_query())
    assert stale.state == "stale"
    assert stale.snapshot == ready.snapshot
    assert "private response body" not in stale.safe_message


@pytest.mark.asyncio
async def test_outlook_bounds_pages_count_and_response_size():
    page = {
        "value": [
            _event("one", "2026-11-01T08:00:00Z", "2026-11-01T09:00:00Z"),
        ],
        "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/calendarView?$skiptoken=next",
    }
    second_page = {
        "value": [
            _event("two", "2026-11-01T09:00:00Z", "2026-11-01T10:00:00Z"),
        ],
        "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/calendarView?$skiptoken=third",
    }
    transport = FakeTransport([page, second_page, page])
    source = _source(transport, FakeTokenProvider(), max_pages=2)

    snapshot = await source.list_events(_query(limit=1))

    assert len(snapshot.events) == 1
    assert len(transport.calls) == 1

    transport = FakeTransport([page, second_page, page])
    source = _source(transport, FakeTokenProvider(), max_pages=2)
    snapshot = await source.list_events(_query(limit=20))
    assert len(snapshot.events) == 2
    assert len(transport.calls) == 2

    oversized = {"value": [{"subject": "x" * 2_000}]}
    transport = FakeTransport([json.dumps(oversized).encode()])
    source = _source(transport, FakeTokenProvider(), max_response_bytes=1_024)
    result = await source.refresh(_query())
    assert result.state == "error"
    assert result.snapshot is None


@pytest.mark.asyncio
async def test_outlook_timeout_and_cancellation_are_bounded_and_propagated():
    token_provider = FakeTokenProvider()
    token_provider.delay = 1.0
    source = _source(FakeTransport(), token_provider, timeout_seconds=0.01)
    with pytest.raises(CalendarUnavailableError):
        await source.list_events(_query())

    transport = FakeTransport([{"value": []}])
    transport.delay = 1.0
    source = _source(transport, FakeTokenProvider(), timeout_seconds=2.0)
    pending = asyncio.create_task(source.list_events(_query()))
    await transport.started.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending


def test_outlook_surface_is_read_only_and_contains_no_assistant_dependency():
    source_path = Path("src/personal_hub/calendar/outlook.py")
    source_text = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source_text)
    imported_modules = [
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    ] + [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    ]
    assert not any(
        module == "assistant" or module.startswith("assistant.") for module in imported_modules
    )
    assert "Calendars.ReadWrite" not in source_text
    assert not any(hasattr(OutlookCalendarSource, name) for name in ("create", "update", "delete"))
    assert not hasattr(OutlookCalendarSource, "write_scope")
