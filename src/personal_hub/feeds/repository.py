"""SQLite repositories for Personal Hub feed subscriptions and entries."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import delete, func, or_, select

from personal_hub.feeds.contracts import (
    FeedEntry,
    FeedEntryRecord,
    FeedReadState,
    FeedReadStateValue,
    FeedSubscription,
    FeedSyncState,
    FeedSyncStateValue,
)
from personal_hub.storage.database import (
    HubDatabase,
    HubFeedEntryRow,
    HubFeedReadStateRow,
    HubFeedSubscriptionRow,
)


def _db_time(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("feed timestamps must be timezone-aware")
    return value.astimezone(UTC).replace(tzinfo=None)


def _contract_time(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _aware_now(value: datetime | None = None) -> datetime:
    timestamp = datetime.now(UTC) if value is None else value
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("feed timestamps must be timezone-aware")
    return timestamp.astimezone(UTC)


def _as_uuid(value: UUID | str) -> UUID:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise TypeError("subscription_id must be a UUID or UUID string")
    try:
        return UUID(value)
    except ValueError as error:
        raise ValueError("subscription_id must be a UUID") from error


def _json_terms(values: tuple[str, ...]) -> str:
    return json.dumps(list(values), ensure_ascii=False, separators=(",", ":"))


def _load_terms(value: str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("stored feed filter terms are invalid") from error
    if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
        raise ValueError("stored feed filter terms are invalid")
    return tuple(decoded)


class FeedSubscriptionRepository:
    """Persist subscription configuration and bounded sync metadata."""

    def __init__(self, database: HubDatabase) -> None:
        self.database = database

    @staticmethod
    def _row(subscription: FeedSubscription) -> HubFeedSubscriptionRow:
        timestamp = datetime.now(UTC)
        return HubFeedSubscriptionRow(
            subscription_id=str(subscription.subscription_id),
            title=subscription.title,
            feed_url=str(subscription.feed_url),
            site_url=None if subscription.site_url is None else str(subscription.site_url),
            enabled=subscription.enabled,
            refresh_minutes=subscription.refresh_minutes,
            include_terms_json=_json_terms(subscription.include_terms),
            exclude_terms_json=_json_terms(subscription.exclude_terms),
            digest_only=subscription.digest_only,
            allow_http=subscription.allow_http,
            reviewed_origin=subscription.reviewed_origin,
            created_at=_db_time(timestamp),
            updated_at=_db_time(timestamp),
        )

    @staticmethod
    def _contract(row: HubFeedSubscriptionRow) -> FeedSubscription:
        return FeedSubscription(
            subscription_id=_as_uuid(row.subscription_id),
            title=row.title,
            feed_url=row.feed_url,
            site_url=row.site_url,
            enabled=bool(row.enabled),
            refresh_minutes=row.refresh_minutes,
            include_terms=_load_terms(row.include_terms_json),
            exclude_terms=_load_terms(row.exclude_terms_json),
            digest_only=bool(row.digest_only),
            allow_http=bool(row.allow_http),
            reviewed_origin=row.reviewed_origin,
        )

    @staticmethod
    def _sync_contract(row: HubFeedSubscriptionRow) -> FeedSyncState:
        return FeedSyncState(
            subscription_id=_as_uuid(row.subscription_id),
            state=cast(FeedSyncStateValue, row.sync_state),
            etag=row.etag,
            last_modified=row.last_modified,
            last_checked_at=_contract_time(row.last_checked_at),
            last_success_at=_contract_time(row.last_success_at),
            failure_count=row.failure_count,
            backoff_until=_contract_time(row.backoff_until),
            safe_error=row.safe_error,
        )

    def add(self, subscription: FeedSubscription) -> FeedSubscription:
        return self.add_at(subscription)

    def add_at(
        self,
        subscription: FeedSubscription,
        *,
        updated_at: datetime | None = None,
    ) -> FeedSubscription:
        """Persist a definition, optionally retaining its durable edit time."""

        if not isinstance(subscription, FeedSubscription):
            raise TypeError("feed subscription repository expects FeedSubscription")
        identifier = str(subscription.subscription_id)
        with self.database.session() as session:
            row = session.get(HubFeedSubscriptionRow, identifier)
            values = self._row(subscription)
            if updated_at is not None:
                values.updated_at = _db_time(updated_at)
            if row is None:
                session.add(values)
            else:
                row.title = values.title
                row.feed_url = values.feed_url
                row.site_url = values.site_url
                row.enabled = values.enabled
                row.refresh_minutes = values.refresh_minutes
                row.include_terms_json = values.include_terms_json
                row.exclude_terms_json = values.exclude_terms_json
                row.digest_only = values.digest_only
                row.allow_http = values.allow_http
                row.reviewed_origin = values.reviewed_origin
                row.updated_at = values.updated_at
            session.flush()
        return subscription

    save = add
    upsert = add
    save_subscription = add
    add_with_timestamp = add_at

    def update(
        self,
        subscription_id: UUID | str,
        subscription: FeedSubscription | None = None,
        **changes: object,
    ) -> FeedSubscription | None:
        """Update one subscription without changing its durable identity."""

        identifier = _as_uuid(subscription_id)
        current = self.get(identifier)
        if current is None:
            return None
        if subscription is not None:
            if changes or subscription.subscription_id != identifier:
                raise ValueError("updated feed identity or arguments do not match")
            return self.add(subscription)
        payload = current.model_dump(mode="python")
        payload.update(changes)
        payload["subscription_id"] = identifier
        return self.add(FeedSubscription.model_validate(payload))

    edit = update

    def create(
        self,
        title: str,
        feed_url: str,
        *,
        subscription_id: UUID | None = None,
        site_url: str | None = None,
        enabled: bool = True,
        refresh_minutes: int = 30,
        include_terms: tuple[str, ...] = (),
        exclude_terms: tuple[str, ...] = (),
        digest_only: bool = False,
        allow_http: bool = False,
        reviewed_origin: str | None = None,
    ) -> FeedSubscription:
        return self.add(
            FeedSubscription(
                subscription_id=uuid4() if subscription_id is None else subscription_id,
                title=title,
                feed_url=feed_url,
                site_url=site_url,
                enabled=enabled,
                refresh_minutes=refresh_minutes,
                include_terms=include_terms,
                exclude_terms=exclude_terms,
                digest_only=digest_only,
                allow_http=allow_http,
                reviewed_origin=reviewed_origin,
            )
        )

    def get(self, subscription_id: UUID | str) -> FeedSubscription | None:
        identifier = str(_as_uuid(subscription_id))
        with self.database.session() as session:
            row = session.get(HubFeedSubscriptionRow, identifier)
            return None if row is None else self._contract(row)

    get_subscription = get

    def list(
        self,
        *,
        enabled: bool | None = None,
        limit: int = 1_000,
    ) -> list[FeedSubscription]:
        if not 1 <= limit <= 10_000:
            raise ValueError("feed subscription limit must be between 1 and 10000")
        with self.database.session() as session:
            statement = select(HubFeedSubscriptionRow).order_by(
                HubFeedSubscriptionRow.title.asc(), HubFeedSubscriptionRow.subscription_id.asc()
            )
            if enabled is not None:
                statement = statement.where(HubFeedSubscriptionRow.enabled == enabled)
            rows = session.scalars(statement.limit(limit)).all()
            return [self._contract(row) for row in rows]

    list_subscriptions = list

    def list_with_timestamps(
        self,
        *,
        enabled: bool | None = None,
        limit: int = 1_000,
    ) -> list[tuple[FeedSubscription, datetime]]:
        """List subscription definitions with their durable edit times."""

        if not 1 <= limit <= 10_000:
            raise ValueError("feed subscription limit must be between 1 and 10000")
        with self.database.session() as session:
            statement = select(HubFeedSubscriptionRow).order_by(
                HubFeedSubscriptionRow.title.asc(), HubFeedSubscriptionRow.subscription_id.asc()
            )
            if enabled is not None:
                statement = statement.where(HubFeedSubscriptionRow.enabled == enabled)
            rows = session.scalars(statement.limit(limit)).all()
            return [
                (
                    self._contract(row),
                    _contract_time(row.updated_at) or datetime.now(UTC),
                )
                for row in rows
            ]

    def count(self, *, enabled: bool | None = None) -> int:
        with self.database.session() as session:
            statement = select(func.count()).select_from(HubFeedSubscriptionRow)
            if enabled is not None:
                statement = statement.where(HubFeedSubscriptionRow.enabled == enabled)
            return int(session.scalar(statement) or 0)

    def delete(self, subscription_id: UUID | str) -> bool:
        identifier = str(_as_uuid(subscription_id))
        with self.database.session() as session:
            row = session.get(HubFeedSubscriptionRow, identifier)
            if row is None:
                return False
            session.delete(row)
            return True

    remove = delete

    def set_enabled(self, subscription_id: UUID | str, enabled: bool) -> FeedSubscription | None:
        identifier = str(_as_uuid(subscription_id))
        if type(enabled) is not bool:
            raise TypeError("enabled must be a bool")
        with self.database.session() as session:
            row = session.get(HubFeedSubscriptionRow, identifier)
            if row is None:
                return None
            row.enabled = enabled
            row.updated_at = _db_time(datetime.now(UTC))
            return self._contract(row)

    enable = set_enabled

    def save_sync_state(self, state: FeedSyncState) -> FeedSyncState:
        if not isinstance(state, FeedSyncState):
            raise TypeError("feed sync repository expects FeedSyncState")
        identifier = str(state.subscription_id)
        with self.database.session() as session:
            row = session.get(HubFeedSubscriptionRow, identifier)
            if row is None:
                raise KeyError(f"feed subscription does not exist: {identifier}")
            row.sync_state = state.state
            row.etag = state.etag
            row.last_modified = state.last_modified
            row.last_checked_at = _db_time(state.last_checked_at)
            row.last_success_at = _db_time(state.last_success_at)
            row.failure_count = state.failure_count
            row.backoff_until = _db_time(state.backoff_until)
            row.safe_error = state.safe_error
            row.updated_at = _db_time(datetime.now(UTC))
            session.flush()
        return state

    update_sync_state = save_sync_state

    def sync_state(self, subscription_id: UUID | str) -> FeedSyncState | None:
        identifier = str(_as_uuid(subscription_id))
        with self.database.session() as session:
            row = session.get(HubFeedSubscriptionRow, identifier)
            return None if row is None else self._sync_contract(row)

    get_sync_state = sync_state

    def record_sync(
        self,
        subscription_id: UUID | str,
        *,
        state: FeedSyncStateValue,
        checked_at: datetime,
        success_at: datetime | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        failure_count: int = 0,
        backoff_until: datetime | None = None,
        safe_error: str | None = None,
    ) -> FeedSyncState:
        return self.save_sync_state(
            FeedSyncState(
                subscription_id=_as_uuid(subscription_id),
                state=state,
                etag=etag,
                last_modified=last_modified,
                last_checked_at=checked_at,
                last_success_at=success_at,
                failure_count=failure_count,
                backoff_until=backoff_until,
                safe_error=safe_error,
            )
        )


class FeedEntryRepository:
    """Persist bounded normalized entries and local read state.

    Entries are bounded both by age and by count.  Count limits apply globally;
    callers can additionally set ``max_entries_per_subscription`` to prevent a
    single high-volume feed from consuming the complete local budget.
    """

    def __init__(
        self,
        database: HubDatabase,
        *,
        max_entries: int = 5_000,
        retention: timedelta = timedelta(days=30),
        max_entries_per_subscription: int | None = None,
    ) -> None:
        if not 1 <= max_entries <= 100_000:
            raise ValueError("feed max_entries must be between 1 and 100000")
        if retention <= timedelta(0):
            raise ValueError("feed retention must be positive")
        if max_entries_per_subscription is not None and not (
            1 <= max_entries_per_subscription <= 100_000
        ):
            raise ValueError("per-subscription feed max_entries must be between 1 and 100000")
        self.database = database
        self.max_entries = max_entries
        self.retention = retention
        self.max_entries_per_subscription = max_entries_per_subscription

    @staticmethod
    def _row(entry: FeedEntry, *, seen_at: datetime) -> HubFeedEntryRow:
        return HubFeedEntryRow(
            entry_id=entry.entry_id,
            subscription_id=str(entry.subscription_id),
            title=entry.title,
            url=str(entry.url),
            author=entry.author,
            published_at=_db_time(entry.published_at),
            summary_text=entry.summary_text,
            first_seen_at=_db_time(entry.first_seen_at),
            last_seen_at=_db_time(seen_at),
        )

    @staticmethod
    def _contract(row: HubFeedEntryRow) -> FeedEntry:
        return FeedEntry(
            entry_id=row.entry_id,
            subscription_id=_as_uuid(row.subscription_id),
            title=row.title,
            url=row.url,
            author=row.author,
            published_at=_contract_time(row.published_at),
            summary_text=row.summary_text,
            first_seen_at=_contract_time(row.first_seen_at),
        )

    @staticmethod
    def _state_contract(row: HubFeedReadStateRow) -> FeedReadState:
        return FeedReadState(
            entry_id=row.entry_id,
            state=cast(FeedReadStateValue, row.state),
            read_at=_contract_time(row.read_at),
            updated_at=_contract_time(row.updated_at),
        )

    def _save_in_session(
        self,
        session: Any,
        entry: FeedEntry,
        *,
        seen_at: datetime,
    ) -> HubFeedEntryRow:
        row = session.get(HubFeedEntryRow, entry.entry_id)
        values = self._row(entry, seen_at=seen_at)
        if row is None:
            session.add(values)
            return values
        if row.subscription_id != values.subscription_id:
            raise ValueError("feed entry identity is already attached to another subscription")
        row.title = values.title
        row.url = values.url
        row.author = values.author
        row.published_at = values.published_at
        row.summary_text = values.summary_text
        existing_first_seen = _contract_time(row.first_seen_at)
        incoming_first_seen = _contract_time(values.first_seen_at)
        if existing_first_seen is None or (
            incoming_first_seen is not None and incoming_first_seen < existing_first_seen
        ):
            row.first_seen_at = values.first_seen_at
        row.last_seen_at = values.last_seen_at
        return row

    @staticmethod
    def _delete_ids(session: Any, identifiers: Any) -> int:
        result = session.execute(
            delete(HubFeedEntryRow).where(HubFeedEntryRow.entry_id.in_(identifiers))
        )
        return int(result.rowcount or 0)

    def _prune_in_session(
        self,
        session: Any,
        now: datetime,
        *,
        subscription_id: UUID | str | None = None,
    ) -> int:
        removed = 0
        cutoff = _db_time(now - self.retention)
        expired = select(HubFeedEntryRow.entry_id).where(HubFeedEntryRow.first_seen_at < cutoff)
        if subscription_id is not None:
            expired = expired.where(
                HubFeedEntryRow.subscription_id == str(_as_uuid(subscription_id))
            )
        removed += self._delete_ids(session, expired)

        def trim(limit: int, scoped_subscription: str | None = None) -> int:
            statement = select(HubFeedEntryRow.entry_id)
            if scoped_subscription is not None:
                statement = statement.where(HubFeedEntryRow.subscription_id == scoped_subscription)
            count_statement = select(func.count()).select_from(HubFeedEntryRow)
            if scoped_subscription is not None:
                count_statement = count_statement.where(
                    HubFeedEntryRow.subscription_id == scoped_subscription
                )
            count = int(session.scalar(count_statement) or 0)
            excess = count - limit
            if excess <= 0:
                return 0
            identifiers = statement.order_by(
                HubFeedEntryRow.first_seen_at.asc(),
                HubFeedEntryRow.last_seen_at.asc(),
                HubFeedEntryRow.entry_id.asc(),
            ).limit(excess)
            return self._delete_ids(session, identifiers)

        removed += trim(self.max_entries)
        if self.max_entries_per_subscription is not None:
            subscriptions = select(HubFeedEntryRow.subscription_id).distinct()
            if subscription_id is not None:
                subscriptions = subscriptions.where(
                    HubFeedEntryRow.subscription_id == str(_as_uuid(subscription_id))
                )
            for (scoped_subscription,) in session.execute(subscriptions):
                removed += trim(self.max_entries_per_subscription, scoped_subscription)
        return removed

    def add(self, entry: FeedEntry, *, seen_at: datetime | None = None) -> FeedEntry:
        if not isinstance(entry, FeedEntry):
            raise TypeError("feed entry repository expects FeedEntry")
        timestamp = _aware_now(seen_at)
        with self.database.session() as session:
            row = self._save_in_session(session, entry, seen_at=timestamp)
            session.flush()
            self._prune_in_session(session, timestamp, subscription_id=entry.subscription_id)
            # Return the normalized contract even if an age/count policy made
            # the row ineligible for retention immediately after this write.
            return self._contract(row)

    save = add
    upsert = add
    add_entry = add

    def upsert_many(
        self,
        entries: Sequence[FeedEntry],
        *,
        seen_at: datetime | None = None,
    ) -> list[FeedEntry]:
        if any(not isinstance(entry, FeedEntry) for entry in entries):
            raise TypeError("feed entry repository expects FeedEntry values")
        timestamp = _aware_now(seen_at)
        with self.database.session() as session:
            rows = [self._save_in_session(session, entry, seen_at=timestamp) for entry in entries]
            session.flush()
            self._prune_in_session(session, timestamp)
            return [self._contract(row) for row in rows]

    save_many = upsert_many
    upsert_entries = upsert_many

    def get(self, entry_id: str) -> FeedEntry | None:
        if not isinstance(entry_id, str) or not entry_id.strip():
            raise ValueError("entry_id must be a non-empty string")
        with self.database.session() as session:
            row = session.get(HubFeedEntryRow, entry_id)
            return None if row is None else self._contract(row)

    get_entry = get

    def get_record(self, entry_id: str) -> FeedEntryRecord | None:
        entry = self.get(entry_id)
        if entry is None:
            return None
        state = self.read_state(entry_id)
        return FeedEntryRecord(entry=entry, state=state.state, read_at=state.read_at)

    def list(
        self,
        *,
        subscription_id: UUID | str | None = None,
        state: FeedReadStateValue | None = None,
        published_after: datetime | None = None,
        published_before: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[FeedEntry]:
        self._validate_page(limit, offset)
        if published_after is not None:
            _aware_now(published_after)
        if published_before is not None:
            _aware_now(published_before)
        if state not in (None, "read", "unread"):
            raise ValueError("feed read state must be read or unread")
        with self.database.session() as session:
            statement = select(HubFeedEntryRow).outerjoin(
                HubFeedReadStateRow,
                HubFeedReadStateRow.entry_id == HubFeedEntryRow.entry_id,
            )
            if subscription_id is not None:
                statement = statement.where(
                    HubFeedEntryRow.subscription_id == str(_as_uuid(subscription_id))
                )
            if state == "read":
                statement = statement.where(HubFeedReadStateRow.state == "read")
            elif state == "unread":
                statement = statement.where(
                    or_(
                        HubFeedReadStateRow.entry_id.is_(None),
                        HubFeedReadStateRow.state == "unread",
                    )
                )
            if published_after is not None:
                statement = statement.where(
                    HubFeedEntryRow.published_at >= _db_time(published_after)
                )
            if published_before is not None:
                statement = statement.where(
                    HubFeedEntryRow.published_at < _db_time(published_before)
                )
            statement = statement.order_by(
                HubFeedEntryRow.published_at.desc().nullslast(),
                HubFeedEntryRow.first_seen_at.desc(),
                HubFeedEntryRow.entry_id.asc(),
            )
            rows = session.scalars(statement.offset(offset).limit(limit)).all()
            return [self._contract(row) for row in rows]

    list_entries = list
    query = list

    def list_records(
        self,
        *,
        subscription_id: UUID | str | None = None,
        state: FeedReadStateValue | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[FeedEntryRecord]:
        self._validate_page(limit, offset)
        if state not in (None, "read", "unread"):
            raise ValueError("feed read state must be read or unread")
        with self.database.session() as session:
            statement = select(HubFeedEntryRow, HubFeedReadStateRow).outerjoin(
                HubFeedReadStateRow,
                HubFeedReadStateRow.entry_id == HubFeedEntryRow.entry_id,
            )
            if subscription_id is not None:
                statement = statement.where(
                    HubFeedEntryRow.subscription_id == str(_as_uuid(subscription_id))
                )
            if state == "read":
                statement = statement.where(HubFeedReadStateRow.state == "read")
            elif state == "unread":
                statement = statement.where(
                    or_(
                        HubFeedReadStateRow.entry_id.is_(None),
                        HubFeedReadStateRow.state == "unread",
                    )
                )
            statement = statement.order_by(
                HubFeedEntryRow.published_at.desc().nullslast(),
                HubFeedEntryRow.first_seen_at.desc(),
                HubFeedEntryRow.entry_id.asc(),
            )
            rows = session.execute(statement.offset(offset).limit(limit)).all()
            records: list[FeedEntryRecord] = []
            for entry_row, state_row in rows:
                records.append(
                    FeedEntryRecord(
                        entry=self._contract(entry_row),
                        state="unread" if state_row is None else state_row.state,
                        read_at=None if state_row is None else _contract_time(state_row.read_at),
                    )
                )
            return records

    entries_with_state = list_records

    @staticmethod
    def _validate_page(limit: int, offset: int) -> None:
        if not 1 <= limit <= 10_000:
            raise ValueError("feed entry limit must be between 1 and 10000")
        if offset < 0 or offset > 1_000_000:
            raise ValueError("feed entry offset must be between 0 and 1000000")

    def count(
        self,
        *,
        subscription_id: UUID | str | None = None,
        state: FeedReadStateValue | None = None,
    ) -> int:
        if state not in (None, "read", "unread"):
            raise ValueError("feed read state must be read or unread")
        with self.database.session() as session:
            statement = (
                select(func.count())
                .select_from(HubFeedEntryRow)
                .outerjoin(
                    HubFeedReadStateRow,
                    HubFeedReadStateRow.entry_id == HubFeedEntryRow.entry_id,
                )
            )
            if subscription_id is not None:
                statement = statement.where(
                    HubFeedEntryRow.subscription_id == str(_as_uuid(subscription_id))
                )
            if state == "read":
                statement = statement.where(HubFeedReadStateRow.state == "read")
            elif state == "unread":
                statement = statement.where(
                    or_(
                        HubFeedReadStateRow.entry_id.is_(None),
                        HubFeedReadStateRow.state == "unread",
                    )
                )
            return int(session.scalar(statement) or 0)

    def delete(self, entry_id: str) -> bool:
        with self.database.session() as session:
            row = session.get(HubFeedEntryRow, entry_id)
            if row is None:
                return False
            session.delete(row)
            return True

    remove = delete

    def read_state(self, entry_id: str) -> FeedReadState:
        with self.database.session() as session:
            entry = session.get(HubFeedEntryRow, entry_id)
            if entry is None:
                raise KeyError(f"feed entry does not exist: {entry_id}")
            row = session.get(HubFeedReadStateRow, entry_id)
            if row is None:
                return FeedReadState(entry_id=entry_id)
            return self._state_contract(row)

    get_read_state = read_state

    def set_read_state(
        self,
        entry_id: str,
        state: FeedReadStateValue,
        *,
        at: datetime | None = None,
    ) -> FeedReadState:
        if state not in ("read", "unread"):
            raise ValueError("feed read state must be read or unread")
        timestamp = _aware_now(at)
        with self.database.session() as session:
            entry = session.get(HubFeedEntryRow, entry_id)
            if entry is None:
                raise KeyError(f"feed entry does not exist: {entry_id}")
            row = session.get(HubFeedReadStateRow, entry_id)
            if row is None:
                row = HubFeedReadStateRow(
                    entry_id=entry_id,
                    state=state,
                    read_at=_db_time(timestamp) if state == "read" else None,
                    updated_at=_db_time(timestamp),
                )
                session.add(row)
            else:
                row.state = state
                row.read_at = _db_time(timestamp) if state == "read" else None
                row.updated_at = _db_time(timestamp)
            session.flush()
            return self._state_contract(row)

    def mark_read(self, entry_id: str, *, at: datetime | None = None) -> FeedReadState:
        return self.set_read_state(entry_id, "read", at=at)

    def mark_unread(self, entry_id: str, *, at: datetime | None = None) -> FeedReadState:
        return self.set_read_state(entry_id, "unread", at=at)

    read = mark_read
    unread = mark_unread

    def cleanup(
        self,
        before: datetime,
        *,
        subscription_id: UUID | str | None = None,
    ) -> int:
        timestamp = _aware_now(before)
        with self.database.session() as session:
            statement = delete(HubFeedEntryRow).where(
                HubFeedEntryRow.first_seen_at < _db_time(timestamp)
            )
            if subscription_id is not None:
                statement = statement.where(
                    HubFeedEntryRow.subscription_id == str(_as_uuid(subscription_id))
                )
            result = session.execute(statement)
            return int(result.rowcount or 0)

    prune = cleanup


class FeedReadStateRepository:
    """Small explicit facade for callers that only manage read state."""

    def __init__(self, database: HubDatabase) -> None:
        self.entries = FeedEntryRepository(database)

    def get(self, entry_id: str) -> FeedReadState:
        return self.entries.read_state(entry_id)

    read_state = get

    def mark_read(self, entry_id: str, *, at: datetime | None = None) -> FeedReadState:
        return self.entries.set_read_state(entry_id, "read", at=at)

    def mark_unread(self, entry_id: str, *, at: datetime | None = None) -> FeedReadState:
        return self.entries.set_read_state(entry_id, "unread", at=at)

    read = mark_read
    unread = mark_unread

    def unread_count(self, *, subscription_id: UUID | str | None = None) -> int:
        return self.entries.count(subscription_id=subscription_id, state="unread")


# Short aliases keep provider adapters readable without exposing generic
# assistant repositories through the Hub package.
SubscriptionRepository = FeedSubscriptionRepository
EntryRepository = FeedEntryRepository


__all__ = [
    "EntryRepository",
    "FeedEntryRepository",
    "FeedReadStateRepository",
    "FeedSubscriptionRepository",
    "SubscriptionRepository",
]
