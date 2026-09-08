"""Restart-safe local task reminder reconciliation."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from personal_hub.contracts import InboxItem
from personal_hub.reminders.contracts import (
    InMemoryReminderClaimStore,
    ReminderCandidate,
    ReminderClaimStoreProtocol,
)
from personal_hub.storage.repositories import NotificationRepository
from personal_hub.today.contracts import TaskLike


class ReminderService:
    """Find due task reminders and atomically claim each occurrence exactly once."""

    def __init__(
        self,
        claim_store: ReminderClaimStoreProtocol | None = None,
        *,
        max_candidates: int = 1_000,
        timezone: str = "UTC",
    ) -> None:
        if not isinstance(max_candidates, int) or isinstance(max_candidates, bool):
            raise ValueError("max_candidates must be an integer")
        if not 1 <= max_candidates <= 10_000:
            raise ValueError("max_candidates must be between 1 and 10000")
        self.claim_store = claim_store or InMemoryReminderClaimStore()
        self.max_candidates = max_candidates
        self.timezone = _validate_timezone(timezone)
        self._claim_lock = asyncio.Lock()

    async def collect_due(
        self,
        tasks: Iterable[TaskLike],
        *,
        now: datetime | None = None,
        limit: int | None = None,
    ) -> tuple[ReminderCandidate, ...]:
        """Claim and return due reminder candidates in deterministic fire order."""

        current = now or datetime.now(UTC)
        _require_aware(current, "now")
        selected_limit = self.max_candidates if limit is None else _validate_limit(limit)
        due_records: list[tuple[datetime, str, TaskLike, datetime, int, ZoneInfo]] = []
        seen_keys: set[str] = set()
        for task in tasks:
            if _task_state(task) != "open":
                continue
            due_value = _task_value(task, "due_at", None)
            if due_value is None:
                continue
            task_zone = _task_timezone(task, self.timezone)
            due_at = _due_datetime(due_value, task_zone)
            task_id = _task_id(task)
            offsets = _task_value(task, "reminder_offsets_minutes", ()) or ()
            for raw_offset in offsets:
                offset = _validate_offset(raw_offset)
                fire_at = (due_at.astimezone(UTC) - timedelta(minutes=offset)).astimezone(task_zone)
                if fire_at.astimezone(UTC) > current.astimezone(UTC):
                    continue
                occurrence_key = build_occurrence_key(
                    task_id,
                    due_at,
                    offset,
                    due_is_all_day=bool(_task_value(task, "due_is_all_day", False)),
                    timezone=task_zone,
                )
                if occurrence_key in seen_keys:
                    continue
                seen_keys.add(occurrence_key)
                due_records.append(
                    (fire_at.astimezone(UTC), occurrence_key, task, due_at, offset, task_zone)
                )

        due_records.sort(key=lambda record: (record[0], record[1]))
        candidates: list[ReminderCandidate] = []
        for fire_at_utc, occurrence_key, task, due_at, offset, task_zone in due_records[
            :selected_limit
        ]:
            claimed = await self._claim(occurrence_key, claimed_at=current)
            if not claimed:
                continue
            title = _safe_text(_task_value(task, "title", ""), max_length=240)
            if not title:
                continue
            candidates.append(
                ReminderCandidate(
                    occurrence_key=occurrence_key,
                    task_id=_task_id(task),
                    title=title,
                    priority=_task_priority(task),
                    due_at=due_at,
                    fire_at=fire_at_utc.astimezone(task_zone),
                    created_at=current,
                    offset_minutes=offset,
                    due_is_all_day=bool(_task_value(task, "due_is_all_day", False)),
                )
            )
        return tuple(candidates)

    async def emit_due(
        self,
        tasks: Iterable[TaskLike],
        *,
        now: datetime | None = None,
        limit: int | None = None,
    ) -> tuple[InboxItem, ...]:
        """Return claimed candidates as Hub ``InboxItem`` values."""

        candidates = await self.collect_due(tasks, now=now, limit=limit)
        return tuple(candidate.to_inbox_item() for candidate in candidates)

    due_candidates = collect_due
    collect_inbox_candidates = collect_due
    reconcile = collect_due
    reconcile_due = collect_due
    due_items = emit_due
    emit_inbox_items = emit_due

    async def _claim(self, occurrence_key: str, *, claimed_at: datetime) -> bool:
        method = getattr(self.claim_store, "claim_occurrence", None)
        if not callable(method):
            method = getattr(self.claim_store, "claim", None)
        if not callable(method):
            method = getattr(self.claim_store, "claim_once", None)
        if not callable(method):
            raise TypeError("reminder claim store must expose claim()")
        async with self._claim_lock:
            try:
                result = method(occurrence_key, claimed_at=claimed_at)
            except TypeError as first_error:
                try:
                    result = method(occurrence_key, now=claimed_at)
                except TypeError:
                    try:
                        result = method(occurrence_key)
                    except TypeError:
                        raise first_error from None
            if inspect.isawaitable(result):
                result = await result
            return bool(result)


class ReminderScheduler(ReminderService):
    """Named alias for callers that reconcile reminders from a scheduler loop."""


class DurableReminderClaimStore:
    """Persist reminder occurrence claims through Hub notification deduplication.

    The storage repository owns the transaction and unique key constraint.  Keeping this
    adapter in the reminder package lets the service remain independent of SQLAlchemy while
    providing an actual restart-safe implementation in addition to the in-memory test store.
    """

    def __init__(self, notifications: NotificationRepository) -> None:
        self.notifications = notifications

    def claim(self, occurrence_key: str, *, claimed_at: datetime) -> bool:
        return self.notifications.claim_key(
            occurrence_key,
            "inbox",
            now=claimed_at,
        )

    claim_occurrence = claim


ReminderClaimStore = DurableReminderClaimStore


def build_occurrence_key(
    task_id: str | UUID,
    due_at: datetime,
    offset_minutes: int,
    *,
    due_is_all_day: bool = False,
    timezone: ZoneInfo | str = "UTC",
) -> str:
    """Build a stable identity from task, due occurrence, and reminder offset."""

    _require_aware(due_at, "due_at")
    task_key = str(task_id).strip()
    if not task_key or any(character in task_key for character in "\r\n"):
        raise ValueError("task_id must be a stable identifier")
    offset = _validate_offset(offset_minutes)
    selected_zone = _validate_timezone(timezone)
    if due_is_all_day:
        due_marker = f"{selected_zone.key}:{due_at.astimezone(selected_zone).date().isoformat()}"
    else:
        due_marker = due_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return f"task-reminder:{task_key}:{due_marker}:{offset}"


def _task_value(task: TaskLike, name: str, default: Any) -> Any:
    if isinstance(task, Mapping):
        return task.get(name, default)
    return getattr(task, name, default)


def _task_id(task: TaskLike) -> str:
    value = _task_value(task, "task_id", None)
    normalized = str(value).strip() if value is not None else ""
    if not normalized or any(character in normalized for character in "\r\n"):
        raise ValueError("task_id must be a stable identifier")
    return normalized


def _task_state(task: TaskLike) -> str:
    value = _task_value(task, "state", "open")
    return str(getattr(value, "value", value)).strip().casefold()


def _task_priority(task: TaskLike) -> str:
    value = _task_value(task, "priority", "none")
    normalized = str(getattr(value, "value", value)).strip().casefold()
    return normalized if normalized in {"none", "low", "normal", "high"} else "none"


def _task_timezone(task: TaskLike, fallback: ZoneInfo) -> ZoneInfo:
    value = _task_value(task, "timezone", None)
    return _validate_timezone(fallback if value is None else str(value).strip())


def _due_datetime(value: datetime | date, timezone: ZoneInfo) -> datetime:
    if isinstance(value, datetime):
        _require_aware(value, "task due_at")
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone)
    raise TypeError("task due_at must be a timezone-aware datetime, date, or None")


def _validate_timezone(value: str | ZoneInfo) -> ZoneInfo:
    if isinstance(value, ZoneInfo):
        return value
    normalized = str(value).strip()
    if normalized.casefold() == "local":
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(normalized)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError(f"Unknown reminder timezone: {normalized}") from error


def _validate_offset(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 1_000_000:
        raise ValueError("reminder offsets must be non-negative bounded integers")
    return value


def _validate_limit(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 10_000:
        raise ValueError("reminder limit must be between 1 and 10000")
    return value


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _safe_text(value: object, *, max_length: int) -> str:
    return " ".join(str(value).split())[:max_length]


__all__ = [
    "DurableReminderClaimStore",
    "ReminderClaimStore",
    "ReminderScheduler",
    "ReminderService",
    "build_occurrence_key",
]
