from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from personal_hub.bootstrap import build_hub_container
from personal_hub.calendar.ical import IcalFeedFetcher
from personal_hub.config import HubSettings
from personal_hub.feeds.contracts import FeedResponse
from personal_hub.integrations.canvas import canvas_assignment_url
from personal_hub.integrations.http import BoundedProviderTransport
from personal_hub.integrations.oauth import OAuthConnectionService
from personal_hub.sources import SourceState


class _ProviderTransport:
    def __init__(self) -> None:
        self.forms: list[tuple[str, dict[str, str]]] = []
        self.requests: list[tuple[str, str]] = []
        self.request_kwargs: list[dict[str, object]] = []
        self.response: dict[str, object] = {"ok": True, "user_id": "U123"}

    async def post_form(
        self,
        url: str,
        data: dict[str, str],
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> dict[str, object]:
        assert timeout_seconds <= 60
        assert max_response_bytes <= 64 * 1024
        self.forms.append((url, dict(data)))
        scopes = (
            "https://www.googleapis.com/auth/calendar.readonly "
            "https://www.googleapis.com/auth/gmail.readonly"
            if "googleapis.com" in url
            else "offline_access Calendars.Read Mail.Read"
        )
        return {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_in": 3_600,
            "scope": scopes,
        }

    async def request(
        self,
        method: str,
        url: str,
        **_kwargs: object,
    ) -> dict[str, object]:
        self.requests.append((method, url))
        self.request_kwargs.append(dict(_kwargs))
        return dict(self.response)


async def _authorize(factory) -> object:
    service: OAuthConnectionService = factory("http://127.0.0.1:49152/personal-hub/oauth/callback")
    request = service.begin_authorization()
    return await service.complete_authorization(code="verified-code", state=request.state)


@pytest.mark.asyncio
async def test_google_connection_persists_no_plaintext_secrets_and_wires_read_sources(
    tmp_path: Path,
) -> None:
    container = build_hub_container(HubSettings(data_dir=tmp_path / "hub"))
    manager = container.platform_connections
    transport = _ProviderTransport()
    manager.transport = transport  # type: ignore[assignment]
    manager.oauth_authorizer = _authorize
    try:
        assert {item.source_id for item in manager.definitions()} == {
            "platform-canvas",
            "platform-google",
            "platform-microsoft",
            "platform-slack",
        }
        assert not any(
            source.enabled
            for source in container.controller.list_sources()
            if source.source_id.startswith("platform-")
        )
        manager.configure(
            "google",
            {
                "account_label": "Work Google",
                "client_id": "desktop-client.apps.googleusercontent.com",
                "client_secret": "private-client-secret",
                "capabilities": ("calendar", "messages"),
            },
        )

        stored = container.settings_repository.get("external_platform_connections.v1")
        assert isinstance(stored, Mapping)
        assert "private-client-secret" not in repr(stored)
        assert manager.vault.get("platform_google_client_secret") == "private-client-secret"
        assert next(
            source
            for source in container.controller.list_sources()
            if source.source_id == "platform-google"
        ).enabled
        assert "google-calendar" in container.calendar_service.source_ids
        assert container.calendar_service.source("google-calendar").discover_calendars is True
        assert "gmail" in container.message_service.source_ids

        connected = await manager.connect("google")
        assert connected.state is SourceState.READY
        assert manager.vault.has("oauth_google_refresh")
        assert transport.forms[0][1]["client_secret"] == "private-client-secret"

        disconnected = await manager.disconnect("google")
        assert disconnected.state is SourceState.DISCONNECTED
        assert not manager.vault.has("oauth_google_access")
        assert not manager.vault.has("oauth_google_refresh")
    finally:
        container.close()


class _CanvasTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def request(
        self,
        _method: str,
        url: str,
        *,
        headers: dict[str, str],
        timeout: float,
        max_bytes: int,
    ) -> FeedResponse:
        assert timeout <= 30
        assert max_bytes <= 20_000_000
        self.calls.append((url, dict(headers)))
        return FeedResponse(
            status_code=200,
            headers={"ETag": '"canvas-v1"'},
            body=b"""BEGIN:VCALENDAR\r
VERSION:2.0\r
BEGIN:VEVENT\r
UID:assignment-101@example.test\r
DTSTART:20260912T235900Z\r
DTEND:20260913T005900Z\r
SUMMARY:Submit research outline\r
DESCRIPTION:<p>Upload the outline.</p><script>ignored()</script>\r
URL:https://canvas.example.edu/calendar?include_contexts=course_10&month=09&year=2026#assignment_101\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:course-event-202@example.test\r
DTSTART;VALUE=DATE:20260915\r
DTEND;VALUE=DATE:20260916\r
SUMMARY:Read module 4\r
URL:https://canvas.example.edu/calendar?include_contexts=course_10&month=09&year=2026#calendar_event_202\r
END:VEVENT\r
END:VCALENDAR\r
""",
            url=url,
        )


def test_canvas_assignment_url_replaces_calendar_links_and_rejects_non_assignments() -> None:
    canvas_origin = "https://canvas.example.edu"
    calendar_url = (
        "https://canvas.example.edu/calendar?include_contexts=course_10&month=09&year=2026"
        "#assignment_101"
    )

    assert canvas_assignment_url(calendar_url, canvas_origin=canvas_origin) == (
        "https://canvas.example.edu/courses/10/assignments/101"
    )
    assert (
        canvas_assignment_url(
            "https://canvas.example.edu/calendar?include_contexts=course_10#calendar_event_202",
            canvas_origin=canvas_origin,
        )
        is None
    )
    assert (
        canvas_assignment_url(
            calendar_url.replace("canvas.example.edu", "other.example.edu"),
            canvas_origin=canvas_origin,
        )
        is None
    )
    assert (
        canvas_assignment_url(
            "https://canvas.example.edu/courses/10/assignments/101?module_item_id=7"
        )
        == "https://canvas.example.edu/courses/10/assignments/101"
    )


@pytest.mark.asyncio
async def test_canvas_ical_connection_imports_stable_due_tasks_without_storing_feed_url(
    tmp_path: Path,
) -> None:
    container = build_hub_container(HubSettings(data_dir=tmp_path / "hub"))
    manager = container.platform_connections
    transport = _CanvasTransport()
    manager.canvas_importer.fetcher = IcalFeedFetcher(transport=transport)
    private_feed_url = "https://canvas.example.edu/feeds/calendars/private-feed-token.ics"
    try:
        manager.configure(
            "canvas",
            {
                "account_label": "School Canvas",
                "capabilities": ("tasks", "calendar"),
                "feed_url": private_feed_url,
            },
        )

        stored = container.settings_repository.get("external_platform_connections.v1")
        assert private_feed_url not in repr(stored)
        assert manager.vault.get("platform_canvas_feed_url") == private_feed_url

        connected = await manager.connect("canvas")
        assert connected.state is SourceState.READY
        tasks = container.task_repository.list(tags=("canvas",), limit=10)
        assert [task.title for task in tasks] == [
            "Submit research outline",
            "Read module 4",
        ]
        assert tasks[0].due_at == datetime(2026, 9, 12, 23, 59, tzinfo=UTC)
        assert tasks[0].due_is_all_day is False
        assert tasks[0].external_url == ("https://canvas.example.edu/courses/10/assignments/101")
        assert tasks[0].notes == (
            "Imported from Canvas.\n\nUpload the outline.\n\n"
            "Open in Canvas: https://canvas.example.edu/courses/10/assignments/101"
        )
        assert tasks[1].due_is_all_day is True
        assert tasks[1].external_url is None

        first_id = tasks[0].task_id
        container.task_repository.complete(
            first_id,
            completed_at=tasks[0].created_at + timedelta(minutes=1),
        )
        refreshed = await manager.refresh_canvas()
        assert refreshed.metadata["imported_task_count"] == 2
        assert container.task_repository.count(tags=("canvas",)) == 2
        completed = container.task_repository.get(first_id)
        assert completed is not None
        assert completed.state == "completed"

        await manager.disconnect("canvas")
        assert manager.vault.get("platform_canvas_feed_url") is None
        assert container.task_repository.count(tags=("canvas",)) == 2
    finally:
        container.close()


def test_canvas_requires_a_private_https_ical_url(tmp_path: Path) -> None:
    container = build_hub_container(HubSettings(data_dir=tmp_path / "hub"))
    try:
        with pytest.raises(ValueError, match="HTTPS iCalendar"):
            container.platform_connections.configure(
                "canvas",
                {
                    "capabilities": ("tasks", "calendar"),
                    "feed_url": "http://canvas.example.edu/calendar.ics",
                },
            )
        assert container.secret_vault.get("platform_canvas_feed_url") is None
    finally:
        container.close()


@pytest.mark.asyncio
async def test_slack_read_only_token_connection_is_configurable_and_disconnects(
    tmp_path: Path,
) -> None:
    container = build_hub_container(HubSettings(data_dir=tmp_path / "hub"))
    manager = container.platform_connections
    transport = _ProviderTransport()
    manager.transport = transport  # type: ignore[assignment]
    try:
        manager.configure(
            "slack",
            {
                "account_label": "Team Slack",
                "capabilities": ("messages",),
                "slack_selection_kind": "channel",
                "slack_selection": "C0123456789",
                "slack_scopes": ("channels:history",),
                "token": "xoxb-read-only-token",
            },
        )
        assert "slack" in container.message_service.source_ids
        assert "xoxb-read-only-token" not in repr(
            container.settings_repository.get("external_platform_connections.v1")
        )

        connected = await manager.connect("slack")
        assert connected.state is SourceState.READY
        assert transport.requests == [("GET", "https://slack.com/api/auth.test")]

        disconnected = await manager.disconnect("slack")
        assert disconnected.state is SourceState.DISCONNECTED
        assert manager.vault.get("platform_slack_token") is None
    finally:
        container.close()


@pytest.mark.asyncio
async def test_provider_transport_rejects_unreviewed_hosts_and_mutating_requests() -> None:
    transport = BoundedProviderTransport()
    with pytest.raises(ValueError, match="read-only"):
        await transport.request("POST", "https://www.googleapis.com/calendar/v3/events")
    with pytest.raises(ValueError, match="reviewed HTTPS"):
        await transport.request("GET", "https://attacker.example/collect")
