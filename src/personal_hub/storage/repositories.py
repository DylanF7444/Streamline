"""Repositories for the Personal Hub Phase 1 durable foundation."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from personal_hub.config import HubSettings
from personal_hub.contracts import (
    AuditEvent,
    Freshness,
    InboxItem,
    InboxState,
    NotificationChannel,
    NotificationDelivery,
    NotificationStatus,
    SourceConnection,
    SourceSyncState,
)
from personal_hub.storage.database import (
    HubAuditEventRow,
    HubDatabase,
    HubInboxItemRow,
    HubNotificationDeliveryRow,
    HubSettingRow,
    HubSourceConnectionRow,
    HubSourceSyncStateRow,
)

JSONScalar = str | int | float | bool | None
JSONValue = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]
_SETTING_KEY_RE = re.compile(r"[a-z][a-z0-9_.-]{0,127}\Z")
_MAX_JSON_BYTES = 64 * 1024
_MAX_JSON_DEPTH = 5
_MAX_JSON_ITEMS = 128


def _as_db_time(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("repository timestamps must be timezone-aware")
    return value.astimezone(UTC).replace(tzinfo=None)


def _as_contract_time(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _json_dump(value: JSONValue) -> str:
    _validate_json_value(value)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > _MAX_JSON_BYTES:
        raise ValueError("JSON value exceeds the Hub storage limit")
    return encoded


def _json_load(value: str) -> JSONValue:
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("Stored Hub JSON is invalid") from error
    _validate_json_value(decoded)
    return cast(JSONValue, decoded)


def _validate_json_value(value: object, *, depth: int = 0) -> None:
    if depth > _MAX_JSON_DEPTH:
        raise ValueError("JSON value is nested too deeply")
    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, str) and len(value) > 4_096:
            raise ValueError("JSON string value is too large")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numeric values must be finite")
        return
    if isinstance(value, list):
        if len(value) > _MAX_JSON_ITEMS:
            raise ValueError("JSON list is too large")
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > _MAX_JSON_ITEMS:
            raise ValueError("JSON object is too large")
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 256:
                raise ValueError("JSON object keys must be bounded strings")
            _validate_json_value(item, depth=depth + 1)
        return
    raise TypeError("Hub settings JSON supports only JSON-compatible values")


def _json_object(value: str | None) -> dict[str, JSONValue]:
    decoded = {} if value is None else _json_load(value)
    if not isinstance(decoded, dict):
        raise ValueError("Stored Hub JSON object is invalid")
    return decoded


class SettingsRepository:
    """Persist individual settings values as bounded JSON."""

    _NON_PERSISTED_SETTINGS = frozenset({"profile", "data_dir", "database_name"})

    def __init__(self, database: HubDatabase) -> None:
        self.database = database

    @staticmethod
    def _validate_key(key: str) -> str:
        normalized = key.strip()
        if not _SETTING_KEY_RE.fullmatch(normalized):
            raise ValueError("Hub setting keys must be simple bounded names")
        return normalized

    def set(self, key: str, value: JSONValue) -> None:
        normalized_key = self._validate_key(key)
        encoded = _json_dump(value)
        with self.database.session() as session:
            row = session.get(HubSettingRow, normalized_key)
            if row is None:
                session.add(
                    HubSettingRow(
                        key=normalized_key,
                        value_json=encoded,
                        updated_at=datetime.now(UTC),
                    )
                )
            else:
                row.value_json = encoded
                row.updated_at = datetime.now(UTC)

    put = set
    set_value = set

    def get(self, key: str, default: JSONValue = None) -> JSONValue:
        normalized_key = self._validate_key(key)
        with self.database.session() as session:
            row = session.get(HubSettingRow, normalized_key)
            return default if row is None else _json_load(row.value_json)

    get_value = get

    def all(self) -> dict[str, JSONValue]:
        with self.database.session() as session:
            rows = session.scalars(select(HubSettingRow).order_by(HubSettingRow.key)).all()
            return {row.key: _json_load(row.value_json) for row in rows}

    values = all

    def delete(self, key: str) -> bool:
        normalized_key = self._validate_key(key)
        with self.database.session() as session:
            row = session.get(HubSettingRow, normalized_key)
            if row is None:
                return False
            session.delete(row)
            return True

    def save_settings(self, settings: HubSettings) -> HubSettings:
        if not isinstance(settings, HubSettings):
            raise TypeError("save_settings expects HubSettings")
        payload = cast(dict[str, JSONValue], settings.model_dump(mode="json"))
        payload = {
            key: value for key, value in payload.items() if key not in self._NON_PERSISTED_SETTINGS
        }
        encoded_values = {key: _json_dump(value) for key, value in payload.items()}
        now = datetime.now(UTC)
        with self.database.session() as session:
            for key, encoded in encoded_values.items():
                row = session.get(HubSettingRow, key)
                if row is None:
                    session.add(HubSettingRow(key=key, value_json=encoded, updated_at=now))
                else:
                    row.value_json = encoded
                    row.updated_at = now
        return settings

    save = save_settings

    def load_settings(self, defaults: HubSettings | None = None) -> HubSettings:
        baseline = defaults or HubSettings()
        values = self.all()
        if not values:
            return baseline
        merged = dict(baseline.model_dump())
        known_fields = frozenset(HubSettings.model_fields)
        merged.update(
            {
                key: value
                for key, value in values.items()
                if key in known_fields and key not in self._NON_PERSISTED_SETTINGS
            }
        )
        # HubSettings performs the final strict/isolated validation, including
        # protecting data_dir from Local Assistant overlap.
        return HubSettings.model_validate(merged)

    load = load_settings


class SourceRepository:
    """Persist source connection identities and their latest health state."""

    def __init__(self, database: HubDatabase) -> None:
        self.database = database

    @staticmethod
    def _connection_row(connection: SourceConnection) -> HubSourceConnectionRow:
        return HubSourceConnectionRow(
            connection_id=connection.connection_id,
            provider_id=connection.provider_id,
            account_label=connection.account_label,
            capabilities_json=_json_dump(list(connection.capabilities)),
            granted_scopes_json=_json_dump(list(connection.granted_scopes)),
            enabled=connection.enabled,
            created_at=_as_db_time(connection.created_at),
            updated_at=_as_db_time(connection.updated_at),
        )

    @staticmethod
    def _connection_contract(row: HubSourceConnectionRow) -> SourceConnection:
        capabilities = _json_load(row.capabilities_json)
        scopes = _json_load(row.granted_scopes_json)
        if not isinstance(capabilities, list) or not all(
            isinstance(item, str) for item in capabilities
        ):
            raise ValueError("Stored source capabilities are invalid")
        if not isinstance(scopes, list) or not all(isinstance(item, str) for item in scopes):
            raise ValueError("Stored source scopes are invalid")
        return SourceConnection(
            connection_id=row.connection_id,
            provider_id=row.provider_id,
            account_label=row.account_label,
            capabilities=tuple(capabilities),
            granted_scopes=tuple(scopes),
            enabled=bool(row.enabled),
            created_at=_as_contract_time(row.created_at),
            updated_at=_as_contract_time(row.updated_at),
        )

    def save(self, connection: SourceConnection) -> SourceConnection:
        if not isinstance(connection, SourceConnection):
            raise TypeError("save expects SourceConnection")
        with self.database.session() as session:
            row = session.get(HubSourceConnectionRow, connection.connection_id)
            if row is None:
                session.add(self._connection_row(connection))
            else:
                row.provider_id = connection.provider_id
                row.account_label = connection.account_label
                row.capabilities_json = _json_dump(list(connection.capabilities))
                row.granted_scopes_json = _json_dump(list(connection.granted_scopes))
                row.enabled = connection.enabled
                row.created_at = _as_db_time(connection.created_at)
                row.updated_at = _as_db_time(connection.updated_at)
        return connection

    upsert = save
    save_connection = save

    def get(self, connection_id: str) -> SourceConnection | None:
        with self.database.session() as session:
            row = session.get(HubSourceConnectionRow, connection_id)
            return None if row is None else self._connection_contract(row)

    get_connection = get

    def list(self, *, enabled_only: bool = False) -> list[SourceConnection]:
        with self.database.session() as session:
            statement = select(HubSourceConnectionRow).order_by(HubSourceConnectionRow.provider_id)
            if enabled_only:
                statement = statement.where(HubSourceConnectionRow.enabled.is_(True))
            rows = session.scalars(statement).all()
            return [self._connection_contract(row) for row in rows]

    list_connections = list

    def delete(self, connection_id: str) -> bool:
        with self.database.session() as session:
            row = session.get(HubSourceConnectionRow, connection_id)
            if row is None:
                return False
            session.delete(row)
            return True

    remove = delete

    @staticmethod
    def _sync_row(state: SourceSyncState) -> HubSourceSyncStateRow:
        return HubSourceSyncStateRow(
            connection_id=state.connection_id,
            freshness_state=state.freshness.state,
            collected_at=_as_db_time(state.freshness.collected_at),
            expires_at=_as_db_time(state.freshness.expires_at),
            safe_message=state.freshness.safe_message,
            last_attempt_at=_as_db_time(state.last_attempt_at),
            last_success_at=_as_db_time(state.last_success_at),
            failure_count=state.failure_count,
            backoff_until=_as_db_time(state.backoff_until),
        )

    @staticmethod
    def _sync_contract(row: HubSourceSyncStateRow) -> SourceSyncState:
        return SourceSyncState(
            connection_id=row.connection_id,
            freshness=Freshness(
                state=row.freshness_state,
                collected_at=_as_contract_time(row.collected_at),
                expires_at=_as_contract_time(row.expires_at),
                safe_message=row.safe_message,
            ),
            last_attempt_at=_as_contract_time(row.last_attempt_at),
            last_success_at=_as_contract_time(row.last_success_at),
            failure_count=row.failure_count,
            backoff_until=_as_contract_time(row.backoff_until),
        )

    def save_sync_state(self, state: SourceSyncState) -> SourceSyncState:
        if not isinstance(state, SourceSyncState):
            raise TypeError("save_sync_state expects SourceSyncState")
        with self.database.session() as session:
            row = session.get(HubSourceSyncStateRow, state.connection_id)
            values = self._sync_row(state)
            if row is None:
                # Foreign-key integrity is intentional: health cannot be
                # recorded for a connection that was never registered.
                if session.get(HubSourceConnectionRow, state.connection_id) is None:
                    raise ValueError("source connection must exist before its health state")
                session.add(values)
            else:
                row.freshness_state = values.freshness_state
                row.collected_at = values.collected_at
                row.expires_at = values.expires_at
                row.safe_message = values.safe_message
                row.last_attempt_at = values.last_attempt_at
                row.last_success_at = values.last_success_at
                row.failure_count = values.failure_count
                row.backoff_until = values.backoff_until
        return state

    save_health = save_sync_state
    upsert_sync_state = save_sync_state

    def get_sync_state(self, connection_id: str) -> SourceSyncState | None:
        with self.database.session() as session:
            row = session.get(HubSourceSyncStateRow, connection_id)
            return None if row is None else self._sync_contract(row)

    get_health = get_sync_state

    def list_sync_states(self) -> list[SourceSyncState]:
        with self.database.session() as session:
            rows = session.scalars(
                select(HubSourceSyncStateRow).order_by(HubSourceSyncStateRow.connection_id)
            ).all()
            return [self._sync_contract(row) for row in rows]


class InboxRepository:
    """Durable Inbox state with explicit unread/read/dismissed/archived transitions."""

    def __init__(self, database: HubDatabase) -> None:
        self.database = database

    @staticmethod
    def _row(item: InboxItem) -> HubInboxItemRow:
        source_json = (
            None
            if item.source is None
            else _json_dump(cast(dict[str, JSONValue], item.source.model_dump()))
        )
        return HubInboxItemRow(
            item_id=item.item_id,
            kind=item.kind,
            title=item.title,
            safe_summary=item.safe_summary,
            reason=item.reason,
            source_json=source_json,
            created_at=_as_db_time(item.created_at),
            severity=item.severity,
            navigation_target=item.navigation_target,
            state=item.state,
            read_at=_as_db_time(item.read_at),
            dismissed_at=_as_db_time(item.dismissed_at),
            archived_at=_as_db_time(item.archived_at),
            deduplication_key=item.deduplication_key,
            metadata_json=_json_dump(cast(dict[str, JSONValue], item.metadata)),
        )

    @staticmethod
    def _contract(row: HubInboxItemRow) -> InboxItem:
        source = None
        if row.source_json is not None:
            source_value = _json_object(row.source_json)
            from personal_hub.contracts import SourceRef

            source = SourceRef.model_validate(source_value)
        metadata = _json_object(row.metadata_json)
        scalar_metadata: dict[str, str | int | float | bool | None] = {}
        for key, value in metadata.items():
            if not isinstance(value, (str, int, float, bool)) and value is not None:
                raise ValueError("Stored Inbox metadata must be scalar")
            scalar_metadata[key] = cast(str | int | float | bool | None, value)
        return InboxItem(
            item_id=row.item_id,
            kind=row.kind,
            title=row.title,
            safe_summary=row.safe_summary,
            reason=row.reason,
            source=source,
            created_at=_as_contract_time(row.created_at),
            severity=row.severity,
            navigation_target=row.navigation_target,
            state=row.state,
            read_at=_as_contract_time(row.read_at),
            dismissed_at=_as_contract_time(row.dismissed_at),
            archived_at=_as_contract_time(row.archived_at),
            deduplication_key=row.deduplication_key,
            metadata=scalar_metadata,
        )

    def add(self, item: InboxItem) -> InboxItem:
        if not isinstance(item, InboxItem):
            raise TypeError("add expects InboxItem")
        with self.database.session() as session:
            if item.deduplication_key:
                existing = session.scalar(
                    select(HubInboxItemRow)
                    .where(HubInboxItemRow.deduplication_key == item.deduplication_key)
                    .order_by(HubInboxItemRow.created_at.desc())
                    .limit(1)
                )
                if existing is not None:
                    return self._contract(existing)
            row = session.get(HubInboxItemRow, item.item_id)
            values = self._row(item)
            if row is None:
                session.add(values)
            else:
                for field in (
                    "kind",
                    "title",
                    "safe_summary",
                    "reason",
                    "source_json",
                    "created_at",
                    "severity",
                    "navigation_target",
                    "state",
                    "read_at",
                    "dismissed_at",
                    "archived_at",
                    "deduplication_key",
                    "metadata_json",
                ):
                    setattr(row, field, getattr(values, field))
        return item

    create = add
    add_item = add

    def get(self, item_id: str) -> InboxItem | None:
        with self.database.session() as session:
            row = session.get(HubInboxItemRow, item_id)
            return None if row is None else self._contract(row)

    get_item = get

    def list(
        self,
        *,
        state: InboxState | None = None,
        limit: int = 100,
    ) -> list[InboxItem]:
        if not 1 <= limit <= 1_000:
            raise ValueError("Inbox limit must be between 1 and 1000")
        with self.database.session() as session:
            statement = select(HubInboxItemRow).order_by(
                HubInboxItemRow.created_at.desc(), HubInboxItemRow.item_id
            )
            if state is not None:
                statement = statement.where(HubInboxItemRow.state == state)
            rows = session.scalars(statement.limit(limit)).all()
            return [self._contract(row) for row in rows]

    list_items = list

    def _transition(
        self,
        item_id: str,
        state: InboxState,
        *,
        at: datetime | None = None,
    ) -> InboxItem | None:
        timestamp = at or datetime.now(UTC)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("Inbox transition timestamp must be timezone-aware")
        with self.database.session() as session:
            row = session.get(HubInboxItemRow, item_id)
            if row is None:
                return None
            row.state = state
            if state == "unread":
                row.read_at = None
                row.dismissed_at = None
                row.archived_at = None
            elif state == "read":
                row.read_at = row.read_at or _as_db_time(timestamp)
                row.dismissed_at = None
                row.archived_at = None
            elif state == "dismissed":
                row.read_at = row.read_at or _as_db_time(timestamp)
                row.dismissed_at = _as_db_time(timestamp)
                row.archived_at = None
            else:
                row.read_at = row.read_at or _as_db_time(timestamp)
                row.archived_at = _as_db_time(timestamp)
            return self._contract(row)

    def set_state(
        self, item_id: str, state: InboxState, *, at: datetime | None = None
    ) -> InboxItem | None:
        return self._transition(item_id, state, at=at)

    def mark_read(self, item_id: str, *, at: datetime | None = None) -> InboxItem | None:
        return self._transition(item_id, "read", at=at)

    read = mark_read
    mark_item_read = mark_read

    def mark_unread(self, item_id: str, *, at: datetime | None = None) -> InboxItem | None:
        return self._transition(item_id, "unread", at=at)

    def dismiss(self, item_id: str, *, at: datetime | None = None) -> InboxItem | None:
        return self._transition(item_id, "dismissed", at=at)

    mark_dismissed = dismiss
    dismiss_item = dismiss

    def archive(self, item_id: str, *, at: datetime | None = None) -> InboxItem | None:
        return self._transition(item_id, "archived", at=at)

    mark_archived = archive
    archive_item = archive

    def unread_count(self) -> int:
        with self.database.session() as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(HubInboxItemRow)
                    .where(HubInboxItemRow.state == "unread")
                )
                or 0
            )

    count_unread = unread_count

    def delete(self, item_id: str) -> bool:
        with self.database.session() as session:
            row = session.get(HubInboxItemRow, item_id)
            if row is None:
                return False
            session.delete(row)
            return True

    def cleanup(self, before: datetime, *, states: Sequence[InboxState] = ("archived",)) -> int:
        if before.tzinfo is None or before.utcoffset() is None:
            raise ValueError("Inbox cleanup timestamp must be timezone-aware")
        if not states:
            return 0
        with self.database.session() as session:
            result = session.execute(
                delete(HubInboxItemRow).where(
                    HubInboxItemRow.state.in_(tuple(states)),
                    HubInboxItemRow.created_at < _as_db_time(before),
                )
            )
            return int(result.rowcount or 0)


class AuditRepository:
    """Metadata-only audit event repository."""

    def __init__(self, database: HubDatabase) -> None:
        self.database = database

    @staticmethod
    def _row(event: AuditEvent) -> HubAuditEventRow:
        return HubAuditEventRow(
            audit_id=event.audit_id,
            event_type=event.event_type,
            source_type=event.source_type,
            source_id=event.source_id,
            outcome=event.outcome,
            occurred_at=_as_db_time(event.occurred_at),
            metadata_json=_json_dump(cast(dict[str, JSONValue], event.metadata)),
        )

    @staticmethod
    def _contract(row: HubAuditEventRow) -> AuditEvent:
        metadata = _json_object(row.metadata_json)
        scalar_metadata: dict[str, str | int | float | bool | None] = {}
        for key, value in metadata.items():
            if not isinstance(value, (str, int, float, bool)) and value is not None:
                raise ValueError("Stored audit metadata must be scalar")
            scalar_metadata[key] = cast(str | int | float | bool | None, value)
        return AuditEvent(
            audit_id=row.audit_id,
            event_type=row.event_type,
            source_type=row.source_type,
            source_id=row.source_id,
            outcome=row.outcome,
            occurred_at=_as_contract_time(row.occurred_at),
            metadata=scalar_metadata,
        )

    def append(self, event: AuditEvent) -> AuditEvent:
        if not isinstance(event, AuditEvent):
            raise TypeError("append expects AuditEvent")
        with self.database.session() as session:
            row = session.get(HubAuditEventRow, event.audit_id)
            values = self._row(event)
            if row is None:
                session.add(values)
            else:
                row.event_type = values.event_type
                row.source_type = values.source_type
                row.source_id = values.source_id
                row.outcome = values.outcome
                row.occurred_at = values.occurred_at
                row.metadata_json = values.metadata_json
        return event

    record = append
    add = append

    def list(self, *, limit: int = 100) -> list[AuditEvent]:
        if not 1 <= limit <= 1_000:
            raise ValueError("Audit limit must be between 1 and 1000")
        with self.database.session() as session:
            rows = session.scalars(
                select(HubAuditEventRow)
                .order_by(HubAuditEventRow.occurred_at.desc(), HubAuditEventRow.audit_id)
                .limit(limit)
            ).all()
            return [self._contract(row) for row in rows]

    list_events = list

    def cleanup(self, before: datetime) -> int:
        if before.tzinfo is None or before.utcoffset() is None:
            raise ValueError("Audit cleanup timestamp must be timezone-aware")
        with self.database.session() as session:
            result = session.execute(
                delete(HubAuditEventRow).where(HubAuditEventRow.occurred_at < _as_db_time(before))
            )
            return int(result.rowcount or 0)


class NotificationRepository:
    """Bounded notification delivery and deduplication state."""

    def __init__(
        self,
        database: HubDatabase,
        *,
        max_entries: int = 1_000,
        retention: timedelta = timedelta(days=30),
    ) -> None:
        if not 1 <= max_entries <= 100_000:
            raise ValueError("notification max_entries must be between 1 and 100000")
        if retention <= timedelta(0):
            raise ValueError("notification retention must be positive")
        self.database = database
        self.max_entries = max_entries
        self.retention = retention

    @staticmethod
    def _row(delivery: NotificationDelivery) -> HubNotificationDeliveryRow:
        return HubNotificationDeliveryRow(
            delivery_id=delivery.delivery_id,
            deduplication_key=delivery.deduplication_key,
            channel=delivery.channel,
            status=delivery.status,
            created_at=_as_db_time(delivery.created_at),
            delivered_at=_as_db_time(delivery.delivered_at),
            expires_at=_as_db_time(delivery.expires_at),
            attempts=delivery.attempts,
            safe_message=delivery.safe_message,
        )

    @staticmethod
    def _contract(row: HubNotificationDeliveryRow) -> NotificationDelivery:
        return NotificationDelivery(
            delivery_id=row.delivery_id,
            deduplication_key=row.deduplication_key,
            channel=row.channel,
            status=row.status,
            created_at=_as_contract_time(row.created_at),
            delivered_at=_as_contract_time(row.delivered_at),
            expires_at=_as_contract_time(row.expires_at),
            attempts=row.attempts,
            safe_message=row.safe_message,
        )

    @staticmethod
    def _validate_now(now: datetime) -> datetime:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("notification timestamp must be timezone-aware")
        return now.astimezone(UTC)

    def _prune_in_session(self, session: Any, now: datetime) -> None:
        cutoff = _as_db_time(now - self.retention)
        session.execute(
            delete(HubNotificationDeliveryRow).where(HubNotificationDeliveryRow.created_at < cutoff)
        )
        count = int(
            session.scalar(select(func.count()).select_from(HubNotificationDeliveryRow)) or 0
        )
        if count <= self.max_entries:
            return
        excess = count - self.max_entries
        oldest_ids = (
            select(HubNotificationDeliveryRow.delivery_id)
            .order_by(
                HubNotificationDeliveryRow.created_at.asc(),
                HubNotificationDeliveryRow.delivery_id.asc(),
            )
            .limit(excess)
        )
        session.execute(
            delete(HubNotificationDeliveryRow).where(
                HubNotificationDeliveryRow.delivery_id.in_(oldest_ids)
            )
        )

    def claim(self, delivery: NotificationDelivery) -> bool:
        """Claim a deduplication key; return False while an unexpired claim exists."""

        if not isinstance(delivery, NotificationDelivery):
            raise TypeError("claim expects NotificationDelivery")
        now = self._validate_now(delivery.created_at)
        with self.database.session() as session:
            existing = session.scalar(
                select(HubNotificationDeliveryRow).where(
                    HubNotificationDeliveryRow.deduplication_key == delivery.deduplication_key,
                    HubNotificationDeliveryRow.channel == delivery.channel,
                )
            )
            if existing is not None:
                expires = _as_contract_time(existing.expires_at)
                if expires is None or expires > now:
                    return False
                session.delete(existing)
                session.flush()
            try:
                # A savepoint lets a concurrent UNIQUE-key race roll back the
                # INSERT while keeping the outer repository transaction usable.
                with session.begin_nested():
                    session.add(self._row(delivery))
                    session.flush()
            except IntegrityError:
                # A second process may have claimed the same key between the
                # lookup and INSERT.  The transaction context rolls back safely.
                return False
            self._prune_in_session(session, now)
            return True

    record = claim
    record_delivery = claim

    def claim_key(
        self,
        deduplication_key: str,
        channel: NotificationChannel,
        *,
        now: datetime | None = None,
        expires_at: datetime | None = None,
        status: NotificationStatus = "pending",
        safe_message: str | None = None,
    ) -> bool:
        timestamp = self._validate_now(now or datetime.now(UTC))
        delivery = NotificationDelivery(
            deduplication_key=deduplication_key,
            channel=channel,
            status=status,
            created_at=timestamp,
            expires_at=expires_at,
            safe_message=safe_message,
        )
        return self.claim(delivery)

    claim_deduplication_key = claim_key

    def get(
        self, deduplication_key: str, channel: NotificationChannel
    ) -> NotificationDelivery | None:
        with self.database.session() as session:
            row = session.scalar(
                select(HubNotificationDeliveryRow).where(
                    HubNotificationDeliveryRow.deduplication_key == deduplication_key,
                    HubNotificationDeliveryRow.channel == channel,
                )
            )
            return None if row is None else self._contract(row)

    def was_claimed(self, deduplication_key: str, channel: NotificationChannel) -> bool:
        return self.get(deduplication_key, channel) is not None

    def mark_delivered(
        self,
        deduplication_key: str,
        channel: NotificationChannel,
        *,
        at: datetime | None = None,
    ) -> NotificationDelivery | None:
        timestamp = self._validate_now(at or datetime.now(UTC))
        with self.database.session() as session:
            row = session.scalar(
                select(HubNotificationDeliveryRow).where(
                    HubNotificationDeliveryRow.deduplication_key == deduplication_key,
                    HubNotificationDeliveryRow.channel == channel,
                )
            )
            if row is None:
                return None
            row.status = "delivered"
            row.delivered_at = _as_db_time(timestamp)
            row.attempts += 1
            return self._contract(row)

    delivered = mark_delivered

    def mark_failed(
        self,
        deduplication_key: str,
        channel: NotificationChannel,
        *,
        at: datetime | None = None,
        safe_message: str | None = None,
    ) -> NotificationDelivery | None:
        timestamp = self._validate_now(at or datetime.now(UTC))
        with self.database.session() as session:
            row = session.scalar(
                select(HubNotificationDeliveryRow).where(
                    HubNotificationDeliveryRow.deduplication_key == deduplication_key,
                    HubNotificationDeliveryRow.channel == channel,
                )
            )
            if row is None:
                return None
            row.status = "failed"
            row.attempts += 1
            if safe_message is not None:
                row.safe_message = " ".join(safe_message.split())[:512] or None
            # Keep the event timestamp observable without introducing a private
            # body/message field in the database.
            if row.delivered_at is None:
                row.delivered_at = _as_db_time(timestamp)
            return self._contract(row)

    failed = mark_failed

    def mark_suppressed(
        self,
        deduplication_key: str,
        channel: NotificationChannel,
        *,
        at: datetime | None = None,
        safe_message: str | None = None,
    ) -> NotificationDelivery | None:
        """Record an intentional policy suppression without treating it as a failure."""

        timestamp = self._validate_now(at or datetime.now(UTC))
        with self.database.session() as session:
            row = session.scalar(
                select(HubNotificationDeliveryRow).where(
                    HubNotificationDeliveryRow.deduplication_key == deduplication_key,
                    HubNotificationDeliveryRow.channel == channel,
                )
            )
            if row is None:
                return None
            row.status = "suppressed"
            row.attempts += 1
            row.delivered_at = _as_db_time(timestamp)
            if safe_message is not None:
                row.safe_message = " ".join(safe_message.split())[:512] or None
            return self._contract(row)

    suppressed = mark_suppressed

    def list(self, *, limit: int = 100) -> list[NotificationDelivery]:
        if not 1 <= limit <= 1_000:
            raise ValueError("notification limit must be between 1 and 1000")
        with self.database.session() as session:
            rows = session.scalars(
                select(HubNotificationDeliveryRow)
                .order_by(HubNotificationDeliveryRow.created_at.desc())
                .limit(limit)
            ).all()
            return [self._contract(row) for row in rows]

    list_deliveries = list

    def cleanup(self, *, now: datetime | None = None) -> int:
        timestamp = self._validate_now(now or datetime.now(UTC))
        with self.database.session() as session:
            before = int(
                session.scalar(select(func.count()).select_from(HubNotificationDeliveryRow)) or 0
            )
            self._prune_in_session(session, timestamp)
            after = int(
                session.scalar(select(func.count()).select_from(HubNotificationDeliveryRow)) or 0
            )
            return before - after


# Explicit repository aliases make the ownership boundary discoverable to later
# source/inbox/notification services.
SourceHealthRepository = SourceRepository
AuditEventRepository = AuditRepository
NotificationDeliveryRepository = NotificationRepository

__all__ = [
    "AuditEventRepository",
    "AuditRepository",
    "InboxRepository",
    "NotificationDeliveryRepository",
    "NotificationRepository",
    "SettingsRepository",
    "SourceHealthRepository",
    "SourceRepository",
]
