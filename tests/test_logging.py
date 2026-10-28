"""Offline lifecycle, privacy, history and encrypted sync acceptance for Logging."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError

from personal_hub.config import HubSettings
from personal_hub.controller import HubController
from personal_hub.logging.contracts import ActivityEntry, LogFilter, ReflectionEntry
from personal_hub.logging.repository import ActivityRepository
from personal_hub.storage.database import HubDatabase
from personal_hub.sync import SyncRecord, SyncSnapshot, decrypt_snapshot, merge_snapshots
from personal_hub.tasks import Recurrence

NOW = datetime(2026, 10, 28, 15, tzinfo=UTC)
PASSPHRASE = "private logging test passphrase"


@pytest.fixture
def hub(tmp_path):
    controller = HubController(settings=HubSettings(data_dir=tmp_path / "hub", offline_mode=True))
    yield controller
    controller.close()


def task(hub, **values):
    return hub.create_task(
        title="Original title",
        created_at=NOW - timedelta(days=2),
        updated_at=NOW - timedelta(days=2),
        **values,
    )


def test_completion_is_atomic_idempotent_and_survives_restart_and_cleanup(hub):
    task_list = hub.create_task_list("Work")
    item = task(hub, list_id=task_list.list_id, tags=("writing",), due_at=NOW - timedelta(days=1))
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: hub.complete_task(str(item.task_id), completed_at=NOW), range(8)))
    (entry,) = hub.list_activity()
    assert (entry.title_snapshot, entry.list_name, entry.tags) == (
        "Original title",
        "Work",
        ("writing",),
    )
    hub.update_task(str(item.task_id), title="New title")
    assert hub.list_activity()[0].description == "Original title"
    hub.update_settings(remove_completed_tasks_after_due_date=True)
    assert hub.cleanup_completed_tasks_after_due_date(now=NOW) == 1
    assert hub.list_activity() == [entry]
    hub.database.migrate()
    assert ActivityRepository(hub.database).list() == [entry]
    assert hub.activity_summary(LogFilter())["task_completion"] == 1


def test_undo_recomplete_creates_new_record_even_at_identical_time(hub):
    item = task(hub)
    hub.complete_task(str(item.task_id), completed_at=NOW)
    (first,) = hub.list_activity()
    hub.undo_task(str(item.task_id), at=NOW)
    assert hub.list_activity() == []
    (corrected,) = hub.list_activity(LogFilter(include_undone=True))
    assert corrected.entry_id == first.entry_id and corrected.undone_at == NOW
    assert hub.activity_summary(LogFilter(include_undone=True))["task_completion"] == 0
    hub.complete_task(str(item.task_id), completed_at=NOW)
    assert hub.list_activity()[0].entry_id != first.entry_id
    assert len(hub.list_activity(LogFilter(include_undone=True))) == 2


def test_recurring_occurrences_are_distinct_and_generic_reopen_corrects(hub):
    item = task(hub, due_at=NOW, recurrence=Recurrence(frequency="daily", max_occurrences=3))
    _, following = hub.complete_task(str(item.task_id), completed_at=NOW)
    hub.complete_task(str(item.task_id), completed_at=NOW)
    hub.complete_task(str(following.task_id), completed_at=NOW + timedelta(days=1))
    entries = hub.list_activity()
    assert len(entries) == 2
    assert len({entry.occurrence_key for entry in entries}) == 2
    hub.update_task(str(following.task_id), state="open")
    assert hub.activity_summary(LogFilter())["task_completion"] == 1


def test_completion_facts_are_immutable_and_notes_are_separate(hub):
    item = task(hub, notes="private task note")
    hub.complete_task(str(item.task_id), completed_at=NOW)
    (entry,) = hub.list_activity()
    assert entry.notes == ""
    revised = hub.update_activity(str(entry.entry_id), notes="Completion note")
    assert revised.occurred_at == NOW
    for changes in (
        {"description": "Changed"},
        {"occurred_at": NOW + timedelta(days=1)},
        {"task_id": None},
        {"duration_minutes": 12},
    ):
        with pytest.raises(ValueError):
            hub.update_activity(str(entry.entry_id), **changes)
    assert hub.get_task(str(item.task_id)).notes == "private task note"


def test_manual_capture_and_edit_never_change_open_task(hub):
    item = task(hub)
    entry = hub.log_progress(
        description="Outlined proposal",
        task_id=item.task_id,
        occurred_at=NOW,
        duration_minutes=25,
        tags=("draft",),
    )
    assert hub.get_task(str(item.task_id)) == item
    hub.update_activity(
        str(entry.entry_id), description="Outlined and reviewed", duration_minutes=40
    )
    facts = hub.activity_summary(LogFilter())
    assert (facts["task_completion"], facts["manual"], facts["duration_minutes"]) == (0, 1, 40)
    assert hub.delete_activity(str(entry.entry_id))
    assert hub.get_task(str(item.task_id)) == item
    assert hub.activity_summary(LogFilter())["duration_minutes"] is None


def test_filters_local_dates_literal_search_and_full_summary(hub):
    task_list = hub.create_task_list("Writing")
    for index in range(3):
        hub.log_progress(
            description=f"Progress {index}",
            notes="100%_literal" if index == 0 else "plain",
            list_id=task_list.list_id,
            tags=("draft",),
            occurred_at=datetime(2026, 10, 29, 2 + index, tzinfo=UTC),
            duration_minutes=10 if index == 0 else None,
        )
    options = LogFilter(
        start=date(2026, 10, 28),
        end=date(2026, 10, 28),
        timezone="America/Chicago",
        list_id=task_list.list_id,
        tag="draft",
        limit=1,
    )
    assert len(hub.list_activity(options)) == 1
    facts = hub.activity_summary(options)
    assert facts["manual"] == 3 and facts["duration_minutes"] == 10
    assert facts["days"] == {"2026-10-28": 3}
    assert len(hub.list_activity(LogFilter(search="%_literal"))) == 1
    assert not hub.list_activity(LogFilter(start=date(2026, 10, 29), timezone="America/Chicago"))


def test_reflections_keep_about_date_allow_empty_and_multiple_and_search(hub):
    item = task(hub)
    first = hub.save_reflection(
        ReflectionEntry(display_date=date(2020, 2, 3), body="Private writing", task_id=item.task_id)
    )
    second = hub.save_reflection(ReflectionEntry(display_date=first.display_date))
    revised = hub.save_reflection(
        first.model_copy(update={"title": "Older day", "body": "Lessons"})
    )
    assert revised.display_date == first.display_date
    assert revised.created_at == first.created_at
    assert revised.updated_at >= first.updated_at
    assert (
        len(hub.list_reflections(LogFilter(start=first.display_date, end=first.display_date))) == 2
    )
    assert hub.list_reflections(LogFilter(search="Lessons")) == [revised]
    assert hub.list_activity() == [] and hub.get_task(str(item.task_id)) == item
    assert hub.delete_reflection(str(second.reflection_id))
    assert hub.list_reflections() == [revised]


def test_logging_bounds_reject_invalid_data():
    for values in (
        {"description": " "},
        {"description": "x", "duration_minutes": 0},
        {"description": "x", "notes": "x" * 20_001},
        {"description": "x", "tags": ("unsafe tag",)},
        {"description": "x", "occurred_at": datetime(2026, 1, 1)},
    ):
        with pytest.raises(ValidationError):
            ActivityEntry(**values)
    with pytest.raises(ValidationError):
        ReflectionEntry(body="x" * 20_001)


def test_duplicate_offline_completions_merge_once_and_corrections_survive_notes(hub):
    item = task(hub)
    hub.complete_task(str(item.task_id), completed_at=NOW)
    (entry,) = hub.list_activity()
    second = ActivityEntry(**{**entry.model_dump(), "entry_id": __import__("uuid").uuid4()})

    def snapshot(entry, device):
        return SyncSnapshot(
            profile_id="test",
            device_id=device,
            generated_at=NOW,
            records=(
                SyncRecord(
                    collection="activity",
                    record_id=str(entry.entry_id),
                    updated_at=entry.updated_at,
                    payload=entry.model_dump(mode="json"),
                ),
            ),
        )

    merged, conflicts = merge_snapshots(
        (snapshot(entry, "a"), snapshot(second, "b")),
        profile_id="test",
        device_id="a",
        generated_at=NOW,
    )
    records = [ActivityEntry.model_validate_json(json.dumps(r.payload)) for r in merged.records]
    assert sum(r.undone_at is None for r in records) == 1 and conflicts == 1
    corrected = next(r for r in records if r.undone_at)
    stale_edit = ActivityEntry(
        **{
            **corrected.model_dump(),
            "undone_at": None,
            "notes": "Offline note",
            "updated_at": NOW + timedelta(days=1),
        }
    )
    merged, _ = merge_snapshots(
        (snapshot(corrected, "a"), snapshot(stale_edit, "b")),
        profile_id="test",
        device_id="a",
        generated_at=NOW,
    )
    result = ActivityEntry.model_validate_json(json.dumps(merged.records[0].payload))
    assert result.undone_at is not None and result.notes == "Offline note"


def test_synced_recurrence_preserves_occurrence_identity_and_undo_link(hub, tmp_path):
    second = HubController(settings=HubSettings(data_dir=tmp_path / "other"))
    try:
        item = task(hub, due_at=NOW, recurrence=Recurrence(frequency="daily", max_occurrences=3))
        _, following = hub.complete_task(str(item.task_id), completed_at=NOW)
        snapshot = hub._build_sync_snapshot(now=NOW)
        second._apply_sync_snapshot(snapshot, current=second._build_sync_snapshot(now=NOW))
        assert second.task_repository.sync_occurrence(
            following.task_id
        ) == hub.task_repository.sync_occurrence(following.task_id)
        assert second.task_repository.next_occurrence(item.task_id).task_id == following.task_id
        second.undo_task(str(item.task_id), at=NOW)
        assert second.get_task(str(following.task_id)) is None
        second.complete_task(str(item.task_id), completed_at=NOW)
        assert len(second.list_activity()) == 1
        assert len(second.list_activity(LogFilter(include_undone=True))) == 2
    finally:
        second.close()


def test_v13_migration_backfills_once_and_old_backup_restores(tmp_path):
    path = tmp_path / "old.db"
    database = HubDatabase(path)
    database.migrate()
    from personal_hub.tasks import TaskRepository
    from personal_hub.tasks.contracts import HubTask

    repo = TaskRepository(database)
    item = HubTask(
        title="Legacy complete", state="completed", created_at=NOW, updated_at=NOW, completed_at=NOW
    )
    repo.add(item, record_activity=False)
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE hub_activity")
        connection.execute("DROP TABLE hub_reflections")
        connection.execute("UPDATE hub_schema_migrations SET version=13")
    restored = HubDatabase(tmp_path / "restored.db")
    restored.migrate()
    restored.restore(path)
    assert len(ActivityRepository(restored).list()) == 1
    assert restored.integrity_check()
    restored.close()
    database = HubDatabase(path)
    database.migrate()
    first = ActivityRepository(database).list()
    assert len(first) == 1 and first[0].occurred_at == NOW
    database.migrate()
    assert ActivityRepository(database).list() == first
    assert database.integrity_check()
    database.close()


def test_backup_export_restore_and_separate_retention(hub, tmp_path):
    activity = hub.log_progress(
        description="Private accomplishment", occurred_at=NOW - timedelta(days=90)
    )
    reflection = hub.save_reflection(
        ReflectionEntry(display_date=date(2020, 1, 1), body="Private reflection")
    )
    backup = hub.backup_database(tmp_path / "backup.db")
    exported = json.loads(hub.export_local_data(tmp_path / "export.json").read_text("utf-8"))
    assert exported["activity"][0]["description"] == activity.description
    assert exported["reflections"][0]["body"] == reflection.body
    hub.run_retention_cleanup(now=NOW + timedelta(days=200))
    assert len(hub.list_activity()) == len(hub.list_reflections()) == 1
    assert hub.logging_retention(days=30, now=NOW) == {"activity": 1, "reflections": 1}
    assert len(hub.list_activity()) == 1
    hub.logging_retention(days=30, now=NOW, apply=True)
    assert hub.list_activity() == hub.list_reflections() == []
    hub.restore_database(backup)
    assert hub.list_activity()[0] == activity
    assert hub.list_reflections()[0] == reflection
    assert all(
        activity.description not in event.model_dump_json()
        and reflection.body not in event.model_dump_json()
        for event in hub.audit_events()
    )


def test_encrypted_sync_activity_reflection_opt_in_and_deletions(hub, tmp_path):
    second = HubController(settings=HubSettings(data_dir=tmp_path / "second", offline_mode=True))
    folder = tmp_path / "shared"
    hub.configure_sync(folder)
    second.configure_sync(folder)
    try:
        item = task(hub)
        hub.complete_task(str(item.task_id), completed_at=NOW)
        reflection = hub.save_reflection(ReflectionEntry(body="Private device writing"))
        hub.sync_now(PASSPHRASE)
        second.sync_now(PASSPHRASE)
        assert len(second.list_activity()) == 1
        assert second.list_reflections() == []
        hub.set_reflection_sync(True)
        hub.sync_now(PASSPHRASE)
        second.sync_now(PASSPHRASE)
        assert second.list_reflections() == []  # Receiving requires its own choice.
        second.set_reflection_sync(True)
        second.sync_now(PASSPHRASE)
        assert second.list_reflections()[0].body == reflection.body
        hub.undo_task(str(item.task_id), at=datetime.now(UTC))
        hub.sync_now(PASSPHRASE)
        second.sync_now(PASSPHRASE)
        assert second.list_activity() == []
        assert second.list_activity(LogFilter(include_undone=True))[0].undone_at
        second.delete_reflection(str(reflection.reflection_id))
        second.sync_now(PASSPHRASE)
        hub.sync_now(PASSPHRASE)
        assert hub.list_reflections() == []
        for shard in folder.rglob("*.sync"):
            assert b"Private device writing" not in shard.read_bytes()
            snapshot = decrypt_snapshot(shard.read_bytes(), PASSPHRASE)
            assert not any(
                record.record_id == "logging.sync_reflections" for record in snapshot.records
            )
        relay = hub._build_provider_relay_snapshot(now=datetime.now(UTC), profile_id="logging-test")
        assert reflection.body not in relay.model_dump_json()
    finally:
        second.close()
