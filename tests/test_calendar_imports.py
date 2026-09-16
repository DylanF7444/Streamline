"""Durable controller coverage for local bulk imports."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from personal_hub.bootstrap import build_hub_container
from personal_hub.calendar.local import LOCAL_IMPORT_SOURCE_ID
from personal_hub.config import HubSettings


def _settings(tmp_path: Path) -> HubSettings:
    return HubSettings(data_dir=tmp_path / "hub")


def test_task_import_is_deterministic_and_preserves_completion(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    container = build_hub_container(settings)
    try:
        first = container.controller.import_tasks_from_csv(
            "id,title,list,tags\n42,Prepare release,Work,Release\n",
            import_name="Release plan",
        )
        assert first.imported_count == 1
        assert first.updated_count == 0
        task = container.controller.list_tasks()[0]
        assert task.title == "Prepare release"
        assert task.tags == ("release",)
        assert container.controller.list_task_lists()[0].name == "Work"

        container.controller.complete_task(task.task_id)
        second = container.controller.import_tasks_from_csv(
            "id,title,list,tags\n42,Prepare final release,Work,Release\n",
            import_name="Release plan",
        )
        revised = container.controller.get_task(task.task_id)
        assert second.updated_count == 1
        assert revised is not None
        assert revised.title == "Prepare final release"
        assert revised.state == "completed"
    finally:
        container.close()


def test_calendar_import_is_named_batch_persistent_and_projected_to_today(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    content = (
        "id,title,start_date,start_time,end_date,end_time,timezone\n"
        "event-1,Planning,2026-09-14,10:00,2026-09-14,11:00,UTC\n"
    )
    container = build_hub_container(settings)
    try:
        first = container.controller.import_calendar_from_csv(content, import_name="Project events")
        assert first.imported_count == 1
        assert first.import_id is not None
        batches = container.controller.list_calendar_imports()
        assert len(batches) == 1
        batch_id = batches[0].import_id
        assert str(batch_id) == first.import_id
        assert container.controller.count_imported_calendar_events(batch_id) == 1
        exported = container.controller.export_local_data(tmp_path / "export.json")
        export_payload = json.loads(exported.read_text(encoding="utf-8"))
        assert export_payload["calendar_imports"][0]["name"] == "Project events"
        assert export_payload["imported_calendar_events"][0]["title"] == "Planning"

        second = container.controller.import_calendar_from_csv(
            content + "event-2,Demo,2026-09-15,13:00,2026-09-15,14:00,UTC\n",
            import_name="Project events",
        )
        assert second.import_id == first.import_id
        assert second.imported_count == 1
        assert second.updated_count == 1
        assert container.controller.count_imported_calendar_events(batch_id) == 2
        today = container.controller.calendar_today_items(
            day=date(2026, 9, 14),
            timezone="UTC",
            selected_calendar_keys=(f"{LOCAL_IMPORT_SOURCE_ID}/{batch_id}",),
        )
        assert [item.title for item in today] == ["Planning"]
    finally:
        container.close()

    reopened = build_hub_container(settings)
    try:
        assert len(reopened.controller.list_calendar_imports()) == 1
        assert reopened.controller.count_imported_calendar_events(batch_id) == 2
        assert reopened.controller.delete_calendar_import(batch_id)
        assert reopened.controller.list_calendar_imports() == []
        assert reopened.controller.list_imported_calendar_events(batch_id) == []
    finally:
        reopened.close()
