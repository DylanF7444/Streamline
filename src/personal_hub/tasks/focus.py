"""Explicit focus sessions, persisted locally and advanced by elapsed active seconds."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session

from personal_hub.logging.contracts import ActivityEntry
from personal_hub.logging.repository import save_activity
from personal_hub.storage.database import (
    HubDatabase,
    HubFocusSessionRow,
    HubTaskListRow,
    HubTaskRow,
)
from personal_hub.tasks.contracts import HubTask


def settle_focus(session: Session, task: HubTaskRow, at: datetime) -> None:
    """Save optional measured duration exactly once, in the caller's transaction."""
    focus = session.get(HubFocusSessionRow, task.task_id)
    if focus is None:
        return
    if task.log_focus_duration and focus.elapsed_seconds >= 60:
        task_list = session.get(HubTaskListRow, task.list_id) if task.list_id else None
        save_activity(
            session,
            ActivityEntry(
                entry_id=UUID(focus.session_id),
                description=("Timed focus: " + task.title)[:240],
                task_id=UUID(task.task_id),
                list_id=UUID(task.list_id) if task.list_id else None,
                list_name=task_list.name if task_list else None,
                tags=tuple(json.loads(task.tags_json)),
                duration_minutes=min(525600, focus.elapsed_seconds // 60),
                duration_source="timer",
                notes="Measured while the focus timer was running; partial minutes are omitted.",
                occurred_at=at,
                created_at=at,
                updated_at=at,
            ),
        )
    session.delete(focus)
    session.flush()


def discard_changed_focus(session: Session, row: HubTaskRow, task: HubTask) -> None:
    if row.state == "open" and task.state == "completed":
        settle_focus(session, row, task.completed_at or task.updated_at)
    elif (row.time_mode, row.target_minutes) != (
        task.time_mode,
        task.target_minutes,
    ) or task.state != "open":
        session.execute(
            delete(HubFocusSessionRow).where(HubFocusSessionRow.task_id == str(task.task_id))
        )


@dataclass(frozen=True)
class FocusState:
    task_id: str
    title: str
    time_mode: str
    target_seconds: int
    elapsed_seconds: int
    running: bool
    reached: bool


class FocusRepository:
    def __init__(self, database: HubDatabase) -> None:
        self.database = database

    def recover(self) -> None:
        """Restarts/restores retain saved seconds but require explicit resume."""
        with self.database.session() as session:
            session.execute(update(HubFocusSessionRow).values(running=False))

    def current(self, task_id: str | None = None) -> FocusState | None:
        with self.database.session() as session:
            statement = (
                select(HubFocusSessionRow, HubTaskRow)
                .join(HubTaskRow, HubTaskRow.task_id == HubFocusSessionRow.task_id)
                .where(HubTaskRow.state == "open", HubTaskRow.time_mode != "none")
            )
            if task_id is not None:
                statement = statement.where(HubFocusSessionRow.task_id == str(UUID(task_id)))
            pair = session.execute(
                statement.order_by(
                    HubFocusSessionRow.running.desc(), HubFocusSessionRow.updated_at.desc()
                ).limit(1)
            ).first()
            if pair is None:
                return None
            focus, task = pair
            return FocusState(
                task.task_id,
                task.title,
                task.time_mode,
                task.target_minutes * 60,
                focus.elapsed_seconds,
                focus.running,
                focus.reached,
            )

    def start(self, task_id: str) -> FocusState:
        identifier = str(UUID(task_id))
        with self.database.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            task = session.get(HubTaskRow, identifier)
            if task is None or task.state != "open" or not task.target_minutes:
                raise ValueError("Choose an open task with a time goal or estimate.")
            session.execute(update(HubFocusSessionRow).values(running=False))
            focus = session.get(HubFocusSessionRow, identifier)
            if focus is None:
                focus = HubFocusSessionRow(
                    task_id=identifier, session_id=str(uuid4()), elapsed_seconds=0, reached=False
                )
                session.add(focus)
            focus.running = True
            focus.updated_at = datetime.now(UTC)
        result = self.current(identifier)
        assert result is not None
        return result

    def pause(self, task_id: str) -> None:
        with self.database.session() as session:
            session.execute(
                update(HubFocusSessionRow)
                .where(HubFocusSessionRow.task_id == str(UUID(task_id)))
                .values(running=False)
            )

    def advance(self, seconds: int) -> FocusState | None:
        if type(seconds) is not int or not 0 <= seconds <= 10:
            raise ValueError("Focus increments must be 0–10 active seconds.")
        from personal_hub.tasks.repository import TaskRepository

        result = None
        with self.database.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            focus = session.scalar(
                select(HubFocusSessionRow).where(HubFocusSessionRow.running.is_(True))
            )
            if focus is None:
                return None
            task = session.get(HubTaskRow, focus.task_id)
            if task is None or task.state != "open" or not task.target_minutes:
                session.delete(focus)
                return None
            focus.elapsed_seconds = min(31_536_000, focus.elapsed_seconds + seconds)
            focus.updated_at = datetime.now(UTC)
            first_reach = not focus.reached and focus.elapsed_seconds >= task.target_minutes * 60
            if first_reach:
                focus.elapsed_seconds = task.target_minutes * 60
                focus.reached = True
                focus.running = False
            result = FocusState(
                task.task_id,
                task.title,
                task.time_mode,
                task.target_minutes * 60,
                focus.elapsed_seconds,
                focus.running,
                focus.reached,
            )
            if first_reach and task.time_mode == "goal":
                timestamp = max(
                    datetime.now(UTC),
                    task.created_at.replace(tzinfo=UTC),
                    task.updated_at.replace(tzinfo=UTC),
                )
                settle_focus(session, task, timestamp)
                task.state = "completed"
                task.completed_at = timestamp.replace(tzinfo=None)
                task.updated_at = max(task.updated_at, timestamp.replace(tzinfo=None))
                occurrence = TaskRepository._occurrence_for_task(session, task)
                if occurrence is not None:
                    occurrence.completed_at = task.completed_at
                from personal_hub.logging.repository import capture_completion

                capture_completion(session, task)
                TaskRepository(self.database)._ensure_next_occurrence(session, task, timestamp)
        return result

    def finish(self, task_id: str, *, complete: bool = False, discard: bool = False) -> None:
        from personal_hub.tasks.repository import TaskRepository

        if complete:
            TaskRepository(self.database).complete(task_id)
            return
        with self.database.session() as session:
            task = session.get(HubTaskRow, str(UUID(task_id)))
            if task is None:
                return
            if discard:
                session.execute(
                    delete(HubFocusSessionRow).where(HubFocusSessionRow.task_id == task.task_id)
                )
            else:
                settle_focus(session, task, datetime.now(UTC))
