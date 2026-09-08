"""Offline-aware, bounded RSS/Atom refresh orchestration.

``FeedRefreshService`` keeps provider state in memory and is intentionally
agnostic about SQLite.  A later repository can persist ``FeedSyncState`` and
``FeedEntry`` values without making the network client aware of storage.  Each
subscription has an independent result and failure path.
"""

from __future__ import annotations

import asyncio
import inspect
import random
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import ValidationError

from personal_hub.feeds.contracts import (
    FeedBackoff,
    FeedOfflineError,
    FeedParseError,
    FeedPermissionRequiredError,
    FeedPolicyError,
    FeedRedirectError,
    FeedResponse,
    FeedResponseTooLargeError,
    FeedResult,
    FeedSnapshot,
    FeedSubscription,
    FeedSyncState,
    FeedSyncStateValue,
    FeedUnavailableError,
)
from personal_hub.feeds.parser import FeedParser, filter_entries
from personal_hub.feeds.security import origin_for_url

_MAX_CONCURRENCY = 2
_DEFAULT_SAFE_MESSAGES: dict[str, str] = {
    "ready": "Feed refreshed successfully.",
    "empty": "Feed contains no matching entries.",
    "stale": "Showing the last successful feed snapshot.",
    "offline": "Feed refresh is paused by offline mode.",
    "permission_required": "Feed access requires attention.",
    "disconnected": "Feed origin is unavailable.",
    "error": "Feed refresh failed safely.",
}


def _utc_now(value: datetime | None = None) -> datetime:
    result = value or datetime.now(UTC)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("feed clock must return a timezone-aware timestamp")
    return result.astimezone(UTC)


def _header(headers: Mapping[str, object], name: str) -> str | None:
    wanted = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == wanted:
            text = str(value).strip()
            return text or None
    return None


async def _invoke_fetch(
    callback: object, subscription: FeedSubscription, kwargs: Mapping[str, object]
) -> object:
    if not callable(callback):
        raise FeedUnavailableError("Feed fetcher is unavailable.")
    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        selected = dict(kwargs)
    else:
        parameters = signature.parameters
        accepts_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
        )
        selected = (
            dict(kwargs)
            if accepts_kwargs
            else {key: value for key, value in kwargs.items() if key in parameters}
        )
    result = callback(subscription, **selected)
    if inspect.isawaitable(result):
        return await result
    return result


class FeedRefreshService:
    """Coordinate feed fetches with offline, conditional, and retry safety."""

    def __init__(
        self,
        fetcher: object,
        parser: FeedParser | None = None,
        *,
        max_concurrency: int = 2,
        max_items: int = 100,
        offline: bool = False,
        offline_getter: Callable[[], bool] | None = None,
        clock: Callable[[], datetime] | None = None,
        backoff_base_seconds: float = 60.0,
        backoff_max_seconds: float = 3_600.0,
        jitter_ratio: float = 0.1,
        timeout_seconds: float | None = None,
        random_source: random.Random | None = None,
        minimum_refresh_minutes: int = 15,
    ) -> None:
        if not callable(getattr(fetcher, "fetch", None)) and not callable(fetcher):
            raise TypeError("Feed fetcher must provide an async fetch() operation")
        if not 1 <= max_concurrency <= _MAX_CONCURRENCY:
            raise ValueError("Feed concurrency must be between 1 and 2")
        if not 1 <= max_items <= 1_000:
            raise ValueError("Feed max_items must be between 1 and 1000")
        if not 15 <= minimum_refresh_minutes <= 7 * 24 * 60:
            raise ValueError("Feed minimum_refresh_minutes must be between 15 and 10080")
        if offline_getter is not None and not callable(offline_getter):
            raise TypeError("offline_getter must be callable")
        if backoff_base_seconds <= 0 or backoff_max_seconds < backoff_base_seconds:
            raise ValueError("Feed backoff bounds are invalid")
        if not 0 <= jitter_ratio <= 1:
            raise ValueError("Feed backoff jitter ratio must be between 0 and 1")
        if timeout_seconds is not None and (timeout_seconds <= 0 or timeout_seconds > 300):
            raise ValueError("Feed timeout must be greater than 0 and at most 300 seconds")
        self.fetcher = fetcher
        self.parser = parser or FeedParser(max_items=max_items)
        self.max_concurrency = max_concurrency
        self.max_items = max_items
        self.minimum_refresh_minutes = minimum_refresh_minutes
        self.timeout_seconds = timeout_seconds
        self._offline_getter = offline_getter
        self._offline_value = bool(offline) if offline_getter is None else False
        self._clock = clock or (lambda: datetime.now(UTC))
        self.backoff_base_seconds = float(backoff_base_seconds)
        self.backoff_max_seconds = float(backoff_max_seconds)
        self.jitter_ratio = float(jitter_ratio)
        self._random = random_source or random.Random()
        self._concurrency = asyncio.Semaphore(max_concurrency)
        self._subscriptions: dict[UUID, FeedSubscription] = {}
        self._subscription_locks: dict[UUID, asyncio.Lock] = {}
        self._origin_locks: dict[str, asyncio.Lock] = {}
        self._last_good: dict[UUID, FeedSnapshot] = {}
        self._last_results: dict[UUID, FeedResult] = {}
        self._sync: dict[UUID, FeedSyncState] = {}
        self._backoff: dict[str, FeedBackoff] = {}

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

    @property
    def subscriptions(self) -> tuple[FeedSubscription, ...]:
        return tuple(self._subscriptions.values())

    @property
    def source_ids(self) -> tuple[UUID, ...]:
        return tuple(self._subscriptions)

    def register(self, subscription: FeedSubscription) -> FeedSubscription:
        if not isinstance(subscription, FeedSubscription):
            raise TypeError("FeedRefreshService expects a FeedSubscription")
        identifier = subscription.subscription_id
        if identifier in self._subscriptions:
            raise ValueError(f"Feed subscription is already registered: {identifier}")
        self._subscriptions[identifier] = subscription
        self._subscription_locks[identifier] = asyncio.Lock()
        state = self._sync.setdefault(identifier, FeedSyncState(subscription_id=identifier))
        if state.backoff_until is not None:
            origin = origin_for_url(str(subscription.feed_url))
            existing = self._backoff.get(origin)
            if (
                existing is None
                or existing.next_allowed_at is None
                or state.backoff_until > existing.next_allowed_at
            ):
                self._backoff[origin] = FeedBackoff(
                    origin,
                    state.failure_count,
                    state.backoff_until,
                )
        return subscription

    add = register

    def upsert(self, subscription: FeedSubscription) -> FeedSubscription:
        if not isinstance(subscription, FeedSubscription):
            raise TypeError("FeedRefreshService expects a FeedSubscription")
        if subscription.subscription_id not in self._subscriptions:
            return self.register(subscription)
        self._subscriptions[subscription.subscription_id] = subscription
        self._subscription_locks.setdefault(subscription.subscription_id, asyncio.Lock())
        self._sync.setdefault(
            subscription.subscription_id,
            FeedSyncState(subscription_id=subscription.subscription_id),
        )
        state = self._sync[subscription.subscription_id]
        if state.backoff_until is not None:
            origin = origin_for_url(str(subscription.feed_url))
            existing = self._backoff.get(origin)
            if (
                existing is None
                or existing.next_allowed_at is None
                or state.backoff_until > existing.next_allowed_at
            ):
                self._backoff[origin] = FeedBackoff(
                    origin,
                    state.failure_count,
                    state.backoff_until,
                )
        return subscription

    def unregister(self, subscription_id: UUID | str) -> FeedSubscription:
        identifier = self._coerce_id(subscription_id)
        try:
            value = self._subscriptions.pop(identifier)
        except KeyError as error:
            raise KeyError(f"Unknown feed subscription: {identifier}") from error
        self._subscription_locks.pop(identifier, None)
        self._last_good.pop(identifier, None)
        self._last_results.pop(identifier, None)
        self._sync.pop(identifier, None)
        return value

    def subscription(self, subscription_id: UUID | str) -> FeedSubscription:
        identifier = self._coerce_id(subscription_id)
        try:
            return self._subscriptions[identifier]
        except KeyError as error:
            raise KeyError(f"Unknown feed subscription: {identifier}") from error

    def sync_state(self, subscription_id: UUID | str) -> FeedSyncState | None:
        return self._sync.get(self._coerce_id(subscription_id))

    def seed_sync_state(self, state: FeedSyncState) -> FeedSyncState:
        """Load persisted conditional/backoff metadata without contacting a source."""

        if not isinstance(state, FeedSyncState):
            raise TypeError("seed_sync_state expects a FeedSyncState")
        self._sync[state.subscription_id] = state
        subscription = self._subscriptions.get(state.subscription_id)
        if subscription is not None and state.backoff_until is not None:
            origin = origin_for_url(str(subscription.feed_url))
            self._backoff[origin] = FeedBackoff(
                origin,
                state.failure_count,
                state.backoff_until,
            )
        return state

    load_state = seed_sync_state

    def seed_last_good(self, snapshot: FeedSnapshot) -> FeedSnapshot:
        """Load a validated cached snapshot for restart-safe 304 handling."""

        if not isinstance(snapshot, FeedSnapshot):
            raise TypeError("seed_last_good expects a FeedSnapshot")
        if any(entry.subscription_id != snapshot.subscription_id for entry in snapshot.entries):
            raise ValueError("feed snapshot entry identity does not match subscription")
        self._last_good[snapshot.subscription_id] = snapshot
        return snapshot

    def restore_state(
        self,
        state: FeedSyncState,
        snapshot: FeedSnapshot | None = None,
    ) -> FeedSyncState:
        """Restore conditional/backoff metadata and an optional cached snapshot."""

        self.seed_sync_state(state)
        if snapshot is not None:
            if snapshot.subscription_id != state.subscription_id:
                raise ValueError("feed snapshot and sync state IDs do not match")
            self.seed_last_good(snapshot)
        return state

    def last_good_snapshot(self, subscription_id: UUID | str) -> FeedSnapshot | None:
        return self._last_good.get(self._coerce_id(subscription_id))

    def last_result(self, subscription_id: UUID | str) -> FeedResult | None:
        return self._last_results.get(self._coerce_id(subscription_id))

    def results(self) -> tuple[FeedResult, ...]:
        return tuple(
            self._last_results[identifier]
            for identifier in self.source_ids
            if identifier in self._last_results
        )

    def backoff(self, origin: str) -> FeedBackoff | None:
        return self._backoff.get(origin_for_url(origin))

    def clear_backoff(self, origin: str) -> None:
        self._backoff.pop(origin_for_url(origin), None)

    async def refresh(
        self,
        subscription: FeedSubscription | UUID | str,
        *,
        force: bool = False,
        now: datetime | None = None,
    ) -> FeedResult:
        """Refresh one subscription; ``force`` bypasses due time, never backoff."""

        selected = (
            subscription
            if isinstance(subscription, FeedSubscription)
            else self.subscription(subscription)
        )
        if selected.subscription_id not in self._subscriptions:
            self.register(selected)
        checked_at = _utc_now(now or self._clock())
        if self.offline:
            return self._remember(self._offline_result(selected, checked_at))
        if not selected.enabled:
            return self._remember(
                self._result(
                    selected,
                    "empty",
                    checked_at=checked_at,
                    snapshot=self._last_good.get(selected.subscription_id),
                    safe_message="Feed subscription is disabled.",
                )
            )
        origin = origin_for_url(str(selected.feed_url))
        current_backoff = self._backoff.get(origin)
        if (
            current_backoff is not None
            and current_backoff.next_allowed_at is not None
            and checked_at < current_backoff.next_allowed_at
        ):
            snapshot = self._last_good.get(selected.subscription_id)
            state: FeedSyncStateValue = "stale" if snapshot is not None else "error"
            return self._remember(
                self._result(
                    selected,
                    state,
                    checked_at=checked_at,
                    snapshot=snapshot,
                    safe_message="Feed refresh is temporarily backed off.",
                    backoff_until=current_backoff.next_allowed_at,
                )
            )
        state = self._sync.get(selected.subscription_id)
        if (
            not force
            and state is not None
            and state.last_checked_at is not None
            and checked_at
            < state.last_checked_at
            + timedelta(minutes=max(self.minimum_refresh_minutes, selected.refresh_minutes))
        ):
            return self._remember(self._not_due_result(selected, checked_at, state))

        lock = self._subscription_locks.setdefault(selected.subscription_id, asyncio.Lock())
        origin_lock = self._origin_locks.setdefault(origin, asyncio.Lock())
        async with lock, origin_lock, self._concurrency:
            # Recheck after waiting for locks so queued work cannot contact the
            # network after offline mode or backoff was enabled.
            checked_at = _utc_now(now or self._clock())
            if self.offline:
                return self._remember(self._offline_result(selected, checked_at))
            current_backoff = self._backoff.get(origin)
            if (
                current_backoff is not None
                and current_backoff.next_allowed_at is not None
                and checked_at < current_backoff.next_allowed_at
            ):
                snapshot = self._last_good.get(selected.subscription_id)
                state = "stale" if snapshot is not None else "error"
                return self._remember(
                    self._result(
                        selected,
                        state,
                        checked_at=checked_at,
                        snapshot=snapshot,
                        safe_message="Feed refresh is temporarily backed off.",
                        backoff_until=current_backoff.next_allowed_at,
                    )
                )
            state = self._sync.get(selected.subscription_id)
            if (
                not force
                and state is not None
                and state.last_checked_at is not None
                and checked_at
                < state.last_checked_at
                + timedelta(minutes=max(self.minimum_refresh_minutes, selected.refresh_minutes))
            ):
                return self._remember(self._not_due_result(selected, checked_at, state))
            return await self._refresh_locked(selected, checked_at)

    refresh_source = refresh
    refresh_feed = refresh
    refresh_subscription = refresh
    fetch = refresh

    async def refresh_all(
        self,
        subscriptions: Iterable[FeedSubscription | UUID | str] | None = None,
        *,
        source_ids: Iterable[FeedSubscription | UUID | str] | None = None,
        force: bool = False,
        now: datetime | None = None,
    ) -> dict[UUID, FeedResult]:
        if subscriptions is not None and source_ids is not None:
            raise ValueError("Use subscriptions or source_ids, not both")
        if source_ids is not None:
            subscriptions = source_ids
        selected = tuple(self._subscriptions) if subscriptions is None else tuple(subscriptions)
        tasks = [asyncio.create_task(self.refresh(item, force=force, now=now)) for item in selected]
        values = await asyncio.gather(*tasks, return_exceptions=True)
        results: dict[UUID, FeedResult] = {}
        for item, value in zip(selected, values, strict=True):
            identifier = (
                item.subscription_id
                if isinstance(item, FeedSubscription)
                else self._coerce_id(item)
            )
            if isinstance(value, asyncio.CancelledError):
                raise value
            if isinstance(value, BaseException):
                subscription = self.subscription(identifier)
                results[identifier] = self._remember(
                    self._failure_result(
                        subscription,
                        _utc_now(now or self._clock()),
                        value,
                    )
                )
            else:
                results[identifier] = value
        return results

    async def _refresh_locked(
        self,
        subscription: FeedSubscription,
        checked_at: datetime,
    ) -> FeedResult:
        identifier = subscription.subscription_id
        previous = self._last_good.get(identifier)
        state = self._sync.get(identifier) or FeedSyncState(subscription_id=identifier)
        state = state.model_copy(update={"last_checked_at": checked_at})
        self._sync[identifier] = state
        started = time.monotonic()
        try:
            kwargs: dict[str, object] = {
                "etag": state.etag,
                "last_modified": state.last_modified,
            }
            callback = getattr(self.fetcher, "fetch", None) or self.fetcher
            if self.timeout_seconds is None:
                raw = await _invoke_fetch(callback, subscription, kwargs)
            else:
                async with asyncio.timeout(self.timeout_seconds):
                    raw = await _invoke_fetch(callback, subscription, kwargs)
            if isinstance(raw, FeedSnapshot):
                if raw.subscription_id != identifier or str(raw.feed_url) != str(
                    subscription.feed_url
                ):
                    raise FeedParseError("Feed snapshot identity did not match its subscription.")
                response = None
                etag = state.etag
                last_modified = state.last_modified
            else:
                response = self._coerce_fetch_value(raw, subscription)
                etag = _header(response.headers, "etag") or state.etag
                last_modified = _header(response.headers, "last-modified") or state.last_modified
            if response is not None and response.status_code == 304:
                if previous is None:
                    raise FeedParseError("Feed returned not-modified without a cached snapshot.")
                snapshot = previous.model_copy(
                    update={
                        "collected_at": checked_at,
                        "etag": etag,
                        "last_modified": last_modified,
                        "not_modified": True,
                    }
                )
                self._last_good[identifier] = snapshot
                self._clear_origin_backoff(subscription, checked_at)
                self._sync[identifier] = state.model_copy(
                    update={
                        "state": "ready" if snapshot.entries else "empty",
                        "etag": etag,
                        "last_modified": last_modified,
                        "last_success_at": checked_at,
                        "failure_count": 0,
                        "backoff_until": None,
                        "safe_error": None,
                    }
                )
                result_state: FeedSyncStateValue = "ready" if snapshot.entries else "empty"
                return self._remember(
                    self._result(
                        subscription,
                        result_state,
                        checked_at=checked_at,
                        snapshot=snapshot,
                        duration_ms=(time.monotonic() - started) * 1000,
                        safe_message="Feed was unchanged; using the cached snapshot.",
                        not_modified=True,
                    )
                )
            if response is not None and response.status_code in {401, 403}:
                raise FeedPermissionRequiredError
            if response is not None and response.status_code in {404, 410}:
                raise FeedUnavailableError
            if response is not None and (response.status_code < 200 or response.status_code >= 300):
                raise FeedUnavailableError
            snapshot = (
                raw
                if isinstance(raw, FeedSnapshot)
                else self._parse_response(
                    response,
                    subscription,
                    checked_at=checked_at,
                    etag=etag,
                    last_modified=last_modified,
                )
            )
            entries = filter_entries(snapshot.entries, subscription)
            snapshot = snapshot.model_copy(update={"entries": entries[: self.max_items]})
            if previous is not None:
                previous_times = {entry.entry_id: entry.first_seen_at for entry in previous.entries}
                snapshot = snapshot.model_copy(
                    update={
                        "entries": tuple(
                            entry.model_copy(
                                update={"first_seen_at": previous_times[entry.entry_id]}
                            )
                            if entry.entry_id in previous_times
                            else entry
                            for entry in snapshot.entries
                        )
                    }
                )
            self._last_good[identifier] = snapshot
            self._clear_origin_backoff(subscription, checked_at)
            result_state = "ready" if snapshot.entries else "empty"
            self._sync[identifier] = state.model_copy(
                update={
                    "state": result_state,
                    "etag": etag,
                    "last_modified": last_modified,
                    "last_success_at": checked_at,
                    "failure_count": 0,
                    "backoff_until": None,
                    "safe_error": None,
                }
            )
            return self._remember(
                self._result(
                    subscription,
                    result_state,
                    checked_at=checked_at,
                    snapshot=snapshot,
                    duration_ms=(time.monotonic() - started) * 1000,
                    safe_message=_DEFAULT_SAFE_MESSAGES[result_state],
                    status_code=None if response is None else response.status_code,
                )
            )
        except asyncio.CancelledError:
            raise
        except FeedOfflineError:
            return self._remember(self._offline_result(subscription, checked_at))
        except FeedPermissionRequiredError as error:
            return self._remember(self._failure_result(subscription, checked_at, error))
        except BaseException as error:
            return self._remember(self._failure_result(subscription, checked_at, error))

    @staticmethod
    def _coerce_fetch_value(value: object, subscription: FeedSubscription) -> FeedResponse:
        if isinstance(value, FeedResponse):
            return value
        if isinstance(value, Mapping):
            status = value.get("status_code", value.get("status", 200))
            body = value.get("body", value.get("content", b""))
            headers = value.get("headers", {})
            url = value.get("url", str(subscription.feed_url))
        elif isinstance(value, (bytes, str)):
            status, body, headers, url = 200, value, {}, str(subscription.feed_url)
        else:
            status = getattr(value, "status_code", getattr(value, "status", 200))
            body = getattr(value, "content", getattr(value, "body", b""))
            headers = getattr(value, "headers", {})
            url = getattr(value, "url", str(subscription.feed_url))
        if isinstance(body, str):
            body = body.encode("utf-8")
        if not isinstance(body, bytes):
            raise FeedUnavailableError("Feed transport returned an invalid body.")
        try:
            return FeedResponse(status_code=int(status), headers=headers, body=body, url=str(url))
        except (TypeError, ValueError) as error:
            raise FeedUnavailableError("Feed transport returned an invalid response.") from error

    def _parse_response(
        self,
        response: FeedResponse,
        subscription: FeedSubscription,
        *,
        checked_at: datetime,
        etag: str | None,
        last_modified: str | None,
    ) -> FeedSnapshot:
        try:
            return self.parser.parse(
                response.body,
                subscription,
                collected_at=checked_at,
                etag=etag,
                last_modified=last_modified,
            )
        except (FeedParseError, ValidationError):
            raise
        except Exception as error:
            raise FeedParseError("Feed response could not be normalized safely.") from error

    def _failure_result(
        self,
        subscription: FeedSubscription,
        checked_at: datetime,
        error: BaseException,
    ) -> FeedResult:
        identifier = subscription.subscription_id
        snapshot = self._last_good.get(identifier)
        if isinstance(error, FeedPermissionRequiredError):
            result_state: FeedSyncStateValue = "permission_required"
            safe_message = _DEFAULT_SAFE_MESSAGES[result_state]
            backoff_until = None
        elif isinstance(error, FeedOfflineError):
            result_state = "offline"
            safe_message = _DEFAULT_SAFE_MESSAGES[result_state]
            backoff_until = None
        elif isinstance(error, FeedUnavailableError):
            result_state = "stale" if snapshot is not None else "disconnected"
            safe_message = (
                _DEFAULT_SAFE_MESSAGES["stale"]
                if snapshot is not None
                else _DEFAULT_SAFE_MESSAGES["disconnected"]
            )
            backoff_until = self._record_failure(subscription, checked_at)
        elif isinstance(error, (FeedPolicyError, FeedRedirectError, FeedResponseTooLargeError)):
            result_state = "stale" if snapshot is not None else "error"
            safe_message = (
                _DEFAULT_SAFE_MESSAGES["stale"]
                if snapshot is not None
                else _DEFAULT_SAFE_MESSAGES["error"]
            )
            backoff_until = None
        else:
            result_state = "stale" if snapshot is not None else "error"
            safe_message = (
                _DEFAULT_SAFE_MESSAGES["stale"]
                if snapshot is not None
                else _DEFAULT_SAFE_MESSAGES["error"]
            )
            backoff_until = self._record_failure(subscription, checked_at)
        state = self._sync.get(identifier) or FeedSyncState(subscription_id=identifier)
        self._sync[identifier] = state.model_copy(
            update={
                "state": result_state,
                "last_checked_at": checked_at,
                "failure_count": state.failure_count + (1 if backoff_until is not None else 0),
                "backoff_until": backoff_until,
                "safe_error": safe_message,
            }
        )
        return self._result(
            subscription,
            result_state,
            checked_at=checked_at,
            snapshot=snapshot,
            safe_message=safe_message,
            backoff_until=backoff_until,
        )

    def _record_failure(self, subscription: FeedSubscription, checked_at: datetime) -> datetime:
        origin = origin_for_url(str(subscription.feed_url))
        previous = self._backoff.get(origin)
        failure_count = (previous.failure_count if previous is not None else 0) + 1
        base = min(
            self.backoff_max_seconds,
            self.backoff_base_seconds * (2 ** min(failure_count - 1, 16)),
        )
        jitter = base * self.jitter_ratio
        delay = max(1.0, base + self._random.uniform(-jitter, jitter))
        next_allowed = checked_at + timedelta(seconds=min(self.backoff_max_seconds, delay))
        self._backoff[origin] = FeedBackoff(origin, failure_count, next_allowed)
        return next_allowed

    def _clear_origin_backoff(self, subscription: FeedSubscription, _checked_at: datetime) -> None:
        self._backoff.pop(origin_for_url(str(subscription.feed_url)), None)

    def _offline_result(self, subscription: FeedSubscription, checked_at: datetime) -> FeedResult:
        snapshot = self._last_good.get(subscription.subscription_id)
        return self._result(
            subscription,
            "offline",
            checked_at=checked_at,
            snapshot=snapshot,
            safe_message=_DEFAULT_SAFE_MESSAGES["offline"],
        )

    def _not_due_result(
        self,
        subscription: FeedSubscription,
        checked_at: datetime,
        state: FeedSyncState,
    ) -> FeedResult:
        snapshot = self._last_good.get(subscription.subscription_id)
        # A floor skip is not itself a freshness failure.  Preserve the
        # persisted ready/empty state; an existing stale/error/permission state
        # remains explicit for the UI to explain.
        result_state: FeedSyncStateValue = state.state
        return self._result(
            subscription,
            result_state,
            checked_at=checked_at,
            snapshot=snapshot,
            safe_message="Feed refresh is not due yet.",
            backoff_until=(
                state.last_checked_at
                + timedelta(minutes=max(self.minimum_refresh_minutes, subscription.refresh_minutes))
                if state.last_checked_at is not None
                else None
            ),
        )

    @staticmethod
    def _coerce_id(value: UUID | str) -> UUID:
        if isinstance(value, UUID):
            return value
        try:
            return UUID(str(value))
        except (ValueError, AttributeError, TypeError) as error:
            raise ValueError("feed subscription ID must be a UUID") from error

    @staticmethod
    def _result(
        subscription: FeedSubscription,
        state: FeedSyncStateValue,
        *,
        checked_at: datetime,
        snapshot: FeedSnapshot | None = None,
        duration_ms: float = 0.0,
        safe_message: str | None = None,
        status_code: int | None = None,
        not_modified: bool = False,
        backoff_until: datetime | None = None,
    ) -> FeedResult:
        return FeedResult(
            subscription_id=subscription.subscription_id,
            feed_url=subscription.feed_url,
            title=subscription.title,
            state=state,
            checked_at=checked_at,
            snapshot=snapshot,
            last_success_at=None if snapshot is None else snapshot.collected_at,
            item_count=0 if snapshot is None else len(snapshot.entries),
            duration_ms=max(0.0, duration_ms),
            safe_message=safe_message or _DEFAULT_SAFE_MESSAGES[state],
            status_code=status_code,
            not_modified=not_modified,
            backoff_until=backoff_until,
        )

    def _remember(self, result: FeedResult) -> FeedResult:
        self._last_results[result.subscription_id] = result
        return result


# Compact aliases for callers that use source/manager terminology.
FeedService = FeedRefreshService
FeedManager = FeedRefreshService
HubFeedService = FeedRefreshService
FeedRefreshCoordinator = FeedRefreshService
FeedOrchestrator = FeedRefreshService


__all__ = [
    "FeedManager",
    "FeedOrchestrator",
    "FeedRefreshCoordinator",
    "FeedRefreshService",
    "FeedService",
    "HubFeedService",
]
