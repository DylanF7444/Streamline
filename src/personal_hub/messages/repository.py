"""Hub SQLite persistence for message source configuration and bounded summaries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import cast

from sqlalchemy import delete, func, select

from personal_hub.messages.categories import MessageCategory
from personal_hub.messages.contracts import (
    MessageProvider,
    MessageSourceConfig,
    MessageState,
    MessageSummary,
    MessageSyncState,
)
from personal_hub.storage.database import HubDatabase, HubMessageSourceRow, HubMessageSummaryRow


def _db_time(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("message timestamps must be timezone-aware")
    return value.astimezone(UTC).replace(tzinfo=None)


def _time(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _summary_id(source_id: str, message_id: str) -> str:
    return sha256(f"{source_id}\0{message_id}".encode()).hexdigest()


class MessageRepository:
    def __init__(self, database: HubDatabase, *, max_summaries_per_source: int = 1_000) -> None:
        if not 10 <= max_summaries_per_source <= 10_000:
            raise ValueError("message summary count must be between 10 and 10000")
        self.database = database
        self.max_summaries_per_source = max_summaries_per_source

    @staticmethod
    def _config(row: HubMessageSourceRow) -> MessageSourceConfig:
        from personal_hub.messages.contracts import MessageQuery

        return MessageSourceConfig(
            source_id=row.source_id,
            provider=cast(MessageProvider, row.provider),
            account_id=row.account_id,
            query=MessageQuery.model_validate_json(row.query_json),
            refresh_minutes=row.refresh_minutes,
            retention_days=row.retention_days,
            enabled=bool(row.enabled),
            created_at=cast(datetime, _time(row.created_at)),
            updated_at=cast(datetime, _time(row.updated_at)),
        )

    @staticmethod
    def _sync(row: HubMessageSourceRow) -> MessageSyncState:
        return MessageSyncState(
            source_id=row.source_id,
            state=cast(MessageState, row.sync_state),
            last_checked_at=_time(row.last_checked_at),
            last_success_at=_time(row.last_success_at),
            failure_count=row.failure_count,
            backoff_until=_time(row.backoff_until),
            safe_error=row.safe_error,
        )

    @staticmethod
    def _summary(row: HubMessageSummaryRow) -> MessageSummary:
        return MessageSummary(
            source_id=row.source_id,
            message_id=row.message_id,
            thread_id=row.thread_id,
            provider=cast(MessageProvider, row.provider),
            account_id=row.account_id,
            sender=row.sender,
            title=row.title,
            preview=row.preview,
            received_at=cast(datetime, _time(row.received_at)),
            unread=bool(row.unread),
            importance=cast(str, row.importance),
            category=cast(MessageCategory, row.category),
            deep_link=row.deep_link,
            sensitive=bool(row.sensitive),
        )

    def add_source(self, config: MessageSourceConfig) -> MessageSourceConfig:
        with self.database.session() as session:
            row = session.get(HubMessageSourceRow, config.source_id)
            if row is None:
                row = HubMessageSourceRow(source_id=config.source_id)
                session.add(row)
            row.provider = config.provider
            row.account_id = config.account_id
            row.query_json = config.query.model_dump_json()
            row.refresh_minutes = config.refresh_minutes
            row.retention_days = config.retention_days
            row.enabled = config.enabled
            row.created_at = _db_time(config.created_at)
            row.updated_at = _db_time(config.updated_at)
        if config.retention_days == 0:
            self.clear_summaries(config.source_id)
        return config

    save_source = add_source

    def get_source(self, source_id: str) -> MessageSourceConfig | None:
        with self.database.session() as session:
            row = session.get(HubMessageSourceRow, source_id)
            return None if row is None else self._config(row)

    def list_sources(
        self, *, enabled: bool | None = None, limit: int = 1_000
    ) -> list[MessageSourceConfig]:
        if not 1 <= limit <= 10_000:
            raise ValueError("message source limit must be between 1 and 10000")
        with self.database.session() as session:
            statement = select(HubMessageSourceRow).order_by(
                HubMessageSourceRow.provider,
                HubMessageSourceRow.account_id,
                HubMessageSourceRow.source_id,
            )
            if enabled is not None:
                statement = statement.where(HubMessageSourceRow.enabled == enabled)
            return [self._config(row) for row in session.scalars(statement.limit(limit)).all()]

    def delete_source(self, source_id: str) -> bool:
        with self.database.session() as session:
            row = session.get(HubMessageSourceRow, source_id)
            if row is None:
                return False
            session.delete(row)
            return True

    def sync_state(self, source_id: str) -> MessageSyncState | None:
        with self.database.session() as session:
            row = session.get(HubMessageSourceRow, source_id)
            return None if row is None else self._sync(row)

    def save_sync_state(self, state: MessageSyncState) -> MessageSyncState:
        with self.database.session() as session:
            row = session.get(HubMessageSourceRow, state.source_id)
            if row is None:
                raise KeyError("message source does not exist")
            row.sync_state = state.state
            row.last_checked_at = _db_time(state.last_checked_at)
            row.last_success_at = _db_time(state.last_success_at)
            row.failure_count = state.failure_count
            row.backoff_until = _db_time(state.backoff_until)
            row.safe_error = state.safe_error
            row.updated_at = _db_time(datetime.now(UTC))
        return state

    def upsert_summaries(
        self,
        config: MessageSourceConfig,
        summaries: tuple[MessageSummary, ...],
        *,
        retained_at: datetime,
    ) -> int:
        if config.retention_days == 0:
            self.clear_summaries(config.source_id)
            return 0
        with self.database.session() as session:
            for summary in summaries[: self.max_summaries_per_source]:
                if summary.source_id != config.source_id:
                    raise ValueError("message summary belongs to a different source")
                identifier = _summary_id(summary.source_id, summary.message_id)
                row = session.get(HubMessageSummaryRow, identifier)
                if row is None:
                    row = HubMessageSummaryRow(summary_id=identifier, source_id=summary.source_id)
                    session.add(row)
                row.message_id = summary.message_id
                row.thread_id = summary.thread_id
                row.provider = summary.provider
                row.account_id = summary.account_id
                row.sender = summary.sender
                row.title = summary.title
                row.preview = summary.preview
                row.received_at = _db_time(summary.received_at)
                row.unread = summary.unread
                row.importance = summary.importance
                row.category = summary.category
                row.deep_link = summary.deep_link
                row.sensitive = summary.sensitive
                row.retained_at = _db_time(retained_at)
            cutoff = _db_time(retained_at - timedelta(days=config.retention_days))
            session.execute(
                delete(HubMessageSummaryRow).where(
                    HubMessageSummaryRow.source_id == config.source_id,
                    HubMessageSummaryRow.received_at < cutoff,
                )
            )
            overflow = session.scalars(
                select(HubMessageSummaryRow.summary_id)
                .where(HubMessageSummaryRow.source_id == config.source_id)
                .order_by(HubMessageSummaryRow.received_at.desc())
                .offset(self.max_summaries_per_source)
            ).all()
            if overflow:
                session.execute(
                    delete(HubMessageSummaryRow).where(
                        HubMessageSummaryRow.summary_id.in_(overflow)
                    )
                )
        return len(summaries)

    def list_summaries(
        self,
        *,
        source_id: str | None = None,
        unread: bool | None = None,
        category: MessageCategory | None = None,
        include_spam: bool = True,
        limit: int = 200,
    ) -> list[MessageSummary]:
        if not 1 <= limit <= 1_000:
            raise ValueError("message summary limit must be between 1 and 1000")
        with self.database.session() as session:
            statement = select(HubMessageSummaryRow).order_by(
                HubMessageSummaryRow.received_at.desc(), HubMessageSummaryRow.summary_id
            )
            if source_id is not None:
                statement = statement.where(HubMessageSummaryRow.source_id == source_id)
            if unread is not None:
                statement = statement.where(HubMessageSummaryRow.unread == unread)
            if category is not None:
                statement = statement.where(HubMessageSummaryRow.category == category)
            elif not include_spam:
                statement = statement.where(HubMessageSummaryRow.category != "spam")
            return [self._summary(row) for row in session.scalars(statement.limit(limit)).all()]

    def clear_summaries(self, source_id: str | None = None) -> int:
        with self.database.session() as session:
            statement = delete(HubMessageSummaryRow)
            if source_id is not None:
                statement = statement.where(HubMessageSummaryRow.source_id == source_id)
            return int(session.execute(statement).rowcount or 0)

    def cleanup(self, *, now: datetime | None = None) -> int:
        """Apply each configured source's retention without contacting its provider."""

        timestamp = now or datetime.now(UTC)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("message cleanup timestamp must be timezone-aware")
        with self.database.session() as session:
            removed = 0
            sources = session.scalars(select(HubMessageSourceRow)).all()
            for source in sources:
                if source.retention_days == 0:
                    statement = delete(HubMessageSummaryRow).where(
                        HubMessageSummaryRow.source_id == source.source_id
                    )
                else:
                    statement = delete(HubMessageSummaryRow).where(
                        HubMessageSummaryRow.source_id == source.source_id,
                        HubMessageSummaryRow.received_at
                        < _db_time(timestamp - timedelta(days=source.retention_days)),
                    )
                removed += int(session.execute(statement).rowcount or 0)
            return removed

    prune = cleanup

    def counts(self) -> tuple[int, int]:
        with self.database.session() as session:
            return (
                int(session.scalar(select(func.count()).select_from(HubMessageSourceRow)) or 0),
                int(session.scalar(select(func.count()).select_from(HubMessageSummaryRow)) or 0),
            )


__all__ = ["MessageRepository"]
