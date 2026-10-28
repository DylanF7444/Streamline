"""Separate history tables with transactional task capture and bounded queries."""

from __future__ import annotations

import json
from datetime import UTC, datetime, time, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from personal_hub.logging.contracts import ActivityEntry, LogFilter, ReflectionEntry
from personal_hub.storage.database import (
    HubActivityRow,
    HubDatabase,
    HubReflectionRow,
    HubTaskListRow,
    HubTaskRow,
)


def _db(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Logging timestamps require a timezone")
    return value.astimezone(UTC).replace(tzinfo=None)


def _now() -> datetime:
    return datetime.now(UTC)


def save_activity(session: Session, entry: ActivityEntry) -> None:
    row = session.get(HubActivityRow, str(entry.entry_id))
    if row is None:
        row = HubActivityRow(entry_id=str(entry.entry_id))
        session.add(row)
    row.kind = entry.kind
    row.occurred_at = _db(entry.occurred_at)
    row.updated_at = _db(entry.updated_at)
    row.task_id = str(entry.task_id) if entry.task_id else None
    row.undone_at = _db(entry.undone_at) if entry.undone_at else None
    row.active_completion_key = (
        f"{entry.task_id}:{entry.occurrence_key or ''}"
        if entry.kind == "task_completion" and entry.undone_at is None
        else None
    )
    row.payload_json = entry.model_dump_json()
    session.flush()


def capture_completion(session: Session, task: HubTaskRow, *, backfill: bool = False) -> None:
    key = f"{task.task_id}:{task.occurrence_key or ''}"
    if session.scalar(
        select(HubActivityRow.entry_id).where(HubActivityRow.active_completion_key == key)
    ):
        return
    if task.completed_at is None:
        return
    timestamp = task.completed_at.replace(tzinfo=UTC)
    task_list = session.get(HubTaskListRow, task.list_id) if task.list_id else None
    values = dict(
        kind="task_completion",
        task_id=UUID(task.task_id),
        occurrence_key=task.occurrence_key,
        description=task.title,
        title_snapshot=task.title,
        occurred_at=timestamp,
        created_at=timestamp,
        updated_at=timestamp,
        list_id=UUID(task.list_id) if task.list_id else None,
        list_name=task_list.name if task_list else None,
        tags=tuple(json.loads(task.tags_json)),
    )
    if backfill:
        values["entry_id"] = uuid5(
            NAMESPACE_URL, f"personal-hub-completion:{key}:{timestamp.isoformat()}"
        )
    save_activity(session, ActivityEntry(**values))


def correct_completion(session: Session, task_id: str, at: datetime) -> None:
    rows = session.scalars(
        select(HubActivityRow).where(
            HubActivityRow.task_id == task_id,
            HubActivityRow.active_completion_key.is_not(None),
        )
    ).all()
    for row in rows:
        original = ActivityEntry.model_validate_json(row.payload_json)
        revised = ActivityEntry.model_validate(
            {
                **original.model_dump(),
                "undone_at": at,
                "updated_at": max(at, original.updated_at + timedelta(microseconds=1)),
            }
        )
        save_activity(session, revised)


def backfill_completions(session: Session) -> None:
    # Existing completed (including archived completed) tasks acquire one snapshot.
    for row in session.scalars(select(HubTaskRow).where(HubTaskRow.completed_at.is_not(None))):
        capture_completion(session, row, backfill=True)


class ActivityRepository:
    def __init__(self, database: HubDatabase) -> None:
        self.database = database

    def get(self, entry_id: UUID | str) -> ActivityEntry | None:
        with self.database.session() as session:
            row = session.get(HubActivityRow, str(UUID(str(entry_id))))
            return ActivityEntry.model_validate_json(row.payload_json) if row else None

    def add(self, entry: ActivityEntry) -> ActivityEntry:
        if entry.kind != "manual":
            raise ValueError("task completions are captured through task actions")
        with self.database.session() as session:
            if session.get(HubActivityRow, str(entry.entry_id)):
                raise ValueError("activity already exists")
            save_activity(session, entry)
        return entry

    def update(self, entry_id: UUID | str, **changes: object) -> ActivityEntry:
        with self.database.session() as session:
            row = session.get(HubActivityRow, str(UUID(str(entry_id))))
            if row is None:
                raise KeyError("activity unavailable")
            current = ActivityEntry.model_validate_json(row.payload_json)
            allowed = (
                {"notes"}
                if current.kind == "task_completion"
                else {
                    "description",
                    "notes",
                    "occurred_at",
                    "duration_minutes",
                    "task_id",
                    "list_id",
                    "list_name",
                    "tags",
                }
            )
            if set(changes).difference(allowed):
                raise ValueError("completion facts and record identity cannot be edited")
            entry = ActivityEntry.model_validate(
                {
                    **current.model_dump(),
                    **changes,
                    "updated_at": max(_now(), current.updated_at + timedelta(microseconds=1)),
                }
            )
            save_activity(session, entry)
            return entry

    def delete(self, entry_id: UUID | str) -> bool:
        with self.database.session() as session:
            row = session.get(HubActivityRow, str(UUID(str(entry_id))))
            if row is None:
                return False
            session.delete(row)
            return True

    @staticmethod
    def _query(options: LogFilter):
        query = select(HubActivityRow)
        if options.start:
            lower = datetime.combine(options.start, time.min, ZoneInfo(options.timezone))
            query = query.where(HubActivityRow.occurred_at >= _db(lower))
        if options.end:
            upper = datetime.combine(
                options.end + timedelta(days=1), time.min, ZoneInfo(options.timezone)
            )
            query = query.where(HubActivityRow.occurred_at < _db(upper))
        if not options.include_undone:
            query = query.where(HubActivityRow.undone_at.is_(None))
        if options.kind:
            query = query.where(HubActivityRow.kind == options.kind)
        if options.list_id:
            query = query.where(
                func.json_extract(HubActivityRow.payload_json, "$.list_id") == str(options.list_id)
            )
        if options.tag:
            tags = func.json_each(HubActivityRow.payload_json, "$.tags").table_valued("value")
            query = query.where(
                select(tags.c.value).where(tags.c.value == options.tag.casefold()).exists()
            )
        if options.search:
            query = query.where(
                or_(
                    *(
                        func.json_extract(HubActivityRow.payload_json, f"$.{field}").icontains(
                            options.search, autoescape=True
                        )
                        for field in ("description", "notes", "title_snapshot")
                    )
                )
            )
        return query

    def list(self, options: LogFilter | None = None) -> list[ActivityEntry]:
        options = options or LogFilter()
        with self.database.session() as session:
            rows = session.scalars(
                self._query(options)
                .order_by(HubActivityRow.occurred_at.desc(), HubActivityRow.entry_id.desc())
                .limit(options.limit)
                .offset(options.offset)
            )
            return [ActivityEntry.model_validate_json(row.payload_json) for row in rows]

    def summary(self, options: LogFilter) -> dict[str, object]:
        # Iterate in batches, never truncate facts at the display/query limit.
        counts = {"task_completion": 0, "manual": 0}
        duration = None
        days: dict[str, int] = {}
        lists: dict[str, int] = {}
        tags: dict[str, int] = {}
        with self.database.session() as session:
            query = self._query(options.model_copy(update={"include_undone": False}))
            for row in session.scalars(query.execution_options(yield_per=200)):
                entry = ActivityEntry.model_validate_json(row.payload_json)
                counts[entry.kind] += 1
                if entry.duration_minutes is not None:
                    duration = (duration or 0) + entry.duration_minutes
                day = entry.occurred_at.astimezone(ZoneInfo(options.timezone)).date().isoformat()
                days[day] = days.get(day, 0) + 1
                label = entry.list_name or (str(entry.list_id) if entry.list_id else "No list")
                lists[label] = lists.get(label, 0) + 1
                for tag in entry.tags:
                    tags[tag] = tags.get(tag, 0) + 1
        return {**counts, "duration_minutes": duration, "days": days, "lists": lists, "tags": tags}

    def all(self) -> list[ActivityEntry]:
        with self.database.session() as session:
            return [
                ActivityEntry.model_validate_json(row.payload_json)
                for row in session.scalars(select(HubActivityRow).execution_options(yield_per=200))
            ]

    def list_context(self) -> list[tuple[str, str]]:
        """Historical list labels without loading private descriptions or notes."""
        identifier = func.json_extract(HubActivityRow.payload_json, "$.list_id")
        label = func.json_extract(HubActivityRow.payload_json, "$.list_name")
        with self.database.session() as session:
            return [
                (key, name or "Unavailable list")
                for key, name in session.execute(
                    select(identifier, label)
                    .where(identifier.is_not(None))
                    .distinct()
                    .limit(10_000)
                )
            ]


class ReflectionRepository:
    def __init__(self, database: HubDatabase) -> None:
        self.database = database

    def get(self, reflection_id: UUID | str) -> ReflectionEntry | None:
        with self.database.session() as session:
            row = session.get(HubReflectionRow, str(UUID(str(reflection_id))))
            return ReflectionEntry.model_validate_json(row.payload_json) if row else None

    def save(self, entry: ReflectionEntry) -> ReflectionEntry:
        with self.database.session() as session:
            self.save_in_session(session, entry)
        return entry

    @staticmethod
    def save_in_session(session: Session, entry: ReflectionEntry) -> None:
        row = session.get(HubReflectionRow, str(entry.reflection_id))
        if row is None:
            row = HubReflectionRow(reflection_id=str(entry.reflection_id))
            session.add(row)
        row.display_date = entry.display_date.isoformat()
        row.updated_at = _db(entry.updated_at)
        row.payload_json = entry.model_dump_json()

    def delete(self, reflection_id: UUID | str) -> bool:
        with self.database.session() as session:
            row = session.get(HubReflectionRow, str(UUID(str(reflection_id))))
            if row is None:
                return False
            session.delete(row)
            return True

    def list(self, options: LogFilter | None = None) -> list[ReflectionEntry]:
        options = options or LogFilter()
        query = select(HubReflectionRow)
        if options.start:
            query = query.where(HubReflectionRow.display_date >= options.start.isoformat())
        if options.end:
            query = query.where(HubReflectionRow.display_date <= options.end.isoformat())
        if options.search:
            query = query.where(
                or_(
                    *(
                        func.json_extract(HubReflectionRow.payload_json, f"$.{field}").icontains(
                            options.search, autoescape=True
                        )
                        for field in ("title", "body")
                    )
                )
            )
        with self.database.session() as session:
            rows = session.scalars(
                query.order_by(
                    HubReflectionRow.display_date.desc(),
                    HubReflectionRow.updated_at.desc(),
                    HubReflectionRow.reflection_id.desc(),
                )
                .limit(options.limit)
                .offset(options.offset)
            )
            return [ReflectionEntry.model_validate_json(row.payload_json) for row in rows]

    def all(self) -> list[ReflectionEntry]:
        with self.database.session() as session:
            return [
                ReflectionEntry.model_validate_json(row.payload_json)
                for row in session.scalars(
                    select(HubReflectionRow).execution_options(yield_per=200)
                )
            ]
