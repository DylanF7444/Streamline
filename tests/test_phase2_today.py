from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from time import perf_counter
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest

from personal_hub.config import HubSettings
from personal_hub.reminders import (
    DurableReminderClaimStore,
    InMemoryReminderClaimStore,
    ReminderService,
    build_occurrence_key,
)
from personal_hub.storage.database import HubDatabase
from personal_hub.storage.repositories import NotificationRepository
from personal_hub.today import DailyDigestBuilder, TodayProjector


@dataclass(frozen=True)
class FakeTask:
    task_id: UUID
    title: str
    state: str = "open"
    priority: str = "normal"
    due_at: datetime | date | None = None
    due_is_all_day: bool = False
    timezone: str = "UTC"
    reminder_offsets_minutes: tuple[int, ...] = ()
    created_at: datetime = datetime(2026, 1, 1, tzinfo=UTC)
    updated_at: datetime = datetime(2026, 1, 1, tzinfo=UTC)


def _task(
    title: str,
    *,
    due_at: datetime | date | None = None,
    task_id: UUID | None = None,
    state: str = "open",
    priority: str = "normal",
    timezone: str = "UTC",
    due_is_all_day: bool = False,
    reminder_offsets_minutes: tuple[int, ...] = (),
) -> FakeTask:
    return FakeTask(
        task_id=task_id or uuid4(),
        title=title,
        due_at=due_at,
        state=state,
        priority=priority,
        timezone=timezone,
        due_is_all_day=due_is_all_day,
        reminder_offsets_minutes=reminder_offsets_minutes,
    )


def test_today_orders_sections_uses_task_timezone_and_skips_large_completed_history():
    now = datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
    tasks = [
        _task("later overdue", due_at=datetime(2026, 10, 31, 23, tzinfo=UTC)),
        _task("earlier overdue", due_at=datetime(2026, 10, 31, 20, tzinfo=UTC)),
        _task("DST future", due_at=datetime(2026, 11, 1, 7, 30, tzinfo=UTC)),
        _task(
            "all-day today",
            due_at=date(2026, 11, 1),
            timezone="America/Chicago",
            due_is_all_day=True,
        ),
        _task("upcoming", due_at=datetime(2026, 11, 3, 15, tzinfo=UTC)),
        _task("unscheduled", priority="high"),
    ]
    tasks.extend(
        _task(
            f"completed-{index}",
            due_at=datetime(2026, 10, 1, tzinfo=UTC),
            state="completed",
        )
        for index in range(10_000)
    )

    started = perf_counter()
    projection = TodayProjector(timezone="UTC", horizon_days=7, section_limit=100).project(
        tasks,
        now=now,
    )
    elapsed = perf_counter() - started

    assert [item.title for item in projection.overdue] == ["earlier overdue", "later overdue"]
    assert [item.title for item in projection.today] == ["all-day today", "DST future"]
    assert [item.title for item in projection.upcoming] == ["upcoming"]
    assert [item.title for item in projection.unscheduled] == ["unscheduled"]
    assert projection.item_count == 6
    assert projection.sections[0][0].value == "overdue"
    assert elapsed < 1.0
    assert all("completed-" not in item.title for item in projection.items)


def test_today_limit_and_repository_fallback_are_deterministic():
    now = datetime(2026, 8, 31, 12, tzinfo=UTC)
    tasks = [
        _task("second", due_at=datetime(2026, 8, 31, 14, tzinfo=UTC)),
        _task("first", due_at=datetime(2026, 8, 31, 13, tzinfo=UTC)),
    ]

    class Repository:
        def list(self):
            return tasks

    projection = TodayProjector(Repository(), section_limit=1).project(now=now)

    assert [item.title for item in projection.today] == ["first"]
    assert projection.timezone == "UTC"


def test_today_rejects_naive_due_timestamps():
    task = _task("invalid", due_at=datetime(2026, 8, 31, 12))

    with pytest.raises(ValueError, match="timezone-aware"):
        TodayProjector().project([task], now=datetime(2026, 8, 31, 12, tzinfo=UTC))


def test_daily_digest_has_stable_section_order_and_plain_text():
    now = datetime(2026, 8, 31, 12, tzinfo=UTC)
    tasks = [
        _task("upcoming", due_at=datetime(2026, 9, 2, 12, tzinfo=UTC)),
        _task("today", due_at=datetime(2026, 8, 31, 13, tzinfo=UTC)),
        _task("overdue", due_at=datetime(2026, 8, 30, 12, tzinfo=UTC)),
        _task("unscheduled"),
    ]

    digest = DailyDigestBuilder(timezone="UTC").build(tasks, now=now)

    assert digest.digest_id == "daily:UTC:2026-08-31"
    assert [section.section_id for section in digest.sections] == [
        "overdue",
        "today",
        "upcoming",
        "unscheduled",
    ]
    assert digest.item_count == 4
    assert digest.to_text().splitlines() == [
        "Daily digest · 2026-08-31 (UTC)",
        "Overdue tasks (1)",
        "- overdue",
        "Due today (1)",
        "- today",
        "Upcoming tasks (1)",
        "- upcoming",
        "Unscheduled tasks (1)",
        "- unscheduled",
    ]


@pytest.mark.asyncio
async def test_reminders_claim_each_occurrence_once_across_service_restart():
    task_id = UUID("00000000-0000-0000-0000-000000000001")
    due = datetime(2026, 8, 31, 12, tzinfo=UTC)
    task = _task(
        "Call the team",
        task_id=task_id,
        due_at=due,
        reminder_offsets_minutes=(15, 0),
        priority="high",
    )
    store = InMemoryReminderClaimStore()
    first = ReminderService(store)
    second = ReminderService(store)
    now = datetime(2026, 8, 31, 12, 1, tzinfo=UTC)

    first_candidates = await first.collect_due([task], now=now)
    second_candidates = await second.collect_due([task], now=now)

    assert [candidate.offset_minutes for candidate in first_candidates] == [15, 0]
    assert second_candidates == ()
    assert len(store.claimed_keys) == 2
    inbox_items = await first.emit_due([task], now=now)
    assert inbox_items == ()


@pytest.mark.asyncio
async def test_durable_reminder_claim_store_survives_database_restart(tmp_path: Path):
    settings = HubSettings(profile="testing", data_dir=tmp_path / "hub")
    database = HubDatabase(settings.database_path)
    database.migrate()
    task = _task(
        "Persisted reminder",
        task_id=UUID("00000000-0000-0000-0000-000000000002"),
        due_at=datetime(2026, 8, 31, 12, tzinfo=UTC),
        reminder_offsets_minutes=(0,),
    )
    try:
        first = ReminderService(DurableReminderClaimStore(NotificationRepository(database)))
        assert (
            len(
                await first.collect_due(
                    [task],
                    now=datetime(2026, 8, 31, 12, 1, tzinfo=UTC),
                )
            )
            == 1
        )
    finally:
        database.close()

    restarted = HubDatabase(settings.database_path)
    restarted.migrate()
    try:
        second = ReminderService(DurableReminderClaimStore(NotificationRepository(restarted)))
        assert (
            await second.collect_due(
                [task],
                now=datetime(2026, 8, 31, 12, 1, tzinfo=UTC),
            )
            == ()
        )
    finally:
        restarted.close()


@pytest.mark.asyncio
async def test_reminders_skip_completed_tasks_and_emit_safe_inbox_candidates():
    due = datetime(2026, 8, 31, 12, tzinfo=UTC)
    open_task = _task(
        "Prepare report",
        due_at=due,
        reminder_offsets_minutes=(0,),
    )
    completed_task = _task(
        "Already done",
        due_at=due,
        state="completed",
        reminder_offsets_minutes=(0,),
    )
    service = ReminderService(InMemoryReminderClaimStore())

    items = await service.emit_due(
        [open_task, completed_task],
        now=datetime(2026, 8, 31, 12, 30, tzinfo=UTC),
    )

    assert len(items) == 1
    assert items[0].kind == "task_reminder"
    assert items[0].title == "Prepare report"
    assert items[0].safe_summary == "A scheduled task reminder is due."
    assert items[0].navigation_target == f"hub://tasks/{open_task.task_id}"
    assert items[0].deduplication_key is not None


def test_occurrence_keys_are_dst_aware_and_all_day_keys_use_local_date():
    chicago = ZoneInfo("America/Chicago")
    first_utc = datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
    second_utc = datetime(2026, 11, 1, 7, 30, tzinfo=UTC)
    assert build_occurrence_key("task", first_utc, 0) != build_occurrence_key("task", second_utc, 0)

    all_day = build_occurrence_key(
        "task",
        datetime(2026, 11, 1, tzinfo=chicago),
        30,
        due_is_all_day=True,
        timezone=chicago,
    )
    assert "2026-11-01" in all_day
