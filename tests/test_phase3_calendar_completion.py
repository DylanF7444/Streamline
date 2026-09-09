"""Calendar deletion reconciliation and retired completion-state coverage."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from personal_hub.bootstrap import build_hub_container
from personal_hub.calendar import (
    CalendarEvent,
    CalendarQuery,
    CalendarSnapshot,
    CalendarStatus,
)
from personal_hub.config import HubSettings
from personal_hub.contracts import SourceRef
from personal_hub.storage.database import (
    HubCalendarEventStateRow,
    HubDatabase,
    HubSchemaVersionRow,
)

NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)


def _event() -> CalendarEvent:
    return CalendarEvent(
        event_id="event-1",
        calendar_id="primary",
        title="Planning session",
        starts_at=NOW,
        ends_at=NOW + timedelta(hours=1),
        timezone="UTC",
        source=SourceRef(provider_id="google-calendar", display_name="Google Calendar"),
    )


def _query() -> CalendarQuery:
    return CalendarQuery(
        starts_at=NOW - timedelta(days=1),
        ends_at=NOW + timedelta(days=2),
        calendar_ids=("primary",),
        timezone="UTC",
    )


def _snapshot(
    events: tuple[CalendarEvent, ...],
    *,
    collected_at: datetime = NOW,
) -> CalendarSnapshot:
    return CalendarSnapshot(
        source_id="google-calendar",
        provider_id="google-calendar",
        collected_at=collected_at,
        events=events,
        selected_calendar_ids=("primary",),
    )


class _SequenceSource:
    source_id = "google-calendar"
    provider_id = "google-calendar"
    display_name = "Google Calendar"

    def __init__(self) -> None:
        self.snapshots = [
            _snapshot((_event(),)),
            _snapshot((), collected_at=NOW + timedelta(hours=1)),
        ]

    async def status(self) -> CalendarStatus:
        return CalendarStatus(
            source_id=self.source_id,
            provider_id=self.provider_id,
            state="ready",
            checked_at=NOW,
        )

    async def list_events(self, _query: CalendarQuery) -> CalendarSnapshot:
        return self.snapshots.pop(0)


@pytest.mark.asyncio
async def test_successful_refresh_removes_google_event_deleted_at_provider(tmp_path: Path) -> None:
    container = build_hub_container(HubSettings(data_dir=tmp_path / "hub"))
    source = _SequenceSource()
    try:
        container.controller.register_calendar_source(source)
        first = await container.controller.refresh_calendars(_query())
        assert [event.event_id for event in first[source.source_id].events] == ["event-1"]

        second = await container.controller.refresh_calendars(_query())
        assert second[source.source_id].state == "empty"
        assert second[source.source_id].events == ()
        assert container.controller.calendar_results()[0].events == ()
        assert container.controller.calendar_today_items(day=NOW.date(), timezone="UTC") == ()
    finally:
        container.close()


def test_calendar_event_completion_commands_are_not_available(tmp_path: Path) -> None:
    container = build_hub_container(HubSettings(data_dir=tmp_path / "hub"))
    try:
        for name in (
            "complete_calendar_event",
            "set_calendar_event_completed",
            "mark_calendar_event_done",
            "uncomplete_calendar_event",
        ):
            assert not hasattr(container.controller, name)
        export_path = container.controller.export_local_data(tmp_path / "export.json")
        assert "calendar_event_completions" not in json.loads(export_path.read_text("utf-8"))
    finally:
        container.close()


def test_schema_migration_removes_legacy_calendar_completion_rows(tmp_path: Path) -> None:
    path = tmp_path / "hub.sqlite"
    database = HubDatabase(path)
    database.migrate()
    with database.session() as session:
        session.add(
            HubCalendarEventStateRow(
                source_id="google-calendar",
                calendar_id="primary",
                event_id="event-1",
                provider_id="google-calendar",
                state="completed",
                completed_at=NOW,
                event_starts_at=NOW,
                event_ends_at=NOW + timedelta(hours=1),
                last_seen_at=NOW,
                updated_at=NOW,
            )
        )
        version = session.get(HubSchemaVersionRow, 1)
        assert version is not None
        version.version = 8
    database.close()

    migrated = HubDatabase(path)
    migrated.migrate()
    try:
        with migrated.session() as session:
            assert (
                session.get(
                    HubCalendarEventStateRow,
                    ("google-calendar", "primary", "event-1"),
                )
                is None
            )
            version = session.get(HubSchemaVersionRow, 1)
            assert version is not None
            assert version.version == HubDatabase.CURRENT_SCHEMA_VERSION
        with sqlite3.connect(migrated.path) as connection:
            task_columns = {row[1] for row in connection.execute("PRAGMA table_info(hub_tasks)")}
        assert "external_url" in task_columns
    finally:
        migrated.close()
