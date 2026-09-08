"""Offline-aware message summary refresh and explicit memory-only body viewing."""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime, timedelta

from personal_hub.messages.categories import MessageCategory
from personal_hub.messages.contracts import (
    MessageBody,
    MessageDisconnectedError,
    MessageError,
    MessageInvalidResponseError,
    MessageOfflineError,
    MessagePermissionRequiredError,
    MessageResult,
    MessageSnapshot,
    MessageSource,
    MessageSourceConfig,
    MessageSummary,
    MessageSyncState,
    MessageUnavailableError,
)
from personal_hub.messages.repository import MessageRepository

_MESSAGES = {
    "ready": "Message summaries refreshed successfully.",
    "empty": "No messages match the selected view.",
    "stale": "Showing the last successful message summaries.",
    "offline": "Message refresh is paused by offline mode.",
    "permission_required": "Message source permission is required.",
    "disconnected": "Message source is disconnected.",
    "error": "Message refresh failed safely.",
}


def _now(value: datetime | None = None) -> datetime:
    timestamp = value or datetime.now(UTC)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("message clock must be timezone-aware")
    return timestamp.astimezone(UTC)


class MessageService:
    def __init__(
        self,
        repository: MessageRepository,
        *,
        offline: bool = False,
        concurrency: int = 2,
        random_source: random.Random | None = None,
    ) -> None:
        if not 1 <= concurrency <= 2:
            raise ValueError("message concurrency must be between 1 and 2")
        self.repository = repository
        self.offline = bool(offline)
        self._sources: dict[str, MessageSource] = {}
        self._session_summaries: dict[str, tuple[MessageSummary, ...]] = {}
        self._body_cache: dict[tuple[str, str], MessageBody] = {}
        self._semaphore = asyncio.Semaphore(concurrency)
        self._in_flight: set[str] = set()
        self._random = random_source or random.Random()

    def set_offline(self, value: bool) -> None:
        self.offline = bool(value)
        for source in self._sources.values():
            callback = getattr(source, "set_offline", None)
            if callable(callback):
                callback(value)

    @property
    def source_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._sources))

    def register(self, source: MessageSource) -> None:
        if not isinstance(source, MessageSource):
            raise TypeError("message source does not satisfy MessageSource")
        if source.source_id in self._sources and self._sources[source.source_id] is not source:
            raise ValueError("message source identity is already registered")
        self._sources[source.source_id] = source
        callback = getattr(source, "set_offline", None)
        if callable(callback):
            callback(self.offline)

    def unregister(self, source_id: str) -> None:
        self._sources.pop(source_id, None)
        self._session_summaries.pop(source_id, None)
        self.clear_bodies(source_id=source_id)

    def due_source_ids(self, *, now: datetime | None = None) -> tuple[str, ...]:
        timestamp = _now(now)
        due: list[tuple[datetime, str]] = []
        for config in self.repository.list_sources(enabled=True, limit=10_000):
            state = self.repository.sync_state(config.source_id)
            eligible = config.created_at
            if state and state.last_checked_at:
                eligible = state.last_checked_at + timedelta(minutes=config.refresh_minutes)
            if state and state.backoff_until:
                eligible = max(eligible, state.backoff_until)
            if eligible <= timestamp:
                due.append((eligible, config.source_id))
        return tuple(source_id for _eligible, source_id in sorted(due))

    async def refresh_all(
        self,
        source_ids: tuple[str, ...] | None = None,
        *,
        force: bool = False,
        now: datetime | None = None,
    ) -> dict[str, MessageResult]:
        timestamp = _now(now)
        selected = (
            self.due_source_ids(now=timestamp)
            if source_ids is None and not force
            else tuple(
                config.source_id
                for config in self.repository.list_sources(enabled=True, limit=10_000)
            )
            if source_ids is None
            else source_ids
        )
        values = await asyncio.gather(
            *(self.refresh(source_id, force=force, now=timestamp) for source_id in selected)
        )
        return dict(zip(selected, values, strict=True))

    async def refresh(
        self,
        source_id: str,
        *,
        force: bool = False,
        now: datetime | None = None,
    ) -> MessageResult:
        timestamp = _now(now)
        config = self.repository.get_source(source_id)
        if config is None:
            raise KeyError("message source does not exist")
        state = self.repository.sync_state(source_id) or MessageSyncState(source_id=source_id)
        if self.offline:
            return self._failure(config, state, "offline", timestamp)
        if not config.enabled:
            return self._result(config, state.state, timestamp)
        if state.backoff_until and timestamp < state.backoff_until:
            return self._result(config, state.state, timestamp)
        if (
            not force
            and state.last_checked_at
            and timestamp < state.last_checked_at + timedelta(minutes=config.refresh_minutes)
        ):
            return self._result(config, state.state, timestamp)
        if source_id in self._in_flight:
            return self._result(config, state.state, timestamp)
        source = self._sources.get(source_id)
        if source is None:
            return self._failure(config, state, "disconnected", timestamp)
        self._in_flight.add(source_id)
        try:
            async with self._semaphore:
                try:
                    snapshot = await source.list_summaries(config.query)
                    self._validate_snapshot(config, snapshot)
                except asyncio.CancelledError:
                    raise
                except MessageOfflineError:
                    return self._failure(config, state, "offline", timestamp)
                except MessagePermissionRequiredError:
                    return self._failure(config, state, "permission_required", timestamp)
                except MessageDisconnectedError:
                    return self._failure(config, state, "disconnected", timestamp)
                except MessageUnavailableError:
                    return self._failure(config, state, "stale", timestamp)
                except (MessageInvalidResponseError, MessageError, ValueError, TypeError):
                    return self._failure(config, state, "error", timestamp)
                except Exception:
                    return self._failure(config, state, "error", timestamp)
                self._session_summaries[source_id] = snapshot.summaries
                self.repository.upsert_summaries(
                    config,
                    snapshot.summaries,
                    retained_at=timestamp,
                )
                ready_state = "ready" if snapshot.summaries else "empty"
                updated = MessageSyncState(
                    source_id=source_id,
                    state=ready_state,
                    last_checked_at=timestamp,
                    last_success_at=timestamp,
                    failure_count=0,
                )
                self.repository.save_sync_state(updated)
                return MessageResult(
                    source_id=source_id,
                    provider=config.provider,
                    account_id=config.account_id,
                    state=ready_state,
                    checked_at=timestamp,
                    safe_message=_MESSAGES[ready_state],
                    snapshot=snapshot,
                    last_success_at=timestamp,
                )
        finally:
            self._in_flight.discard(source_id)

    def summaries(
        self,
        *,
        source_id: str | None = None,
        unread: bool | None = None,
        category: MessageCategory | None = None,
        include_spam: bool = True,
        limit: int = 200,
    ) -> tuple[MessageSummary, ...]:
        values = self.repository.list_summaries(
            source_id=source_id,
            unread=unread,
            category=category,
            include_spam=include_spam,
            limit=limit,
        )
        persisted = {(item.source_id, item.message_id) for item in values}
        session_values = (
            self._session_summaries.get(source_id, ())
            if source_id is not None
            else tuple(item for group in self._session_summaries.values() for item in group)
        )
        for item in session_values:
            if (item.source_id, item.message_id) not in persisted and (
                (unread is None or item.unread == unread)
                and (category is None or item.category == category)
                and (include_spam or item.category != "spam")
            ):
                values.append(item)
        values.sort(
            key=lambda item: (item.received_at, item.source_id, item.message_id), reverse=True
        )
        return tuple(values[:limit])

    async def get_body(self, source_id: str, message_id: str) -> MessageBody:
        """Fetch only after explicit UI open; keep the result in process memory."""

        if self.offline:
            raise MessageOfflineError("Message body fetch is paused by offline mode.")
        config = self.repository.get_source(source_id)
        source = self._sources.get(source_id)
        if config is None or source is None:
            raise MessageDisconnectedError("Message source is disconnected.")
        if not any(
            item.message_id == message_id
            for item in self.summaries(source_id=source_id, limit=1_000)
        ):
            raise KeyError("message summary is not in the selected retained view")
        body = await source.get_body(message_id)
        if body.source_id != source_id or body.message_id != message_id:
            raise MessageInvalidResponseError("Message body identity was invalid.")
        self._body_cache[(source_id, message_id)] = body
        return body

    def cached_body(self, source_id: str, message_id: str) -> MessageBody | None:
        return self._body_cache.get((source_id, message_id))

    def clear_bodies(self, *, source_id: str | None = None) -> None:
        if source_id is None:
            self._body_cache.clear()
        else:
            for key in tuple(self._body_cache):
                if key[0] == source_id:
                    self._body_cache.pop(key, None)

    def clear_summaries(self, source_id: str | None = None) -> int:
        if source_id is None:
            self._session_summaries.clear()
        else:
            self._session_summaries.pop(source_id, None)
        return self.repository.clear_summaries(source_id)

    def _failure(
        self,
        config: MessageSourceConfig,
        previous: MessageSyncState,
        state: str,
        timestamp: datetime,
    ) -> MessageResult:
        failures = previous.failure_count + (0 if state == "offline" else 1)
        backoff = previous.backoff_until
        if state in {"stale", "error", "disconnected"}:
            seconds = min(21_600, 60 * (2 ** min(failures, 8)))
            backoff = timestamp + timedelta(
                seconds=seconds + self._random.uniform(0, seconds * 0.2)
            )
        elif state == "permission_required":
            backoff = timestamp + timedelta(hours=24)
        updated = MessageSyncState(
            source_id=config.source_id,
            state=state,
            last_checked_at=timestamp,
            last_success_at=previous.last_success_at,
            failure_count=failures,
            backoff_until=backoff,
            safe_error=_MESSAGES[state],
        )
        self.repository.save_sync_state(updated)
        snapshot = (
            self._snapshot_from_cache(config, timestamp) if state in {"stale", "offline"} else None
        )
        return MessageResult(
            source_id=config.source_id,
            provider=config.provider,
            account_id=config.account_id,
            state=state,
            checked_at=timestamp,
            safe_message=_MESSAGES[state],
            snapshot=snapshot,
            last_success_at=previous.last_success_at,
        )

    def _result(
        self, config: MessageSourceConfig, state: str, timestamp: datetime
    ) -> MessageResult:
        previous = self.repository.sync_state(config.source_id)
        return MessageResult(
            source_id=config.source_id,
            provider=config.provider,
            account_id=config.account_id,
            state=state,
            checked_at=timestamp,
            safe_message=_MESSAGES[state],
            snapshot=self._snapshot_from_cache(config, timestamp),
            last_success_at=None if previous is None else previous.last_success_at,
        )

    def _snapshot_from_cache(
        self, config: MessageSourceConfig, timestamp: datetime
    ) -> MessageSnapshot:
        return MessageSnapshot(
            source_id=config.source_id,
            provider=config.provider,
            account_id=config.account_id,
            collected_at=timestamp,
            summaries=self.summaries(source_id=config.source_id, limit=config.query.limit),
        )

    @staticmethod
    def _validate_snapshot(config: MessageSourceConfig, snapshot: MessageSnapshot) -> None:
        if (
            snapshot.source_id != config.source_id
            or snapshot.provider != config.provider
            or snapshot.account_id != config.account_id
            or len(snapshot.summaries) > config.query.limit
            or any(item.source_id != config.source_id for item in snapshot.summaries)
        ):
            raise MessageInvalidResponseError("Message snapshot crossed its configured boundary.")


__all__ = ["MessageService"]
