"""Durable local calendar items created by CSV and text-list imports.

Connected calendar adapters remain read-only.  This module owns the separate
local-import table used when a user deliberately chooses to add calendar items
from a file or pasted list.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Literal, cast
from urllib.parse import urlsplit
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
from sqlalchemy import delete, select

from personal_hub.calendar.contracts import CalendarEvent
from personal_hub.contracts import HubContract, SourceRef
from personal_hub.storage.database import (
    HubCalendarImportRow,
    HubDatabase,
    HubImportedCalendarEventRow,
)

LocalCalendarImportFormat = Literal["csv", "text"]
LOCAL_IMPORT_PROVIDER_ID = "personal-hub-import"
LOCAL_IMPORT_SOURCE_ID = "personal-hub-imported-calendar"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("local calendar timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _db_time(value: datetime) -> datetime:
    return _utc(value).replace(tzinfo=None)


def _contract_time(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _name(value: str) -> str:
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > 160:
        raise ValueError("calendar import name must be a bounded non-empty string")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError("calendar import name contains control characters")
    return normalized


def _timezone(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 128:
        raise ValueError("local calendar timezone must be bounded")
    try:
        ZoneInfo(normalized)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"Unknown local calendar timezone: {normalized}") from error
    return normalized


def _text(value: str, field_name: str, limit: int, *, collapse: bool) -> str:
    if any(
        (ord(character) < 32 and character not in {"\n", "\r", "\t"}) or ord(character) == 127
        for character in value
    ):
        raise ValueError(f"local calendar {field_name} contains control characters")
    normalized = " ".join(value.split()) if collapse else value.strip()
    if len(normalized) > limit:
        raise ValueError(f"local calendar {field_name} is too long")
    return normalized


def _safe_url(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        return ""
    if len(normalized) > 1_024 or any(
        ord(character) < 33 or ord(character) == 127 for character in normalized
    ):
        raise ValueError("local calendar link must be a safe HTTP or HTTPS URL")
    try:
        parsed = urlsplit(normalized)
    except ValueError as error:
        raise ValueError("local calendar link must be a safe HTTP or HTTPS URL") from error
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("local calendar link must use HTTP or HTTPS without credentials")
    return normalized


class CalendarImport(HubContract):
    """One named local import batch shown as a selectable calendar."""

    import_id: UUID = Field(default_factory=uuid4)
    name: str = Field(min_length=1, max_length=160)
    source_format: LocalCalendarImportFormat
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("name")
    @classmethod
    def _name_is_safe(cls, value: str) -> str:
        return _name(value)

    @field_validator("created_at", "updated_at")
    @classmethod
    def _times_are_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def _times_are_ordered(self) -> CalendarImport:
        if self.updated_at < self.created_at:
            raise ValueError("calendar import updated_at cannot precede created_at")
        return self


class ImportedCalendarEvent(HubContract):
    """A persisted event that belongs only to a local import batch."""

    event_id: UUID = Field(default_factory=uuid4)
    import_id: UUID
    stable_key: str = Field(min_length=1, max_length=320)
    title: str = Field(min_length=1, max_length=240)
    starts_at: datetime
    ends_at: datetime
    all_day: bool = False
    timezone: str = "UTC"
    location: str = Field(default="", max_length=512)
    description: str = Field(default="", max_length=20_000)
    deep_link: str = Field(default="", max_length=1_024)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("stable_key")
    @classmethod
    def _stable_key_is_safe(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(
            ord(character) < 32 or ord(character) == 127 for character in normalized
        ):
            raise ValueError("local calendar stable key is invalid")
        return normalized

    @field_validator("title")
    @classmethod
    def _title_is_safe(cls, value: str) -> str:
        normalized = _text(value, "title", 240, collapse=True)
        if not normalized:
            raise ValueError("local calendar title cannot be blank")
        return normalized

    @field_validator("location")
    @classmethod
    def _location_is_safe(cls, value: str) -> str:
        return _text(value, "location", 512, collapse=True)

    @field_validator("description")
    @classmethod
    def _description_is_safe(cls, value: str) -> str:
        return _text(value, "description", 20_000, collapse=False)

    @field_validator("deep_link")
    @classmethod
    def _link_is_safe(cls, value: str) -> str:
        return _safe_url(value)

    @field_validator("starts_at", "ends_at", "created_at", "updated_at")
    @classmethod
    def _times_are_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("timezone")
    @classmethod
    def _timezone_is_known(cls, value: str) -> str:
        return _timezone(value)

    @model_validator(mode="after")
    def _event_window_is_ordered(self) -> ImportedCalendarEvent:
        if self.ends_at <= self.starts_at:
            raise ValueError("local calendar event end must follow its start")
        if self.updated_at < self.created_at:
            raise ValueError("local calendar event updated_at cannot precede created_at")
        return self

    def as_calendar_event(self, *, calendar_name: str) -> CalendarEvent:
        """Project this local row into the normal read-only calendar contract."""

        return CalendarEvent(
            event_id=str(self.event_id),
            calendar_id=str(self.import_id),
            title=self.title,
            starts_at=self.starts_at,
            ends_at=self.ends_at,
            all_day=self.all_day,
            timezone=self.timezone,
            source=SourceRef(
                provider_id=LOCAL_IMPORT_PROVIDER_ID,
                resource_id=str(self.import_id),
                display_name=calendar_name or "Personal Hub local import",
            ),
            deep_link=self.deep_link or None,
            location=self.location or None,
            sensitive=True,
        )


class CalendarImportRepository:
    """Persist and replace bounded local calendar import batches."""

    def __init__(self, database: HubDatabase, *, max_events: int = 5_000) -> None:
        if not 1 <= max_events <= 5_000:
            raise ValueError("local calendar import max_events must be between 1 and 5000")
        self.database = database
        self.max_events = max_events

    def list_imports(self, *, limit: int = 1_000) -> list[CalendarImport]:
        if not 1 <= limit <= 5_000:
            raise ValueError("calendar import limit must be between 1 and 5000")
        with self.database.session() as session:
            rows = session.scalars(
                select(HubCalendarImportRow)
                .order_by(HubCalendarImportRow.name, HubCalendarImportRow.import_id)
                .limit(limit)
            ).all()
            return [self._import_contract(row) for row in rows]

    list_calendar_imports = list_imports

    def get(self, import_id: UUID | str) -> CalendarImport | None:
        identifier = str(_as_uuid(import_id))
        with self.database.session() as session:
            row = session.get(HubCalendarImportRow, identifier)
            return None if row is None else self._import_contract(row)

    def get_by_name(self, name: str) -> CalendarImport | None:
        normalized = _name(name)
        for item in self.list_imports(limit=5_000):
            if item.name.casefold() == normalized.casefold():
                return item
        return None

    find_by_name = get_by_name

    def list_events(
        self,
        import_id: UUID | str | None = None,
        *,
        starts_at: datetime | None = None,
        ends_at: datetime | None = None,
        limit: int = 5_000,
    ) -> list[ImportedCalendarEvent]:
        if not 1 <= limit <= self.max_events:
            raise ValueError(f"calendar event limit must be between 1 and {self.max_events}")
        if starts_at is not None and ends_at is not None and _utc(ends_at) <= _utc(starts_at):
            raise ValueError("calendar event query end must follow its start")
        with self.database.session() as session:
            statement = select(HubImportedCalendarEventRow)
            if import_id is not None:
                statement = statement.where(
                    HubImportedCalendarEventRow.import_id == str(_as_uuid(import_id))
                )
            if starts_at is not None:
                statement = statement.where(
                    HubImportedCalendarEventRow.ends_at > _db_time(starts_at)
                )
            if ends_at is not None:
                statement = statement.where(
                    HubImportedCalendarEventRow.starts_at < _db_time(ends_at)
                )
            rows = session.scalars(
                statement.order_by(
                    HubImportedCalendarEventRow.starts_at,
                    HubImportedCalendarEventRow.title,
                    HubImportedCalendarEventRow.event_id,
                ).limit(limit)
            ).all()
            return [self._event_contract(row) for row in rows]

    list_calendar_events = list_events

    def count(self, import_id: UUID | str | None = None) -> int:
        with self.database.session() as session:
            statement = select(HubImportedCalendarEventRow.event_id)
            if import_id is not None:
                statement = statement.where(
                    HubImportedCalendarEventRow.import_id == str(_as_uuid(import_id))
                )
            return len(session.scalars(statement).all())

    def replace_import(
        self,
        *,
        name: str,
        source_format: LocalCalendarImportFormat,
        events: Iterable[ImportedCalendarEvent],
        import_id: UUID | str | None = None,
        remove_missing: bool = True,
        now: datetime | None = None,
    ) -> tuple[CalendarImport, tuple[ImportedCalendarEvent, ...]]:
        """Create or replace one named batch without leaving stale event rows."""

        timestamp = _utc(now or datetime.now(UTC))
        normalized_name = _name(name)
        batch = self.get(import_id) if import_id is not None else self.get_by_name(normalized_name)
        selected_id = (
            batch.import_id
            if batch is not None
            else _as_uuid(import_id)
            if import_id is not None
            else uuid4()
        )
        batch = CalendarImport(
            import_id=selected_id,
            name=normalized_name,
            source_format=source_format,
            created_at=batch.created_at if batch is not None else timestamp,
            updated_at=timestamp,
        )
        selected_events = tuple(events)
        if len(selected_events) > self.max_events:
            raise ValueError(f"calendar imports are limited to {self.max_events} events")
        keys: set[str] = set()
        for event in selected_events:
            if event.import_id != batch.import_id:
                raise ValueError("calendar event belongs to a different import batch")
            if event.stable_key in keys:
                raise ValueError("calendar import stable keys must be unique")
            keys.add(event.stable_key)
        with self.database.session() as session:
            row = session.get(HubCalendarImportRow, str(batch.import_id))
            if row is None:
                row = HubCalendarImportRow(import_id=str(batch.import_id))
                session.add(row)
            row.name = batch.name
            row.source_format = batch.source_format
            row.created_at = _db_time(batch.created_at)
            row.updated_at = _db_time(batch.updated_at)
            existing_rows = session.scalars(
                select(HubImportedCalendarEventRow).where(
                    HubImportedCalendarEventRow.import_id == str(batch.import_id)
                )
            ).all()
            if remove_missing:
                for existing in existing_rows:
                    if existing.stable_key not in keys:
                        session.delete(existing)
            for event in selected_events:
                event_row = session.scalar(
                    select(HubImportedCalendarEventRow).where(
                        HubImportedCalendarEventRow.import_id == str(batch.import_id),
                        HubImportedCalendarEventRow.stable_key == event.stable_key,
                    )
                )
                if event_row is None:
                    event_row = HubImportedCalendarEventRow(event_id=str(event.event_id))
                    session.add(event_row)
                event_row.event_id = str(event.event_id)
                event_row.import_id = str(event.import_id)
                event_row.stable_key = event.stable_key
                event_row.title = event.title
                event_row.starts_at = _db_time(event.starts_at)
                event_row.ends_at = _db_time(event.ends_at)
                event_row.all_day = event.all_day
                event_row.timezone = event.timezone
                event_row.location = event.location
                event_row.description = event.description
                event_row.deep_link = event.deep_link
                event_row.created_at = _db_time(event.created_at)
                event_row.updated_at = _db_time(event.updated_at)
            session.flush()
            saved = session.scalars(
                select(HubImportedCalendarEventRow)
                .where(HubImportedCalendarEventRow.import_id == str(batch.import_id))
                .order_by(
                    HubImportedCalendarEventRow.starts_at,
                    HubImportedCalendarEventRow.title,
                    HubImportedCalendarEventRow.event_id,
                )
            ).all()
            return batch, tuple(self._event_contract(row) for row in saved)

    save_import = replace_import

    def delete_import(self, import_id: UUID | str) -> bool:
        identifier = str(_as_uuid(import_id))
        with self.database.session() as session:
            row = session.get(HubCalendarImportRow, identifier)
            if row is None:
                return False
            session.execute(
                delete(HubImportedCalendarEventRow).where(
                    HubImportedCalendarEventRow.import_id == identifier
                )
            )
            session.delete(row)
            return True

    remove_import = delete_import

    @staticmethod
    def _import_contract(row: HubCalendarImportRow) -> CalendarImport:
        created_at = _contract_time(row.created_at)
        updated_at = _contract_time(row.updated_at)
        assert created_at is not None
        assert updated_at is not None
        return CalendarImport(
            import_id=UUID(row.import_id),
            name=row.name,
            source_format=cast(LocalCalendarImportFormat, row.source_format),
            created_at=created_at,
            updated_at=updated_at,
        )

    @staticmethod
    def _event_contract(row: HubImportedCalendarEventRow) -> ImportedCalendarEvent:
        created_at = _contract_time(row.created_at)
        updated_at = _contract_time(row.updated_at)
        starts_at = _contract_time(row.starts_at)
        ends_at = _contract_time(row.ends_at)
        assert created_at is not None
        assert updated_at is not None
        assert starts_at is not None
        assert ends_at is not None
        return ImportedCalendarEvent(
            event_id=UUID(row.event_id),
            import_id=UUID(row.import_id),
            stable_key=row.stable_key,
            title=row.title,
            starts_at=starts_at,
            ends_at=ends_at,
            all_day=row.all_day,
            timezone=row.timezone,
            location=row.location or "",
            description=row.description or "",
            deep_link=row.deep_link or "",
            created_at=created_at,
            updated_at=updated_at,
        )


def _as_uuid(value: UUID | str) -> UUID:
    if isinstance(value, UUID):
        return value
    if type(value) is str:
        try:
            return UUID(value)
        except ValueError as error:
            raise ValueError("calendar import identifier must be a UUID") from error
    raise TypeError("calendar import identifier must be a UUID")


LocalCalendarEvent = ImportedCalendarEvent
LocalCalendarImport = CalendarImport
LocalCalendarRepository = CalendarImportRepository


__all__ = [
    "CalendarImport",
    "CalendarImportRepository",
    "LOCAL_IMPORT_PROVIDER_ID",
    "LOCAL_IMPORT_SOURCE_ID",
    "LocalCalendarEvent",
    "LocalCalendarImport",
    "LocalCalendarImportFormat",
    "LocalCalendarRepository",
    "ImportedCalendarEvent",
]
