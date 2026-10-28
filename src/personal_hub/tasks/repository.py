"""Durable local task repository for Personal Hub."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, cast
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError

from personal_hub.storage.database import (
    HubDatabase,
    HubTaskDeletionMarkerRow,
    HubTaskListRow,
    HubTaskOccurrenceRow,
    HubTaskRow,
    HubTaskTagRow,
)
from personal_hub.tasks.contracts import (
    HubTask,
    HubTaskList,
    Recurrence,
    TaskFilter,
    TaskPriority,
    TaskSortField,
    TaskState,
)
from personal_hub.tasks.recurrence import next_due_at, occurrence_key

_AUTO_OCCURRENCE_METADATA = object()


def _db_time(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("task timestamps must be timezone-aware")
    return value.astimezone(UTC).replace(tzinfo=None)


def _contract_time(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _as_uuid(value: UUID | str) -> UUID:
    if isinstance(value, UUID):
        return value
    if type(value) is str:
        try:
            return UUID(value)
        except ValueError as error:
            raise ValueError("task identifier must be a UUID") from error
    raise TypeError("task identifier must be a UUID")


def _marker_value(value: str, *, name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > max_length
        or any(character in normalized for character in "\r\n\x00")
    ):
        raise ValueError(f"{name} is invalid")
    return normalized


def _aware_now(value: datetime | None = None) -> datetime:
    timestamp = datetime.now(UTC) if value is None else value
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("task timestamp must be timezone-aware")
    return timestamp.astimezone(UTC)


def _json_dump(value: object, *, limit: int = 64 * 1024) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise ValueError("task JSON value is not serializable") from error
    if len(encoded.encode("utf-8")) > limit:
        raise ValueError("task JSON value exceeds the size limit")
    return encoded


def _json_load(value: str | None, *, default: object) -> object:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("stored task JSON is invalid") from error


def _recurrence_dump(value: Recurrence | None) -> str | None:
    if value is None:
        return None
    payload = cast(dict[str, object], value.model_dump(mode="python"))
    if payload["until"] is not None:
        until = cast(datetime, payload["until"])
        payload["until"] = until.isoformat()
    return _json_dump(payload)


def _recurrence_load(value: str | None) -> Recurrence | None:
    if value is None:
        return None
    payload = _json_load(value, default={})
    if not isinstance(payload, dict):
        raise ValueError("stored task recurrence is invalid")
    if isinstance(payload.get("weekdays"), list):
        payload["weekdays"] = tuple(payload["weekdays"])
    if payload.get("until") is not None:
        until = payload["until"]
        if not isinstance(until, str):
            raise ValueError("stored task recurrence end is invalid")
        try:
            payload["until"] = datetime.fromisoformat(until)
        except ValueError as error:
            raise ValueError("stored task recurrence end is invalid") from error
    return Recurrence.model_validate(payload)


def _tags_dump(tags: tuple[str, ...]) -> str:
    return _json_dump(list(tags))


def _tags_load(value: str | None) -> tuple[str, ...]:
    payload = _json_load(value, default=[])
    if not isinstance(payload, list) or any(not isinstance(item, str) for item in payload):
        raise ValueError("stored task tags are invalid")
    return tuple(cast(str, item) for item in payload)


def _reminders_dump(reminders: tuple[int, ...]) -> str:
    return _json_dump(list(reminders))


def _reminders_load(value: str | None) -> tuple[int, ...]:
    payload = _json_load(value, default=[])
    if not isinstance(payload, list) or any(type(item) is not int for item in payload):
        raise ValueError("stored task reminder offsets are invalid")
    return tuple(cast(int, item) for item in payload)


class TaskListRepository:
    """Persist named task lists in the Hub database."""

    def __init__(self, database: HubDatabase) -> None:
        self.database = database

    def add(self, task_list: HubTaskList) -> HubTaskList:
        if not isinstance(task_list, HubTaskList):
            raise TypeError("task list repository expects HubTaskList")
        with self.database.session() as session:
            row = session.get(HubTaskListRow, str(task_list.list_id))
            if row is None:
                row = HubTaskListRow(
                    list_id=str(task_list.list_id),
                    name=task_list.name,
                    created_at=_db_time(task_list.created_at),
                    updated_at=_db_time(task_list.updated_at),
                )
                session.add(row)
            else:
                row.name = task_list.name
                row.updated_at = _db_time(task_list.updated_at)
            session.flush()
            return self._contract(row)

    save = add

    def create(
        self,
        name: str,
        *,
        list_id: UUID | None = None,
        created_at: datetime | None = None,
    ) -> HubTaskList:
        timestamp = _aware_now(created_at)
        return self.add(
            HubTaskList(
                list_id=uuid4() if list_id is None else list_id,
                name=name,
                created_at=timestamp,
                updated_at=timestamp,
            )
        )

    def get(self, list_id: UUID | str) -> HubTaskList | None:
        identifier = str(_as_uuid(list_id))
        with self.database.session() as session:
            row = session.get(HubTaskListRow, identifier)
            return None if row is None else self._contract(row)

    def list(self, *, limit: int = 1_000) -> list[HubTaskList]:
        if not 1 <= limit <= 10_000:
            raise ValueError("task list limit must be between 1 and 10000")
        with self.database.session() as session:
            rows = session.scalars(
                select(HubTaskListRow)
                .order_by(HubTaskListRow.name.asc(), HubTaskListRow.list_id.asc())
                .limit(limit)
            ).all()
            return [self._contract(row) for row in rows]

    list_lists = list

    def delete(self, list_id: UUID | str) -> bool:
        identifier = str(_as_uuid(list_id))
        with self.database.session() as session:
            row = session.get(HubTaskListRow, identifier)
            if row is None:
                return False
            session.delete(row)
            return True

    @staticmethod
    def _contract(row: HubTaskListRow) -> HubTaskList:
        return HubTaskList(
            list_id=UUID(row.list_id),
            name=row.name,
            created_at=_contract_time(row.created_at),
            updated_at=_contract_time(row.updated_at),
        )


class TaskRepository:
    """Persist Hub tasks and idempotent recurring occurrences."""

    _SORT_COLUMNS: dict[TaskSortField, Any] = {
        "due_at": HubTaskRow.due_at,
        "created_at": HubTaskRow.created_at,
        "updated_at": HubTaskRow.updated_at,
        "priority": HubTaskRow.priority,
        "title": HubTaskRow.title,
    }

    def __init__(self, database: HubDatabase) -> None:
        self.database = database
        self.lists = TaskListRepository(database)

    def add(self, task: HubTask) -> HubTask:
        if not isinstance(task, HubTask):
            raise TypeError("task repository expects HubTask")
        with self.database.session() as session:
            row = self._save_in_session(session, task)
            session.flush()
            return self._contract(row)

    create_from_contract = add
    save = add

    def create(self, task: HubTask | None = None, **values: object) -> HubTask:
        if task is not None:
            if values:
                raise TypeError("create accepts either a task or task fields")
            return self.add(task)
        return self.add(HubTask(**values))

    def get(self, task_id: UUID | str) -> HubTask | None:
        identifier = str(_as_uuid(task_id))
        with self.database.session() as session:
            row = session.get(HubTaskRow, identifier)
            return None if row is None else self._contract(row)

    def update(
        self,
        task_id: UUID | str,
        task: HubTask | None = None,
        **changes: object,
    ) -> HubTask | None:
        identifier = _as_uuid(task_id)
        current = self.get(identifier)
        if current is None:
            return None
        if task is not None:
            if changes or task.task_id != identifier:
                raise ValueError("updated task identity or arguments do not match")
            return self.add(task)
        payload = current.model_dump()
        payload.update(changes)
        payload["task_id"] = identifier
        if "updated_at" not in changes:
            payload["updated_at"] = datetime.now(UTC)
        if payload.get("state") == "completed" and "completed_at" not in changes:
            payload["completed_at"] = payload["updated_at"]
        elif payload.get("state") == "open":
            payload["completed_at"] = None
        revised = HubTask.model_validate(payload)
        return self.add(revised)

    edit = update

    def delete(self, task_id: UUID | str) -> bool:
        identifier = str(_as_uuid(task_id))
        with self.database.session() as session:
            row = session.get(HubTaskRow, identifier)
            if row is None:
                return False
            session.delete(row)
            return True

    def has_external_deletion_marker(
        self,
        *,
        provider_id: str,
        external_key: str | None = None,
        task_id: UUID | str | None = None,
    ) -> bool:
        """Return whether an external task was previously deleted in Personal Hub."""

        provider = _marker_value(provider_id, name="provider_id", max_length=64)
        identities: list[Any] = []
        if external_key is not None:
            key = _marker_value(external_key, name="external_key", max_length=1_024)
            identities.append(HubTaskDeletionMarkerRow.external_key == key)
        if task_id is not None:
            identities.append(HubTaskDeletionMarkerRow.task_id == str(_as_uuid(task_id)))
        if not identities:
            raise ValueError("an external key or task identifier is required")
        with self.database.session() as session:
            statement = select(HubTaskDeletionMarkerRow.provider_id).where(
                HubTaskDeletionMarkerRow.provider_id == provider,
                or_(*identities),
            )
            return session.scalar(statement) is not None

    is_external_task_deleted = has_external_deletion_marker

    def mark_deleted_on_hub(
        self,
        task_id: UUID | str,
        *,
        provider_id: str,
        external_key: str,
        deleted_at: datetime | None = None,
    ) -> bool:
        """Delete a completed external task while retaining a durable local tombstone."""

        identifier = str(_as_uuid(task_id))
        provider = _marker_value(provider_id, name="provider_id", max_length=64)
        key = _marker_value(external_key, name="external_key", max_length=1_024)
        timestamp = _aware_now(deleted_at)
        with self.database.session() as session:
            row = session.get(HubTaskRow, identifier)
            if row is None or row.state != "completed":
                return False
            marker = session.get(HubTaskDeletionMarkerRow, (provider, key))
            completed_at = _contract_time(row.completed_at) or timestamp
            if marker is None:
                session.add(
                    HubTaskDeletionMarkerRow(
                        provider_id=provider,
                        external_key=key,
                        task_id=identifier,
                        completed_at=_db_time(completed_at),
                        deleted_at=_db_time(timestamp),
                    )
                )
            else:
                marker.task_id = identifier
                marker.completed_at = _db_time(completed_at)
                marker.deleted_at = _db_time(timestamp)
            session.delete(row)
            session.flush()
            return True

    mark_external_deleted = mark_deleted_on_hub

    def complete(
        self,
        task_id: UUID | str,
        *,
        completed_at: datetime | None = None,
    ) -> HubTask | None:
        identifier = str(_as_uuid(task_id))
        timestamp = _aware_now(completed_at)
        with self.database.session() as session:
            row = session.get(HubTaskRow, identifier)
            if row is None:
                return None
            if row.state == "archived":
                return self._contract(row)
            if row.state != "completed":
                row.state = "completed"
                row.completed_at = _db_time(timestamp)
                row.updated_at = _db_time(timestamp)
                occurrence = self._occurrence_for_task(session, row)
                if occurrence is not None:
                    occurrence.completed_at = _db_time(timestamp)
            # Re-running completion, including after a process restart, is a
            # no-op on the completed row but repairs/observes its one next link.
            self._ensure_next_occurrence(session, row, timestamp)
            session.flush()
            return self._contract(row)

    complete_task = complete

    def complete_with_next(
        self,
        task_id: UUID | str,
        *,
        completed_at: datetime | None = None,
    ) -> tuple[HubTask | None, HubTask | None]:
        completed = self.complete(task_id, completed_at=completed_at)
        if completed is None:
            return None, None
        return completed, self.next_occurrence(completed.task_id)

    def undo(self, task_id: UUID | str, *, at: datetime | None = None) -> HubTask | None:
        identifier = str(_as_uuid(task_id))
        timestamp = _aware_now(at)
        with self.database.session() as session:
            row = session.get(HubTaskRow, identifier)
            if row is None:
                return None
            if row.state != "completed":
                return self._contract(row)
            occurrence = self._occurrence_for_task(session, row)
            next_task_id = None if occurrence is None else occurrence.next_task_id
            if occurrence is not None:
                occurrence.completed_at = None
                occurrence.next_task_id = None
            if next_task_id is not None:
                next_row = session.get(HubTaskRow, next_task_id)
                if (
                    next_row is not None
                    and next_row.state == "open"
                    and next_row.generated_from_task_id == row.task_id
                ):
                    session.delete(next_row)
            row.state = "open"
            row.completed_at = None
            row.updated_at = _db_time(timestamp)
            session.flush()
            return self._contract(row)

    undo_completion = undo

    def archive(self, task_id: UUID | str, *, at: datetime | None = None) -> HubTask | None:
        return self._set_state(task_id, "archived", at=at)

    archive_task = archive

    def unarchive(self, task_id: UUID | str, *, at: datetime | None = None) -> HubTask | None:
        return self._set_state(task_id, "open", at=at, clear_completed=True)

    restore = unarchive

    def next_occurrence(self, task_id: UUID | str) -> HubTask | None:
        identifier = str(_as_uuid(task_id))
        with self.database.session() as session:
            occurrence = session.scalar(
                select(HubTaskOccurrenceRow).where(HubTaskOccurrenceRow.task_id == identifier)
            )
            if occurrence is None or occurrence.next_task_id is None:
                return None
            row = session.get(HubTaskRow, occurrence.next_task_id)
            return None if row is None else self._contract(row)

    get_next_occurrence = next_occurrence

    def list(
        self,
        filters: TaskFilter | None = None,
        *,
        state: TaskState | None = None,
        states: Sequence[TaskState] | None = None,
        priority: TaskPriority | None = None,
        priorities: Sequence[TaskPriority] | None = None,
        list_id: UUID | str | None = None,
        tags: Sequence[str] | None = None,
        search: str | None = None,
        due_before: datetime | None = None,
        due_after: datetime | None = None,
        due_on: date | None = None,
        include_archived: bool = True,
        sort_by: TaskSortField = "due_at",
        descending: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[HubTask]:
        options = self._options(
            filters,
            state=state,
            states=states,
            priority=priority,
            priorities=priorities,
            list_id=list_id,
            tags=tags,
            search=search,
            due_before=due_before,
            due_after=due_after,
            due_on=due_on,
            include_archived=include_archived,
            sort_by=sort_by,
            descending=descending,
            limit=limit,
            offset=offset,
        )
        with self.database.session() as session:
            rows = session.scalars(self._statement(options)).all()
            return [self._contract(row) for row in rows]

    list_tasks = list
    query = list
    filter = list

    def count(self, filters: TaskFilter | None = None, **kwargs: object) -> int:
        options = self._options(filters, limit=10_000, offset=0, **kwargs)
        statement = self._statement(options, count=True)
        with self.database.session() as session:
            return int(session.scalar(statement) or 0)

    def due_today(
        self,
        *,
        timezone: str = "UTC",
        day: date | None = None,
        limit: int = 1_000,
    ) -> list[HubTask]:
        zone = ZoneInfo(timezone)
        target_day = datetime.now(zone).date() if day is None else day
        start = datetime.combine(target_day, time.min, tzinfo=zone).astimezone(UTC)
        end = datetime.combine(target_day + timedelta(days=1), time.min, tzinfo=zone).astimezone(
            UTC
        )
        return self.list(
            filters=TaskFilter(
                states=("open",),
                due_after=start,
                due_before=end,
                include_archived=False,
                limit=limit,
            )
        )

    today = due_today

    def overdue(self, *, now: datetime | None = None, limit: int = 1_000) -> list[HubTask]:
        timestamp = _aware_now(now)
        return self.list(
            filters=TaskFilter(
                states=("open",),
                due_before=timestamp,
                include_archived=False,
                limit=limit,
            )
        )

    def completed_past_due(
        self,
        *,
        now: datetime | None = None,
        limit: int = 10_000,
    ) -> list[HubTask]:
        """Return completed tasks whose timed due instant or all-day due date passed."""

        timestamp = _aware_now(now)
        if not 1 <= limit <= 10_000:
            raise ValueError("completed task cleanup limit must be between 1 and 10000")
        candidates = self.list(
            state="completed",
            due_before=timestamp,
            sort_by="due_at",
            limit=limit,
        )
        expired: list[HubTask] = []
        for task in candidates:
            if task.due_at is None:
                continue
            if task.due_is_all_day:
                zone = ZoneInfo(task.timezone)
                if task.due_at.astimezone(zone).date() >= timestamp.astimezone(zone).date():
                    continue
            expired.append(task)
        return expired

    def is_active_recurrence_anchor(self, task_id: UUID | str) -> bool:
        """Return whether a task anchors an unfinished recurring series."""

        identifier = str(_as_uuid(task_id))
        with self.database.session() as session:
            row = session.get(HubTaskRow, identifier)
            if row is None or row.recurrence_json is None:
                return False
            occurrence = self._occurrence_for_task(session, row)
            if occurrence is None or occurrence.occurrence_index != 1:
                return False
            latest = session.scalar(
                select(HubTaskOccurrenceRow)
                .where(HubTaskOccurrenceRow.series_id == occurrence.series_id)
                .order_by(HubTaskOccurrenceRow.occurrence_index.desc())
                .limit(1)
            )
            if latest is None:
                return False
            if latest.next_task_id is not None:
                return session.get(HubTaskRow, latest.next_task_id) is not None
            latest_task = session.get(HubTaskRow, latest.task_id)
            return latest_task is not None and latest_task.state == "open"

    def upcoming(
        self,
        *,
        now: datetime | None = None,
        horizon: timedelta = timedelta(days=30),
        limit: int = 1_000,
    ) -> list[HubTask]:
        if horizon <= timedelta(0) or horizon > timedelta(days=366):
            raise ValueError("task horizon must be positive and at most one year")
        timestamp = _aware_now(now)
        return self.list(
            filters=TaskFilter(
                states=("open",),
                due_after=timestamp,
                due_before=timestamp + horizon,
                include_archived=False,
                limit=limit,
            )
        )

    def reminder_events(
        self,
        *,
        now: datetime | None = None,
        limit: int = 1_000,
    ) -> list[tuple[HubTask, int]]:
        timestamp = _aware_now(now)
        candidates = self.list(
            filters=TaskFilter(states=("open",), include_archived=False, limit=limit)
        )
        events: list[tuple[HubTask, int]] = []
        for task in candidates:
            if task.due_at is None:
                continue
            for offset in task.reminder_offsets_minutes:
                reminder_at = task.due_at - timedelta(minutes=offset)
                if reminder_at <= timestamp < reminder_at + timedelta(minutes=1):
                    events.append((task, offset))
        return events

    def due_reminders(self, *, now: datetime | None = None, limit: int = 1_000) -> list[HubTask]:
        return [task for task, _ in self.reminder_events(now=now, limit=limit)]

    reminders_due = due_reminders

    @staticmethod
    def _contract(row: HubTaskRow) -> HubTask:
        list_id = None if row.list_id is None else UUID(row.list_id)
        return HubTask(
            task_id=UUID(row.task_id),
            title=row.title,
            notes=row.notes,
            external_url=row.external_url,
            state=cast(Any, row.state),
            priority=cast(Any, row.priority),
            due_at=_contract_time(row.due_at),
            due_is_all_day=row.due_is_all_day,
            timezone=row.timezone,
            list_id=list_id,
            tags=_tags_load(row.tags_json),
            recurrence=_recurrence_load(row.recurrence_json),
            reminder_offsets_minutes=_reminders_load(row.reminder_offsets_json),
            created_at=_contract_time(row.created_at),
            updated_at=_contract_time(row.updated_at),
            completed_at=_contract_time(row.completed_at),
        )

    def _save_in_session(
        self,
        session: Any,
        task: HubTask,
        *,
        occurrence_metadata: tuple[str, int, str, str | None]
        | None
        | object = _AUTO_OCCURRENCE_METADATA,
    ) -> HubTaskRow:
        identifier = str(task.task_id)
        row = session.get(HubTaskRow, identifier)
        if occurrence_metadata is _AUTO_OCCURRENCE_METADATA:
            metadata = None if row is None else self._occurrence_metadata(session, row)
        else:
            metadata = cast(tuple[str, int, str, str | None] | None, occurrence_metadata)
        if metadata is None and task.recurrence is not None:
            metadata = (str(task.task_id), 1, occurrence_key(task.task_id, 1), None)
        if row is None:
            row = HubTaskRow(task_id=identifier)
            session.add(row)
        row.title = task.title
        row.notes = task.notes
        row.external_url = task.external_url
        row.state = task.state
        row.priority = task.priority
        row.due_at = _db_time(task.due_at)
        row.due_is_all_day = task.due_is_all_day
        row.timezone = task.timezone
        row.list_id = None if task.list_id is None else str(task.list_id)
        row.tags_json = _tags_dump(task.tags)
        row.recurrence_json = _recurrence_dump(task.recurrence)
        row.reminder_offsets_json = _reminders_dump(task.reminder_offsets_minutes)
        row.created_at = _db_time(task.created_at)
        row.updated_at = _db_time(task.updated_at)
        row.completed_at = _db_time(task.completed_at)
        row.series_id = None if metadata is None else metadata[0]
        row.occurrence_index = None if metadata is None else metadata[1]
        row.occurrence_key = None if metadata is None else metadata[2]
        row.generated_from_task_id = None if metadata is None else metadata[3]
        session.flush()
        self._sync_tags(session, identifier, task.tags)
        if metadata is not None:
            key = cast(str, metadata[2])
            occurrence = session.get(HubTaskOccurrenceRow, key)
            if occurrence is None:
                session.add(
                    HubTaskOccurrenceRow(
                        occurrence_key=key,
                        series_id=cast(str, metadata[0]),
                        task_id=identifier,
                        occurrence_index=cast(int, metadata[1]),
                        due_at=_db_time(task.due_at),
                        completed_at=_db_time(task.completed_at),
                    )
                )
            else:
                occurrence.due_at = _db_time(task.due_at)
                occurrence.completed_at = _db_time(task.completed_at)
            session.flush()
        return row

    @staticmethod
    def _sync_tags(session: Any, task_id: str, tags: tuple[str, ...]) -> None:
        session.execute(delete(HubTaskTagRow).where(HubTaskTagRow.task_id == task_id))
        for tag in tags:
            session.add(HubTaskTagRow(task_id=task_id, tag=tag))

    @staticmethod
    def _occurrence_metadata(
        session: Any,
        row: HubTaskRow,
    ) -> tuple[str, int, str, str | None] | None:
        if row.series_id is None or row.occurrence_index is None or row.occurrence_key is None:
            return None
        return (
            row.series_id,
            row.occurrence_index,
            row.occurrence_key,
            row.generated_from_task_id,
        )

    @staticmethod
    def _occurrence_for_task(session: Any, row: HubTaskRow) -> HubTaskOccurrenceRow | None:
        if row.occurrence_key is not None:
            occurrence = session.get(HubTaskOccurrenceRow, row.occurrence_key)
            if occurrence is not None:
                return occurrence
        return session.scalar(
            select(HubTaskOccurrenceRow).where(HubTaskOccurrenceRow.task_id == row.task_id)
        )

    def _ensure_next_occurrence(
        self,
        session: Any,
        row: HubTaskRow,
        completed_at: datetime,
    ) -> HubTaskRow | None:
        task = self._contract(row)
        recurrence = task.recurrence
        if recurrence is None or task.due_at is None:
            return None
        occurrence = self._occurrence_for_task(session, row)
        if occurrence is None:
            index = row.occurrence_index or 1
            key = row.occurrence_key or occurrence_key(task.task_id, index)
            occurrence = HubTaskOccurrenceRow(
                occurrence_key=key,
                series_id=row.series_id or str(task.task_id),
                task_id=row.task_id,
                occurrence_index=index,
                due_at=_db_time(task.due_at),
                completed_at=_db_time(completed_at),
            )
            session.add(occurrence)
            session.flush()
        if occurrence.next_task_id is not None:
            return session.get(HubTaskRow, occurrence.next_task_id)

        next_index = occurrence.occurrence_index + 1
        anchor_occurrence = session.scalar(
            select(HubTaskOccurrenceRow).where(
                HubTaskOccurrenceRow.series_id == occurrence.series_id,
                HubTaskOccurrenceRow.occurrence_index == 1,
            )
        )
        anchor_due = (
            task.due_at
            if anchor_occurrence is None
            else _contract_time(anchor_occurrence.due_at) or task.due_at
        )
        next_due = next_due_at(
            task.due_at,
            recurrence,
            timezone=task.timezone,
            occurrence_number=occurrence.occurrence_index,
            anchor_due_at=anchor_due,
        )
        if next_due is None:
            return None
        series_id = occurrence.series_id
        next_key = occurrence_key(series_id, next_index)
        existing_occurrence = session.get(HubTaskOccurrenceRow, next_key)
        if existing_occurrence is not None:
            occurrence.next_task_id = existing_occurrence.task_id
            return session.get(HubTaskRow, existing_occurrence.task_id)

        next_id = uuid4()
        timestamp = _aware_now(completed_at)
        payload = task.model_dump()
        payload.update(
            {
                "task_id": next_id,
                "state": "open",
                "due_at": next_due,
                "created_at": timestamp,
                "updated_at": timestamp,
                "completed_at": None,
            }
        )
        next_task = HubTask.model_validate(payload)
        next_row: HubTaskRow | None = None
        try:
            with session.begin_nested():
                next_row = self._save_in_session(
                    session,
                    next_task,
                    occurrence_metadata=(series_id, next_index, next_key, row.task_id),
                )
                session.flush()
        except IntegrityError:
            existing_occurrence = session.get(HubTaskOccurrenceRow, next_key)
            if existing_occurrence is None:
                raise
            next_row = session.get(HubTaskRow, existing_occurrence.task_id)
        occurrence.next_task_id = None if next_row is None else next_row.task_id
        return next_row

    def _set_state(
        self,
        task_id: UUID | str,
        state: TaskState,
        *,
        at: datetime | None = None,
        clear_completed: bool = False,
    ) -> HubTask | None:
        identifier = str(_as_uuid(task_id))
        timestamp = _aware_now(at)
        with self.database.session() as session:
            row = session.get(HubTaskRow, identifier)
            if row is None:
                return None
            row.state = state
            row.updated_at = _db_time(timestamp)
            if clear_completed:
                row.completed_at = None
            session.flush()
            return self._contract(row)

    @staticmethod
    def _options(
        filters: TaskFilter | None,
        *,
        state: TaskState | None = None,
        states: Sequence[TaskState] | None = None,
        priority: TaskPriority | None = None,
        priorities: Sequence[TaskPriority] | None = None,
        list_id: UUID | str | None = None,
        tags: Sequence[str] | None = None,
        search: str | None = None,
        due_before: datetime | None = None,
        due_after: datetime | None = None,
        due_on: date | None = None,
        include_archived: bool = True,
        sort_by: TaskSortField = "due_at",
        descending: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> TaskFilter:
        if filters is not None:
            if any(
                value is not None
                for value in (
                    state,
                    states,
                    priority,
                    priorities,
                    list_id,
                    tags,
                    search,
                    due_before,
                    due_after,
                    due_on,
                )
            ):
                raise ValueError("pass either TaskFilter or keyword filters, not both")
            return filters
        if state is not None:
            if states is not None:
                raise ValueError("state and states cannot both be supplied")
            states = (state,)
        if priority is not None:
            if priorities is not None:
                raise ValueError("priority and priorities cannot both be supplied")
            priorities = (priority,)
        if list_id is not None:
            list_id = _as_uuid(list_id)
        return TaskFilter(
            states=tuple(states or ()),
            priorities=tuple(priorities or ()),
            list_id=cast(UUID | None, list_id),
            tags=tuple(tags or ()),
            search=search,
            due_before=due_before,
            due_after=due_after,
            due_on=due_on,
            include_archived=include_archived,
            sort_by=sort_by,
            descending=descending,
            limit=limit,
            offset=offset,
        )

    def _statement(self, options: TaskFilter, *, count: bool = False) -> Any:
        conditions: list[Any] = []
        if options.states:
            conditions.append(HubTaskRow.state.in_(options.states))
        elif not options.include_archived:
            conditions.append(HubTaskRow.state != "archived")
        if options.priorities:
            conditions.append(HubTaskRow.priority.in_(options.priorities))
        if options.list_id is not None:
            conditions.append(HubTaskRow.list_id == str(options.list_id))
        if options.search:
            escaped = options.search.replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            conditions.append(
                or_(
                    HubTaskRow.title.ilike(pattern, escape="\\"),
                    HubTaskRow.notes.ilike(pattern, escape="\\"),
                )
            )
        if options.due_after is not None:
            conditions.append(HubTaskRow.due_at >= _db_time(options.due_after))
        if options.due_before is not None:
            conditions.append(HubTaskRow.due_at < _db_time(options.due_before))
        if options.due_on is not None:
            start = datetime.combine(options.due_on, time.min, tzinfo=UTC)
            end = start + timedelta(days=1)
            conditions.extend(
                [HubTaskRow.due_at >= _db_time(start), HubTaskRow.due_at < _db_time(end)]
            )
        for tag in options.tags:
            conditions.append(
                HubTaskRow.task_id.in_(
                    select(HubTaskTagRow.task_id).where(HubTaskTagRow.tag == tag)
                )
            )
        if count:
            statement = select(func.count()).select_from(HubTaskRow)
        else:
            column = self._SORT_COLUMNS[options.sort_by]
            ordering = column.desc() if options.descending else column.asc()
            statement = select(HubTaskRow).order_by(ordering.nulls_last(), HubTaskRow.task_id.asc())
        if conditions:
            statement = statement.where(*conditions)
        if not count:
            statement = statement.offset(options.offset).limit(options.limit)
        return statement


HubTaskRepository = TaskRepository
TasksRepository = TaskRepository


__all__ = [
    "HubTaskRepository",
    "TaskListRepository",
    "TaskRepository",
    "TasksRepository",
]
