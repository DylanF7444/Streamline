"""Inert calendar adapter for snapshots received from a provider collector."""

from __future__ import annotations

from personal_hub.calendar.contracts import (
    CalendarQuery,
    CalendarSnapshot,
    CalendarStatus,
)


class ProviderRelayCalendarSource:
    """Expose cached collector events through the normal read-only calendar surface."""

    def __init__(self, snapshot: CalendarSnapshot, *, display_name: str) -> None:
        self.source_id = snapshot.source_id
        self.provider_id = snapshot.provider_id
        self.display_name = display_name
        self.snapshot = snapshot

    async def status(self) -> CalendarStatus:
        return CalendarStatus(
            source_id=self.source_id,
            provider_id=self.provider_id,
            state="stale",
            checked_at=self.snapshot.collected_at,
            item_count=len(self.snapshot.events),
            safe_message="Showing the last collector snapshot; refresh on the collector device.",
        )

    async def list_events(self, query: CalendarQuery) -> CalendarSnapshot:
        selected = set(query.calendar_ids)
        events = tuple(
            event
            for event in self.snapshot.events
            if event.starts_at < query.ends_at
            and event.ends_at > query.starts_at
            and (not selected or event.calendar_id in selected)
        )
        return self.snapshot.model_copy(
            update={
                "events": events[: query.limit],
                "is_complete": self.snapshot.is_complete and len(events) <= query.limit,
            }
        )


__all__ = ["ProviderRelayCalendarSource"]
