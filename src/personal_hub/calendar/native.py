"""Local event authoring and durable, device-local provider sync bindings."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field, field_validator

from personal_hub.calendar.contracts import CalendarQuery
from personal_hub.calendar.local import CalendarImportRepository, ImportedCalendarEvent
from personal_hub.contracts import HubContract
from personal_hub.storage.repositories import SettingsRepository

LOCAL_CALENDAR_ID = UUID("e5940710-4841-4801-bbc2-f367e3339828")
_SYNC_PREFIX = "calendar.google_sync."


class CalendarSyncBinding(HubContract):
    event_id: UUID
    calendar_id: str = Field(min_length=1, max_length=256)
    etag: str = Field(default="", max_length=512, pattern=r"^[^\r\n\x00]*$")
    fingerprint: str = Field(default="", max_length=64, pattern=r"^[a-f0-9]*$")
    state: Literal["pending", "synced", "conflict", "remote_deleted"] = "pending"
    resolution: Literal["local", "google"] | None = None

    @field_validator("calendar_id")
    @classmethod
    def _calendar_id_is_bounded(cls, value: str) -> str:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        return CalendarQuery(
            starts_at=start, ends_at=datetime(2026, 1, 2, tzinfo=UTC), calendar_ids=(value,)
        ).calendar_ids[0]

    @property
    def remote_id(self) -> str:
        # Google accepts base32hex IDs; UUID hex also survives an uncertain insert retry.
        return "ph" + self.event_id.hex


def event_fingerprint(event: ImportedCalendarEvent) -> str:
    payload = event.model_dump(
        mode="json",
        include={"title", "starts_at", "ends_at", "all_day", "timezone", "location", "description"},
    )
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class LocalCalendarItems:
    """Author local events independently of connectivity or provider credentials."""

    def __init__(self, repository: CalendarImportRepository, settings: SettingsRepository) -> None:
        self.repository = repository
        self.settings = settings
        self._lock = threading.RLock()

    def get(self, event_id: UUID | str) -> ImportedCalendarEvent | None:
        identifier = UUID(str(event_id))
        event = self.repository.get_event(identifier)
        return event if event is not None and event.import_id == LOCAL_CALENDAR_ID else None

    def save(
        self,
        *,
        event_id: UUID | str | None = None,
        google_calendar_id: str | None = None,
        **fields: object,
    ) -> ImportedCalendarEvent:
        with self._lock:
            current = self.get(event_id) if event_id is not None else None
            if current is None and self.repository.count() >= self.repository.max_events:
                raise ValueError("The local calendar event limit has been reached")
            if event_id is not None and current is None:
                raise ValueError("Local calendar item was not found")
            now = datetime.now(UTC)
            payload = (
                current.model_dump()
                if current
                else {
                    "event_id": uuid4(),
                    "import_id": LOCAL_CALENDAR_ID,
                    "stable_key": str(uuid4()),
                    "created_at": now,
                }
            )
            allowed = {
                "title",
                "starts_at",
                "ends_at",
                "all_day",
                "timezone",
                "location",
                "description",
            }
            if set(fields).difference(allowed):
                raise ValueError("Unsupported local calendar fields")
            payload.update(fields)
            payload["updated_at"] = now
            event = ImportedCalendarEvent.model_validate(payload)
            if event.all_day:
                from zoneinfo import ZoneInfo

                zone = ZoneInfo(event.timezone)
                if any(
                    value.astimezone(zone).time().isoformat() != "00:00:00"
                    for value in (event.starts_at, event.ends_at)
                ):
                    raise ValueError("All-day items must start and end at local midnight")
            binding = None
            if google_calendar_id is not None:
                # Apply the same bounded calendar identifier contract as read queries.
                target = CalendarQuery(
                    starts_at=event.starts_at,
                    ends_at=event.ends_at,
                    calendar_ids=(google_calendar_id,),
                ).calendar_ids[0]
                binding = self.binding(event.event_id)
                if binding is not None and binding.calendar_id != target:
                    raise ValueError("A synced item cannot move to another calendar")
                binding = binding or CalendarSyncBinding(
                    event_id=event.event_id, calendar_id=target
                )
            self._store(event)
            if binding is not None:
                self.save_binding(binding)
            return event

    def _store(self, event: ImportedCalendarEvent) -> None:
        self.repository.replace_import(
            name="My calendar",
            source_format="manual",
            import_id=LOCAL_CALENDAR_ID,
            events=(event,),
            remove_missing=False,
            now=event.updated_at,
        )

    def accept_remote(
        self,
        event: ImportedCalendarEvent,
        *,
        expected: ImportedCalendarEvent,
        binding: CalendarSyncBinding,
    ) -> bool:
        with self._lock:
            if self.get(event.event_id) != expected or self.binding(event.event_id) != binding:
                return False
            self._store(event)
            return True

    def delete(self, event_id: UUID | str) -> bool:
        """Remove the local item; provider deletion is deliberately a separate action."""
        with self._lock:
            current = self.get(event_id)
            if current is None:
                return False
            remaining = [
                event
                for event in self.repository.list_events(LOCAL_CALENDAR_ID)
                if event.event_id != current.event_id
            ]
            self.repository.replace_import(
                name="My calendar",
                source_format="manual",
                import_id=LOCAL_CALENDAR_ID,
                events=remaining,
            )
            self.settings.delete(_SYNC_PREFIX + str(current.event_id))
            return True

    def binding(self, event_id: UUID | str) -> CalendarSyncBinding | None:
        value = self.settings.get(_SYNC_PREFIX + str(UUID(str(event_id))))
        if value is None:
            return None
        binding = CalendarSyncBinding.model_validate_json(json.dumps(value))
        if binding.event_id != UUID(str(event_id)):
            raise ValueError("Calendar sync binding has the wrong identity")
        return binding

    def save_binding(
        self, binding: CalendarSyncBinding, *, expected: CalendarSyncBinding | None = None
    ) -> bool:
        with self._lock:
            if expected is not None and self.binding(binding.event_id) != expected:
                return False
            self.settings.set(_SYNC_PREFIX + str(binding.event_id), binding.model_dump(mode="json"))
            return True

    def stop_sync(self, event_id: UUID | str) -> None:
        with self._lock:
            self.settings.delete(_SYNC_PREFIX + str(UUID(str(event_id))))

    def bindings(self) -> tuple[CalendarSyncBinding, ...]:
        values = self.settings.all()
        bindings = []
        for event in self.repository.list_events(LOCAL_CALENDAR_ID):
            value = values.get(_SYNC_PREFIX + str(event.event_id))
            if value is not None:
                binding = CalendarSyncBinding.model_validate_json(json.dumps(value))
                if binding.event_id != event.event_id:
                    raise ValueError("Calendar sync binding has the wrong identity")
                bindings.append(binding)
        return tuple(bindings)

    def remote_ids(self) -> frozenset[tuple[str, str]]:
        return frozenset((binding.calendar_id, binding.remote_id) for binding in self.bindings())
