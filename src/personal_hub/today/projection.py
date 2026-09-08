"""Deterministic, timezone-aware Today projections over local Hub tasks."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from personal_hub.contracts import SourceRef
from personal_hub.today.contracts import (
    TaskLike,
    TaskRepositoryProtocol,
    TodayItem,
    TodayProjection,
)

_SECTION_NAMES = ("overdue", "today", "upcoming", "unscheduled")
_PRIORITY_ORDER = {"high": 0, "normal": 1, "low": 2, "none": 3}


class TodayProjector:
    """Project open tasks into bounded Today sections without mutating storage."""

    def __init__(
        self,
        repository: TaskRepositoryProtocol | None = None,
        *,
        timezone: str = "UTC",
        horizon_days: int = 7,
        section_limit: int = 100,
        max_items: int | None = None,
        limit: int | None = None,
    ) -> None:
        self.repository = repository
        self.timezone = _validate_timezone(timezone)
        self.horizon_days = _validate_horizon(horizon_days)
        selected_limit = section_limit
        if max_items is not None:
            selected_limit = max_items
        if limit is not None:
            selected_limit = limit
        self.section_limit = _validate_limit(selected_limit)

    def project(
        self,
        tasks: Iterable[TaskLike] | None = None,
        *,
        now: datetime | None = None,
        timezone: str | None = None,
        horizon_days: int | None = None,
        limit: int | None = None,
        section_limit: int | None = None,
    ) -> TodayProjection:
        """Return overdue, today, upcoming, and unscheduled task sections.

        Task due dates are interpreted in each task's declared timezone.  Comparisons use
        UTC instants for timed tasks and local calendar dates for all-day tasks, so a DST
        transition never becomes a fabricated 24-hour interval.
        """

        selected_timezone = _validate_timezone(timezone or self.timezone)
        selected_horizon = (
            self.horizon_days if horizon_days is None else _validate_horizon(horizon_days)
        )
        selected_limit = self.section_limit
        if section_limit is not None:
            selected_limit = _validate_limit(section_limit)
        if limit is not None:
            selected_limit = _validate_limit(limit)
        current = now or datetime.now(UTC)
        _require_aware(current, "now")
        current_utc = current.astimezone(UTC)
        source = self._task_iterable(tasks)
        buckets: dict[str, list[tuple[tuple[Any, ...], TodayItem]]] = {
            name: [] for name in _SECTION_NAMES
        }

        for task in source:
            if _task_state(task) != "open":
                # Completed and archived records are skipped before any sorting.  This
                # keeps a large completed history cheap for the Today surface.
                continue
            projected = self._project_task(
                task,
                now=current,
                now_utc=current_utc,
                horizon_days=selected_horizon,
            )
            if projected is None:
                continue
            section, sort_key, item = projected
            buckets[section].append((sort_key, item))

        output: dict[str, tuple[TodayItem, ...]] = {}
        for section in _SECTION_NAMES:
            values = buckets[section]
            values.sort(key=lambda value: value[0])
            output[section] = tuple(item for _, item in values[:selected_limit])
        return TodayProjection(
            generated_at=current,
            timezone=getattr(selected_timezone, "key", str(selected_timezone)),
            overdue=output["overdue"],
            today=output["today"],
            upcoming=output["upcoming"],
            unscheduled=output["unscheduled"],
        )

    build = project
    build_today = project

    def _task_iterable(self, tasks: Iterable[TaskLike] | None) -> Iterable[TaskLike]:
        if tasks is not None:
            return tasks
        if self.repository is None:
            return ()
        reader = getattr(self.repository, "list", None)
        if reader is None:
            reader = getattr(self.repository, "list_tasks", None)
        if reader is None:
            reader = getattr(self.repository, "list_open", None)
        if reader is None:
            reader = getattr(self.repository, "all", None)
        if reader is None:
            try:
                return cast(Iterable[TaskLike], iter(self.repository))
            except TypeError as error:
                raise TypeError(
                    "task repository must expose list(), list_tasks(), list_open(), or all()"
                ) from error
        try:
            values = reader(state="open")
        except TypeError:
            # A narrow fake/repository may not accept a state filter.  The projector
            # still applies its own state gate below.
            values = reader()
        if isinstance(values, (Mapping, str, bytes)):
            raise TypeError("task repository list must return an iterable of tasks")
        return values

    def _project_task(
        self,
        task: TaskLike,
        *,
        now: datetime,
        now_utc: datetime,
        horizon_days: int,
    ) -> tuple[str, tuple[Any, ...], TodayItem] | None:
        task_id = _task_id(task)
        title = _safe_text(_task_value(task, "title", ""), max_length=240)
        if not title:
            return None
        task_timezone = _task_timezone(task, self.timezone)
        due_value = _task_value(task, "due_at", None)
        due_is_all_day = bool(_task_value(task, "due_is_all_day", False))
        priority = _task_priority(task)
        item = TodayItem(
            item_id=f"task:{task_id}",
            title=title,
            summary=None,
            starts_at=None,
            due_at=_aware_due(due_value, task_timezone),
            severity="attention" if priority == "high" else "info",
            source=SourceRef(
                provider_id="local-tasks",
                resource_id=task_id,
                display_name="Tasks",
            ),
            navigation_target=f"hub://tasks/{task_id}",
            sensitive=False,
            due_is_all_day=due_is_all_day,
        )

        if due_value is None:
            section = "unscheduled"
            sort_key = (
                _PRIORITY_ORDER.get(priority, 4),
                _timestamp(_task_value(task, "updated_at", None)),
                _timestamp(_task_value(task, "created_at", None)),
                task_id,
            )
            return section, sort_key, item

        due = _aware_due(due_value, task_timezone)
        if due is None:  # pragma: no cover - guarded by due_value above
            return None
        due_local = due.astimezone(task_timezone)
        task_now = now.astimezone(task_timezone)
        today_date = task_now.date()
        horizon_date = today_date + timedelta(days=horizon_days)
        if due_is_all_day:
            due_date = due_local.date()
            if due_date < today_date:
                section = "overdue"
            elif due_date == today_date:
                section = "today"
            elif due_date <= horizon_date:
                section = "upcoming"
            else:
                return None
            due_order = (due_date.toordinal(), 0.0)
        else:
            due_utc = due.astimezone(UTC)
            if due_utc < now_utc:
                section = "overdue"
            elif due_local.date() == today_date:
                section = "today"
            elif due_local.date() <= horizon_date:
                section = "upcoming"
            else:
                return None
            due_order = (due_local.date().toordinal(), due_utc.timestamp())

        sort_key = (
            due_order,
            _PRIORITY_ORDER.get(priority, 4),
            _timestamp(_task_value(task, "updated_at", None)),
            task_id,
        )
        return section, sort_key, item


class TodayService(TodayProjector):
    """Named service alias convenient for desktop composition."""


TodayProjectionService = TodayService


def project_today(
    tasks: Iterable[TaskLike],
    *,
    now: datetime | None = None,
    timezone: str = "UTC",
    horizon_days: int = 7,
    limit: int = 100,
) -> TodayProjection:
    return TodayProjector(
        timezone=timezone,
        horizon_days=horizon_days,
        section_limit=limit,
    ).project(tasks, now=now)


build_today = project_today


def _task_value(task: TaskLike, name: str, default: Any) -> Any:
    if isinstance(task, Mapping):
        return task.get(name, default)
    return getattr(task, name, default)


def _task_id(task: TaskLike) -> str:
    value = _task_value(task, "task_id", None)
    if isinstance(value, UUID):
        return str(value)
    normalized = str(value).strip() if value is not None else ""
    if not normalized or any(character in normalized for character in "\r\n"):
        raise ValueError("task_id must be a stable identifier")
    return normalized


def _task_state(task: TaskLike) -> str:
    value = _task_value(task, "state", "open")
    return str(getattr(value, "value", value)).strip().casefold()


def _task_priority(task: TaskLike) -> str:
    value = _task_value(task, "priority", "none")
    return str(getattr(value, "value", value)).strip().casefold()


def _task_timezone(task: TaskLike, fallback: str | ZoneInfo) -> ZoneInfo:
    value = _task_value(task, "timezone", None)
    selected = fallback if value is None else str(value).strip()
    return _validate_timezone(selected)


def _aware_due(value: datetime | date | None, timezone: ZoneInfo) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        _require_aware(value, "task due_at")
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone)
    raise TypeError("task due_at must be a timezone-aware datetime, date, or None")


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _validate_timezone(value: str | ZoneInfo) -> ZoneInfo:
    if isinstance(value, ZoneInfo):
        return value
    normalized = str(value).strip()
    if normalized.casefold() == "local":
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(normalized)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError(f"Unknown task timezone: {normalized}") from error


def _validate_horizon(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 366:
        raise ValueError("Today horizon_days must be between 0 and 366")
    return value


def _validate_limit(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 1_000:
        raise ValueError("Today section limit must be between 1 and 1000")
    return value


def _timestamp(value: object) -> float:
    if isinstance(value, datetime):
        _require_aware(value, "task timestamp")
        return value.astimezone(UTC).timestamp()
    return float("inf")


def _safe_text(value: object, *, max_length: int) -> str:
    normalized = " ".join(str(value).split())
    return normalized[:max_length]


__all__ = [
    "TodayProjectionService",
    "TodayProjector",
    "TodayService",
    "build_today",
    "project_today",
]
