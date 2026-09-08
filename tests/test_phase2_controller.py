from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from personal_hub.bootstrap import build_hub_container
from personal_hub.config import HubSettings
from personal_hub.tasks import Recurrence


def test_controller_task_workflow_projects_today_and_recurring_next(tmp_path: Path):
    container = build_hub_container(HubSettings(data_dir=tmp_path / "hub"))
    controller = container.controller
    now = datetime(2026, 8, 31, 12, tzinfo=UTC)
    try:
        created = controller.create_task(
            title="Prepare launch",
            notes="Private local details",
            due_at=now + timedelta(hours=1),
            timezone="UTC",
            priority="high",
            tags=("launch",),
            recurrence=Recurrence(frequency="daily"),
            reminder_offsets_minutes=(0,),
            created_at=now,
            updated_at=now,
        )
        edited = controller.update_task(created.task_id, title="Prepare release")
        assert edited is not None and edited.title == "Prepare release"

        projection = controller.today_projection(now=now)
        assert [item.title for item in projection.today] == ["Prepare release"]

        completed, following = controller.complete_task(
            str(created.task_id),
            completed_at=now + timedelta(hours=2),
        )
        assert completed is not None and completed.state == "completed"
        assert following is not None and following.due_at == created.due_at + timedelta(days=1)

        archived = controller.archive_task(str(following.task_id), at=now + timedelta(hours=3))
        assert archived is not None and archived.state == "archived"
        restored = controller.undo_task(str(following.task_id), at=now + timedelta(hours=4))
        assert restored is not None and restored.state == "open"

        audit_payload = " ".join(
            event.model_dump_json() for event in controller.audit_events(limit=20)
        )
        assert "Private local details" not in audit_payload
        assert "Prepare release" not in audit_payload
    finally:
        container.close()


@pytest.mark.asyncio
async def test_controller_reminder_inbox_and_timezone_setting_survive_restart(tmp_path: Path):
    settings = HubSettings(data_dir=tmp_path / "hub")
    now = datetime(2026, 8, 31, 12, tzinfo=UTC)
    container = build_hub_container(settings)
    try:
        task = container.controller.create_task(
            title="Due reminder",
            due_at=now,
            timezone="UTC",
            reminder_offsets_minutes=(0,),
            created_at=now - timedelta(hours=1),
            updated_at=now - timedelta(hours=1),
        )
        first = await container.controller.reconcile_task_reminders(now=now + timedelta(minutes=1))
        second = await container.controller.reconcile_task_reminders(now=now + timedelta(minutes=1))
        assert len(first) == 1
        assert first[0].navigation_target == f"hub://tasks/{task.task_id}"
        assert second == ()
        container.controller.update_settings(default_timezone="America/Chicago")
        assert container.controller.today_projector.timezone.key == "America/Chicago"
    finally:
        container.close()

    restarted = build_hub_container(settings)
    try:
        assert restarted.controller.settings.default_timezone == "America/Chicago"
        assert restarted.controller.today_projector.timezone.key == "America/Chicago"
        assert (
            await restarted.controller.reconcile_task_reminders(now=now + timedelta(minutes=1))
            == ()
        )
        assert restarted.controller.unread_inbox_count() == 1
    finally:
        restarted.close()
