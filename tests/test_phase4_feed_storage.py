"""Phase 4 feed identity, persistence, read state, and retention tests."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from personal_hub.feeds.contracts import FeedEntry, FeedReadState, FeedSubscription, FeedSyncState
from personal_hub.feeds.identity import (
    canonicalize_entry_url,
    entry_identity_key,
    stable_feed_entry_id,
)
from personal_hub.feeds.repository import FeedEntryRepository, FeedSubscriptionRepository
from personal_hub.storage.database import (
    HubAuditEventRow,
    HubBase,
    HubDatabase,
    HubInboxItemRow,
    HubNotificationDeliveryRow,
    HubSchemaVersionRow,
    HubSettingRow,
    HubSourceConnectionRow,
    HubSourceSyncStateRow,
    HubTaskListRow,
    HubTaskOccurrenceRow,
    HubTaskRow,
    HubTaskTagRow,
)


def _database(tmp_path: Path, name: str = "hub.db") -> HubDatabase:
    database = HubDatabase(tmp_path / name)
    database.migrate()
    return database


def _subscription(
    subscription_id: UUID | None = None, *, title: str = "Release feed"
) -> FeedSubscription:
    return FeedSubscription(
        subscription_id=subscription_id or UUID("00000000-0000-0000-0000-000000000101"),
        title=title,
        feed_url="https://news.example.test/feed.xml",
        site_url="https://news.example.test/",
        include_terms=("  Personal   Hub ", "hub"),
        exclude_terms=("sponsored",),
    )


def _entry(
    subscription: FeedSubscription,
    *,
    key: str,
    published_at: datetime | None = None,
    first_seen_at: datetime | None = None,
    title: str | None = None,
) -> FeedEntry:
    url = f"https://news.example.test/articles/{key}"
    return FeedEntry(
        entry_id=stable_feed_entry_id(subscription.subscription_id, guid=key, url=url),
        subscription_id=subscription.subscription_id,
        title=title or f"Article {key}",
        url=url,
        author="Author",
        published_at=published_at,
        summary_text=f"Summary for {key}",
        first_seen_at=first_seen_at or datetime(2026, 8, 31, 12, tzinfo=UTC),
    )


def test_v4_feed_schema_migration_is_idempotent_and_upgrades_v3(tmp_path: Path) -> None:
    legacy_path = tmp_path / "legacy-v3.db"
    legacy = HubDatabase(legacy_path)
    try:
        HubBase.metadata.create_all(
            legacy.engine,
            tables=[
                HubSchemaVersionRow.__table__,
                HubSettingRow.__table__,
                HubSourceConnectionRow.__table__,
                HubSourceSyncStateRow.__table__,
                HubInboxItemRow.__table__,
                HubAuditEventRow.__table__,
                HubNotificationDeliveryRow.__table__,
                HubTaskListRow.__table__,
                HubTaskRow.__table__,
                HubTaskTagRow.__table__,
                HubTaskOccurrenceRow.__table__,
            ],
        )
        with legacy.session() as session:
            session.add(HubSchemaVersionRow(id=1, version=3))
    finally:
        legacy.close()

    database = HubDatabase(legacy_path)
    try:
        database.migrate()
        database.migrate()
        with sqlite3.connect(database.path) as connection:
            version = connection.execute(
                "SELECT version FROM hub_schema_migrations WHERE id = 1"
            ).fetchone()[0]
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        # Later additive phases may advance the database while preserving the
        # Phase 4 migration guarantee and feed tables.
        assert version == HubDatabase.CURRENT_SCHEMA_VERSION
        assert {
            "hub_feed_subscriptions",
            "hub_feed_entries",
            "hub_feed_read_state",
        } <= tables
        assert database.integrity_check()
    finally:
        database.close()


def test_feed_identity_prefers_guid_and_normalizes_canonical_url() -> None:
    subscription_id = UUID("00000000-0000-0000-0000-000000000102")
    assert stable_feed_entry_id(
        subscription_id,
        guid=" article-1 ",
        url="https://news.example.test/changed-link",
    ) == stable_feed_entry_id(subscription_id, guid="article-1", url="https://other.test/")
    assert stable_feed_entry_id(
        subscription_id,
        url="HTTPS://News.Example.Test:443/a/../article?b=2#comments",
    ) == stable_feed_entry_id(
        subscription_id,
        url="https://news.example.test/article?b=2",
    )
    assert stable_feed_entry_id(
        subscription_id,
        url="https://news.example.test/article",
    ) != stable_feed_entry_id(
        UUID("00000000-0000-0000-0000-000000000103"),
        url="https://news.example.test/article",
    )
    assert canonicalize_entry_url("https://News.Example.Test:443/a/../article#fragment") == (
        "https://news.example.test/article"
    )
    assert entry_identity_key(guid="GUID-1") == "guid:GUID-1"
    with pytest.raises(ValueError):
        entry_identity_key()
    with pytest.raises(ValueError):
        canonicalize_entry_url("file:///private")


def test_feed_subscription_and_sync_metadata_survive_restart(tmp_path: Path) -> None:
    database = _database(tmp_path)
    subscription = _subscription()
    checked_at = datetime(2026, 8, 31, 12, tzinfo=UTC)
    try:
        repository = FeedSubscriptionRepository(database)
        assert repository.add(subscription) == subscription
        assert repository.get(subscription.subscription_id) == subscription
        state = repository.record_sync(
            subscription.subscription_id,
            state="ready",
            checked_at=checked_at,
            success_at=checked_at,
            etag='"v1"',
            last_modified="Mon, 31 Aug 2026 12:00:00 GMT",
        )
        assert state.state == "ready"
        assert repository.sync_state(subscription.subscription_id) == state
    finally:
        database.close()

    restarted = _database(tmp_path)
    try:
        repository = FeedSubscriptionRepository(restarted)
        assert repository.get(subscription.subscription_id) == subscription
        restored_state = repository.sync_state(subscription.subscription_id)
        assert restored_state is not None
        assert restored_state.etag == '"v1"'
        assert restored_state.last_modified == "Mon, 31 Aug 2026 12:00:00 GMT"
        assert restored_state.last_success_at == checked_at
    finally:
        restarted.close()


def test_feed_reordering_upsert_is_idempotent_and_read_state_is_durable(tmp_path: Path) -> None:
    database = _database(tmp_path)
    subscription = _subscription()
    now = datetime(2026, 8, 31, 12, tzinfo=UTC)
    entries = [
        _entry(
            subscription,
            key=str(index),
            published_at=now + timedelta(minutes=index),
            first_seen_at=now,
        )
        for index in range(3)
    ]
    try:
        FeedSubscriptionRepository(database).add(subscription)
        repository = FeedEntryRepository(database)
        repository.upsert_many(entries, seen_at=now)
        updated = entries[1].model_copy(update={"title": "Updated after reorder"})
        repository.upsert_many([entries[2], updated, entries[0]], seen_at=now + timedelta(hours=1))
        assert repository.count(subscription_id=subscription.subscription_id) == 3
        assert repository.get(entries[1].entry_id).title == "Updated after reorder"
        assert repository.get(entries[1].entry_id).first_seen_at == now
        assert [
            entry.entry_id
            for entry in repository.list(subscription_id=subscription.subscription_id)
        ] == [
            entries[2].entry_id,
            entries[1].entry_id,
            entries[0].entry_id,
        ]
        assert repository.count(state="unread") == 3
        read_state = repository.mark_read(entries[1].entry_id, at=now + timedelta(hours=2))
        assert read_state == FeedReadState(
            entry_id=entries[1].entry_id,
            state="read",
            read_at=now + timedelta(hours=2),
            updated_at=now + timedelta(hours=2),
        )
        assert repository.count(state="unread") == 2
        assert repository.list_records(state="read")[0].entry.title == "Updated after reorder"
    finally:
        database.close()

    restarted = _database(tmp_path)
    try:
        repository = FeedEntryRepository(restarted)
        assert repository.count(subscription_id=subscription.subscription_id) == 3
        restored = repository.get_record(entries[1].entry_id)
        assert restored is not None
        assert restored.state == "read"
        assert restored.read_at == now + timedelta(hours=2)
        assert repository.mark_unread(entries[1].entry_id, at=now + timedelta(hours=3)).state == (
            "unread"
        )
    finally:
        restarted.close()


def test_feed_retention_and_count_bounds_remove_oldest_transactionally(tmp_path: Path) -> None:
    database = _database(tmp_path)
    subscription = _subscription()
    now = datetime(2026, 8, 31, 12, tzinfo=UTC)
    try:
        FeedSubscriptionRepository(database).add(subscription)
        repository = FeedEntryRepository(
            database,
            max_entries=3,
            max_entries_per_subscription=3,
            retention=timedelta(days=30),
        )
        entries = [
            _entry(
                subscription,
                key=str(index),
                first_seen_at=now - timedelta(days=40 if index < 2 else 10 - index),
            )
            for index in range(5)
        ]
        repository.upsert_many(entries, seen_at=now)
        assert repository.count() == 3
        remaining_ids = {entry.entry_id for entry in repository.list(limit=10)}
        assert remaining_ids == {entries[2].entry_id, entries[3].entry_id, entries[4].entry_id}
        assert repository.cleanup(now - timedelta(days=30)) == 0

        # Read rows are foreign-key dependent and disappear with the entry.
        repository.mark_read(entries[4].entry_id, at=now)
        assert repository.delete(entries[4].entry_id)
        with sqlite3.connect(database.path) as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM hub_feed_read_state WHERE entry_id = ?",
                    (entries[4].entry_id,),
                ).fetchone()[0]
                == 0
            )
    finally:
        database.close()


def test_feed_contracts_reject_unsafe_urls_and_unbounded_values() -> None:
    with pytest.raises(ValidationError):
        FeedSubscription(
            title="bad",
            feed_url="https://user:password@example.test/feed",
            site_url="https://example.test/",
        )
    with pytest.raises(ValidationError):
        FeedSubscription(
            title="bad",
            feed_url="https://example.test/feed",
            site_url="https://example.test/",
            refresh_minutes=1,
        )
    with pytest.raises(ValidationError):
        FeedSubscription(
            title="bad",
            feed_url="https://example.test/feed",
            site_url="https://example.test/",
            include_terms=("",),
        )
    with pytest.raises(ValidationError):
        FeedSubscription(title="plain HTTP", feed_url="http://example.test/feed")
    http_subscription = FeedSubscription(
        title="Reviewed HTTP",
        feed_url="http://example.test/feed",
        allow_http=True,
        reviewed_origin="http://example.test/",
    )
    assert http_subscription.reviewed_origin == "http://example.test"
    with pytest.raises(ValidationError):
        FeedSubscription(
            title="Wrong review",
            feed_url="http://example.test/feed",
            allow_http=True,
            reviewed_origin="http://other.example.test",
        )
    with pytest.raises(ValidationError):
        FeedSyncState(
            subscription_id=UUID("00000000-0000-0000-0000-000000000104"),
            etag="bad\r\nX-Leak: yes",
        )
    with pytest.raises(ValidationError):
        FeedEntry(
            entry_id="entry",
            subscription_id=UUID("00000000-0000-0000-0000-000000000105"),
            title="Title",
            url="https://example.test/article",
            first_seen_at=datetime(2026, 8, 31, 12),
        )
