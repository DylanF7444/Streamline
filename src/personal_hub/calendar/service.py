"""Offline-aware calendar orchestration with bounded concurrency and stale snapshots."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from personal_hub.calendar.contracts import (
    CalendarDisconnectedError,
    CalendarEvent,
    CalendarInvalidResponseError,
    CalendarOfflineError,
    CalendarPermissionRequiredError,
    CalendarQuery,
    CalendarResult,
    CalendarSnapshot,
    CalendarSource,
    CalendarState,
    CalendarStatus,
    CalendarUnavailableError,
)

_STATE_MESSAGES: dict[CalendarState, str] = {
    "ready": "Calendar is ready.",
    "empty": "Calendar is ready but has no events.",
    "stale": "Showing the last successful calendar snapshot.",
    "offline": "Calendar refresh is paused by offline mode.",
    "permission_required": "Calendar permission is required.",
    "disconnected": "Calendar is disconnected.",
    "error": "Calendar refresh failed safely.",
}


def normalize_calendar_error(error: BaseException) -> tuple[CalendarState, str]:
    """Map known adapter failures to fixed, non-content-bearing states."""

    if isinstance(error, (CalendarPermissionRequiredError, PermissionError)):
        return "permission_required", _STATE_MESSAGES["permission_required"]
    if isinstance(error, CalendarDisconnectedError):
        return "disconnected", _STATE_MESSAGES["disconnected"]
    if isinstance(error, CalendarOfflineError):
        return "offline", _STATE_MESSAGES["offline"]
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return "error", "Calendar refresh timed out."
    if isinstance(error, CalendarUnavailableError):
        return "error", _STATE_MESSAGES["error"]
    if isinstance(error, CalendarInvalidResponseError):
        return "error", _STATE_MESSAGES["error"]
    if isinstance(error, (ConnectionError, OSError)):
        return "disconnected", _STATE_MESSAGES["disconnected"]
    return "error", _STATE_MESSAGES["error"]


class CalendarService:
    """Coordinate read-only calendar sources without persisting event content."""

    def __init__(
        self,
        sources: Iterable[CalendarSource] | Mapping[str, CalendarSource] | None = None,
        *,
        timeout_seconds: float = 30.0,
        max_concurrency: int = 2,
        max_events: int = 1_000,
        max_calendars: int = 50,
        offline: bool = False,
        offline_getter: Any | None = None,
    ) -> None:
        if not 1 <= max_concurrency <= 2:
            raise ValueError("Personal Hub calendar concurrency must be between 1 and 2")
        self._validate_timeout(timeout_seconds)
        if not 1 <= max_events <= 5_000:
            raise ValueError("Personal Hub calendar max_events must be between 1 and 5000")
        if not 1 <= max_calendars <= 100:
            raise ValueError("Personal Hub calendar max_calendars must be between 1 and 100")
        self.timeout_seconds = float(timeout_seconds)
        self.max_concurrency = max_concurrency
        self.max_events = max_events
        self.max_calendars = max_calendars
        self._offline_getter = offline_getter if callable(offline_getter) else None
        self._offline_value = bool(offline) if self._offline_getter is None else False
        self._sources: dict[str, CalendarSource] = {}
        self._source_locks: dict[str, asyncio.Lock] = {}
        self._concurrency = asyncio.Semaphore(max_concurrency)
        self._last_good: dict[str, CalendarSnapshot] = {}
        self._last_results: dict[str, CalendarResult] = {}
        if sources is not None:
            entries = (
                sources.items()
                if isinstance(sources, Mapping)
                else ((None, item) for item in sources)
            )
            for selected_id, source in entries:
                self.register(source, source_id=selected_id)

    @property
    def offline(self) -> bool:
        return (
            bool(self._offline_getter())
            if self._offline_getter is not None
            else self._offline_value
        )

    @property
    def offline_mode(self) -> bool:
        return self.offline

    def set_offline(self, value: bool) -> None:
        self._offline_getter = None
        self._offline_value = bool(value)

    set_offline_mode = set_offline

    @property
    def source_ids(self) -> tuple[str, ...]:
        return tuple(self._sources)

    def register(self, source: CalendarSource, *, source_id: str | None = None) -> CalendarSource:
        """Register one source with a stable identity and async read method."""

        selected_id = source_id or getattr(source, "source_id", None)
        if not isinstance(selected_id, str) or not selected_id.strip():
            raise ValueError("calendar source must expose a stable source_id")
        selected_id = selected_id.strip()
        if selected_id in self._sources:
            raise ValueError(f"Calendar source is already registered: {selected_id}")
        method = getattr(source, "list_events", None)
        if not callable(method) or not inspect.iscoroutinefunction(method):
            raise TypeError("calendar source must provide async list_events()")
        self._sources[selected_id] = source
        self._source_locks[selected_id] = asyncio.Lock()
        return source

    add = register

    def unregister(self, source_id: str) -> CalendarSource:
        try:
            self._source_locks.pop(source_id, None)
            self._last_good.pop(source_id, None)
            self._last_results.pop(source_id, None)
            return self._sources.pop(source_id)
        except KeyError as error:
            raise KeyError(f"Unknown Personal Hub calendar source: {source_id}") from error

    def source(self, source_id: str) -> CalendarSource:
        try:
            return self._sources[source_id]
        except KeyError as error:
            raise KeyError(f"Unknown Personal Hub calendar source: {source_id}") from error

    def last_good_snapshot(self, source_id: str) -> CalendarSnapshot | None:
        return self._last_good.get(source_id)

    def last_result(self, source_id: str) -> CalendarResult | None:
        return self._last_results.get(source_id)

    def seed_relay_snapshot(self, snapshot: CalendarSnapshot) -> CalendarResult:
        """Seed a validated collector snapshot for an offline consumer device."""

        if not isinstance(snapshot, CalendarSnapshot):
            raise TypeError("calendar relay cache expects CalendarSnapshot")
        source = self.source(snapshot.source_id)
        provider_id = str(getattr(source, "provider_id", snapshot.provider_id))
        if snapshot.provider_id != provider_id:
            raise ValueError("calendar relay provider identity does not match its source")
        self._last_good[snapshot.source_id] = snapshot
        state: CalendarState = "offline" if self.offline else "stale"
        result = CalendarResult(
            source_id=snapshot.source_id,
            provider_id=provider_id,
            state=state,
            checked_at=snapshot.collected_at,
            snapshot=snapshot,
            last_success_at=snapshot.collected_at,
            safe_message=("Showing the last collector snapshot; refresh on the collector device."),
        )
        self._last_results[snapshot.source_id] = result
        return result

    def clear_snapshot(self, source_id: str) -> None:
        """Forget retained provider content while keeping its source registration."""

        self._last_good.pop(source_id, None)
        self._last_results.pop(source_id, None)

    def results(self) -> tuple[CalendarResult, ...]:
        return tuple(
            self._last_results[source_id]
            for source_id in self.source_ids
            if source_id in self._last_results
        )

    async def refresh(
        self,
        source_id: str,
        query: CalendarQuery,
        *,
        timeout_seconds: float | None = None,
    ) -> CalendarResult:
        """Refresh one source, retaining its last-good snapshot on failure."""

        if not isinstance(query, CalendarQuery):
            raise TypeError("calendar refresh expects CalendarQuery")
        source = self.source(source_id)
        if len(query.calendar_ids) > self.max_calendars:
            raise ValueError("calendar query selects too many calendars")
        if self.offline:
            return self._remember(self._offline_result(source_id, source))
        started = time.monotonic()
        try:
            value = await self._invoke(source_id, source, query, timeout_seconds)
            snapshot = self._snapshot(source_id, source, value, query)
            state: CalendarState = "ready" if snapshot.events else "empty"
            result = self._result(
                source_id,
                source,
                state,
                snapshot=snapshot,
                started=started,
                last_success_at=snapshot.collected_at,
            )
            self._last_good[source_id] = snapshot
            return self._remember(result)
        except _OfflineGateHit:
            return self._remember(self._offline_result(source_id, source))
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            state, _ = normalize_calendar_error(error)
            snapshot = self._last_good.get(source_id)
            # A generic provider failure can safely fall back to the last-good
            # snapshot. Permission and connection states remain explicit so the
            # UI can offer the appropriate recovery action.
            if snapshot is not None and state == "error":
                state = "stale"
            result = self._result(
                source_id,
                source,
                state,
                snapshot=snapshot,
                started=started,
                last_success_at=(snapshot.collected_at if snapshot is not None else None),
            )
            return self._remember(result)

    refresh_source = refresh

    async def list_events(
        self,
        query: CalendarQuery,
        *,
        source_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> CalendarResult:
        """Query one source, or the sole registered source when omitted."""

        selected = source_id
        if selected is None:
            if len(self._sources) != 1:
                raise ValueError("source_id is required when multiple calendar sources exist")
            selected = next(iter(self._sources))
        return await self.refresh(selected, query, timeout_seconds=timeout_seconds)

    query = list_events
    fetch = list_events

    async def refresh_all(
        self,
        query: CalendarQuery,
        source_ids: Iterable[str] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, CalendarResult]:
        """Refresh selected sources independently; one failure does not cancel others."""

        selected = tuple(self.source_ids if source_ids is None else source_ids)
        tasks = [
            asyncio.create_task(self.refresh(source_id, query, timeout_seconds=timeout_seconds))
            for source_id in selected
        ]
        values = await asyncio.gather(*tasks, return_exceptions=True)
        results: dict[str, CalendarResult] = {}
        for source_id, value in zip(selected, values, strict=True):
            if isinstance(value, asyncio.CancelledError):
                raise value
            if isinstance(value, BaseException):
                source = self.source(source_id)
                results[source_id] = self._remember(
                    self._result(
                        source_id,
                        source,
                        "error",
                        snapshot=self._last_good.get(source_id),
                        safe_message=_STATE_MESSAGES["error"],
                    )
                )
            else:
                results[source_id] = value
        return results

    async def status(
        self,
        source_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> CalendarStatus:
        """Return an operational status without fetching event content."""

        source = self.source(source_id)
        provider_id = str(getattr(source, "provider_id", "calendar"))
        if self.offline:
            return CalendarStatus(
                source_id=source_id,
                provider_id=provider_id,
                state="offline",
                checked_at=datetime.now(UTC),
                item_count=(
                    len(self._last_good[source_id].events) if source_id in self._last_good else 0
                ),
                safe_message=_STATE_MESSAGES["offline"],
            )

        method = getattr(source, "status", None)
        if not callable(method):
            last = self._last_results.get(source_id)
            if last is not None:
                return CalendarStatus(
                    source_id=source_id,
                    provider_id=provider_id,
                    state=last.state,
                    checked_at=last.checked_at,
                    item_count=0 if last.snapshot is None else len(last.snapshot.events),
                    safe_message=last.safe_message,
                )
            return CalendarStatus(
                source_id=source_id,
                provider_id=provider_id,
                state="empty",
                checked_at=datetime.now(UTC),
                item_count=0,
                safe_message=_STATE_MESSAGES["empty"],
            )

        try:
            value = await self._invoke_status(source_id, source, method, timeout_seconds)
            if not isinstance(value, CalendarStatus):
                raise CalendarInvalidResponseError("calendar source returned an invalid status")
            if value.source_id != source_id or value.provider_id != provider_id:
                raise CalendarInvalidResponseError("calendar status identity mismatch")
            return value
        except _OfflineGateHit:
            return CalendarStatus(
                source_id=source_id,
                provider_id=provider_id,
                state="offline",
                checked_at=datetime.now(UTC),
                item_count=0,
                safe_message=_STATE_MESSAGES["offline"],
            )
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            state, message = normalize_calendar_error(error)
            if state == "error" and source_id in self._last_good:
                state = "stale"
                message = _STATE_MESSAGES["stale"]
            snapshot = self._last_good.get(source_id)
            return CalendarStatus(
                source_id=source_id,
                provider_id=provider_id,
                state=state,
                checked_at=datetime.now(UTC),
                item_count=0 if snapshot is None else len(snapshot.events),
                safe_message=message,
            )

    async def _invoke(
        self,
        source_id: str,
        source: CalendarSource,
        query: CalendarQuery,
        timeout_seconds: float | None,
    ) -> object:
        timeout = (
            self.timeout_seconds
            if timeout_seconds is None
            else self._validate_timeout(timeout_seconds)
        )
        async with self._source_locks[source_id], self._concurrency:
            # This second gate is deliberately inside the queue so waiting work
            # cannot touch a transport after offline mode is enabled.
            if self.offline:
                raise _OfflineGateHit
            callback = source.list_events(query)
            if not inspect.isawaitable(callback):
                raise TypeError("calendar source list_events() must return an awaitable")
            async with asyncio.timeout(timeout):
                return await callback

    async def _invoke_status(
        self,
        source_id: str,
        source: CalendarSource,
        method: object,
        timeout_seconds: float | None = None,
    ) -> object:
        timeout = (
            self.timeout_seconds
            if timeout_seconds is None
            else self._validate_timeout(timeout_seconds)
        )
        async with self._source_locks[source_id], self._concurrency:
            if self.offline:
                raise _OfflineGateHit
            callback = method()
            if not inspect.isawaitable(callback):
                raise TypeError("calendar source status() must return an awaitable")
            async with asyncio.timeout(timeout):
                return await callback

    def _snapshot(
        self,
        source_id: str,
        source: CalendarSource,
        value: object,
        query: CalendarQuery,
    ) -> CalendarSnapshot:
        if not isinstance(value, CalendarSnapshot):
            raise CalendarInvalidResponseError("calendar source returned an invalid snapshot")
        if value.source_id != source_id:
            raise CalendarInvalidResponseError("calendar snapshot identity mismatch")
        provider_id = str(getattr(source, "provider_id", value.provider_id))
        if value.provider_id != provider_id:
            raise CalendarInvalidResponseError("calendar snapshot provider mismatch")
        events = tuple(sorted(value.events, key=_event_sort_key))
        limit = min(query.limit, self.max_events)
        is_complete = value.is_complete
        if len(events) > limit:
            is_complete = False
            events = events[: min(query.limit, self.max_events)]
        selected = value.selected_calendar_ids or query.calendar_ids
        return value.model_copy(
            update={
                "events": events,
                "is_complete": is_complete,
                "selected_calendar_ids": selected,
            }
        )

    def _offline_result(self, source_id: str, source: CalendarSource) -> CalendarResult:
        snapshot = self._last_good.get(source_id)
        return self._result(
            source_id,
            source,
            "offline",
            snapshot=snapshot,
            last_success_at=None if snapshot is None else snapshot.collected_at,
            safe_message=_STATE_MESSAGES["offline"],
        )

    def _result(
        self,
        source_id: str,
        source: CalendarSource,
        state: CalendarState,
        *,
        snapshot: CalendarSnapshot | None = None,
        started: float | None = None,
        last_success_at: datetime | None = None,
        safe_message: str | None = None,
    ) -> CalendarResult:
        return CalendarResult(
            source_id=source_id,
            provider_id=str(getattr(source, "provider_id", "calendar")),
            state=state,
            checked_at=datetime.now(UTC),
            snapshot=snapshot,
            last_success_at=last_success_at,
            safe_message=safe_message or _STATE_MESSAGES[state],
        )

    def _remember(self, result: CalendarResult) -> CalendarResult:
        self._last_results[result.source_id] = result
        return result

    @staticmethod
    def _validate_timeout(value: float) -> float:
        if value <= 0 or value > 300:
            raise ValueError(
                "Personal Hub calendar timeout must be greater than 0 and at most 300 seconds"
            )
        return float(value)


class _OfflineGateHit(Exception):
    """Internal marker for a queued operation that became offline."""


def _event_sort_key(event: CalendarEvent) -> tuple[object, ...]:
    return (
        event.starts_at.astimezone(UTC),
        0 if event.all_day else 1,
        event.ends_at.astimezone(UTC),
        event.title.casefold(),
        event.calendar_id,
        event.event_id,
    )


HubCalendarService = CalendarService
CalendarManager = CalendarService


__all__ = [
    "CalendarManager",
    "CalendarService",
    "HubCalendarService",
    "normalize_calendar_error",
]
