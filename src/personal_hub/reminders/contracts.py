"""Durable-identity contracts for local task reminders."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from personal_hub.contracts import InboxItem, SourceRef


class ReminderCandidate(BaseModel):
    """A due reminder whose claim has succeeded in the injected durable store."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
    )

    occurrence_key: str = Field(min_length=1, max_length=256)
    task_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=240)
    priority: str = Field(default="none", pattern=r"^(?:none|low|normal|high)$")
    due_at: datetime
    fire_at: datetime
    created_at: datetime
    offset_minutes: int = Field(ge=0, le=1_000_000)
    due_is_all_day: bool = False

    @property
    def occurrence_id(self) -> str:
        return self.occurrence_key

    @property
    def reminder_id(self) -> str:
        return self.occurrence_key

    @field_validator("due_at", "fire_at", "created_at")
    @classmethod
    def _timestamps_are_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("reminder timestamps must be timezone-aware")
        return value

    def to_inbox_item(self) -> InboxItem:
        """Convert to a safe local Inbox candidate without notes or provider data."""

        item_id = self.occurrence_key
        if len(item_id) > 128:
            import hashlib

            item_id = f"task-reminder:{hashlib.sha256(item_id.encode()).hexdigest()[:32]}"
        return InboxItem(
            item_id=item_id,
            kind="task_reminder",
            title=self.title,
            safe_summary="A scheduled task reminder is due.",
            reason="scheduled task reminder",
            source=SourceRef(
                provider_id="local-tasks",
                resource_id=self.task_id,
                display_name="Tasks",
            ),
            created_at=self.created_at,
            severity="attention" if self.priority == "high" else "info",
            navigation_target=f"hub://tasks/{self.task_id}",
            deduplication_key=self.occurrence_key,
            metadata={
                "offset_minutes": self.offset_minutes,
                "all_day": self.due_is_all_day,
            },
        )

    as_inbox_item = to_inbox_item

    inbox_item = to_inbox_item


@runtime_checkable
class ReminderClaimStoreProtocol(Protocol):
    """Minimal atomic claim boundary used to survive service restarts."""

    def claim(self, occurrence_key: str, *, claimed_at: datetime) -> bool: ...


class InMemoryReminderClaimStore:
    """Deterministic claim store useful for tests and ephemeral local previews."""

    def __init__(self) -> None:
        self._claimed: set[str] = set()

    def claim(self, occurrence_key: str, *, claimed_at: datetime) -> bool:
        del claimed_at
        if occurrence_key in self._claimed:
            return False
        self._claimed.add(occurrence_key)
        return True

    claim_occurrence = claim

    def has_claimed(self, occurrence_key: str) -> bool:
        return occurrence_key in self._claimed

    @property
    def claimed_keys(self) -> frozenset[str]:
        return frozenset(self._claimed)


# A concise alias makes the durable identity explicit at call sites.
ReminderOccurrence = ReminderCandidate


__all__ = [
    "InMemoryReminderClaimStore",
    "ReminderCandidate",
    "ReminderClaimStoreProtocol",
    "ReminderOccurrence",
]
