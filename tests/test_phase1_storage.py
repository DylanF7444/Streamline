from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from personal_hub.config import HubSettings
from personal_hub.contracts import (
    AuditEvent,
    Freshness,
    InboxItem,
    NotificationDelivery,
    SourceConnection,
    SourceRef,
    SourceSyncState,
)
from personal_hub.storage.database import HubDatabase
from personal_hub.storage.repositories import (
    AuditRepository,
    InboxRepository,
    NotificationRepository,
    SettingsRepository,
    SourceRepository,
)


def _database(tmp_path: Path, name: str = "personal_hub.db") -> HubDatabase:
    database = HubDatabase(tmp_path / name)
    database.migrate()
    return database


def _source_and_health() -> tuple[SourceConnection, SourceSyncState]:
    now = datetime(2026, 8, 31, 12, tzinfo=UTC)
    connection = SourceConnection(
        connection_id="calendar-1",
        provider_id="google",
        account_label="Calendar",
        capabilities=("calendar.read",),
        granted_scopes=("calendar.readonly",),
        created_at=now,
        updated_at=now,
    )
    health = SourceSyncState(
        connection_id=connection.connection_id,
        freshness=Freshness(state="ready", collected_at=now),
        last_attempt_at=now,
        last_success_at=now,
    )
    return connection, health


def test_phase1_schema_migration_is_idempotent_and_upgrades_phase0(tmp_path: Path):
    path = tmp_path / "hub.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE hub_schema_migrations (id INTEGER PRIMARY KEY, version INTEGER NOT NULL)"
        )
        connection.execute("INSERT INTO hub_schema_migrations(id, version) VALUES (1, 1)")

    database = HubDatabase(path)
    try:
        database.migrate()
        database.migrate()
        with sqlite3.connect(path) as connection:
            version = connection.execute(
                "SELECT version FROM hub_schema_migrations WHERE id = 1"
            ).fetchone()
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
        assert version == (HubDatabase.CURRENT_SCHEMA_VERSION,)
        assert tables >= HubDatabase.REQUIRED_TABLES
        assert database.integrity_check()
    finally:
        database.close()


def test_settings_sources_and_inbox_survive_database_restart(tmp_path: Path):
    database = _database(tmp_path)
    connection, health = _source_and_health()
    item = InboxItem(
        item_id="alert-1",
        kind="source_failure",
        title="Calendar needs attention",
        safe_summary="Reconnect the calendar source.",
        reason="authentication expired",
        source=SourceRef(
            provider_id="google",
            connection_id=connection.connection_id,
            display_name="Calendar",
        ),
        navigation_target="hub://sources/calendar-1",
        severity="attention",
        deduplication_key="source:calendar-1:auth",
    )
    try:
        SettingsRepository(database).set("offline_mode", True)
        source_repository = SourceRepository(database)
        source_repository.save(connection)
        source_repository.save_sync_state(health)
        inbox = InboxRepository(database)
        inbox.add(item)
        assert inbox.mark_read(item.item_id) is not None
    finally:
        database.close()

    restarted = _database(tmp_path)
    try:
        assert SettingsRepository(restarted).get("offline_mode") is True
        assert SourceRepository(restarted).get("calendar-1") == connection
        assert SourceRepository(restarted).get_health("calendar-1") == health
        restored = InboxRepository(restarted).get(item.item_id)
        assert restored is not None
        assert restored.state == "read"
        assert restored.read_at is not None
        assert InboxRepository(restarted).unread_count() == 0
    finally:
        restarted.close()


def test_inbox_state_transitions_and_deduplication_are_durable(tmp_path: Path):
    database = _database(tmp_path)
    try:
        repository = InboxRepository(database)
        item = InboxItem(
            item_id="alert-2",
            kind="watch_alert",
            title="Price changed",
            safe_summary="The watched value changed.",
            reason="threshold crossed",
            navigation_target="hub://watches/2",
            deduplication_key="watch:2:threshold",
        )
        assert repository.add(item) == item
        duplicate = item.model_copy(update={"item_id": "alert-3"})
        assert repository.add(duplicate).item_id == item.item_id
        assert repository.unread_count() == 1
        assert repository.dismiss(item.item_id).state == "dismissed"
        assert repository.archive(item.item_id).state == "archived"
        assert repository.mark_unread(item.item_id).state == "unread"
        assert repository.mark_read(item.item_id).state == "read"
    finally:
        database.close()


def test_notification_deduplication_is_bounded_and_persistent(tmp_path: Path):
    database = _database(tmp_path)
    now = datetime(2026, 8, 31, 12, tzinfo=UTC)
    try:
        repository = NotificationRepository(database, max_entries=2)
        for key in ("a", "b"):
            assert repository.claim(
                NotificationDelivery(
                    deduplication_key=key,
                    channel="windows",
                    created_at=now,
                    expires_at=now + timedelta(days=1),
                )
            )
        assert not repository.claim(
            NotificationDelivery(
                deduplication_key="a",
                channel="windows",
                created_at=now,
                expires_at=now + timedelta(days=1),
            )
        )
        assert repository.mark_delivered("a", "windows", at=now) is not None
        suppressed = repository.mark_suppressed(
            "b",
            "windows",
            at=now,
            safe_message="Notification was suppressed by quiet hours.",
        )
        assert suppressed is not None
        assert suppressed.status == "suppressed"
        assert suppressed.safe_message == "Notification was suppressed by quiet hours."
        assert len(repository.list()) == 2
        assert repository.claim(
            NotificationDelivery(
                deduplication_key="c",
                channel="windows",
                created_at=now + timedelta(minutes=1),
                expires_at=now + timedelta(days=1),
            )
        )
        assert len(repository.list()) == 2
    finally:
        database.close()

    restarted = _database(tmp_path)
    try:
        repository = NotificationRepository(restarted, max_entries=2)
        assert repository.was_claimed("c", "windows")
        assert len(repository.list()) == 2
    finally:
        restarted.close()


def test_backup_restore_round_trip_and_corrupt_source_rejection(tmp_path: Path):
    database = _database(tmp_path)
    try:
        SettingsRepository(database).set("offline_mode", True)
        backup = database.backup(tmp_path / "backups" / "hub.sqlite")
        assert backup.is_file()
        SettingsRepository(database).set("offline_mode", False)
        assert SettingsRepository(database).get("offline_mode") is False
        assert database.restore(backup) == database.path
        assert SettingsRepository(database).get("offline_mode") is True

        before = database.path.read_bytes()
        corrupt = tmp_path / "corrupt.sqlite"
        corrupt.write_bytes(b"this is not a sqlite database")
        with pytest.raises(ValueError):
            database.restore(corrupt)
        assert database.path.read_bytes() == before
        assert database.integrity_check()
    finally:
        database.close()


def test_databases_are_isolated_and_audit_rejects_private_content(tmp_path: Path):
    first = _database(tmp_path / "first")
    second = _database(tmp_path / "second")
    try:
        SettingsRepository(first).set("offline_mode", True)
        assert SettingsRepository(second).get("offline_mode") is None

        audit = AuditRepository(first)
        event = AuditEvent(
            event_type="source.refresh",
            source_type="calendar",
            source_id="calendar-1",
            metadata={"item_count": 2, "response_size_bucket": "small"},
        )
        assert audit.append(event) == event
        assert audit.list()[0].metadata == event.metadata
        with pytest.raises(ValidationError):
            AuditEvent(
                event_type="source.refresh",
                metadata={"message_body": "private message text"},
            )
        with pytest.raises(ValidationError):
            AuditEvent(
                event_type="source.refresh",
                metadata={"oauth_token": "secret"},
            )
        raw = first.path.read_bytes()
        assert b"private message text" not in raw
        assert b"secret" not in raw
    finally:
        first.close()
        second.close()


def test_phase1_contracts_are_strict_and_timezone_aware():
    with pytest.raises(ValidationError):
        SourceRef(provider_id="google", display_name="Calendar", extra_field="nope")
    with pytest.raises(ValidationError):
        Freshness(state="ready", collected_at=datetime(2026, 8, 31, 12))
    with pytest.raises(ValidationError):
        InboxItem(
            kind="alert",
            title="title",
            safe_summary="summary",
            reason="reason",
            navigation_target="hub://alerts/1",
            metadata={"not-valid": "key"},
        )


def test_settings_restart_never_redirects_hub_identity_paths(tmp_path: Path):
    database = _database(tmp_path / "first")
    try:
        repository = SettingsRepository(database)
        first = HubSettings(
            profile="testing",
            data_dir=tmp_path / "first",
            database_name="first-hub.db",
            offline_mode=True,
        )
        repository.save_settings(first)

        second_defaults = HubSettings(
            profile="production",
            data_dir=tmp_path / "second",
            database_name="second-hub.db",
        )
        loaded = repository.load_settings(second_defaults)

        assert loaded.profile == "production"
        assert loaded.data_dir == second_defaults.data_dir
        assert loaded.database_name == "second-hub.db"
        assert loaded.offline_mode is True
        assert "data_dir" not in repository.all()
    finally:
        database.close()
