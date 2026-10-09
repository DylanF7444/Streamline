"""Offline authoring, provider round trips, and opt-in calendar write regressions."""

from __future__ import annotations

import copy
import io
import json
import urllib.request
from datetime import UTC, date, datetime, timedelta

import pytest

from personal_hub.bootstrap import build_hub_container
from personal_hub.calendar.contracts import CalendarEvent, CalendarQuery, CalendarSnapshot
from personal_hub.calendar.google_sync import GoogleCalendarSync
from personal_hub.calendar.local import LOCAL_IMPORT_PROVIDER_ID
from personal_hub.calendar.native import LOCAL_CALENDAR_ID
from personal_hub.config import HubSettings
from personal_hub.contracts import SourceRef
from personal_hub.integrations.http import BoundedProviderTransport
from personal_hub.integrations.oauth import GOOGLE_CALENDAR_EVENTS_SCOPE
from personal_hub.integrations.platforms import ExternalPlatformConfig


@pytest.fixture
def container(tmp_path):
    value = build_hub_container(HubSettings(data_dir=tmp_path / "hub", offline_mode=True))
    yield value
    value.close()


def _create(container, **changes):
    start = datetime(2026, 10, 12, 15, tzinfo=UTC)
    return container.controller.create_calendar_item(
        **{
            "title": "Planning",
            "starts_at": start,
            "ends_at": start + timedelta(hours=1),
            "timezone": "America/Chicago",
            **changes,
        }
    )


class Provider:
    def __init__(self):
        self.events = {}
        self.calls = []
        self.etag = 0
        self.lose_reply = False
        self.patch_conflict = False

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/primary"):
            return {"id": "user@example.com"}
        return copy.deepcopy(self.events.get(url, {"status_code": 404}))

    async def write_calendar_event(self, method, url, *, headers, payload):
        self.calls.append((method, url, {"headers": headers, "payload": copy.deepcopy(payload)}))
        key = url + "/" + payload["id"] if method == "POST" else url
        if method == "POST" and key in self.events:
            return {"status_code": 409}
        if method == "PATCH" and (
            self.patch_conflict or headers["If-Match"] != self.events[key]["etag"]
        ):
            return {"status_code": 412}
        self.etag += 1
        value = {
            **self.events.get(key, {}),
            **copy.deepcopy(payload),
            "etag": f'"version-{self.etag}"',
        }
        self.events[key] = value
        if self.lose_reply:
            self.lose_reply = False
            raise TimeoutError("Provider reply lost")
        return copy.deepcopy(value)

    def change(self, **changes):
        self.etag += 1
        value = next(iter(self.events.values()))
        value.update(changes, etag=f'"version-{self.etag}"')


def _sync(container, provider, allowed=lambda: True):
    async def token():
        return "fake-token"

    return GoogleCalendarSync(container.controller.local_calendar, provider, token, allowed)


def test_local_items_persist_offline_and_project_today_and_export(container, tmp_path):
    event = _create(container, description="Local notes", location="Library")
    assert event.import_id == LOCAL_CALENDAR_ID
    assert container.controller.local_calendar.binding(event.event_id) is None
    assert [
        item.title for item in container.controller.calendar_today_items(day=date(2026, 10, 12))
    ] == ["Planning"]
    path = container.controller.export_local_data(tmp_path / "export.json")
    assert (
        json.loads(path.read_text())["imported_calendar_events"][0]["description"] == "Local notes"
    )
    settings = container.settings
    backup = container.database.backup(tmp_path / "backup.db")
    container.close()
    reopened = build_hub_container(settings)
    try:
        assert reopened.controller.local_calendar.get(event.event_id) == event
        revised = reopened.controller.update_calendar_item(event.event_id, title="Revised")
        assert revised.title == "Revised"
        reopened.database.restore(backup)
        assert reopened.controller.local_calendar.get(event.event_id).title == "Planning"
        assert reopened.controller.delete_calendar_item(event.event_id)
        assert reopened.controller.local_calendar.get(event.event_id) is None
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"title": " "},
        {"timezone": "invalid/zone"},
        {"starts_at": datetime(2026, 10, 12)},
        {"ends_at": datetime(2026, 10, 11, tzinfo=UTC)},
        {"description": "x" * 20_001},
        {"google_calendar_id": "../other"},
    ],
)
def test_invalid_item_does_not_leave_local_rows(container, changes):
    with pytest.raises(ValueError):
        _create(container, **changes)
    assert container.controller.list_local_calendar_events() == []


def test_import_cannot_replace_manually_authored_items(container):
    event = _create(container)
    result = container.controller.import_calendar_from_text(
        "2026-10-12 | Imported", import_name="My calendar"
    )
    assert result.imported_count == 0
    assert result.issues
    assert container.controller.local_calendar.get(event.event_id) == event


@pytest.mark.asyncio
async def test_linked_event_projection_honors_calendar_selection_without_duplicates(container):
    local = _create(container, google_calendar_id="user@example.com")
    binding = container.controller.local_calendar.binding(local.event_id)
    remote = CalendarEvent(
        event_id=binding.remote_id,
        calendar_id=binding.calendar_id,
        title="Google copy",
        starts_at=local.starts_at,
        ends_at=local.ends_at,
        source=SourceRef(
            provider_id="google-calendar", resource_id=binding.remote_id, display_name="Google"
        ),
    )

    class Source:
        source_id = "google-calendar"
        provider_id = "google-calendar"
        display_name = "Google"

        async def list_events(self, query):
            return CalendarSnapshot(
                source_id=self.source_id,
                provider_id=self.provider_id,
                collected_at=datetime.now(UTC),
                events=(remote,),
            )

    container.controller.set_offline_mode(False)
    container.controller.register_calendar_source(Source())
    await container.controller.refresh_calendars(
        CalendarQuery(starts_at=local.starts_at, ends_at=local.ends_at)
    )
    both = (f"{LOCAL_IMPORT_PROVIDER_ID}/{LOCAL_CALENDAR_ID}", "google-calendar/user@example.com")
    items = container.controller.calendar_today_items(
        day=date(2026, 10, 12), selected_calendar_keys=both
    )
    assert [item.title for item in items] == ["Planning"]
    items = container.controller.calendar_today_items(
        day=date(2026, 10, 12), selected_calendar_keys=(both[1],)
    )
    assert [item.title for item in items] == ["Google copy"]


def test_shared_folder_sync_keeps_google_authorization_on_originating_device(container, tmp_path):
    event = _create(container, google_calendar_id="user@example.com")
    destination = build_hub_container(HubSettings(data_dir=tmp_path / "second"))
    try:
        container.controller.configure_sync(tmp_path / "shared")
        container.controller.sync_now("test passphrase for calendar sync")
        destination.controller.configure_sync(tmp_path / "shared")
        destination.controller.sync_now("test passphrase for calendar sync")
        assert destination.controller.local_calendar.get(event.event_id) == event
        assert destination.controller.local_calendar.binding(event.event_id) is None
    finally:
        destination.close()


@pytest.mark.asyncio
async def test_selected_item_sync_roundtrip_is_idempotent_and_keeps_local_baseline(container):
    event = _create(container, google_calendar_id="primary")
    local_only = _create(container, title="Device only")
    provider = Provider()
    sync = _sync(container, provider)
    assert (await sync.sync()).uploaded == 1
    binding = container.controller.local_calendar.binding(event.event_id)
    assert binding.calendar_id == "user@example.com"
    assert (await sync.sync()).uploaded == 0
    assert len(provider.events) == 1
    provider.change(summary="Google edited", location="Remote room")
    assert (await sync.sync()).downloaded == 1
    revised = container.controller.local_calendar.get(event.event_id)
    assert revised.title == "Google edited"
    assert revised.location == "Remote room"
    container.controller.update_calendar_item(event.event_id, title="Local edit")
    assert (await sync.sync()).uploaded == 1
    assert next(iter(provider.events.values()))["summary"] == "Local edit"
    assert container.controller.local_calendar.get(local_only.event_id).title == "Device only"
    assert {call[0] for call in provider.calls} == {"GET", "POST", "PATCH"}


@pytest.mark.asyncio
async def test_uncertain_insert_retry_reuses_remote_id_after_restart(container):
    event = _create(container, google_calendar_id="user@example.com")
    provider = Provider()
    provider.lose_reply = True
    assert (await _sync(container, provider).sync()).failed == 1
    assert container.controller.local_calendar.get(event.event_id) == event
    settings = container.settings
    container.close()
    reopened = build_hub_container(settings)
    try:
        assert (await _sync(reopened, provider).sync()).failed == 0
        assert len(provider.events) == 1
        assert sum(call[0] == "POST" for call in provider.calls) == 1
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_uncertain_insert_preserves_later_google_edit(container):
    event = _create(container, google_calendar_id="user@example.com")
    provider = Provider()
    provider.lose_reply = True
    sync = _sync(container, provider)
    assert (await sync.sync()).failed == 1
    provider.change(summary="Edited after uncertain insert")
    assert (await sync.sync()).conflicts == 1
    assert container.controller.local_calendar.get(event.event_id).title == "Planning"
    assert not any(call[0] == "PATCH" for call in provider.calls)


@pytest.mark.asyncio
async def test_revoking_item_sync_during_provider_read_prevents_write(container):
    event = _create(container, google_calendar_id="user@example.com")
    provider = Provider()
    original = provider.request

    async def read_then_revoke(*args, **kwargs):
        value = await original(*args, **kwargs)
        container.controller.local_calendar.stop_sync(event.event_id)
        return value

    provider.request = read_then_revoke
    assert (await _sync(container, provider).sync()).failed == 1
    assert container.controller.local_calendar.binding(event.event_id) is None
    assert provider.events == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("resolution", ["local", "google"])
async def test_conflicts_preserve_both_until_explicit_resolution(container, resolution):
    event = _create(container, google_calendar_id="user@example.com")
    provider = Provider()
    sync = _sync(container, provider)
    await sync.sync()
    container.controller.update_calendar_item(event.event_id, title="Local change")
    provider.change(summary="Remote change")
    assert (await sync.sync()).conflicts == 1
    assert container.controller.local_calendar.get(event.event_id).title == "Local change"
    assert next(iter(provider.events.values()))["summary"] == "Remote change"
    binding = container.controller.local_calendar.binding(event.event_id)
    container.controller.local_calendar.save_binding(
        binding.model_copy(update={"resolution": resolution})
    )
    report = await sync.sync()
    assert report.conflicts == 0
    expected = "Local change" if resolution == "local" else "Remote change"
    assert container.controller.local_calendar.get(event.event_id).title == expected
    assert next(iter(provider.events.values()))["summary"] == expected


@pytest.mark.asyncio
async def test_version_precondition_race_reports_conflict(container):
    event = _create(container, google_calendar_id="user@example.com")
    provider = Provider()
    sync = _sync(container, provider)
    await sync.sync()
    container.controller.update_calendar_item(event.event_id, title="Local edit")
    provider.patch_conflict = True
    assert (await sync.sync()).conflicts == 1
    assert next(iter(provider.events.values()))["summary"] == "Planning"


@pytest.mark.asyncio
async def test_remote_cancellation_never_resurrects_and_local_delete_keeps_google(container):
    event = _create(container, google_calendar_id="user@example.com")
    provider = Provider()
    sync = _sync(container, provider)
    await sync.sync()
    provider.change(status="cancelled")
    assert (await sync.sync()).remote_deleted == 1
    assert container.controller.local_calendar.get(event.event_id) is not None
    assert (await sync.sync()).uploaded == 0
    assert container.controller.delete_calendar_item(event.event_id)
    await sync.sync()
    assert len(provider.events) == 1
    assert not any(call[0] == "DELETE" for call in provider.calls)


@pytest.mark.asyncio
async def test_offline_or_disabled_sync_never_reads_credentials_or_transport(container):
    _create(container, google_calendar_id="primary")

    async def token():
        raise AssertionError("Credentials must not be read")

    provider = Provider()
    report = await GoogleCalendarSync(
        container.controller.local_calendar, provider, token, lambda: False
    ).sync()
    assert "paused" in report.message
    assert provider.calls == []
    assert (
        await container.platform_connections.sync_calendar_items(
            container.controller.local_calendar
        )
    ).failed == 0
    assert provider.calls == []


@pytest.mark.asyncio
async def test_unowned_remote_resource_cannot_be_overwritten(container):
    event = _create(container, google_calendar_id="user@example.com")
    provider = Provider()
    sync = _sync(container, provider)
    await sync.sync()
    provider.change(extendedProperties={"private": {"personalHubEvent": "somebody-else"}})
    container.controller.update_calendar_item(event.event_id, title="Edit")
    assert (await sync.sync()).failed == 1
    assert not any(call[0] == "PATCH" for call in provider.calls)


def test_google_write_scope_is_separate_opt_in_and_other_providers_reject_it(container):
    manager = container.platform_connections
    manager.configure("google", {"client_id": "desktop-client", "capabilities": ("calendar",)})
    assert GOOGLE_CALENDAR_EVENTS_SCOPE not in manager._oauth_service("google").config.scopes
    manager.configure("google", {"calendar_sync_enabled": True})
    assert GOOGLE_CALENDAR_EVENTS_SCOPE in manager._oauth_service("google").config.scopes
    with pytest.raises(ValueError):
        ExternalPlatformConfig(
            platform_id="microsoft",
            client_id="desktop",
            capabilities=("calendar",),
            calendar_sync_enabled=True,
        )


@pytest.mark.asyncio
async def test_google_existing_read_connection_requires_fresh_event_grant(container):
    manager = container.platform_connections
    container.controller.set_offline_mode(False)
    manager.configure(
        "google",
        {"client_id": "desktop", "capabilities": ("calendar",), "calendar_sync_enabled": True},
    )
    manager.vault.set("oauth_google_refresh", "old-read-refresh")
    manager.vault.set(
        "oauth_google_scopes", json.dumps(["https://www.googleapis.com/auth/calendar.readonly"])
    )
    _create(container, google_calendar_id="primary")
    provider = Provider()
    manager.transport = provider
    report = await manager.sync_calendar_items(container.controller.local_calendar)
    assert "Reconnect" in report.message
    assert provider.calls == []


@pytest.mark.asyncio
async def test_calendar_write_transport_has_bounded_json_and_no_guest_notifications():
    calls = []

    class Reply(io.BytesIO):
        status = 200
        headers = {}

    def opener(request, **kwargs):
        calls.append(request)
        return Reply(b'{"id":"synthetic"}')

    transport = BoundedProviderTransport(opener=opener)
    collection = "https://www.googleapis.com/calendar/v3/calendars/user%40example.com/events"
    result = await transport.write_calendar_event(
        "POST",
        collection,
        headers={"Authorization": "Bearer fake"},
        payload={"id": "ph" + "a" * 32, "summary": "Planning"},
    )
    assert result["id"] == "synthetic"
    assert calls[0].method == "POST"
    assert calls[0].full_url.endswith("?sendUpdates=none")
    assert json.loads(calls[0].data)["summary"] == "Planning"
    assert any(
        isinstance(handler, urllib.request.HTTPRedirectHandler)
        and handler.redirect_request(None, None, 302, "", {}, "https://attacker.example") is None
        for handler in BoundedProviderTransport()._opener.__self__.handlers
        if isinstance(handler, urllib.request.HTTPRedirectHandler)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,url,payload,headers",
    [
        ("DELETE", "https://www.googleapis.com/calendar/v3/calendars/primary/events/a", {}, {}),
        ("POST", "https://graph.microsoft.com/calendar/v3/calendars/primary/events", {}, {}),
        (
            "POST",
            "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            {"attendees": []},
            {},
        ),
        ("PATCH", "https://www.googleapis.com/calendar/v3/calendars/primary/events/abc", {}, {}),
        (
            "POST",
            "https://www.googleapis.com/calendar/v3/calendars/primary/events?sendUpdates=all",
            {},
            {},
        ),
    ],
)
async def test_write_transport_rejects_unapproved_operations_before_io(
    method, url, payload, headers
):
    def opener(*args, **kwargs):
        raise AssertionError("No network is allowed")

    transport = BoundedProviderTransport(opener=opener)
    with pytest.raises(ValueError):
        await transport.write_calendar_event(method, url, payload=payload, headers=headers)
