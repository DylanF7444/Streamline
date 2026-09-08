from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from personal_hub.config import HubSettings
from personal_hub.contracts import InboxItem
from personal_hub.controller import HubController
from personal_hub.sources import SourceRegistry, SourceService, SourceSnapshot
from personal_hub.storage.database import HubDatabase
from personal_hub.storage.repositories import SettingsRepository

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


class FakeSource:
    source_id = "calendar"
    provider_id = "fake-calendar"
    display_name = "Calendar"

    def __init__(self, *values: SourceSnapshot | BaseException, delay: float = 0.0) -> None:
        self.values = list(values)
        self.delay = delay
        self.calls: list[str] = []
        self.started = asyncio.Event()

    async def connect(self) -> None:
        self.calls.append("connect")

    async def status(self):
        self.calls.append("status")
        return {"state": "ready", "checked_at": NOW}

    async def refresh(self) -> SourceSnapshot:
        self.calls.append("refresh")
        self.started.set()
        if self.delay:
            await asyncio.sleep(self.delay)
        value = self.values.pop(0) if self.values else _snapshot("calendar", "item")
        if isinstance(value, BaseException):
            raise value
        return value

    async def disconnect(self) -> None:
        self.calls.append("disconnect")


def _snapshot(source_id: str, *items: Any) -> SourceSnapshot:
    return SourceSnapshot(
        source_id=source_id,
        provider_id="fake-calendar",
        collected_at=NOW,
        items=items,
    )


def _controller(
    tmp_path: Path,
    source: FakeSource,
) -> tuple[HubController, HubDatabase]:
    settings = HubSettings(profile="testing", data_dir=tmp_path / "hub")
    database = HubDatabase(settings.database_path)
    database.migrate()
    registry = SourceRegistry()
    registry.register(
        source,
        source_id=source.source_id,
        provider_id=source.provider_id,
        display_name=source.display_name,
    )
    service = SourceService(registry)
    return HubController(settings=settings, database=database, source_service=service), database


@pytest.mark.asyncio
async def test_offline_setting_is_durable_and_gates_provider_calls(tmp_path: Path):
    source = FakeSource()
    controller, database = _controller(tmp_path, source)
    try:
        updated = controller.set_offline_mode(True)
        assert updated.offline_mode is True
        result = await controller.refresh_source(source.source_id)

        assert result.state.value == "offline"
        assert source.calls == []
        assert SettingsRepository(database).load_settings(defaults=updated).offline_mode is True
    finally:
        await controller.shutdown()
        database.close()


@pytest.mark.asyncio
async def test_refresh_persists_only_safe_health_and_deduplicates_failure_inbox(tmp_path: Path):
    secret_title = "private event title should stay in memory"
    secret_error = "oauth_token=private-token body=private message"
    source = FakeSource(
        _snapshot("calendar", {"title": secret_title}),
        RuntimeError(secret_error),
        RuntimeError(secret_error),
    )
    controller, database = _controller(tmp_path, source)
    try:
        ready = await controller.refresh_source(source.source_id)
        first_failure = await controller.refresh_source(source.source_id)
        second_failure = await controller.refresh_source(source.source_id)

        assert ready.state.value == "ready"
        assert first_failure.state.value == "stale"
        assert second_failure.state.value == "stale"
        health = controller.source_sync_state(source.source_id)
        assert health is not None
        assert health.freshness.state == "stale"
        assert health.failure_count == 2
        assert len(controller.list_inbox()) == 1
        assert controller.list_inbox()[0].kind == "source_failure"
        assert len(controller.audit_events()) == 3
        assert all("private" not in repr(event.metadata) for event in controller.audit_events())
        raw_database = database.path.read_bytes()
        assert secret_title.encode() not in raw_database
        assert secret_error.encode() not in raw_database

        payload = controller.diagnostics_json(generated_at=NOW)
        assert secret_title not in payload
        assert secret_error not in payload
        assert "stale" in payload
    finally:
        await controller.shutdown()
        database.close()


def test_inbox_state_transitions_and_source_projections_are_controller_friendly(tmp_path: Path):
    controller, database = _controller(tmp_path, FakeSource(RuntimeError("failure")))
    try:
        assert [source.source_id for source in controller.list_sources()] == ["calendar"]
        assert controller.list_source_results() == ()
        item = controller.inbox_repository.add(
            InboxItem(
                kind="system",
                title="Safe item",
                safe_summary="A safe summary.",
                reason="test",
                navigation_target="hub://today",
                deduplication_key="test:item",
            )
        )

        assert controller.mark_inbox_read(item.item_id) is not None
        assert controller.list_inbox()[0].state == "read"
        assert controller.dismiss_inbox(item.item_id) is not None
        assert controller.archive_inbox(item.item_id) is not None
        assert controller.unread_inbox_count() == 0
    finally:
        controller.close()
        database.close()


@pytest.mark.asyncio
async def test_source_enabled_state_survives_restart_and_skips_scheduled_refresh(tmp_path: Path):
    first_source = FakeSource()
    first, first_database = _controller(tmp_path, first_source)
    try:
        first.set_source_enabled(first_source.source_id, False)
        assert first.list_sources()[0].enabled is False
    finally:
        first.close()
        first_database.close()

    second_source = FakeSource()
    second, second_database = _controller(tmp_path, second_source)
    try:
        assert second.list_sources()[0].enabled is False
        assert await second.refresh_sources() == {}
        assert second_source.calls == []
    finally:
        second.close()
        second_database.close()


@pytest.mark.asyncio
async def test_shutdown_cancels_inflight_refresh_without_closing_external_database(tmp_path: Path):
    source = FakeSource(delay=30)
    controller, database = _controller(tmp_path, source)
    task = asyncio.create_task(controller.refresh_source(source.source_id))
    await source.started.wait()

    await controller.shutdown()

    assert task.cancelled()
    assert controller.is_closed
    assert not database._closed
    database.close()


def test_backup_and_restore_are_exposed_without_content_export(tmp_path: Path):
    controller, database = _controller(tmp_path, FakeSource())
    try:
        backup = controller.backup_database(tmp_path / "backup" / "hub.sqlite")
        assert backup.is_file()
        controller.set_offline_mode(True)
        assert controller.restore_database(backup) == database.path
        assert controller.settings.offline_mode is False
        controller.create_task(title="Exported local task")
        exported = controller.export_local_data(
            tmp_path / "exports" / "hub.json",
            exported_at=NOW,
        )
        payload = json.loads(exported.read_text(encoding="utf-8"))
        assert payload["format"] == "personal-hub-local-export-v1"
        assert payload["tasks"][0]["title"] == "Exported local task"
        assert "data_dir" not in payload["settings"]
        assert "credentials" not in payload
        assert "message_bodies" not in payload
    finally:
        controller.close()
        database.close()
