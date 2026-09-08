from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from personal_hub.bootstrap import build_hub_container
from personal_hub.calendar.contracts import (
    CalendarEvent,
    CalendarPermissionRequiredError,
    CalendarQuery,
    CalendarSnapshot,
    CalendarStatus,
)
from personal_hub.config import HubSettings
from personal_hub.contracts import SourceRef


class _CalendarSource:
    provider_id = "test-calendar"

    def __init__(self, source_id: str, calendar_id: str, title: str) -> None:
        self.source_id = source_id
        self.calendar_id = calendar_id
        self.display_name = title
        self.calls = 0
        self.failure: BaseException | None = None
        self.queries: list[CalendarQuery] = []

    async def status(self) -> CalendarStatus:
        return CalendarStatus(
            source_id=self.source_id,
            provider_id=self.provider_id,
            state="ready",
            checked_at=datetime.now(UTC),
        )

    async def list_events(self, query: CalendarQuery) -> CalendarSnapshot:
        self.calls += 1
        self.queries.append(query)
        if self.failure is not None:
            raise self.failure
        starts_at = datetime(2026, 8, 31, 15, tzinfo=UTC)
        event = CalendarEvent(
            event_id=f"event-{self.source_id}",
            calendar_id=self.calendar_id,
            title=self.display_name,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=1),
            timezone="UTC",
            source=SourceRef(
                provider_id=self.provider_id,
                connection_id=self.source_id,
                resource_id=self.calendar_id,
                display_name=self.display_name,
            ),
            deep_link=f"https://calendar.example/{self.source_id}",
        )
        return CalendarSnapshot(
            source_id=self.source_id,
            provider_id=self.provider_id,
            collected_at=datetime.now(UTC),
            events=(event,),
            selected_calendar_ids=(self.calendar_id,),
        )


def _query() -> CalendarQuery:
    return CalendarQuery(
        starts_at=datetime(2026, 8, 31, tzinfo=UTC),
        ends_at=datetime(2026, 9, 2, tzinfo=UTC),
        timezone="UTC",
    )


@pytest.mark.asyncio
async def test_controller_refreshes_account_scoped_calendars_and_projects_today(
    tmp_path: Path,
) -> None:
    container = build_hub_container(HubSettings(data_dir=tmp_path / "hub"))
    work = _CalendarSource("account.work", "work", "Work meeting")
    home = _CalendarSource("account.home", "home", "Home appointment")
    try:
        container.controller.register_calendar_source(work)
        container.controller.register_calendar_source(home)
        base = _query()
        results = await container.controller.refresh_calendars(
            base,
            queries_by_source={
                work.source_id: base.model_copy(update={"calendar_ids": ("work",)}),
                home.source_id: base.model_copy(update={"calendar_ids": ("home",)}),
            },
        )

        assert set(results) == {work.source_id, home.source_id}
        assert work.queries[0].calendar_ids == ("work",)
        assert home.queries[0].calendar_ids == ("home",)
        selected = container.controller.calendar_today_items(
            day=date(2026, 8, 31),
            timezone="UTC",
            selected_calendar_keys=("account.work/work",),
        )
        assert [item.title for item in selected] == ["Work meeting"]
        assert selected[0].navigation_target == "https://calendar.example/account.work"
        selected_source = container.controller.calendar_today_items(
            day=date(2026, 8, 31),
            timezone="UTC",
            selected_calendar_keys=("account.work/*",),
        )
        assert [item.title for item in selected_source] == ["Work meeting"]

        diagnostic_payload = container.controller.diagnostics_report().model_dump_json()
        audit_payload = " ".join(
            event.model_dump_json() for event in container.controller.audit_events(limit=100)
        )
        assert "Work meeting" not in diagnostic_payload
        assert "Home appointment" not in diagnostic_payload
        assert "Work meeting" not in audit_payload
        assert "Home appointment" not in audit_payload
    finally:
        container.close()


@pytest.mark.asyncio
async def test_controller_calendar_offline_stale_and_permission_states_are_distinct(
    tmp_path: Path,
) -> None:
    container = build_hub_container(HubSettings(data_dir=tmp_path / "hub"))
    source = _CalendarSource("account.work", "work", "Private event title")
    try:
        container.controller.register_calendar_source(source)
        assert (await container.controller.refresh_calendars(_query()))[source.source_id].state == (
            "ready"
        )

        container.controller.set_offline_mode(True)
        offline = (await container.controller.refresh_calendars(_query()))[source.source_id]
        assert offline.state == "offline"
        assert source.calls == 1
        assert offline.snapshot is not None

        container.controller.set_offline_mode(False)
        source.failure = RuntimeError("private event title from provider")
        stale = (await container.controller.refresh_calendars(_query()))[source.source_id]
        assert stale.state == "stale"
        assert stale.snapshot is not None
        assert "private" not in stale.safe_message.casefold()

        source.failure = CalendarPermissionRequiredError("private token response")
        permission = (await container.controller.refresh_calendars(_query()))[source.source_id]
        assert permission.state == "permission_required"
        assert permission.safe_message == "Calendar permission is required."
    finally:
        container.close()
