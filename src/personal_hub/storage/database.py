"""Hub-owned SQLite schema, migrations, and verified backup/restore.

The database layer is deliberately independent from ``assistant.storage``.  It
owns its metadata, schema-version table, and all state needed by Personal Hub's
local-first foundation.  SQLite online backup is used for both backup and restore;
restore validates a temporary copy before atomically replacing the active file.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    delete,
    event,
)
from sqlalchemy import (
    inspect as sqlalchemy_inspect,
)
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


def _now() -> datetime:
    return datetime.now(UTC)


class HubBase(DeclarativeBase):
    """Declarative metadata owned exclusively by Personal Hub."""


class HubSchemaVersionRow(HubBase):
    __tablename__ = "hub_schema_migrations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class HubSettingRow(HubBase):
    __tablename__ = "hub_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value_json: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class HubSourceConnectionRow(HubBase):
    __tablename__ = "hub_source_connections"

    connection_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    provider_id: Mapped[str] = mapped_column(String(64), nullable=False)
    account_label: Mapped[str] = mapped_column(String(160), nullable=False)
    capabilities_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    granted_scopes_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class HubSourceSyncStateRow(HubBase):
    __tablename__ = "hub_source_sync_state"

    connection_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("hub_source_connections.connection_id", ondelete="CASCADE"),
        primary_key=True,
    )
    freshness_state: Mapped[str] = mapped_column(String(32), nullable=False)
    collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    safe_message: Mapped[str | None] = mapped_column(String(512))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    backoff_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class HubInboxItemRow(HubBase):
    __tablename__ = "hub_inbox_items"

    item_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    safe_summary: Mapped[str] = mapped_column(String(1_000), nullable=False)
    reason: Mapped[str] = mapped_column(String(240), nullable=False)
    source_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    navigation_target: Mapped[str] = mapped_column(String(512), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="unread")
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deduplication_key: Mapped[str | None] = mapped_column(String(256))
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    __table_args__ = (
        Index("ix_hub_inbox_items_state_created", "state", "created_at"),
        Index("ix_hub_inbox_items_deduplication", "deduplication_key"),
    )


class HubAuditEventRow(HubBase):
    __tablename__ = "hub_audit_events"

    audit_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(96), nullable=False)
    source_type: Mapped[str | None] = mapped_column(String(64))
    source_id: Mapped[str | None] = mapped_column(String(128))
    outcome: Mapped[str] = mapped_column(String(16), nullable=False, default="succeeded")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    __table_args__ = (Index("ix_hub_audit_events_occurred", "occurred_at"),)


class HubNotificationDeliveryRow(HubBase):
    __tablename__ = "hub_notification_deliveries"

    delivery_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    deduplication_key: Mapped[str] = mapped_column(String(256), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    safe_message: Mapped[str | None] = mapped_column(String(512))

    __table_args__ = (
        UniqueConstraint(
            "deduplication_key",
            "channel",
            name="uq_hub_notification_delivery_key_channel",
        ),
        Index("ix_hub_notification_deliveries_expires", "expires_at"),
    )


class HubTaskListRow(HubBase):
    __tablename__ = "hub_task_lists"

    list_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (Index("ix_hub_task_lists_name", "name"),)


class HubTaskRow(HubBase):
    __tablename__ = "hub_tasks"

    task_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    external_url: Mapped[str | None] = mapped_column(String(1_024))
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    priority: Mapped[str] = mapped_column(String(16), nullable=False, default="normal")
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_is_all_day: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    timezone: Mapped[str] = mapped_column(String(128), nullable=False, default="UTC")
    list_id: Mapped[str | None] = mapped_column(String(36))
    tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    recurrence_json: Mapped[str | None] = mapped_column(Text)
    reminder_offsets_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    series_id: Mapped[str | None] = mapped_column(String(36))
    occurrence_index: Mapped[int | None] = mapped_column(Integer)
    occurrence_key: Mapped[str | None] = mapped_column(String(256), unique=True)
    generated_from_task_id: Mapped[str | None] = mapped_column(String(36))

    __table_args__ = (
        Index("ix_hub_tasks_state_due", "state", "due_at"),
        Index("ix_hub_tasks_state_updated", "state", "updated_at"),
        Index("ix_hub_tasks_list_state_due", "list_id", "state", "due_at"),
        Index("ix_hub_tasks_completed_at", "completed_at"),
        Index("ix_hub_tasks_series_occurrence", "series_id", "occurrence_index"),
    )


class HubTaskDeletionMarkerRow(HubBase):
    """Durable marker for an externally sourced task deleted in Personal Hub."""

    __tablename__ = "hub_task_deletion_markers"

    provider_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    external_key: Mapped[str] = mapped_column(String(1_024), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(36), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_hub_task_deletion_markers_task", "task_id"),
        Index("ix_hub_task_deletion_markers_deleted", "deleted_at"),
    )


class HubTaskTagRow(HubBase):
    __tablename__ = "hub_task_tags"

    task_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("hub_tasks.task_id", ondelete="CASCADE"),
        primary_key=True,
    )
    tag: Mapped[str] = mapped_column(String(64), primary_key=True)

    __table_args__ = (Index("ix_hub_task_tags_tag_task", "tag", "task_id"),)


class HubTaskOccurrenceRow(HubBase):
    __tablename__ = "hub_task_occurrences"

    occurrence_key: Mapped[str] = mapped_column(String(256), primary_key=True)
    series_id: Mapped[str] = mapped_column(String(36), nullable=False)
    task_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("hub_tasks.task_id", ondelete="CASCADE"),
        nullable=False,
    )
    occurrence_index: Mapped[int] = mapped_column(Integer, nullable=False)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_task_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        UniqueConstraint(
            "series_id",
            "occurrence_index",
            name="uq_hub_task_occurrences_series_index",
        ),
        Index("ix_hub_task_occurrences_task", "task_id"),
        Index("ix_hub_task_occurrences_series_due", "series_id", "due_at"),
    )


class HubFeedSubscriptionRow(HubBase):
    """Durable RSS/Atom subscription configuration and sync metadata."""

    __tablename__ = "hub_feed_subscriptions"

    subscription_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    feed_url: Mapped[str] = mapped_column(String(2_048), nullable=False)
    site_url: Mapped[str | None] = mapped_column(String(2_048))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    refresh_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    include_terms_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    exclude_terms_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    digest_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    allow_http: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reviewed_origin: Mapped[str | None] = mapped_column(String(512))
    sync_state: Mapped[str] = mapped_column(String(32), nullable=False, default="stale")
    etag: Mapped[str | None] = mapped_column(String(512))
    last_modified: Mapped[str | None] = mapped_column(String(512))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    backoff_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    safe_error: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        Index("ix_hub_feed_subscriptions_enabled", "enabled"),
        Index("ix_hub_feed_subscriptions_feed_url", "feed_url"),
    )


class HubFeedEntryRow(HubBase):
    """Bounded normalized feed entry content keyed by stable entry identity."""

    __tablename__ = "hub_feed_entries"

    entry_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    subscription_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("hub_feed_subscriptions.subscription_id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    url: Mapped[str] = mapped_column(String(2_048), nullable=False)
    author: Mapped[str | None] = mapped_column(String(240))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary_text: Mapped[str | None] = mapped_column(String(8_000))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        Index(
            "ix_hub_feed_entries_subscription_published",
            "subscription_id",
            "published_at",
        ),
        Index("ix_hub_feed_entries_first_seen", "first_seen_at"),
    )


class HubFeedReadStateRow(HubBase):
    """Per-entry local read state, separated from provider content."""

    __tablename__ = "hub_feed_read_state"

    entry_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("hub_feed_entries.entry_id", ondelete="CASCADE"),
        primary_key=True,
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="unread")
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (Index("ix_hub_feed_read_state_state", "state"),)


class HubWatchDefinitionRow(HubBase):
    """Durable typed webpage watch configuration and last-good scheduler state."""

    __tablename__ = "hub_watch_definitions"

    watch_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_url: Mapped[str] = mapped_column(String(2_048), nullable=False)
    condition_json: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notification_policy: Mapped[str] = mapped_column(
        String(16), nullable=False, default="immediate"
    )
    cooldown_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    allow_http: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reviewed_origin: Mapped[str] = mapped_column(String(512), nullable=False)
    approved_redirect_origins_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    sync_state: Mapped[str] = mapped_column(String(32), nullable=False, default="stale")
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    backoff_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    safe_error: Mapped[str | None] = mapped_column(String(512))
    baseline_fingerprint: Mapped[str | None] = mapped_column(String(64))
    baseline_value_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        Index("ix_hub_watch_definitions_enabled", "enabled"),
        Index("ix_hub_watch_definitions_origin", "reviewed_origin"),
        Index("ix_hub_watch_definitions_due", "enabled", "last_checked_at"),
    )


class HubWatchObservationRow(HubBase):
    """Bounded normalized observation; raw page bodies are never stored."""

    __tablename__ = "hub_watch_observations"

    observation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    watch_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("hub_watch_definitions.watch_id", ondelete="CASCADE"),
        nullable=False,
    )
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_value_json: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    __table_args__ = (Index("ix_hub_watch_observations_watch_checked", "watch_id", "checked_at"),)


class HubWatchAlertRow(HubBase):
    """Explainable, deduplicated alert created by a typed watch transition."""

    __tablename__ = "hub_watch_alerts"

    alert_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    watch_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("hub_watch_definitions.watch_id", ondelete="CASCADE"),
        nullable=False,
    )
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    safe_summary: Mapped[str] = mapped_column(String(1_000), nullable=False)
    previous_value_json: Mapped[str] = mapped_column(Text, nullable=False)
    current_value_json: Mapped[str] = mapped_column(Text, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deduplication_key: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    inbox_item_id: Mapped[str | None] = mapped_column(String(128))

    __table_args__ = (Index("ix_hub_watch_alerts_watch_observed", "watch_id", "observed_at"),)


class HubMessageSourceRow(HubBase):
    """Configured, explicitly bounded message-summary source."""

    __tablename__ = "hub_message_sources"

    source_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    account_id: Mapped[str] = mapped_column(String(160), nullable=False)
    query_json: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    retention_days: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sync_state: Mapped[str] = mapped_column(String(32), nullable=False, default="stale")
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    backoff_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    safe_error: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        Index("ix_hub_message_sources_provider", "provider"),
        Index("ix_hub_message_sources_enabled", "enabled"),
    )


class HubMessageSummaryRow(HubBase):
    """Retained bounded summary only; full message bodies have no database table."""

    __tablename__ = "hub_message_summaries"

    summary_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("hub_message_sources.source_id", ondelete="CASCADE"),
        nullable=False,
    )
    message_id: Mapped[str] = mapped_column(String(256), nullable=False)
    thread_id: Mapped[str | None] = mapped_column(String(256))
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    account_id: Mapped[str] = mapped_column(String(160), nullable=False)
    sender: Mapped[str] = mapped_column(String(320), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    preview: Mapped[str | None] = mapped_column(String(1_000))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    unread: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    importance: Mapped[str] = mapped_column(String(16), nullable=False, default="normal")
    category: Mapped[str] = mapped_column(String(16), nullable=False, default="primary")
    deep_link: Mapped[str | None] = mapped_column(String(2_048))
    sensitive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    retained_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        UniqueConstraint("source_id", "message_id", name="uq_hub_message_summary_source_id"),
        Index("ix_hub_message_summaries_source_received", "source_id", "received_at"),
        Index("ix_hub_message_summaries_unread_received", "unread", "received_at"),
    )


class HubCalendarEventStateRow(HubBase):
    """Legacy event-completion rows retained only for safe schema migration."""

    __tablename__ = "hub_calendar_event_states"

    source_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    calendar_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    provider_id: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="completed")
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    external_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        Index("ix_hub_calendar_event_states_state_updated", "state", "updated_at"),
        Index(
            "ix_hub_calendar_event_states_interval",
            "calendar_id",
            "event_starts_at",
            "event_ends_at",
        ),
    )


class HubCalendarImportRow(HubBase):
    """A named batch of explicitly imported, local calendar items."""

    __tablename__ = "hub_calendar_imports"

    import_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    source_format: Mapped[str] = mapped_column(String(8), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (Index("ix_hub_calendar_imports_name", "name"),)


class HubImportedCalendarEventRow(HubBase):
    """Bounded local event content owned by one CSV/text import batch."""

    __tablename__ = "hub_imported_calendar_events"

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    import_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("hub_calendar_imports.import_id", ondelete="CASCADE"),
        nullable=False,
    )
    stable_key: Mapped[str] = mapped_column(String(320), nullable=False)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    all_day: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    timezone: Mapped[str] = mapped_column(String(128), nullable=False, default="UTC")
    location: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    deep_link: Mapped[str] = mapped_column(String(1_024), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        UniqueConstraint(
            "import_id",
            "stable_key",
            name="uq_hub_imported_calendar_events_import_key",
        ),
        Index("ix_hub_imported_calendar_events_import_start", "import_id", "starts_at"),
    )


class HubDatabase:
    """Small, explicit database boundary used by the Hub composition root."""

    # Version 1 was the Phase 0 migration table. Version 2 adds the foundation
    # tables, version 3 adds local task/recurrence tables, version 4 adds
    # RSS/Atom subscriptions, entries, and local read state, and version 5 adds
    # webpage watch definitions, normalized observations, and alerts. Version 6 adds
    # message-source configuration and retained summaries; bodies intentionally have no
    # table. Version 7 adds identity-only local calendar completion state and
    # external-deletion tombstones. Version 8 adds the local Gmail-style
    # message category. Declarative table
    # Version 9 retires calendar-event completion and deletes those legacy rows.
    # Version 10 adds a safe provider URL to local tasks so imported tasks can
    # open their source platform without storing a private feed URL. Version
    # 11 adds explicitly local CSV/text calendar import batches and events.
    # Version 12 adds durable local deletion markers for external tasks.
    # Declarative table creation remains idempotent so an older backup can be
    # restored and then migrated safely.
    CURRENT_SCHEMA_VERSION: ClassVar[int] = 12
    REQUIRED_TABLES: ClassVar[frozenset[str]] = frozenset(
        {
            "hub_schema_migrations",
            "hub_settings",
            "hub_source_connections",
            "hub_source_sync_state",
            "hub_inbox_items",
            "hub_audit_events",
            "hub_notification_deliveries",
            "hub_task_lists",
            "hub_tasks",
            "hub_task_tags",
            "hub_task_occurrences",
            "hub_task_deletion_markers",
            "hub_feed_subscriptions",
            "hub_feed_entries",
            "hub_feed_read_state",
            "hub_watch_definitions",
            "hub_watch_observations",
            "hub_watch_alerts",
            "hub_message_sources",
            "hub_message_summaries",
            "hub_calendar_event_states",
            "hub_calendar_imports",
            "hub_imported_calendar_events",
        }
    )
    PHASE0_REQUIRED_TABLES: ClassVar[frozenset[str]] = frozenset({"hub_schema_migrations"})
    PHASE1_REQUIRED_TABLES: ClassVar[frozenset[str]] = frozenset(
        {
            "hub_schema_migrations",
            "hub_settings",
            "hub_source_connections",
            "hub_source_sync_state",
            "hub_inbox_items",
            "hub_audit_events",
            "hub_notification_deliveries",
        }
    )
    PHASE2_REQUIRED_TABLES: ClassVar[frozenset[str]] = frozenset(
        {
            "hub_schema_migrations",
            "hub_settings",
            "hub_source_connections",
            "hub_source_sync_state",
            "hub_inbox_items",
            "hub_audit_events",
            "hub_notification_deliveries",
            "hub_task_lists",
            "hub_tasks",
            "hub_task_tags",
            "hub_task_occurrences",
        }
    )
    PHASE4_REQUIRED_TABLES: ClassVar[frozenset[str]] = frozenset(
        REQUIRED_TABLES.difference(
            {
                "hub_watch_definitions",
                "hub_watch_observations",
                "hub_watch_alerts",
                "hub_message_sources",
                "hub_message_summaries",
                "hub_calendar_event_states",
                "hub_task_deletion_markers",
            }
        )
    )
    PHASE5_REQUIRED_TABLES: ClassVar[frozenset[str]] = frozenset(
        REQUIRED_TABLES.difference(
            {
                "hub_message_sources",
                "hub_message_summaries",
                "hub_calendar_event_states",
                "hub_task_deletion_markers",
            }
        )
    )
    PHASE6_REQUIRED_TABLES: ClassVar[frozenset[str]] = frozenset(
        REQUIRED_TABLES.difference(
            {
                "hub_calendar_event_states",
                "hub_calendar_imports",
                "hub_imported_calendar_events",
                "hub_task_deletion_markers",
            }
        )
    )
    REQUIRED_TABLES_BY_VERSION: ClassVar[dict[int, frozenset[str]]] = {
        1: PHASE0_REQUIRED_TABLES,
        2: PHASE1_REQUIRED_TABLES,
        3: PHASE2_REQUIRED_TABLES,
        4: PHASE4_REQUIRED_TABLES,
        5: PHASE5_REQUIRED_TABLES,
        6: PHASE6_REQUIRED_TABLES,
        7: frozenset(REQUIRED_TABLES.difference({"hub_task_deletion_markers"})),
        8: frozenset(REQUIRED_TABLES.difference({"hub_task_deletion_markers"})),
        9: frozenset(REQUIRED_TABLES.difference({"hub_task_deletion_markers"})),
        10: frozenset(
            REQUIRED_TABLES.difference(
                {
                    "hub_calendar_imports",
                    "hub_imported_calendar_events",
                    "hub_task_deletion_markers",
                }
            )
        ),
        11: frozenset(REQUIRED_TABLES.difference({"hub_task_deletion_markers"})),
        12: REQUIRED_TABLES,
    }

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser().resolve(strict=False)
        self._assert_not_assistant_path(self.path)
        self.url = f"sqlite:///{self.path.as_posix()}"
        self.database_path = self.path
        self.database_url = self.url
        self.engine = self._create_engine()
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self._closed = False

    def _create_engine(self):
        engine = create_engine(
            self.url,
            future=True,
            connect_args={"check_same_thread": False, "timeout": 30},
        )
        event.listen(engine, "connect", self._configure_sqlite)
        return engine

    @staticmethod
    def _configure_sqlite(dbapi_connection: Any, _: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=FULL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Personal Hub database is closed")

    @staticmethod
    def _assert_not_assistant_path(path: Path) -> None:
        components = {component.casefold() for component in path.parts}
        if path.name.casefold() == "assistant.db" or "localassistant" in components:
            raise ValueError("Personal Hub storage cannot target Local Assistant data")

    def migrate(self) -> None:
        """Create or advance the idempotent Hub schema."""

        self._ensure_open()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            HubBase.metadata.create_all(self.engine)
        except OperationalError as error:
            # Concurrent launch can race between SQLite's table check and DDL.
            # Repeating idempotent creation is safe; every other failure remains
            # visible to the composition root.
            if "already exists" not in str(error).casefold():
                raise
            HubBase.metadata.create_all(self.engine)

        # A development database may have been created by an earlier Phase 4
        # build before conditional-request metadata was added.  SQLite cannot
        # express those additions through ``create_all``; add only the known,
        # non-secret columns with safe defaults so the migration stays
        # idempotent and existing feed content remains intact.
        self._ensure_feed_subscription_columns()
        self._ensure_message_summary_columns()
        self._ensure_task_columns()

        with self.session() as session:
            row = session.get(HubSchemaVersionRow, 1)
            if row is None:
                session.add(HubSchemaVersionRow(id=1, version=self.CURRENT_SCHEMA_VERSION))
            elif row.version > self.CURRENT_SCHEMA_VERSION:
                raise RuntimeError("Personal Hub database uses a newer schema version")
            elif row.version < self.CURRENT_SCHEMA_VERSION:
                self._run_migrations(session, row.version)

    def _ensure_feed_subscription_columns(self) -> None:
        inspector = sqlalchemy_inspect(self.engine)
        table_names = set(inspector.get_table_names())
        if "hub_feed_subscriptions" not in table_names:
            return
        existing = {column["name"] for column in inspector.get_columns("hub_feed_subscriptions")}
        additions = {
            "allow_http": "BOOLEAN NOT NULL DEFAULT 0",
            "reviewed_origin": "VARCHAR(512)",
            "sync_state": "VARCHAR(32) NOT NULL DEFAULT 'stale'",
        }
        missing = [
            (name, definition) for name, definition in additions.items() if name not in existing
        ]
        if not missing:
            return
        with self.engine.begin() as connection:
            for name, definition in missing:
                # Names are fixed above, not user input; keeping the whitelist
                # explicit prevents accidental identifier injection in DDL.
                connection.exec_driver_sql(
                    f"ALTER TABLE hub_feed_subscriptions ADD COLUMN {name} {definition}"
                )

    def _ensure_message_summary_columns(self) -> None:
        inspector = sqlalchemy_inspect(self.engine)
        table_names = set(inspector.get_table_names())
        if "hub_message_summaries" not in table_names:
            return
        existing = {column["name"] for column in inspector.get_columns("hub_message_summaries")}
        if "category" in existing:
            return
        with self.engine.begin() as connection:
            # This is a fixed schema identifier and default, never user input.
            connection.exec_driver_sql(
                "ALTER TABLE hub_message_summaries "
                "ADD COLUMN category VARCHAR(16) NOT NULL DEFAULT 'primary'"
            )

    def _ensure_task_columns(self) -> None:
        inspector = sqlalchemy_inspect(self.engine)
        table_names = set(inspector.get_table_names())
        if "hub_tasks" not in table_names:
            return
        existing = {column["name"] for column in inspector.get_columns("hub_tasks")}
        if "external_url" in existing:
            return
        with self.engine.begin() as connection:
            # This is a fixed schema identifier; provider URLs are values, not
            # interpolated SQL.
            connection.exec_driver_sql(
                "ALTER TABLE hub_tasks ADD COLUMN external_url VARCHAR(1024)"
            )

    def _run_migrations(self, session: Session, from_version: int) -> None:
        # All Hub tables are declaratively created above. Keeping the version
        # loop explicit makes later migrations additive and idempotent.
        version = from_version
        while version < self.CURRENT_SCHEMA_VERSION:
            version += 1
            if version == 9:
                session.execute(delete(HubCalendarEventStateRow))
        row = session.get(HubSchemaVersionRow, 1)
        if row is None:
            raise RuntimeError("Personal Hub schema version record is missing")
        row.version = version

    @contextmanager
    def session(self) -> Iterator[Session]:
        self._ensure_open()
        session = self.sessions()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def _readonly_connection(path: Path) -> sqlite3.Connection:
        # ``mode=ro`` prevents integrity/restore validation from creating a
        # missing or malformed source file as a side effect.
        uri = f"file:{path.as_posix()}?mode=ro"
        return sqlite3.connect(uri, uri=True, timeout=1)

    @classmethod
    def _validate_database_file(cls, path: Path) -> int:
        if not path.is_file():
            raise ValueError(f"Hub database source does not exist: {path}")
        connection: sqlite3.Connection | None = None
        try:
            connection = cls._readonly_connection(path)
            result = connection.execute("PRAGMA integrity_check").fetchone()
            if result is None or result[0] != "ok":
                raise ValueError("Hub database integrity check failed")
            version_row = connection.execute(
                "SELECT version FROM hub_schema_migrations WHERE id = 1"
            ).fetchone()
            if version_row is None:
                raise ValueError("Hub database schema version record is missing")
            version = int(version_row[0])
            if version > cls.CURRENT_SCHEMA_VERSION:
                raise ValueError("Hub database uses a newer schema version")
            required_tables = cls.REQUIRED_TABLES_BY_VERSION.get(version)
            if required_tables is None:
                raise ValueError(f"Hub database uses unsupported schema version: {version}")
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
            table_names = {str(row[0]) for row in rows}
            missing = required_tables.difference(table_names)
            if missing:
                raise ValueError(
                    "Hub database is missing required tables: " + ", ".join(sorted(missing))
                )
            return version
        except (OSError, sqlite3.DatabaseError) as error:
            raise ValueError("Hub database validation failed") from error
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _temporary_path(parent: Path, prefix: str) -> Path:
        descriptor, name = tempfile.mkstemp(prefix=prefix, suffix=".sqlite", dir=parent)
        os.close(descriptor)
        return Path(name)

    @staticmethod
    def _remove_sidecars(path: Path) -> None:
        for suffix in ("-wal", "-shm"):
            sidecar = path.with_name(path.name + suffix)
            with suppress(FileNotFoundError):
                sidecar.unlink()

    @classmethod
    def _online_copy(cls, source: Path, destination: Path) -> None:
        source_connection: sqlite3.Connection | None = None
        destination_connection: sqlite3.Connection | None = None
        try:
            source_connection = cls._readonly_connection(source)
            destination_connection = sqlite3.connect(str(destination), timeout=30)
            source_connection.backup(destination_connection)
            destination_connection.commit()
        finally:
            if destination_connection is not None:
                destination_connection.close()
            if source_connection is not None:
                source_connection.close()

    def integrity_check(self) -> bool:
        """Return whether the active file passes SQLite integrity validation."""

        if self._closed or not self.path.is_file():
            return False
        try:
            self._validate_database_file(self.path)
        except ValueError:
            return False
        return True

    def backup(self, destination: Path | str) -> Path:
        """Write and verify a consistent SQLite backup using SQLite's backup API."""

        self._ensure_open()
        self._validate_database_file(self.path)
        resolved = Path(destination).expanduser().resolve(strict=False)
        self._assert_not_assistant_path(resolved)
        if resolved == self.path:
            raise ValueError("Backup destination must differ from the active Hub database")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._temporary_path(resolved.parent, ".personal-hub-backup-")
        try:
            self._online_copy(self.path, temporary)
            self._validate_database_file(temporary)
            os.replace(temporary, resolved)
        finally:
            with suppress(FileNotFoundError):
                temporary.unlink()
        return resolved

    def restore(self, source: Path | str, *, destination: Path | str | None = None) -> Path:
        """Verify a source backup, then atomically restore it into the active DB."""

        self._ensure_open()
        source_path = Path(source).expanduser().resolve(strict=False)
        target = (
            self.path
            if destination is None
            else Path(destination).expanduser().resolve(strict=False)
        )
        self._assert_not_assistant_path(source_path)
        self._assert_not_assistant_path(target)
        if source_path == target:
            raise ValueError("Restore source must differ from the active Hub database")
        self._validate_database_file(source_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._temporary_path(target.parent, ".personal-hub-restore-")
        try:
            self._online_copy(source_path, temporary)
            self._validate_database_file(temporary)
            if target == self.path:
                # Dispose every pooled connection before replacing the file, and
                # recreate the engine regardless of whether replacement fails.
                self.engine.dispose()
                self._remove_sidecars(target)
                try:
                    os.replace(temporary, target)
                except BaseException:
                    self.engine = self._create_engine()
                    self.sessions = sessionmaker(self.engine, expire_on_commit=False)
                    raise
                self.engine = self._create_engine()
                self.sessions = sessionmaker(self.engine, expire_on_commit=False)
                # A valid older Hub backup may be restored into a newer
                # application.  Re-run idempotent migrations only after the
                # verified file has been atomically installed.
                self.migrate()
            else:
                os.replace(temporary, target)
        finally:
            with suppress(FileNotFoundError):
                temporary.unlink()
        return target

    restore_from_backup = restore
    restore_backup = restore

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.engine.dispose()

    def __enter__(self) -> HubDatabase:
        self._ensure_open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


__all__ = [
    "HubAuditEventRow",
    "HubBase",
    "HubCalendarImportRow",
    "HubCalendarEventStateRow",
    "HubDatabase",
    "HubInboxItemRow",
    "HubImportedCalendarEventRow",
    "HubNotificationDeliveryRow",
    "HubSchemaVersionRow",
    "HubSettingRow",
    "HubSourceConnectionRow",
    "HubSourceSyncStateRow",
    "HubTaskDeletionMarkerRow",
    "HubTaskListRow",
    "HubTaskOccurrenceRow",
    "HubTaskRow",
    "HubTaskTagRow",
    "HubFeedEntryRow",
    "HubFeedReadStateRow",
    "HubFeedSubscriptionRow",
    "HubWatchDefinitionRow",
    "HubWatchObservationRow",
    "HubWatchAlertRow",
    "HubMessageSourceRow",
    "HubMessageSummaryRow",
]
