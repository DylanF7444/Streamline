"""Offline-aware orchestration for Personal Hub source adapters."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any, TypeVar

from personal_hub.sources.contracts import (
    SourceDisconnectedError,
    SourcePermissionRequiredError,
    SourceResult,
    SourceSnapshot,
    SourceState,
    SourceStatus,
    SourceUnavailableError,
    safe_state_message,
)
from personal_hub.sources.registry import RegisteredSource, SourceRegistry

_T = TypeVar("_T")
_OfflineGetter = Callable[[], bool]


class _OfflineGateHit(Exception):
    """Internal sentinel raised before an adapter callback is invoked."""


def normalize_source_error(error: BaseException) -> tuple[SourceState, str]:
    """Map an adapter failure to a safe state/message pair.

    Raw exception text is intentionally ignored.  Provider responses can contain account
    names, URLs, tokens, or untrusted remote content, none of which belongs in a source
    status or diagnostic report.
    """

    if isinstance(error, (SourcePermissionRequiredError, PermissionError)):
        return SourceState.PERMISSION_REQUIRED, safe_state_message(SourceState.PERMISSION_REQUIRED)
    if isinstance(error, SourceDisconnectedError):
        return SourceState.DISCONNECTED, safe_state_message(SourceState.DISCONNECTED)
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return SourceState.ERROR, "Source refresh timed out."
    if isinstance(error, SourceUnavailableError):
        return SourceState.ERROR, "Source is temporarily unavailable."
    if isinstance(error, (ConnectionError, OSError)):
        return SourceState.DISCONNECTED, "Source is temporarily unavailable."
    return SourceState.ERROR, safe_state_message(SourceState.ERROR)


class SourceService:
    """Coordinate Hub sources with cancellation, timeout, freshness, and offline safety."""

    def __init__(
        self,
        registry: SourceRegistry,
        *,
        timeout_seconds: float = 30.0,
        max_concurrency: int = 2,
        max_items: int = 1_000,
        offline: bool = False,
        offline_getter: _OfflineGetter | None = None,
        offline_lock: bool | _OfflineGetter | None = None,
    ) -> None:
        if max_concurrency < 1 or max_concurrency > 2:
            raise ValueError("Personal Hub source concurrency must be between 1 and 2")
        self._validate_timeout(timeout_seconds)
        if max_items < 1 or max_items > 5_000:
            raise ValueError("Personal Hub source max_items must be between 1 and 5000")
        if offline_getter is not None and offline_lock is not None:
            raise ValueError("Use only one of offline_getter or offline_lock")

        self.registry = registry
        self.timeout_seconds = float(timeout_seconds)
        self.max_concurrency = max_concurrency
        self.max_items = max_items
        selected_offline = offline_lock if offline_lock is not None else offline
        if offline_getter is not None:
            self._offline_getter = offline_getter
            self._offline_value = False
        elif callable(selected_offline):
            self._offline_getter = selected_offline
            self._offline_value = False
        else:
            self._offline_getter = None
            self._offline_value = bool(selected_offline)

        self._concurrency = asyncio.Semaphore(max_concurrency)
        self._source_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_good: dict[str, SourceSnapshot] = {}
        self._last_success_at: dict[str, datetime] = {}
        self._last_results: dict[str, SourceResult] = {}

    @property
    def offline(self) -> bool:
        if self._offline_getter is not None:
            return bool(self._offline_getter())
        return self._offline_value

    @property
    def offline_mode(self) -> bool:
        return self.offline

    def set_offline(self, value: bool) -> None:
        self._offline_getter = None
        self._offline_value = bool(value)

    set_offline_mode = set_offline
    set_offline_lock = set_offline

    def last_good_snapshot(self, source_id: str) -> SourceSnapshot | None:
        return self._last_good.get(source_id)

    def last_success_at(self, source_id: str) -> datetime | None:
        return self._last_success_at.get(source_id)

    def last_result(self, source_id: str) -> SourceResult | None:
        return self._last_results.get(source_id)

    def results(self) -> tuple[SourceResult, ...]:
        return tuple(
            self._last_results[source_id]
            for source_id in self.registry.source_ids
            if source_id in self._last_results
        )

    async def connect(
        self,
        source_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> SourceResult:
        """Connect one adapter if it exposes a connect operation."""

        entry = self.registry.get(source_id)
        if self.offline:
            return self._remember_result(self._offline_result(entry))
        method = getattr(entry.adapter, "connect", None)
        if method is None:
            return self._remember_result(self._result(entry, SourceState.READY))
        started = time.monotonic()
        try:
            value = await self._invoke(entry, method, timeout_seconds)
            if isinstance(value, (SourceStatus, Mapping)):
                return self._remember_result(self._status_result(entry, value, started))
            return self._remember_result(self._result(entry, SourceState.READY, started=started))
        except _OfflineGateHit:
            return self._remember_result(self._offline_result(entry, started=started))
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            return self._remember_result(self._failure_result(entry, error, started=started))

    async def disconnect(
        self,
        source_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> SourceResult:
        """Disconnect one adapter without discarding its registered definition."""

        entry = self.registry.get(source_id)
        if self.offline:
            return self._remember_result(self._offline_result(entry))
        method = getattr(entry.adapter, "disconnect", None)
        started = time.monotonic()
        if method is not None:
            try:
                await self._invoke(entry, method, timeout_seconds)
            except _OfflineGateHit:
                return self._remember_result(self._offline_result(entry, started=started))
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                return self._remember_result(self._failure_result(entry, error, started=started))
        self._last_good.pop(source_id, None)
        self._last_success_at.pop(source_id, None)
        return self._remember_result(self._result(entry, SourceState.DISCONNECTED, started=started))

    async def status(
        self,
        source_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> SourceResult:
        """Read one adapter's status without refreshing source data."""

        entry = self.registry.get(source_id)
        if self.offline:
            return self._remember_result(self._offline_result(entry))
        method = getattr(entry.adapter, "status", None)
        if method is None:
            return self._remember_result(self._result(entry, SourceState.READY))
        started = time.monotonic()
        try:
            value = await self._invoke(entry, method, timeout_seconds)
            return self._remember_result(self._status_result(entry, value, started))
        except _OfflineGateHit:
            return self._remember_result(self._offline_result(entry, started=started))
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            return self._remember_result(self._failure_result(entry, error, started=started))

    async def refresh(
        self,
        source_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> SourceResult:
        """Refresh one source, preserving its last good snapshot on failure."""

        entry = self.registry.get(source_id)
        if self.offline:
            return self._remember_result(self._offline_result(entry))
        method = getattr(entry.adapter, "refresh", None)
        if method is None:
            return self._remember_result(
                self._failure_result(entry, TypeError("missing refresh"), started=time.monotonic())
            )
        started = time.monotonic()
        try:
            value = await self._invoke(entry, method, timeout_seconds)
            result = self._snapshot_result(entry, value, started)
            if (
                result.state in (SourceState.READY, SourceState.EMPTY)
                and result.snapshot is not None
            ):
                self._last_good[source_id] = result.snapshot
                self._last_success_at[source_id] = result.checked_at
            return self._remember_result(result)
        except _OfflineGateHit:
            return self._remember_result(self._offline_result(entry, started=started))
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            return self._remember_result(self._failure_result(entry, error, started=started))

    async def refresh_all(
        self,
        source_ids: Iterable[str] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, SourceResult]:
        """Refresh all selected sources independently with at most two active calls."""

        selected = tuple(self.registry.enabled_source_ids if source_ids is None else source_ids)
        tasks = [
            asyncio.create_task(self.refresh(source_id, timeout_seconds=timeout_seconds))
            for source_id in selected
        ]
        values = await asyncio.gather(*tasks, return_exceptions=True)
        results: dict[str, SourceResult] = {}
        for source_id, value in zip(selected, values, strict=True):
            if isinstance(value, asyncio.CancelledError):
                raise value
            if isinstance(value, BaseException):
                # SourceService handles ordinary adapter failures.  This branch is
                # defensive for unusual BaseException subclasses and keeps unrelated
                # source results available to the caller.
                entry = self.registry.get(source_id)
                results[source_id] = self._remember_result(
                    self._failure_result(entry, value, started=time.monotonic())
                )
            else:
                results[source_id] = value
        return results

    async def _invoke(
        self,
        entry: RegisteredSource,
        method: Callable[[], Awaitable[_T]],
        timeout_seconds: float | None,
    ) -> _T:
        """Invoke one adapter method only after all offline gates have passed."""

        timeout = (
            self.timeout_seconds
            if timeout_seconds is None
            else self._validate_timeout(timeout_seconds)
        )
        async with self._source_locks[entry.source_id], self._concurrency:
            # Recheck after waiting for both locks.  This prevents a queued call from
            # touching the provider when offline mode was enabled while it waited.
            if self.offline:
                raise _OfflineGateHit
            callback_result = method()
            if not inspect.isawaitable(callback_result):
                raise TypeError("Source adapter operations must return awaitables")
            async with asyncio.timeout(timeout):
                return await callback_result

    def _snapshot_result(
        self,
        entry: RegisteredSource,
        value: SourceSnapshot | SourceStatus | SourceResult | Mapping[str, Any],
        started: float,
    ) -> SourceResult:
        if isinstance(value, SourceResult):
            if value.source_id != entry.source_id or value.provider_id != entry.provider_id:
                raise ValueError("source result identity mismatch")
            snapshot = value.snapshot or self._last_good.get(entry.source_id)
            if value.state in (SourceState.READY, SourceState.EMPTY) and snapshot is not None:
                snapshot = self._bounded_snapshot(entry, snapshot)
            return self._result(
                entry,
                value.state,
                snapshot=snapshot,
                started=started,
                safe_message=safe_state_message(value.state),
                last_success_at=value.last_success_at,
            )
        if isinstance(value, SourceStatus):
            return self._status_result(entry, value, started)
        if isinstance(value, Mapping):
            value = SourceSnapshot.model_validate(
                {
                    "source_id": entry.source_id,
                    "provider_id": entry.provider_id,
                    "collected_at": datetime.now(UTC),
                    **value,
                }
            )
        if not isinstance(value, SourceSnapshot):
            raise TypeError("Source refresh returned an invalid snapshot")
        if value.source_id != entry.source_id or value.provider_id != entry.provider_id:
            raise ValueError("source snapshot identity mismatch")
        bounded = self._bounded_snapshot(entry, value)
        state = SourceState.EMPTY if not bounded.items else SourceState.READY
        return self._result(
            entry,
            state,
            snapshot=bounded,
            started=started,
            safe_message=safe_state_message(state),
            last_success_at=bounded.collected_at,
        )

    def _status_result(
        self,
        entry: RegisteredSource,
        value: SourceStatus | Mapping[str, Any] | Any,
        started: float,
    ) -> SourceResult:
        if isinstance(value, Mapping):
            value = SourceStatus.model_validate(
                {
                    "source_id": entry.source_id,
                    "provider_id": entry.provider_id,
                    "checked_at": datetime.now(UTC),
                    **value,
                }
            )
        if not isinstance(value, SourceStatus):
            raise TypeError("Source status returned an invalid status")
        if value.source_id != entry.source_id or value.provider_id != entry.provider_id:
            raise ValueError("source status identity mismatch")
        snapshot = self._last_good.get(entry.source_id)
        state = value.state
        if state in (SourceState.READY, SourceState.EMPTY):
            item_count = value.item_count
        else:
            item_count = len(snapshot.items) if snapshot is not None else value.item_count
        if state == SourceState.ERROR and snapshot is not None:
            state = SourceState.STALE
        if state == SourceState.STALE and snapshot is None:
            state = SourceState.ERROR
        return self._result(
            entry,
            state,
            snapshot=snapshot,
            started=started,
            item_count=item_count,
            safe_message=safe_state_message(state),
            last_success_at=self._last_success_at.get(entry.source_id),
            checked_at=value.checked_at,
        )

    def _failure_result(
        self,
        entry: RegisteredSource,
        error: BaseException,
        *,
        started: float,
    ) -> SourceResult:
        state, _ = normalize_source_error(error)
        snapshot = self._last_good.get(entry.source_id)
        if snapshot is not None and state == SourceState.ERROR:
            state = SourceState.STALE
        return self._result(
            entry,
            state,
            snapshot=snapshot,
            started=started,
            item_count=len(snapshot.items) if snapshot is not None else 0,
            safe_message=(
                "Showing the last successful source snapshot after a refresh failure."
                if state == SourceState.STALE
                else safe_state_message(state)
            ),
            last_success_at=self._last_success_at.get(entry.source_id),
        )

    def _offline_result(
        self,
        entry: RegisteredSource,
        *,
        started: float | None = None,
    ) -> SourceResult:
        snapshot = self._last_good.get(entry.source_id)
        return self._result(
            entry,
            SourceState.OFFLINE,
            snapshot=snapshot,
            started=started,
            item_count=len(snapshot.items) if snapshot is not None else 0,
            safe_message=safe_state_message(SourceState.OFFLINE),
            last_success_at=self._last_success_at.get(entry.source_id),
        )

    def _result(
        self,
        entry: RegisteredSource,
        state: SourceState,
        *,
        snapshot: SourceSnapshot | None = None,
        started: float | None = None,
        item_count: int | None = None,
        safe_message: str | None = None,
        last_success_at: datetime | None = None,
        checked_at: datetime | None = None,
    ) -> SourceResult:
        if snapshot is not None:
            snapshot = self._bounded_snapshot(entry, snapshot)
        count = len(snapshot.items) if snapshot is not None else (item_count or 0)
        return SourceResult(
            source_id=entry.source_id,
            provider_id=entry.provider_id,
            display_name=entry.definition.display_name,
            state=state,
            checked_at=checked_at or datetime.now(UTC),
            snapshot=snapshot,
            last_success_at=last_success_at,
            item_count=count,
            duration_ms=max(0.0, (time.monotonic() - started) * 1000)
            if started is not None
            else 0.0,
            safe_message=safe_message or safe_state_message(state),
        )

    def _bounded_snapshot(
        self, entry: RegisteredSource, snapshot: SourceSnapshot
    ) -> SourceSnapshot:
        if snapshot.source_id != entry.source_id or snapshot.provider_id != entry.provider_id:
            raise ValueError("source snapshot identity mismatch")
        if len(snapshot.items) <= self.max_items:
            return snapshot
        return snapshot.model_copy(update={"items": snapshot.items[: self.max_items]})

    def _remember_result(self, result: SourceResult) -> SourceResult:
        self._last_results[result.source_id] = result
        return result

    @staticmethod
    def _validate_timeout(value: float) -> float:
        if value <= 0 or value > 300:
            raise ValueError(
                "Personal Hub source timeout must be greater than 0 and at most 300 seconds"
            )
        return float(value)


# Friendly aliases preserve a compact API for callers while keeping one implementation.
HubSourceService = SourceService
SourceManager = SourceService

__all__ = [
    "HubSourceService",
    "SourceManager",
    "SourceService",
    "normalize_source_error",
]
