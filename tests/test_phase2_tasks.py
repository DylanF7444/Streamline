"""Phase 2 task contracts, recurrence, and repository tests."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from time import perf_counter
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from personal_hub.storage.database import (
    HubAuditEventRow,
    HubBase,
    HubDatabase,
    HubInboxItemRow,
    HubNotificationDeliveryRow,
    HubSchemaVersionRow,
    HubSettingRow,
    HubSourceConnectionRow,
    HubSourceSyncStateRow,
    HubTaskRow,
)
from personal_hub.tasks import (
    HubTask,
    Recurrence,
    TaskRepository,
    next_due_at,
)


def _database(tmp_path: Path) -> HubDatabase:
    database = HubDatabase(tmp_path / "hub.db")
    database.migrate()
    return database


def test_v2_hub_database_restores_and_migrates_to_task_schema(tmp_path: Path) -> None:
    legacy_path = tmp_path / "legacy-v2.db"
    legacy = HubDatabase(legacy_path)
    try:
        HubBase.metadata.create_all(
            legacy.engine,
            tables=[
                HubSchemaVersionRow.__table__,
                HubSettingRow.__table__,
                HubSourceConnectionRow.__table__,
                HubSourceSyncStateRow.__table__,
                HubInboxItemRow.__table__,
                HubAuditEventRow.__table__,
                HubNotificationDeliveryRow.__table__,
            ],
        )
        with legacy.session() as session:
            session.add(HubSchemaVersionRow(id=1, version=2))
    finally:
        legacy.close()

    active = _database(tmp_path / "active")
    try:
        active.restore(legacy_path)
        assert active.integrity_check()
        with sqlite3.connect(active.path) as connection:
            version = connection.execute(
                "SELECT version FROM hub_schema_migrations WHERE id = 1"
            ).fetchone()[0]
            task_tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
        assert version == HubDatabase.CURRENT_SCHEMA_VERSION
        assert {"hub_tasks", "hub_task_lists", "hub_task_tags", "hub_task_occurrences"} <= (
            task_tables
        )
        assert TaskRepository(active).create(title="after restore") is not None
    finally:
        active.close()


def test_task_contract_is_strict_and_recurrence_is_bounded() -> None:
    now = datetime(2026, 8, 31, 12, tzinfo=UTC)
    task = HubTask(
        title="Prepare release",
        notes="Keep the notes local.",
        external_url="https://canvas.example.test/courses/10/assignments/101",
        due_at=now,
        tags=("Work", "release"),
        reminder_offsets_minutes=(60, 0),
        recurrence=Recurrence(frequency="weekly", weekdays=(0, 4)),
    )
    assert task.tags == ("release", "work")
    assert task.reminder_offsets_minutes == (0, 60)
    assert task.recurrence is not None
    assert task.external_url == "https://canvas.example.test/courses/10/assignments/101"
    with pytest.raises(ValidationError):
        HubTask(title="bad", due_at=datetime(2026, 8, 31, 12))
    with pytest.raises(ValidationError):
        HubTask(title="bad", unexpected="value")
    with pytest.raises(ValidationError):
        HubTask(title="bad", due_at=now, tags=("../outside",))
    with pytest.raises(ValidationError):
        HubTask(title="bad", external_url="javascript:alert(1)")
    with pytest.raises(ValidationError):
        HubTask(title="bad", external_url="https://user:password@example.test/task")
    with pytest.raises(ValidationError):
        Recurrence(frequency="daily", weekdays=(1,))
    with pytest.raises(ValidationError):
        Recurrence(frequency="weekly", weekdays=(7,))
    with pytest.raises(ValidationError):
        Recurrence(frequency="daily", interval=0)


def test_recurrence_arithmetic_supports_bounded_patterns_and_dst() -> None:
    chicago = "America/Chicago"
    spring_before = datetime(2026, 3, 7, 15, tzinfo=UTC)
    daily = next_due_at(spring_before, Recurrence(frequency="daily"), timezone=chicago)
    assert daily is not None
    assert daily.astimezone(ZoneInfo(chicago)).hour == 9

    monday = datetime(2026, 8, 31, 14, tzinfo=UTC)
    weekly = next_due_at(
        monday,
        Recurrence(frequency="weekly", weekdays=(0,)),
        timezone=chicago,
    )
    assert weekly == datetime(2026, 9, 7, 14, tzinfo=UTC)

    month_end = next_due_at(
        datetime(2026, 1, 31, 15, tzinfo=UTC),
        Recurrence(frequency="monthly"),
        timezone="UTC",
    )
    assert month_end == datetime(2026, 2, 28, 15, tzinfo=UTC)
    month_end_again = next_due_at(
        month_end,
        Recurrence(frequency="monthly"),
        timezone="UTC",
        anchor_due_at=datetime(2026, 1, 31, 15, tzinfo=UTC),
    )
    assert month_end_again == datetime(2026, 3, 31, 15, tzinfo=UTC)

    bounded = Recurrence(frequency="daily", max_occurrences=2)
    assert next_due_at(spring_before, bounded, occurrence_number=2) is None


def test_task_crud_lists_tags_filters_and_restart_persistence(tmp_path: Path) -> None:
    database = _database(tmp_path)
    due = datetime(2026, 8, 31, 13, tzinfo=UTC)
    repository = TaskRepository(database)
    task_list = repository.lists.create(
        "Work", list_id=UUID("00000000-0000-0000-0000-000000000010")
    )
    task = repository.create(
        title="Ship task storage",
        notes="Verify migration and restart.",
        external_url="https://canvas.example.test/courses/10/assignments/202",
        priority="high",
        due_at=due,
        list_id=task_list.list_id,
        tags=("phase2", "work"),
        reminder_offsets_minutes=(15, 0),
        created_at=due - timedelta(days=1),
        updated_at=due - timedelta(days=1),
    )
    old_updated_at = task.updated_at
    assert task.external_url == "https://canvas.example.test/courses/10/assignments/202"
    edited = repository.edit(task.task_id, title="Ship task storage safely", priority="normal")
    assert edited is not None
    assert edited.title == "Ship task storage safely"
    assert edited.updated_at > old_updated_at
    assert repository.list(list_id=task_list.list_id, tags=("work",), priority="normal") == [edited]
    assert repository.list(search="storage", state="open") == [edited]

    database.close()
    restarted = _database(tmp_path)
    try:
        repository = TaskRepository(restarted)
        restored = repository.get(task.task_id)
        assert restored == edited
        completed = repository.complete(task.task_id, completed_at=due)
        assert completed is not None and completed.state == "completed"
        undone = repository.undo(task.task_id, at=due + timedelta(minutes=1))
        assert undone is not None and undone.state == "open"
        archived = repository.archive(task.task_id, at=due + timedelta(minutes=2))
        assert archived is not None and archived.state == "archived"
        unarchived = repository.unarchive(task.task_id, at=due + timedelta(minutes=3))
        assert unarchived is not None and unarchived.state == "open"
    finally:
        restarted.close()


def test_recurring_completion_creates_one_next_occurrence_across_restarts(tmp_path: Path) -> None:
    database = _database(tmp_path)
    first_due = datetime(2026, 1, 1, 9, tzinfo=UTC)
    repository = TaskRepository(database)
    task = repository.create(
        title="Review recurring report",
        due_at=first_due,
        recurrence=Recurrence(frequency="daily", max_occurrences=4),
        created_at=first_due,
        updated_at=first_due,
    )
    completed = repository.complete(task.task_id, completed_at=first_due)
    first_next = repository.next_occurrence(task.task_id)
    assert completed is not None and completed.state == "completed"
    assert first_next is not None and first_next.due_at == first_due + timedelta(days=1)
    assert len(repository.list()) == 2
    database.close()

    database = _database(tmp_path)
    try:
        repository = TaskRepository(database)
        assert first_next is not None
        second_completed = repository.complete(
            first_next.task_id,
            completed_at=first_due + timedelta(days=1),
        )
        second_next = repository.next_occurrence(first_next.task_id)
        assert second_completed is not None and second_completed.state == "completed"
        assert second_next is not None
        assert len(repository.list()) == 3

        # Retrying the same completion after its transaction has committed must
        # not add a second task or occurrence row.
        repository.complete(first_next.task_id, completed_at=first_due + timedelta(days=1))
        assert len(repository.list()) == 3
        with sqlite3.connect(database.path) as connection:
            occurrence_count = connection.execute(
                "SELECT COUNT(*) FROM hub_task_occurrences"
            ).fetchone()[0]
        assert occurrence_count == 3
    finally:
        database.close()

    database = _database(tmp_path)
    try:
        repository = TaskRepository(database)
        assert second_next is not None
        third_completed = repository.complete(
            second_next.task_id,
            completed_at=first_due + timedelta(days=2),
        )
        assert third_completed is not None and third_completed.state == "completed"
        assert repository.next_occurrence(second_next.task_id) is not None
        final_next = repository.next_occurrence(second_next.task_id)
        assert final_next is not None
        repository.complete(final_next.task_id, completed_at=first_due + timedelta(days=3))
        assert repository.next_occurrence(final_next.task_id) is None
        assert len(repository.list(state="completed")) == 4
        assert len(repository.list(state="open")) == 0
    finally:
        database.close()


def test_concurrent_recurring_completion_is_idempotent(tmp_path: Path) -> None:
    database = _database(tmp_path)
    due = datetime(2026, 4, 1, 9, tzinfo=UTC)
    task = TaskRepository(database).create(
        title="Concurrent recurring task",
        due_at=due,
        recurrence=Recurrence(frequency="daily"),
        created_at=due,
        updated_at=due,
    )
    database.close()
    first_database = _database(tmp_path)
    second_database = _database(tmp_path)
    try:
        first = TaskRepository(first_database)
        second = TaskRepository(second_database)

        def complete(repository: TaskRepository) -> HubTask | None:
            return repository.complete(task.task_id, completed_at=due)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(complete, (first, second)))
        assert all(result is not None and result.state == "completed" for result in results)
        assert len(first.list()) == 2
        assert first.next_occurrence(task.task_id) is not None
        with sqlite3.connect(first_database.path) as connection:
            assert (
                connection.execute("SELECT COUNT(*) FROM hub_task_occurrences").fetchone()[0] == 2
            )
    finally:
        first_database.close()
        second_database.close()


def test_undo_recurring_completion_removes_only_its_generated_open_next(tmp_path: Path) -> None:
    database = _database(tmp_path)
    try:
        repository = TaskRepository(database)
        due = datetime(2026, 2, 1, 9, tzinfo=UTC)
        task = repository.create(
            title="Undo recurring task",
            due_at=due,
            recurrence=Recurrence(frequency="weekly"),
            created_at=due,
            updated_at=due,
        )
        repository.complete(task.task_id, completed_at=due)
        generated = repository.next_occurrence(task.task_id)
        assert generated is not None
        repository.undo(task.task_id, at=due + timedelta(minutes=1))
        assert repository.get(generated.task_id) is None
        assert repository.list(state="open") == [repository.get(task.task_id)]
    finally:
        database.close()


def test_due_today_and_reminders_respect_task_timezone_and_dst(tmp_path: Path) -> None:
    database = _database(tmp_path)
    try:
        repository = TaskRepository(database)
        chicago = ZoneInfo("America/Chicago")
        local_due = datetime(2026, 3, 8, 9, tzinfo=chicago)
        timed = repository.create(
            title="DST timed task",
            due_at=local_due,
            timezone="America/Chicago",
            reminder_offsets_minutes=(15,),
        )
        all_day = repository.create(
            title="DST all day task",
            due_at=datetime(2026, 3, 8, 0, tzinfo=chicago),
            timezone="America/Chicago",
            due_is_all_day=True,
        )
        due_today = repository.due_today(timezone="America/Chicago", day=date(2026, 3, 8))
        assert {task.task_id for task in due_today} == {timed.task_id, all_day.task_id}
        reminders = repository.reminder_events(now=local_due - timedelta(minutes=15))
        assert [(task.task_id, offset) for task, offset in reminders] == [(timed.task_id, 15)]
    finally:
        database.close()


def test_completed_task_query_stays_bounded_with_ten_thousand_rows(tmp_path: Path) -> None:
    database = _database(tmp_path)
    try:
        now = datetime(2026, 8, 31, tzinfo=UTC)
        with database.session() as session:
            session.add_all(
                HubTaskRow(
                    task_id=str(uuid4()),
                    title=f"completed-{index}",
                    notes="",
                    state="completed",
                    priority="normal",
                    due_at=None,
                    due_is_all_day=False,
                    timezone="UTC",
                    list_id=None,
                    tags_json="[]",
                    recurrence_json=None,
                    reminder_offsets_json="[]",
                    created_at=now.replace(tzinfo=None),
                    updated_at=now.replace(tzinfo=None),
                    completed_at=now.replace(tzinfo=None),
                )
                for index in range(10_000)
            )
        repository = TaskRepository(database)
        started = perf_counter()
        values = repository.list(state="completed", limit=25)
        elapsed = perf_counter() - started
        assert len(values) == 25
        assert repository.count(state="completed") == 10_000
        assert elapsed < 1.0
    finally:
        database.close()
