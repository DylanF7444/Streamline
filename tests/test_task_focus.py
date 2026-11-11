"""Offline, deterministic coverage of durable task time goals."""

import sqlite3
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from personal_hub.logging.repository import ActivityRepository
from personal_hub.storage.database import HubDatabase
from personal_hub.tasks import HubTask, Recurrence, TaskRepository
from personal_hub.tasks.focus import FocusRepository


@pytest.fixture
def focus(tmp_path):
    database = HubDatabase(tmp_path / "hub.db")
    database.migrate()
    yield TaskRepository(database), FocusRepository(database), ActivityRepository(database)
    database.close()


def advance(repository, seconds):
    result = None
    while seconds:
        step = min(seconds, 10)
        result = repository.advance(step)
        seconds -= step
    return result


@pytest.mark.parametrize(
    "values",
    [
        dict(time_mode="goal"),
        dict(target_minutes=60),
        dict(time_mode="goal", target_minutes=0),
        dict(time_mode="estimate", target_minutes=1441),
    ],
)
def test_invalid_targets(values):
    with pytest.raises(ValidationError):
        HubTask(title="Study", **values)


def test_goal_completes_once_logs_only_measured_time_and_undo(focus):
    tasks, timer, activity = focus
    task = tasks.create(title="Study", time_mode="goal", target_minutes=1, log_focus_duration=True)
    timer.start(str(task.task_id))
    assert advance(timer, 59).elapsed_seconds == 59
    assert tasks.get(task.task_id).state == "open"
    assert advance(timer, 1).reached
    assert tasks.get(task.task_id).state == "completed"
    assert timer.current() is None
    assert timer.advance(10) is None
    records = activity.all()
    assert len(records) == 2
    timed = next(item for item in records if item.kind == "manual")
    assert timed.duration_minutes == 1 and timed.duration_source == "timer"
    tasks.undo(task.task_id)
    assert timer.current() is None
    assert tasks.get(task.task_id).state == "open"


def test_pause_switch_restart_and_cancel(focus):
    tasks, timer, activity = focus
    one = tasks.create(title="One", time_mode="goal", target_minutes=60)
    two = tasks.create(title="Two", time_mode="goal", target_minutes=60)
    timer.start(str(one.task_id))
    advance(timer, 15)
    timer.start(str(two.task_id))
    advance(timer, 5)
    assert timer.current(str(one.task_id)).elapsed_seconds == 15
    assert not timer.current(str(one.task_id)).running
    timer.recover()
    assert timer.advance(5) is None
    timer.start(str(one.task_id))
    assert advance(timer, 5).elapsed_seconds == 20
    timer.finish(str(one.task_id), discard=True)
    assert timer.current(str(one.task_id)) is None
    assert not activity.all()


def test_estimate_requires_confirmation_and_can_continue(focus):
    tasks, timer, activity = focus
    task = tasks.create(title="Report", time_mode="estimate", target_minutes=1)
    timer.start(str(task.task_id))
    assert advance(timer, 60).reached
    assert tasks.get(task.task_id).state == "open"
    assert not timer.current().running
    timer.start(str(task.task_id))
    assert advance(timer, 10).elapsed_seconds == 70
    tasks.complete(task.task_id)
    assert timer.current() is None
    assert len(activity.all()) == 1


def test_recurrence_inherits_configuration_without_progress(focus):
    tasks, timer, _ = focus
    task = tasks.create(
        title="Study",
        time_mode="goal",
        target_minutes=1,
        due_at=datetime.now(UTC),
        recurrence=Recurrence(frequency="daily"),
    )
    timer.start(str(task.task_id))
    advance(timer, 60)
    next_task = tasks.next_occurrence(task.task_id)
    assert next_task.time_mode == "goal" and next_task.target_minutes == 1
    assert timer.current(str(next_task.task_id)) is None
    tasks.complete(task.task_id)
    assert tasks.next_occurrence(task.task_id).task_id == next_task.task_id


def test_edit_archive_delete_and_partial_session(focus):
    tasks, timer, activity = focus
    task = tasks.create(title="Study", time_mode="goal", target_minutes=60, log_focus_duration=True)
    timer.start(str(task.task_id))
    advance(timer, 65)
    tasks.update(task.task_id, title="Study math")
    assert timer.current().elapsed_seconds == 65
    timer.finish(str(task.task_id))
    assert activity.all()[0].duration_minutes == 1
    timer.start(str(task.task_id))
    tasks.update(task.task_id, target_minutes=30)
    assert timer.current() is None
    timer.start(str(task.task_id))
    tasks.archive(task.task_id)
    assert timer.current() is None
    tasks.unarchive(task.task_id)
    timer.start(str(task.task_id))
    tasks.delete(task.task_id)
    assert timer.current() is None


def test_migrates_old_task_columns_and_backup(focus, tmp_path):
    tasks, timer, _ = focus
    task = tasks.create(title="Study", time_mode="goal", target_minutes=60)
    timer.start(str(task.task_id))
    advance(timer, 15)
    backup = tmp_path / "saved.db"
    tasks.database.backup(backup)
    tasks.database.restore(backup)
    timer.recover()
    assert timer.current().elapsed_seconds == 15
    assert not timer.current().running
    legacy = tmp_path / "legacy.db"
    tasks.database.backup(legacy)
    with sqlite3.connect(legacy) as connection:
        connection.execute("DROP TABLE hub_focus_sessions")
        for column in ("time_mode", "target_minutes", "log_focus_duration"):
            connection.execute(f"ALTER TABLE hub_tasks DROP COLUMN {column}")
        connection.execute("UPDATE hub_schema_migrations SET version = 16")
    old = HubDatabase(legacy)
    try:
        old.migrate()
        assert TaskRepository(old).get(task.task_id).time_mode == "none"
        old.migrate()
    finally:
        old.close()


def test_completion_race_keeps_single_fact_and_duration(focus):
    from concurrent.futures import ThreadPoolExecutor

    tasks, timer, activity = focus
    task = tasks.create(title="Race", time_mode="goal", target_minutes=1, log_focus_duration=True)
    timer.start(str(task.task_id))
    advance(timer, 59)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(timer.advance, 1), pool.submit(tasks.complete, task.task_id)]
        for future in futures:
            future.result()
    assert tasks.get(task.task_id).state == "completed"
    assert sum(item.kind == "task_completion" for item in activity.all()) == 1
    assert sum(item.duration_minutes or 0 for item in activity.all()) <= 1


def test_manual_duration_correction_relabels_measured_time(focus):
    tasks, timer, activity = focus
    task = tasks.create(
        title="Study", time_mode="estimate", target_minutes=60, log_focus_duration=True
    )
    timer.start(str(task.task_id))
    advance(timer, 90)
    timer.finish(str(task.task_id))
    entry = activity.all()[0]
    assert entry.duration_source == "timer"
    assert activity.update(entry.entry_id, notes="Still timed").duration_source == "timer"
    assert activity.update(entry.entry_id, duration_minutes=5).duration_source == "entered"


def test_concurrent_starts_have_only_one_running_timer(focus):
    from concurrent.futures import ThreadPoolExecutor

    tasks, timer, _ = focus
    ids = [
        str(tasks.create(title=str(number), time_mode="goal", target_minutes=60).task_id)
        for number in range(2)
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(timer.start, ids))
    assert sum(timer.current(identifier).running for identifier in ids) == 1
    with pytest.raises(ValueError):
        timer.advance(11)


def test_clock_change_does_not_invalidate_automatic_completion(focus):
    from datetime import timedelta

    tasks, timer, _ = focus
    future = datetime.now(UTC) + timedelta(days=1)
    task = tasks.create(
        title="Clock shift",
        time_mode="goal",
        target_minutes=1,
        created_at=future,
        updated_at=future,
    )
    timer.start(str(task.task_id))
    advance(timer, 60)
    completed = tasks.get(task.task_id)
    assert completed.state == "completed"
    assert completed.completed_at >= completed.created_at
