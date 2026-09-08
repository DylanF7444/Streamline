"""Bounded watch scheduling, extraction, typed transition evaluation, and alerting."""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from urllib.parse import urlsplit
from uuid import UUID

from personal_hub.contracts import InboxItem, SourceRef
from personal_hub.watches.contracts import (
    AlertEvent,
    WatchDefinition,
    WatchObservation,
    WatchOfflineError,
    WatchParseError,
    WatchPermissionRequiredError,
    WatchPolicyError,
    WatchResult,
    WatchSyncState,
    WatchUnavailableError,
    WatchValue,
)
from personal_hub.watches.extractor import extract_observation
from personal_hub.watches.fetcher import PageFetcher
from personal_hub.watches.repository import WatchRepository

_MESSAGES = {
    "ready": "Watch checked successfully.",
    "stale": "Showing the last successful watch observation.",
    "offline": "Watch refresh is paused by offline mode.",
    "permission_required": "Watch access requires attention.",
    "disconnected": "The watched origin is unavailable.",
    "error": "Watch refresh failed safely.",
}


def _now(value: datetime | None = None) -> datetime:
    timestamp = value or datetime.now(UTC)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("watch clock must be timezone-aware")
    return timestamp.astimezone(UTC)


def _number(value: WatchValue) -> float | None:
    return float(value) if type(value) in {float, int} else None


def _condition_transition(
    watch: WatchDefinition,
    previous: WatchObservation | None,
    current: WatchObservation,
) -> tuple[str, str] | None:
    condition = watch.condition
    before = None if previous is None else previous.normalized_value
    after = current.normalized_value
    prior_fingerprint = None if previous is None else previous.fingerprint
    if previous is not None and prior_fingerprint == current.fingerprint:
        return None
    if condition.kind == "changed":
        return "value_changed", "The watched value changed."
    if condition.kind == "contains":
        term = (condition.term or "").casefold()
        current_match = isinstance(after, str) and term in after.casefold()
        previous_match = isinstance(before, str) and term in before.casefold()
        return (
            ("term_matched", "The configured term appeared in the watched value.")
            if current_match and not previous_match
            else None
        )
    if condition.kind == "price_below":
        current_number = _number(after)
        previous_number = _number(before)
        threshold = condition.threshold
        matched = (
            current_number is not None and threshold is not None and current_number < threshold
        )
        matched_before = (
            previous_number is not None and threshold is not None and previous_number < threshold
        )
        if matched and not matched_before:
            return (
                "price_below",
                f"Price moved from {_display(before)} to {_display(after)}, below {_display(threshold)}.",
            )
        return None
    if condition.kind == "percentage_decrease":
        current_number = _number(after)
        previous_number = _number(before)
        percentage = condition.percentage
        if (
            current_number is not None
            and previous_number is not None
            and previous_number > 0
            and percentage is not None
            and ((previous_number - current_number) / previous_number) * 100 >= percentage
        ):
            return (
                "percentage_decrease",
                f"Price decreased from {_display(before)} to {_display(after)} by at least {percentage:g}%.",
            )
        return None
    if condition.kind == "restocked" and after is True and before is not True:
        return "restocked", "The product changed from unavailable to available."
    return None


def _display(value: WatchValue) -> str:
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    if isinstance(value, bool):
        return "available" if value else "unavailable"
    if value is None:
        return "unknown"
    normalized = " ".join(value.split())
    return normalized if len(normalized) <= 80 else "a different value"


def _deduplication_key(watch: WatchDefinition, reason: str, observation: WatchObservation) -> str:
    bucket = observation.fingerprint
    payload = f"{watch.watch_id}:{reason}:{bucket}"
    return f"watch:{sha256(payload.encode('utf-8')).hexdigest()}"


def _origin_key(watch: WatchDefinition) -> str:
    parts = urlsplit(str(watch.source_url))
    return f"{parts.scheme.casefold()}://{parts.netloc.casefold()}"


class WatchService:
    """One priority/due scheduler boundary with per-origin and per-watch safety."""

    def __init__(
        self,
        repository: WatchRepository,
        fetcher: PageFetcher | object | None = None,
        *,
        offline: bool = False,
        concurrency: int = 2,
        random_source: random.Random | None = None,
    ) -> None:
        if not 1 <= concurrency <= 2:
            raise ValueError("watch concurrency must be between 1 and 2")
        self.repository = repository
        self.fetcher = fetcher or PageFetcher(offline=offline)
        self.offline = bool(offline)
        self._semaphore = asyncio.Semaphore(concurrency)
        self._origin_locks: dict[str, asyncio.Lock] = {}
        self._in_flight: set[UUID] = set()
        self._random = random_source or random.Random()

    def set_offline(self, value: bool) -> None:
        self.offline = bool(value)
        callback = getattr(self.fetcher, "set_offline", None)
        if callable(callback):
            callback(value)

    def due_watch_ids(self, *, now: datetime | None = None) -> tuple[UUID, ...]:
        timestamp = _now(now)
        due: list[tuple[datetime, UUID]] = []
        for watch in self.repository.list(enabled=True, limit=10_000):
            state = self.repository.sync_state(watch.watch_id)
            eligible = watch.created_at
            if state is not None and state.last_checked_at is not None:
                eligible = state.last_checked_at + timedelta(minutes=watch.refresh_minutes)
            if state is not None and state.backoff_until is not None:
                eligible = max(eligible, state.backoff_until)
            if eligible <= timestamp:
                due.append((eligible, watch.watch_id))
        return tuple(identifier for _eligible, identifier in sorted(due))

    async def refresh_due(self, *, now: datetime | None = None) -> dict[UUID, WatchResult]:
        timestamp = _now(now)
        identifiers = self.due_watch_ids(now=timestamp)
        values = await asyncio.gather(*(self.refresh(item, now=timestamp) for item in identifiers))
        return dict(zip(identifiers, values, strict=True))

    async def refresh(
        self,
        watch_id: UUID | str,
        *,
        force: bool = False,
        now: datetime | None = None,
    ) -> WatchResult:
        timestamp = _now(now)
        watch = self.repository.get(watch_id)
        if watch is None:
            raise KeyError("watch does not exist")
        if not watch.enabled:
            return self._result(watch, "stale", timestamp)
        state = self.repository.sync_state(watch.watch_id) or WatchSyncState(
            watch_id=watch.watch_id
        )
        if self.offline:
            return self._failure(watch, state, "offline", timestamp)
        if state.backoff_until is not None and timestamp < state.backoff_until:
            return self._result(watch, state.state, timestamp)
        if (
            not force
            and state.last_checked_at is not None
            and timestamp < state.last_checked_at + timedelta(minutes=watch.refresh_minutes)
        ):
            return self._result(watch, state.state, timestamp)
        if watch.watch_id in self._in_flight:
            return self._result(watch, state.state, timestamp)
        self._in_flight.add(watch.watch_id)
        try:
            async with (
                self._semaphore,
                self._origin_locks.setdefault(_origin_key(watch), asyncio.Lock()),
            ):
                return await self._collect(watch, state, timestamp)
        finally:
            self._in_flight.discard(watch.watch_id)

    async def test_now(
        self,
        watch: WatchDefinition,
        *,
        now: datetime | None = None,
    ) -> WatchObservation:
        """Test extraction without mutating a baseline, observation, alert, or Inbox."""

        timestamp = _now(now)
        if self.offline:
            raise WatchOfflineError("Watch refresh is paused by offline mode.")
        response = await self.fetcher.fetch(watch)
        if response.status_code in {401, 403}:
            raise WatchPermissionRequiredError("Watch access requires attention.")
        if response.status_code == 429 or response.status_code >= 500:
            raise WatchUnavailableError("The watched origin is unavailable.")
        return extract_observation(watch, response, checked_at=timestamp)

    async def _collect(
        self,
        watch: WatchDefinition,
        state: WatchSyncState,
        timestamp: datetime,
    ) -> WatchResult:
        try:
            response = await self.fetcher.fetch(watch)
            if response.status_code in {401, 403}:
                raise WatchPermissionRequiredError("Watch access requires attention.")
            if response.status_code == 429 or response.status_code >= 500:
                raise WatchUnavailableError("The watched origin is unavailable.")
            observation = extract_observation(watch, response, checked_at=timestamp)
        except WatchOfflineError as error:
            return self._failure(watch, state, "offline", timestamp, error)
        except WatchPermissionRequiredError as error:
            return self._failure(watch, state, "permission_required", timestamp, error)
        except WatchUnavailableError as error:
            return self._failure(watch, state, "disconnected", timestamp, error)
        except (WatchPolicyError, WatchParseError) as error:
            return self._failure(watch, state, "error", timestamp, error)
        except Exception:
            return self._failure(watch, state, "error", timestamp)

        previous = self.repository.latest_observation(watch.watch_id)
        first = previous is None and state.baseline_fingerprint is None
        transition = _condition_transition(watch, previous, observation)
        if first and not watch.condition.alert_on_current_match:
            transition = None
        alert: AlertEvent | None = None
        inbox: InboxItem | None = None
        if transition is not None and not self._cooldown_active(watch, timestamp):
            reason, summary = transition
            alert = AlertEvent(
                watch_id=watch.watch_id,
                reason_code=reason,
                title=watch.name,
                safe_summary=summary,
                previous_value=None if previous is None else previous.normalized_value,
                current_value=observation.normalized_value,
                observed_at=timestamp,
                deduplication_key=_deduplication_key(watch, reason, observation),
            )
            inbox = InboxItem(
                item_id=f"watch.{alert.alert_id}",
                kind="watch_alert",
                title=watch.name,
                safe_summary=summary,
                reason=reason.replace("_", " ").capitalize(),
                source=SourceRef(
                    provider_id="web-watch",
                    connection_id=str(watch.watch_id),
                    display_name="Web watch",
                ),
                created_at=timestamp,
                severity="attention",
                navigation_target=f"hub://watchlists/{watch.watch_id}",
                deduplication_key=alert.deduplication_key,
                metadata={
                    "watch_id": str(watch.watch_id),
                    "reason_code": reason,
                    "notification_policy": watch.notification_policy,
                },
            )
        next_state = WatchSyncState(
            watch_id=watch.watch_id,
            state="ready",
            last_checked_at=timestamp,
            last_success_at=timestamp,
            failure_count=0,
            safe_error=None,
            baseline_fingerprint=observation.fingerprint,
            baseline_value=observation.normalized_value,
        )
        persisted = self.repository.persist_evaluation(
            observation,
            next_state,
            alert=alert,
            inbox_item=inbox,
        )
        return WatchResult(
            watch_id=watch.watch_id,
            name=watch.name,
            state="ready",
            checked_at=timestamp,
            safe_message=_MESSAGES["ready"],
            observation=observation,
            alert=persisted,
            baseline_established=first,
        )

    def _failure(
        self,
        watch: WatchDefinition,
        state: WatchSyncState,
        failure_state: str,
        timestamp: datetime,
        _error: Exception | None = None,
    ) -> WatchResult:
        failure_count = state.failure_count + (0 if failure_state == "offline" else 1)
        backoff = state.backoff_until
        if failure_state in {"disconnected", "error"}:
            seconds = min(21_600, 60 * (2 ** min(failure_count, 8)))
            seconds += self._random.uniform(0, seconds * 0.2)
            backoff = timestamp + timedelta(seconds=seconds)
        elif failure_state == "permission_required":
            backoff = timestamp + timedelta(hours=24)
        updated = WatchSyncState(
            watch_id=watch.watch_id,
            state=failure_state,
            last_checked_at=timestamp,
            last_success_at=state.last_success_at,
            failure_count=failure_count,
            backoff_until=backoff,
            safe_error=_MESSAGES[failure_state],
            baseline_fingerprint=state.baseline_fingerprint,
            baseline_value=state.baseline_value,
        )
        self.repository.save_sync_state(updated)
        return self._result(watch, failure_state, timestamp)

    def _cooldown_active(self, watch: WatchDefinition, timestamp: datetime) -> bool:
        latest = self.repository.latest_alert(watch.watch_id)
        return bool(
            latest is not None
            and timestamp < latest.observed_at + timedelta(minutes=watch.cooldown_minutes)
        )

    @staticmethod
    def _result(watch: WatchDefinition, state: str, timestamp: datetime) -> WatchResult:
        return WatchResult(
            watch_id=watch.watch_id,
            name=watch.name,
            state=state,
            checked_at=timestamp,
            safe_message=_MESSAGES[state],
        )


__all__ = ["WatchService"]
