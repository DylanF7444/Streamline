"""Explicit two-way synchronization of individually selected local events."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, date, datetime, time
from typing import Protocol
from urllib.parse import quote
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from personal_hub.calendar.contracts import CalendarQuery
from personal_hub.calendar.local import ImportedCalendarEvent
from personal_hub.calendar.native import CalendarSyncBinding, LocalCalendarItems, event_fingerprint

_API = "https://www.googleapis.com/calendar/v3/calendars/"


class SyncTransport(Protocol):
    async def request(
        self, method: str, url: str, *, headers: Mapping[str, str]
    ) -> dict[str, object]: ...
    async def write_calendar_event(
        self, method: str, url: str, *, headers: Mapping[str, str], payload: Mapping[str, object]
    ) -> dict[str, object]: ...


class CalendarSyncReport(BaseModel):
    uploaded: int = 0
    downloaded: int = 0
    conflicts: int = 0
    remote_deleted: int = 0
    failed: int = 0
    message: str = Field(default="Calendar sync finished.", max_length=240)


class GoogleCalendarSync:
    def __init__(
        self,
        local: LocalCalendarItems,
        transport: SyncTransport,
        token: Callable[[], Awaitable[str]],
        allowed: Callable[[], bool],
    ) -> None:
        self.local = local
        self.transport = transport
        self.token = token
        self.allowed = allowed

    async def sync(self) -> CalendarSyncReport:
        report = CalendarSyncReport()
        # Offline/disabled checks precede credentials and every HTTP operation.
        if not self.allowed():
            report.message = (
                "Google sync is disabled or paused by offline mode. Local items are saved."
            )
            return report
        bindings = self.local.bindings()
        if len(bindings) > 500:
            cursor = str(self.local.settings.get("calendar.google_sync_cursor", ""))
            ordered = sorted(bindings, key=lambda item: str(item.event_id))
            bindings = tuple(
                [item for item in ordered if str(item.event_id) > cursor]
                + [item for item in ordered if str(item.event_id) <= cursor]
            )[:500]
            self.local.settings.set("calendar.google_sync_cursor", str(bindings[-1].event_id))
            report.message = "Synced up to 500 selected items. Run sync again for remaining items."
        for binding in bindings:
            if not self.allowed():
                report.message = "Calendar sync paused. Local items are saved."
                break
            event = self.local.get(binding.event_id)
            if event is None or binding.state == "remote_deleted":
                continue
            try:
                await self._sync_one(event, binding, report)
            except Exception:
                # Preserve the last baseline so failed/uncertain writes can be retried.
                report.failed += 1
        return report

    async def _request(
        self,
        method: str,
        url: str,
        *,
        event_id: UUID,
        payload: dict[str, object] | None = None,
        etag: str = "",
    ) -> dict[str, object]:
        if not self.allowed() or self.local.binding(event_id) is None:
            raise ValueError("Calendar sync is paused")
        token = await self.token()
        if not self.allowed() or self.local.binding(event_id) is None:
            raise ValueError("Calendar sync is paused")
        headers = {"Authorization": "Bearer " + token}
        if etag:
            headers["If-Match"] = etag
        if payload is None:
            value = await self.transport.request(method, url, headers=headers)
        else:
            value = await self.transport.write_calendar_event(
                method, url, headers=headers, payload=payload
            )
        # Fakes and production responses pass the same bounded object boundary.
        if not isinstance(value, dict) or len(json.dumps(value, default=str)) > 2_000_000:
            raise ValueError("Invalid calendar sync response")
        return value

    async def _sync_one(
        self, event: ImportedCalendarEvent, binding: CalendarSyncBinding, report: CalendarSyncReport
    ) -> None:
        if binding.calendar_id == "primary":
            calendar = await self._request("GET", _API + "primary", event_id=event.event_id)
            identifier = calendar.get("id")
            if not isinstance(identifier, str):
                raise ValueError("Cannot resolve the selected Google calendar")
            selected = CalendarQuery(
                starts_at=event.starts_at, ends_at=event.ends_at, calendar_ids=(identifier,)
            ).calendar_ids[0]
            previous = binding
            binding = binding.model_copy(update={"calendar_id": selected})
            # Persist before inserting: reconnecting a different account cannot retarget a retry.
            if not self.local.save_binding(binding, expected=previous):
                raise ValueError("Calendar sync selection changed")
        collection = _API + quote(binding.calendar_id, safe="") + "/events"
        url = collection + "/" + binding.remote_id
        remote = await self._request("GET", url, event_id=event.event_id)
        status = remote.get("status_code", 200)
        if status in {404, 410} or remote.get("status") == "cancelled":
            if binding.etag:
                self.local.save_binding(
                    binding.model_copy(update={"state": "remote_deleted"}), expected=binding
                )
                report.remote_deleted += 1
                return
            if status != 404:
                raise ValueError("Cannot recreate a cancelled Google event")
            result = await self._request(
                "POST", collection, event_id=event.event_id, payload=_payload(event, binding)
            )
            if result.get("status_code") == 409:
                # The provider may have accepted an earlier insert whose reply was lost.
                result = await self._request("GET", url, event_id=event.event_id)
            remote_event = _remote_event(result, event, binding)
            if event_fingerprint(remote_event) != event_fingerprint(event):
                self.local.save_binding(
                    binding.model_copy(update={"state": "conflict"}), expected=binding
                )
                report.conflicts += 1
                return
            self._remember(binding, event, result)
            report.uploaded += 1
            return
        remote_event = _remote_event(remote, event, binding)
        if not binding.etag and binding.resolution is None:
            # An insert can succeed while its response is lost. Do not overwrite
            # subsequent provider edits before establishing the first baseline.
            if event_fingerprint(remote_event) != event_fingerprint(event):
                self.local.save_binding(
                    binding.model_copy(update={"state": "conflict"}), expected=binding
                )
                report.conflicts += 1
            else:
                self._remember(binding, event, remote)
            return
        local_changed = event_fingerprint(event) != binding.fingerprint
        remote_changed = bool(binding.etag and remote.get("etag") != binding.etag)
        if binding.resolution is None and (
            binding.state == "conflict" or (local_changed and remote_changed)
        ):
            self.local.save_binding(
                binding.model_copy(update={"state": "conflict"}), expected=binding
            )
            report.conflicts += 1
            return
        if binding.resolution == "google" or (remote_changed and binding.resolution != "local"):
            if not self.local.accept_remote(remote_event, expected=event, binding=binding):
                self.local.save_binding(
                    binding.model_copy(update={"state": "conflict"}), expected=binding
                )
                report.conflicts += 1
                return
            self._remember(binding, remote_event, remote)
            report.downloaded += 1
        elif local_changed or binding.resolution == "local":
            payload = _payload(event, binding)
            payload.pop("id")
            result = await self._request(
                "PATCH", url, event_id=event.event_id, payload=payload, etag=str(remote["etag"])
            )
            if result.get("status_code") == 412:
                self.local.save_binding(
                    binding.model_copy(update={"state": "conflict"}), expected=binding
                )
                report.conflicts += 1
                return
            _remote_event(result, event, binding)
            self._remember(binding, event, result)
            report.uploaded += 1
        else:
            self._remember(binding, event, remote)

    def _remember(
        self, binding: CalendarSyncBinding, event: ImportedCalendarEvent, remote: dict[str, object]
    ) -> None:
        validated = CalendarSyncBinding(
            event_id=binding.event_id,
            calendar_id=binding.calendar_id,
            etag=remote.get("etag"),
            fingerprint=event_fingerprint(event),
            state="synced",
        )
        self.local.save_binding(validated, expected=binding)


def _payload(event: ImportedCalendarEvent, binding: CalendarSyncBinding) -> dict[str, object]:
    zone = ZoneInfo(event.timezone)
    start = event.starts_at.astimezone(zone)
    end = event.ends_at.astimezone(zone)
    return {
        "id": binding.remote_id,
        "summary": event.title,
        "description": event.description,
        "location": event.location,
        "start": {"date": start.date().isoformat()}
        if event.all_day
        else {"dateTime": start.isoformat(), "timeZone": event.timezone},
        "end": {"date": end.date().isoformat()}
        if event.all_day
        else {"dateTime": end.isoformat(), "timeZone": event.timezone},
        "extendedProperties": {"private": {"personalHubEvent": str(event.event_id)}},
    }


def _remote_event(
    value: dict[str, object], local: ImportedCalendarEvent, binding: CalendarSyncBinding
) -> ImportedCalendarEvent:
    properties = value.get("extendedProperties")
    private = properties.get("private") if isinstance(properties, dict) else None
    if (
        value.get("id") != binding.remote_id
        or not isinstance(private, dict)
        or private.get("personalHubEvent") != str(local.event_id)
        or not isinstance(value.get("etag"), str)
        or not value["etag"]
    ):
        raise ValueError("Calendar sync response is not the linked event")
    CalendarSyncBinding(
        event_id=binding.event_id, calendar_id=binding.calendar_id, etag=value["etag"]
    )
    bounds = []
    all_day = None
    timezone = local.timezone
    for name in ("start", "end"):
        part = value.get(name)
        if not isinstance(part, dict):
            raise ValueError("Calendar event time is missing")
        date_only = "date" in part
        if date_only == ("dateTime" in part) or (all_day is not None and all_day != date_only):
            raise ValueError("Calendar event bounds must use the same time representation")
        all_day = date_only
        timezone = part.get("timeZone", timezone)
        zone = ZoneInfo(timezone)
        if "date" in part:
            bounds.append(datetime.combine(date.fromisoformat(part["date"]), time.min, tzinfo=zone))
        else:
            parsed = datetime.fromisoformat(part["dateTime"].replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("Google event timestamp lacks an offset")
            bounds.append(parsed)
    return ImportedCalendarEvent.model_validate(
        {
            **local.model_dump(),
            "title": value.get("summary", "Untitled event"),
            "starts_at": bounds[0],
            "ends_at": bounds[1],
            "all_day": all_day,
            "timezone": timezone,
            "location": value.get("location", ""),
            "description": value.get("description", ""),
            "updated_at": datetime.now(UTC),
        }
    )
