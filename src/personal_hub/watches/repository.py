"""SQLite persistence for watch definitions, normalized observations, and alerts."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from sqlalchemy import delete, func, select

from personal_hub.contracts import InboxItem
from personal_hub.storage.database import (
    HubDatabase,
    HubWatchAlertRow,
    HubWatchDefinitionRow,
    HubWatchObservationRow,
)
from personal_hub.storage.repositories import InboxRepository
from personal_hub.watches.contracts import (
    AlertEvent,
    NotificationPolicy,
    WatchCondition,
    WatchDefinition,
    WatchKind,
    WatchObservation,
    WatchState,
    WatchSyncState,
    WatchValue,
)


def _db_time(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("watch timestamps must be timezone-aware")
    return value.astimezone(UTC).replace(tzinfo=None)


def _time(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _value(value: str | None) -> WatchValue:
    if value is None:
        return None
    decoded = json.loads(value)
    if decoded is None or type(decoded) in {str, float, bool, int}:
        return float(decoded) if type(decoded) is int else cast(WatchValue, decoded)
    raise ValueError("stored watch value is invalid")


def _uuid(value: UUID | str) -> UUID:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise TypeError("watch_id must be a UUID or UUID string")
    try:
        return UUID(value)
    except ValueError as error:
        raise ValueError("watch_id must be a UUID") from error


class WatchRepository:
    """Own the transactional observation-to-Inbox boundary."""

    def __init__(
        self,
        database: HubDatabase,
        *,
        observation_retention: timedelta = timedelta(days=90),
        max_observations_per_watch: int = 500,
    ) -> None:
        if observation_retention <= timedelta(0):
            raise ValueError("watch observation retention must be positive")
        if not 10 <= max_observations_per_watch <= 10_000:
            raise ValueError("watch observation count must be between 10 and 10000")
        self.database = database
        self.observation_retention = observation_retention
        self.max_observations_per_watch = max_observations_per_watch

    @staticmethod
    def _definition(row: HubWatchDefinitionRow) -> WatchDefinition:
        origins = json.loads(row.approved_redirect_origins_json)
        if not isinstance(origins, list) or any(not isinstance(item, str) for item in origins):
            raise ValueError("stored watch redirect origins are invalid")
        return WatchDefinition(
            watch_id=_uuid(row.watch_id),
            name=row.name,
            kind=cast(WatchKind, row.kind),
            source_url=row.source_url,
            condition=WatchCondition.model_validate_json(row.condition_json),
            refresh_minutes=row.refresh_minutes,
            enabled=bool(row.enabled),
            notification_policy=cast(NotificationPolicy, row.notification_policy),
            cooldown_minutes=row.cooldown_minutes,
            allow_http=bool(row.allow_http),
            reviewed_origin=row.reviewed_origin,
            approved_redirect_origins=tuple(origins),
            created_at=cast(datetime, _time(row.created_at)),
            updated_at=cast(datetime, _time(row.updated_at)),
        )

    @staticmethod
    def _sync(row: HubWatchDefinitionRow) -> WatchSyncState:
        return WatchSyncState(
            watch_id=_uuid(row.watch_id),
            state=cast(WatchState, row.sync_state),
            last_checked_at=_time(row.last_checked_at),
            last_success_at=_time(row.last_success_at),
            failure_count=row.failure_count,
            backoff_until=_time(row.backoff_until),
            safe_error=row.safe_error,
            baseline_fingerprint=row.baseline_fingerprint,
            baseline_value=_value(row.baseline_value_json),
        )

    @staticmethod
    def _observation(row: HubWatchObservationRow) -> WatchObservation:
        metadata = json.loads(row.metadata_json)
        if not isinstance(metadata, dict):
            raise ValueError("stored watch metadata is invalid")
        return WatchObservation(
            observation_id=_uuid(row.observation_id),
            watch_id=_uuid(row.watch_id),
            checked_at=cast(datetime, _time(row.checked_at)),
            fingerprint=row.fingerprint,
            normalized_value=_value(row.normalized_value_json),
            metadata=metadata,
        )

    @staticmethod
    def _alert(row: HubWatchAlertRow) -> AlertEvent:
        return AlertEvent(
            alert_id=_uuid(row.alert_id),
            watch_id=_uuid(row.watch_id),
            reason_code=row.reason_code,
            title=row.title,
            safe_summary=row.safe_summary,
            previous_value=_value(row.previous_value_json),
            current_value=_value(row.current_value_json),
            observed_at=cast(datetime, _time(row.observed_at)),
            deduplication_key=row.deduplication_key,
        )

    def add(self, watch: WatchDefinition) -> WatchDefinition:
        if not isinstance(watch, WatchDefinition):
            raise TypeError("watch repository expects WatchDefinition")
        with self.database.session() as session:
            row = session.get(HubWatchDefinitionRow, str(watch.watch_id))
            if row is None:
                row = HubWatchDefinitionRow(watch_id=str(watch.watch_id))
                session.add(row)
            row.name = watch.name
            row.kind = watch.kind
            row.source_url = str(watch.source_url)
            row.condition_json = watch.condition.model_dump_json()
            row.refresh_minutes = watch.refresh_minutes
            row.enabled = watch.enabled
            row.notification_policy = watch.notification_policy
            row.cooldown_minutes = watch.cooldown_minutes
            row.allow_http = watch.allow_http
            row.reviewed_origin = watch.reviewed_origin
            row.approved_redirect_origins_json = _json(watch.approved_redirect_origins)
            row.created_at = _db_time(watch.created_at)
            row.updated_at = _db_time(watch.updated_at)
        return watch

    save = add

    def get(self, watch_id: UUID | str) -> WatchDefinition | None:
        with self.database.session() as session:
            row = session.get(HubWatchDefinitionRow, str(_uuid(watch_id)))
            return None if row is None else self._definition(row)

    def list(self, *, enabled: bool | None = None, limit: int = 1_000) -> list[WatchDefinition]:
        if not 1 <= limit <= 10_000:
            raise ValueError("watch limit must be between 1 and 10000")
        with self.database.session() as session:
            statement = select(HubWatchDefinitionRow).order_by(
                HubWatchDefinitionRow.name, HubWatchDefinitionRow.watch_id
            )
            if enabled is not None:
                statement = statement.where(HubWatchDefinitionRow.enabled == enabled)
            return [self._definition(row) for row in session.scalars(statement.limit(limit)).all()]

    def delete(self, watch_id: UUID | str) -> bool:
        with self.database.session() as session:
            row = session.get(HubWatchDefinitionRow, str(_uuid(watch_id)))
            if row is None:
                return False
            session.delete(row)
            return True

    def sync_state(self, watch_id: UUID | str) -> WatchSyncState | None:
        with self.database.session() as session:
            row = session.get(HubWatchDefinitionRow, str(_uuid(watch_id)))
            return None if row is None else self._sync(row)

    def save_sync_state(self, state: WatchSyncState) -> WatchSyncState:
        with self.database.session() as session:
            row = session.get(HubWatchDefinitionRow, str(state.watch_id))
            if row is None:
                raise KeyError("watch does not exist")
            self._apply_sync(row, state)
        return state

    def reset_baseline(self, watch_id: UUID | str) -> None:
        """Clear observations and last-good comparison state after semantic edits."""

        identifier = str(_uuid(watch_id))
        with self.database.session() as session:
            row = session.get(HubWatchDefinitionRow, identifier)
            if row is None:
                raise KeyError("watch does not exist")
            session.execute(
                delete(HubWatchObservationRow).where(HubWatchObservationRow.watch_id == identifier)
            )
            row.sync_state = "stale"
            row.last_checked_at = None
            row.last_success_at = None
            row.failure_count = 0
            row.backoff_until = None
            row.safe_error = None
            row.baseline_fingerprint = None
            row.baseline_value_json = None

    @staticmethod
    def _apply_sync(row: HubWatchDefinitionRow, state: WatchSyncState) -> None:
        row.sync_state = state.state
        row.last_checked_at = _db_time(state.last_checked_at)
        row.last_success_at = _db_time(state.last_success_at)
        row.failure_count = state.failure_count
        row.backoff_until = _db_time(state.backoff_until)
        row.safe_error = state.safe_error
        row.baseline_fingerprint = state.baseline_fingerprint
        row.baseline_value_json = _json(state.baseline_value)
        row.updated_at = _db_time(datetime.now(UTC))

    def latest_observation(self, watch_id: UUID | str) -> WatchObservation | None:
        with self.database.session() as session:
            row = session.scalar(
                select(HubWatchObservationRow)
                .where(HubWatchObservationRow.watch_id == str(_uuid(watch_id)))
                .order_by(HubWatchObservationRow.checked_at.desc())
                .limit(1)
            )
            return None if row is None else self._observation(row)

    def list_observations(
        self, watch_id: UUID | str, *, limit: int = 100
    ) -> list[WatchObservation]:
        if not 1 <= limit <= 1_000:
            raise ValueError("observation limit must be between 1 and 1000")
        with self.database.session() as session:
            rows = session.scalars(
                select(HubWatchObservationRow)
                .where(HubWatchObservationRow.watch_id == str(_uuid(watch_id)))
                .order_by(HubWatchObservationRow.checked_at.desc())
                .limit(limit)
            ).all()
            return [self._observation(row) for row in rows]

    def latest_alert(self, watch_id: UUID | str) -> AlertEvent | None:
        with self.database.session() as session:
            row = session.scalar(
                select(HubWatchAlertRow)
                .where(HubWatchAlertRow.watch_id == str(_uuid(watch_id)))
                .order_by(HubWatchAlertRow.observed_at.desc())
                .limit(1)
            )
            return None if row is None else self._alert(row)

    def list_alerts(
        self, *, watch_id: UUID | str | None = None, limit: int = 100
    ) -> list[AlertEvent]:
        if not 1 <= limit <= 1_000:
            raise ValueError("alert limit must be between 1 and 1000")
        with self.database.session() as session:
            statement = select(HubWatchAlertRow).order_by(HubWatchAlertRow.observed_at.desc())
            if watch_id is not None:
                statement = statement.where(HubWatchAlertRow.watch_id == str(_uuid(watch_id)))
            return [self._alert(row) for row in session.scalars(statement.limit(limit)).all()]

    def persist_evaluation(
        self,
        observation: WatchObservation,
        state: WatchSyncState,
        *,
        alert: AlertEvent | None = None,
        inbox_item: InboxItem | None = None,
    ) -> AlertEvent | None:
        """Atomically persist the observation, state, alert, and Inbox projection."""

        if (alert is None) != (inbox_item is None):
            raise ValueError("watch alert and Inbox item must be persisted together")
        if observation.watch_id != state.watch_id:
            raise ValueError("watch observation and state identities differ")
        with self.database.session() as session:
            watch_row = session.get(HubWatchDefinitionRow, str(observation.watch_id))
            if watch_row is None:
                raise KeyError("watch does not exist")
            session.add(
                HubWatchObservationRow(
                    observation_id=str(observation.observation_id),
                    watch_id=str(observation.watch_id),
                    checked_at=_db_time(observation.checked_at),
                    fingerprint=observation.fingerprint,
                    normalized_value_json=_json(observation.normalized_value),
                    metadata_json=_json(observation.metadata),
                )
            )
            self._apply_sync(watch_row, state)
            persisted_alert = alert
            if alert is not None and inbox_item is not None:
                existing = session.scalar(
                    select(HubWatchAlertRow).where(
                        HubWatchAlertRow.deduplication_key == alert.deduplication_key
                    )
                )
                if existing is not None:
                    persisted_alert = None
                else:
                    session.add(
                        HubWatchAlertRow(
                            alert_id=str(alert.alert_id),
                            watch_id=str(alert.watch_id),
                            reason_code=alert.reason_code,
                            title=alert.title,
                            safe_summary=alert.safe_summary,
                            previous_value_json=_json(alert.previous_value),
                            current_value_json=_json(alert.current_value),
                            observed_at=_db_time(alert.observed_at),
                            deduplication_key=alert.deduplication_key,
                            inbox_item_id=inbox_item.item_id,
                        )
                    )
                    session.add(InboxRepository._row(inbox_item))
            self._prune_session(session, observation.watch_id, now=observation.checked_at)
        return persisted_alert

    def _prune_session(self, session, watch_id: UUID, *, now: datetime) -> None:
        cutoff = _db_time(now - self.observation_retention)
        newest_id = session.scalar(
            select(HubWatchObservationRow.observation_id)
            .where(HubWatchObservationRow.watch_id == str(watch_id))
            .order_by(HubWatchObservationRow.checked_at.desc())
            .limit(1)
        )
        retention_filter = [
            HubWatchObservationRow.watch_id == str(watch_id),
            HubWatchObservationRow.checked_at < cutoff,
        ]
        if newest_id is not None:
            # Preserve one comparison baseline even after a long inactive period.
            retention_filter.append(HubWatchObservationRow.observation_id != newest_id)
        session.execute(delete(HubWatchObservationRow).where(*retention_filter))
        retained = session.scalars(
            select(HubWatchObservationRow.observation_id)
            .where(HubWatchObservationRow.watch_id == str(watch_id))
            .order_by(HubWatchObservationRow.checked_at.desc())
            .offset(self.max_observations_per_watch)
        ).all()
        if retained:
            session.execute(
                delete(HubWatchObservationRow).where(
                    HubWatchObservationRow.observation_id.in_(retained)
                )
            )

    def cleanup(self, *, now: datetime | None = None) -> int:
        """Apply observation/alert retention while preserving each watch baseline."""

        timestamp = now or datetime.now(UTC)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("watch cleanup timestamp must be timezone-aware")
        with self.database.session() as session:
            before = int(
                session.scalar(select(func.count()).select_from(HubWatchObservationRow)) or 0
            )
            before += int(session.scalar(select(func.count()).select_from(HubWatchAlertRow)) or 0)
            watch_ids = session.scalars(select(HubWatchDefinitionRow.watch_id)).all()
            for watch_id in watch_ids:
                self._prune_session(session, _uuid(watch_id), now=timestamp)
            session.execute(
                delete(HubWatchAlertRow).where(
                    HubWatchAlertRow.observed_at < _db_time(timestamp - self.observation_retention)
                )
            )
            after = int(
                session.scalar(select(func.count()).select_from(HubWatchObservationRow)) or 0
            )
            after += int(session.scalar(select(func.count()).select_from(HubWatchAlertRow)) or 0)
            return before - after

    prune = cleanup

    def counts(self) -> tuple[int, int, int]:
        with self.database.session() as session:
            return (
                int(session.scalar(select(func.count()).select_from(HubWatchDefinitionRow)) or 0),
                int(session.scalar(select(func.count()).select_from(HubWatchObservationRow)) or 0),
                int(session.scalar(select(func.count()).select_from(HubWatchAlertRow)) or 0),
            )


__all__ = ["WatchRepository"]
