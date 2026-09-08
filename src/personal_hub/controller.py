"""Typed, non-Qt application boundary for Personal Hub.

The controller is intentionally a small composition layer.  Desktop code can use it to
read and mutate local Hub state without knowing about SQLAlchemy, provider adapters, or
the shape of the process bootstrap.  Remote source data remains in memory only; the
controller persists source identity and safe operational health metadata, never snapshots
or adapter exception text.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

from personal_hub import __version__
from personal_hub.calendar.contracts import (
    CalendarQuery,
    CalendarResult,
    CalendarSource,
)
from personal_hub.calendar.ical import (
    IcalCalendarFeed,
    IcalFeedFetcher,
    IcalFeedManager,
)
from personal_hub.calendar.service import CalendarService
from personal_hub.calendar.today import project_calendar_today
from personal_hub.config import HubSettings
from personal_hub.contracts import (
    AuditEvent,
    Freshness,
    InboxItem,
    InboxState,
    SourceConnection,
    SourceRef,
    SourceSyncState,
)
from personal_hub.diagnostics import DiagnosticsAggregator, DiagnosticsReport
from personal_hub.feeds.contracts import (
    FeedEntryRecord,
    FeedResult,
    FeedSnapshot,
    FeedSubscription,
)
from personal_hub.feeds.fetcher import FeedFetcher
from personal_hub.feeds.parser import matches_filters
from personal_hub.feeds.repository import FeedEntryRepository, FeedSubscriptionRepository
from personal_hub.feeds.security import origin_for_url
from personal_hub.feeds.service import FeedRefreshService
from personal_hub.feeds.today import project_feed_today
from personal_hub.messages import (
    MessageBody,
    MessageQuery,
    MessageRepository,
    MessageResult,
    MessageService,
    MessageSource,
    MessageSourceConfig,
    MessageSummary,
)
from personal_hub.messages.categories import MessageCategory
from personal_hub.reminders import DurableReminderClaimStore, ReminderService
from personal_hub.sources import (
    SourceDefinition,
    SourceRegistry,
    SourceResult,
    SourceService,
    SourceState,
    safe_state_message,
)
from personal_hub.storage.backup import backup_database, restore_database
from personal_hub.storage.database import HubDatabase
from personal_hub.storage.repositories import (
    AuditRepository,
    InboxRepository,
    NotificationRepository,
    SettingsRepository,
    SourceRepository,
)
from personal_hub.tasks import HubTask, HubTaskList, TaskFilter, TaskRepository
from personal_hub.today import (
    DailyDigest,
    DailyDigestBuilder,
    TodayItem,
    TodayProjection,
    TodayProjector,
)
from personal_hub.watches import (
    AlertEvent,
    PageFetcher,
    WatchCondition,
    WatchDefinition,
    WatchObservation,
    WatchRepository,
    WatchResult,
    WatchService,
)


class HubContainerLike(Protocol):
    """The small part of the Hub container needed by the controller."""

    settings: HubSettings
    database: HubDatabase


class HubController:
    """Typed bridge between Hub UI composition and Hub-owned services.

    ``HubController(container)`` is the normal desktop construction form.  Explicit
    dependencies are also accepted so tests and headless tools can compose the same
    boundary without importing Qt.  All repositories are Hub-owned and all provider work
    is delegated to :class:`personal_hub.sources.SourceService`.
    """

    _FAILURE_STATES = frozenset(
        {
            SourceState.STALE,
            SourceState.PERMISSION_REQUIRED,
            SourceState.DISCONNECTED,
            SourceState.ERROR,
        }
    )
    _SUCCESS_STATES = frozenset({SourceState.READY, SourceState.EMPTY})

    def __init__(
        self,
        container: HubContainerLike | HubSettings | HubDatabase | None = None,
        *,
        settings: HubSettings | None = None,
        database: HubDatabase | None = None,
        source_service: SourceService | None = None,
        registry: SourceRegistry | None = None,
        settings_repository: SettingsRepository | None = None,
        source_repository: SourceRepository | None = None,
        inbox_repository: InboxRepository | None = None,
        audit_repository: AuditRepository | None = None,
        notification_repository: NotificationRepository | None = None,
        task_repository: TaskRepository | None = None,
        calendar_service: CalendarService | None = None,
        ical_feed_manager: IcalFeedManager | None = None,
        feed_subscription_repository: FeedSubscriptionRepository | None = None,
        feed_entry_repository: FeedEntryRepository | None = None,
        feed_service: FeedRefreshService | None = None,
        watch_repository: WatchRepository | None = None,
        watch_service: WatchService | None = None,
        message_repository: MessageRepository | None = None,
        message_service: MessageService | None = None,
        diagnostics: DiagnosticsAggregator | None = None,
        application_version: str = __version__,
    ) -> None:
        """Build a controller around a migrated Hub database and source service.

        A database supplied by a composition container is not owned by the controller.
        When no database is supplied, one is created from ``settings`` and closed by the
        asynchronous shutdown hook.  Construction performs only idempotent Hub migration;
        it never calls a source adapter.
        """

        container_settings, container_database = _container_values(container)
        initial_settings = settings or container_settings
        selected_database = database or container_database

        if selected_database is None:
            if initial_settings is None:
                initial_settings = HubSettings()
            selected_database = HubDatabase(initial_settings.database_path)
            owns_database = True
        else:
            owns_database = False
            if initial_settings is None:
                initial_settings = HubSettings(
                    data_dir=selected_database.path.parent,
                    database_name=selected_database.path.name,
                )

        if initial_settings is None:  # pragma: no cover - defensive type narrowing
            initial_settings = HubSettings()
        if not _same_path(selected_database.path, initial_settings.database_path):
            raise ValueError("Hub controller settings and database paths must match")

        selected_database.migrate()
        self.container = container
        self.database = selected_database
        self._owns_database = owns_database
        self.settings_repository = settings_repository or SettingsRepository(selected_database)
        self.source_repository = source_repository or SourceRepository(selected_database)
        self.inbox_repository = inbox_repository or InboxRepository(selected_database)
        self.audit_repository = audit_repository or AuditRepository(selected_database)
        self.notification_repository = notification_repository or NotificationRepository(
            selected_database
        )
        self.task_repository = task_repository or TaskRepository(selected_database)
        self.task_list_repository = self.task_repository.lists
        self._settings_defaults = initial_settings
        self._settings = _load_controller_settings(
            self.settings_repository,
            defaults=initial_settings,
            database_path=selected_database.path,
        )
        self.calendar_service = calendar_service or CalendarService(
            offline=self._settings.offline_mode
        )
        self.calendar_service.set_offline(self._settings.offline_mode)
        selected_ical_manager = ical_feed_manager or getattr(container, "ical_feed_manager", None)
        self.ical_feed_manager = selected_ical_manager or IcalFeedManager(
            self.settings_repository,
            self.calendar_service,
            fetcher=IcalFeedFetcher(
                allow_http=self._settings.allow_insecure_feed_http,
                offline=self._settings.offline_mode,
            ),
        )
        self.feed_subscription_repository = (
            feed_subscription_repository or FeedSubscriptionRepository(selected_database)
        )
        self.feed_entry_repository = feed_entry_repository or FeedEntryRepository(
            selected_database,
            max_entries=max(5_000, self._settings.feed_max_entries_per_subscription),
            max_entries_per_subscription=self._settings.feed_max_entries_per_subscription,
            retention=timedelta(days=self._settings.feed_retention_days),
        )
        self.feed_service = feed_service or FeedRefreshService(
            FeedFetcher(
                allow_http=self._settings.allow_insecure_feed_http,
                offline=self._settings.offline_mode,
            ),
            offline=self._settings.offline_mode,
        )
        self.feed_service.set_offline(self._settings.offline_mode)
        self._restore_feed_service()
        self.watch_repository = watch_repository or WatchRepository(
            selected_database,
            observation_retention=timedelta(days=self._settings.watch_observation_retention_days),
            max_observations_per_watch=self._settings.watch_max_observations_per_watch,
        )
        self.watch_service = watch_service or WatchService(
            self.watch_repository,
            PageFetcher(
                allow_http=self._settings.allow_insecure_watch_http,
                offline=self._settings.offline_mode,
            ),
            offline=self._settings.offline_mode,
        )
        self.watch_service.set_offline(self._settings.offline_mode)
        self.message_repository = message_repository or MessageRepository(
            selected_database,
            max_summaries_per_source=self._settings.message_max_summaries_per_source,
        )
        self.message_service = message_service or MessageService(
            self.message_repository,
            offline=self._settings.offline_mode,
        )
        self.message_service.set_offline(self._settings.offline_mode)

        if source_service is None:
            source_service = cast(
                SourceService | None,
                getattr(container, "source_service", None) or getattr(container, "sources", None),
            )
        service_registry = getattr(source_service, "registry", None)
        selected_registry = (
            registry
            or service_registry
            or cast(SourceRegistry | None, getattr(container, "registry", None))
            or SourceRegistry()
        )
        if source_service is None:
            source_service = SourceService(
                selected_registry,
                offline_getter=lambda: self._settings.offline_mode,
            )
        elif (
            registry is not None
            and service_registry is not None
            and registry is not service_registry
        ):
            raise ValueError("source_service and registry must refer to the same registry")
        self.source_service = source_service
        self.registry = cast(SourceRegistry, getattr(source_service, "registry", selected_registry))
        _set_service_offline(source_service, self._settings.offline_mode)

        self._diagnostics = diagnostics or DiagnosticsAggregator(
            application_version=application_version,
        )
        self.today_projector = TodayProjector(
            self.task_repository,
            timezone=self._settings.default_timezone,
        )
        self.digest_builder = DailyDigestBuilder(projector=self.today_projector)
        self.reminder_service = ReminderService(
            DurableReminderClaimStore(self.notification_repository),
            timezone=self._settings.default_timezone,
        )
        self._refresh_tasks: set[asyncio.Task[Any]] = set()
        self._closed = False

    @property
    def settings(self) -> HubSettings:
        """Return the current validated Hub settings snapshot."""

        return self._settings

    @property
    def offline_mode(self) -> bool:
        return self._settings.offline_mode

    @property
    def offline(self) -> bool:
        return self._settings.offline_mode

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def closed(self) -> bool:
        return self._closed

    def get_settings(self) -> HubSettings:
        return self._settings

    current_settings = get_settings

    def update_settings(
        self,
        settings: HubSettings | Mapping[str, Any] | None = None,
        **changes: Any,
    ) -> HubSettings:
        """Validate and persist a complete settings snapshot.

        Storage location is fixed after the controller opens its database.  This prevents
        a UI settings edit from silently directing subsequent writes to another database.
        """

        self._ensure_open()
        if settings is None:
            values: dict[str, Any] = self._settings.model_dump()
        elif isinstance(settings, HubSettings):
            values = settings.model_dump()
        elif isinstance(settings, Mapping):
            values = self._settings.model_dump()
            values.update(settings)
        else:
            raise TypeError("settings must be HubSettings, a mapping, or None")
        values.update(changes)
        updated = HubSettings.model_validate(values)
        if not _same_path(updated.database_path, self.database.path):
            raise ValueError("Hub controller settings cannot change the active database path")
        self.settings_repository.save_settings(updated)
        self._settings = updated
        _set_service_offline(self.source_service, updated.offline_mode)
        _set_service_offline(self.calendar_service, updated.offline_mode)
        self.ical_feed_manager.set_offline(updated.offline_mode)
        self.ical_feed_manager.set_allow_http(updated.allow_insecure_feed_http)
        _set_service_offline(self.feed_service, updated.offline_mode)
        _set_service_offline(self.watch_service, updated.offline_mode)
        _set_service_offline(self.message_service, updated.offline_mode)
        if isinstance(self.feed_service.fetcher, FeedFetcher):
            self.feed_service.fetcher.allow_http = updated.allow_insecure_feed_http
        self.feed_entry_repository.retention = timedelta(days=updated.feed_retention_days)
        self.feed_entry_repository.max_entries_per_subscription = (
            updated.feed_max_entries_per_subscription
        )
        self.feed_entry_repository.max_entries = max(
            5_000,
            updated.feed_max_entries_per_subscription,
        )
        self.watch_repository.observation_retention = timedelta(
            days=updated.watch_observation_retention_days
        )
        self.watch_repository.max_observations_per_watch = updated.watch_max_observations_per_watch
        if isinstance(self.watch_service.fetcher, PageFetcher):
            self.watch_service.fetcher.allow_http = updated.allow_insecure_watch_http
        self.message_repository.max_summaries_per_source = updated.message_max_summaries_per_source
        if updated.default_timezone != self.today_projector.timezone.key:
            self.today_projector = TodayProjector(
                self.task_repository,
                timezone=updated.default_timezone,
            )
            self.digest_builder = DailyDigestBuilder(projector=self.today_projector)
            self.reminder_service = ReminderService(
                DurableReminderClaimStore(self.notification_repository),
                timezone=updated.default_timezone,
            )
        return updated

    save_settings = update_settings

    def set_offline_mode(self, enabled: bool) -> HubSettings:
        """Persist offline mode and gate all subsequent source adapter calls."""

        return self.update_settings(offline_mode=bool(enabled))

    set_offline = set_offline_mode
    set_offline_lock = set_offline_mode
    toggle_offline_mode = set_offline_mode

    # ------------------------------------------------------------------
    # Source and Inbox projections used by desktop composition.

    def list_sources(self, *, enabled_only: bool = False) -> tuple[SourceDefinition, ...]:
        entries = sorted(self.registry.all(), key=lambda entry: entry.source_id)
        for entry in entries:
            persisted = self.source_repository.get(entry.source_id)
            if persisted is not None and persisted.enabled != entry.definition.enabled:
                self.registry.set_enabled(entry.source_id, persisted.enabled)
        entries = sorted(self.registry.all(), key=lambda entry: entry.source_id)
        if enabled_only:
            entries = [entry for entry in entries if entry.definition.enabled]
        return tuple(entry.definition for entry in entries)

    source_definitions = list_sources
    source_list = list_sources
    sources = list_sources

    def list_source_connections(self, *, enabled_only: bool = False) -> list[SourceConnection]:
        return self.source_repository.list(enabled_only=enabled_only)

    source_connections = list_source_connections

    def set_source_enabled(self, source_id: str, enabled: bool) -> SourceDefinition:
        self._ensure_open()
        entry = self.registry.set_enabled(source_id, enabled)
        connection = self.source_repository.get(source_id)
        timestamp = datetime.now(UTC)
        if connection is None:
            connection = SourceConnection(
                connection_id=source_id,
                provider_id=entry.provider_id,
                account_label=entry.definition.display_name,
                enabled=enabled,
                created_at=timestamp,
                updated_at=timestamp,
            )
        else:
            connection = connection.model_copy(update={"enabled": enabled, "updated_at": timestamp})
        self.source_repository.save(connection)
        return entry.definition

    def source_result(self, source_id: str) -> SourceResult | None:
        result = self.source_service.last_result(source_id)
        if result is not None:
            return result
        state = self.source_repository.get_sync_state(source_id)
        if state is None:
            return None
        return self._result_from_persisted_state(source_id, state)

    source_status = source_result
    get_source_status = source_result

    def list_source_results(self) -> tuple[SourceResult, ...]:
        source_ids = set(self.registry.source_ids)
        source_ids.update(
            state.connection_id for state in self.source_repository.list_sync_states()
        )
        values = [
            result
            for source_id in sorted(source_ids)
            if (result := self.source_result(source_id)) is not None
        ]
        return tuple(values)

    source_results = list_source_results
    source_statuses = list_source_results

    def source_sync_state(self, source_id: str) -> SourceSyncState | None:
        return self.source_repository.get_sync_state(source_id)

    source_health = source_sync_state

    def list_source_sync_states(self) -> list[SourceSyncState]:
        return self.source_repository.list_sync_states()

    source_sync_states = list_source_sync_states

    def list_inbox(
        self,
        *,
        state: InboxState | None = None,
        limit: int = 100,
    ) -> list[InboxItem]:
        return self.inbox_repository.list(state=state, limit=limit)

    inbox_items = list_inbox
    list_inbox_items = list_inbox
    get_inbox = list_inbox
    inbox = list_inbox

    def unread_inbox_count(self) -> int:
        return self.inbox_repository.unread_count()

    unread_count = unread_inbox_count

    def set_inbox_state(
        self,
        item_id: str,
        state: InboxState,
        *,
        at: datetime | None = None,
    ) -> InboxItem | None:
        return self.inbox_repository.set_state(item_id, state, at=at)

    def mark_inbox_read(self, item_id: str, *, at: datetime | None = None) -> InboxItem | None:
        return self.inbox_repository.mark_read(item_id, at=at)

    def mark_inbox_unread(self, item_id: str, *, at: datetime | None = None) -> InboxItem | None:
        return self.inbox_repository.mark_unread(item_id, at=at)

    def dismiss_inbox(self, item_id: str, *, at: datetime | None = None) -> InboxItem | None:
        return self.inbox_repository.dismiss(item_id, at=at)

    def archive_inbox(self, item_id: str, *, at: datetime | None = None) -> InboxItem | None:
        return self.inbox_repository.archive(item_id, at=at)

    read_inbox = mark_inbox_read
    unread_inbox = mark_inbox_unread
    dismiss = dismiss_inbox
    archive = archive_inbox

    def run_retention_cleanup(self, *, now: datetime | None = None) -> dict[str, int]:
        """Apply every Hub-owned retention policy without performing network work."""

        self._ensure_open()
        timestamp = now or datetime.now(UTC)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("retention cleanup timestamp must be timezone-aware")
        timestamp = timestamp.astimezone(UTC)
        removed = {
            "inbox": self.inbox_repository.cleanup(
                timestamp - timedelta(days=self._settings.inbox_retention_days),
                states=("archived", "dismissed"),
            ),
            "audit": self.audit_repository.cleanup(
                timestamp - timedelta(days=self._settings.audit_retention_days)
            ),
            "feeds": self.feed_entry_repository.cleanup(
                timestamp - timedelta(days=self._settings.feed_retention_days)
            ),
            "watches": self.watch_repository.cleanup(now=timestamp),
            "messages": self.message_repository.cleanup(now=timestamp),
            "notifications": self.notification_repository.cleanup(now=timestamp),
        }
        self.audit_repository.add(
            AuditEvent(
                event_type="maintenance.retention",
                source_type="hub",
                source_id=None,
                outcome="succeeded",
                occurred_at=timestamp,
                metadata={
                    "records_removed": sum(removed.values()),
                    "category_count": len(removed),
                },
            )
        )
        return removed

    cleanup_retained_data = run_retention_cleanup

    # ------------------------------------------------------------------
    # Local tasks, Today, reminders, and deterministic digests.

    def list_tasks(
        self,
        filters: TaskFilter | None = None,
        **options: Any,
    ) -> list[HubTask]:
        self._ensure_open()
        return self.task_repository.list(filters, **options)

    tasks = list_tasks

    def get_task(self, task_id: str) -> HubTask | None:
        self._ensure_open()
        return self.task_repository.get(task_id)

    def create_task(self, task: HubTask | None = None, **values: object) -> HubTask:
        self._ensure_open()
        created = self.task_repository.create(task, **values)
        self._audit_task_change("task.created", created)
        return created

    add_task = create_task

    def update_task(self, task_id: str, **changes: object) -> HubTask | None:
        self._ensure_open()
        updated = self.task_repository.update(task_id, **changes)
        if updated is not None:
            self._audit_task_change("task.updated", updated)
        return updated

    edit_task = update_task

    def complete_task(
        self,
        task_id: str,
        *,
        completed_at: datetime | None = None,
    ) -> tuple[HubTask | None, HubTask | None]:
        self._ensure_open()
        completed, following = self.task_repository.complete_with_next(
            task_id,
            completed_at=completed_at,
        )
        if completed is not None:
            self._audit_task_change("task.completed", completed)
        return completed, following

    def undo_task(self, task_id: str, *, at: datetime | None = None) -> HubTask | None:
        self._ensure_open()
        current = self.task_repository.get(task_id)
        if current is None:
            return None
        restored = (
            self.task_repository.unarchive(task_id, at=at)
            if current.state == "archived"
            else self.task_repository.undo(task_id, at=at)
        )
        if restored is not None:
            self._audit_task_change("task.restored", restored)
        return restored

    undo_task_action = undo_task

    def archive_task(self, task_id: str, *, at: datetime | None = None) -> HubTask | None:
        self._ensure_open()
        archived = self.task_repository.archive(task_id, at=at)
        if archived is not None:
            self._audit_task_change("task.archived", archived)
        return archived

    def list_task_lists(self, *, limit: int = 1_000) -> list[HubTaskList]:
        self._ensure_open()
        return self.task_list_repository.list(limit=limit)

    def create_task_list(self, name: str) -> HubTaskList:
        self._ensure_open()
        return self.task_list_repository.create(name)

    def today_projection(
        self,
        *,
        now: datetime | None = None,
        limit: int | None = None,
    ) -> TodayProjection:
        self._ensure_open()
        return self.today_projector.project(now=now, limit=limit)

    today = today_projection

    def daily_digest(self, *, now: datetime | None = None) -> DailyDigest:
        self._ensure_open()
        projection = self.today_projection(now=now)
        return self.digest_builder.build(projection)

    async def reconcile_task_reminders(
        self,
        *,
        now: datetime | None = None,
        limit: int = 1_000,
    ) -> tuple[InboxItem, ...]:
        """Claim due reminder identities and persist their safe Inbox projections."""

        self._ensure_open()
        tasks = self.task_repository.list(state="open", limit=10_000)
        items = await self.reminder_service.emit_due(tasks, now=now, limit=limit)
        return tuple(self.inbox_repository.add(item) for item in items)

    # ------------------------------------------------------------------
    # Read-only calendar sources and Today event projection.

    def register_calendar_source(
        self,
        source: CalendarSource,
        *,
        source_id: str | None = None,
    ) -> CalendarSource:
        self._ensure_open()
        return self.calendar_service.register(source, source_id=source_id)

    def calendar_results(self) -> tuple[CalendarResult, ...]:
        return self.calendar_service.results()

    async def refresh_calendars(
        self,
        query: CalendarQuery,
        source_ids: Iterable[str] | None = None,
        *,
        queries_by_source: Mapping[str, CalendarQuery] | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, CalendarResult]:
        self._ensure_open()
        task = asyncio.current_task()
        if task is not None:
            self._refresh_tasks.add(task)
        try:
            if queries_by_source is not None:
                selected = tuple(
                    self.calendar_service.source_ids if source_ids is None else source_ids
                )
                unknown = set(queries_by_source).difference(selected)
                if unknown:
                    raise ValueError("calendar query mapping contains an unselected source")
                values = await asyncio.gather(
                    *(
                        self.calendar_service.refresh(
                            source_id,
                            queries_by_source.get(source_id, query),
                            timeout_seconds=timeout_seconds,
                        )
                        for source_id in selected
                    )
                )
                results = dict(zip(selected, values, strict=True))
                return results
            results = await self.calendar_service.refresh_all(
                query,
                source_ids,
                timeout_seconds=timeout_seconds,
            )
            return results
        finally:
            if task is not None:
                self._refresh_tasks.discard(task)

    def calendar_today_items(
        self,
        *,
        day: date | None = None,
        timezone: str | None = None,
        selected_calendar_keys: Iterable[str] | None = None,
        limit: int = 200,
    ) -> tuple[TodayItem, ...]:
        selected = None if selected_calendar_keys is None else set(selected_calendar_keys)
        events_by_identity = {
            (result.source_id, event.calendar_id, event.event_id): event
            for result in self.calendar_service.results()
            if result.snapshot is not None
            for event in result.snapshot.events
            if selected is None
            or _calendar_selection_matches(
                result.source_id,
                event.calendar_id,
                selected,
            )
        }
        display_timezone = timezone or self._settings.default_timezone
        zone = ZoneInfo(display_timezone)
        selected_day = day or datetime.now(zone).date()
        return project_calendar_today(
            events_by_identity.values(),
            day=selected_day,
            timezone=display_timezone,
            limit=limit,
        )

    def list_ical_feeds(
        self,
        *,
        enabled: bool | None = None,
    ) -> tuple[IcalCalendarFeed, ...]:
        self._ensure_open()
        return self.ical_feed_manager.list(enabled=enabled)

    list_ical_calendar_feeds = list_ical_feeds

    def create_ical_feed(
        self,
        *,
        title: str,
        feed_url: str,
        feed_id: UUID | None = None,
        enabled: bool = True,
        refresh_minutes: int | None = None,
        allow_http: bool = False,
    ) -> IcalCalendarFeed:
        self._ensure_open()
        return self.ical_feed_manager.create(
            title=title,
            feed_url=feed_url,
            feed_id=feed_id,
            enabled=enabled,
            refresh_minutes=(
                self._settings.feed_refresh_minutes if refresh_minutes is None else refresh_minutes
            ),
            allow_http=allow_http,
        )

    import_ical_feed = create_ical_feed
    create_ical_calendar_feed = create_ical_feed

    def update_ical_feed(
        self,
        feed_id: UUID | str,
        **changes: object,
    ) -> IcalCalendarFeed | None:
        self._ensure_open()
        return self.ical_feed_manager.update(feed_id, **changes)

    update_ical_calendar_feed = update_ical_feed

    def delete_ical_feed(self, feed_id: UUID | str) -> bool:
        self._ensure_open()
        return self.ical_feed_manager.delete(feed_id)

    delete_ical_calendar_feed = delete_ical_feed

    def set_ical_feed_enabled(
        self,
        feed_id: UUID | str,
        enabled: bool,
    ) -> IcalCalendarFeed | None:
        self._ensure_open()
        return self.ical_feed_manager.set_enabled(feed_id, enabled)

    set_ical_calendar_feed_enabled = set_ical_feed_enabled

    # ------------------------------------------------------------------
    # Secure RSS/Atom subscriptions, durable News state, and digest grouping.

    def _restore_feed_service(self, *, reset: bool = False) -> None:
        if reset:
            for subscription_id in tuple(self.feed_service.source_ids):
                self.feed_service.unregister(subscription_id)
        for subscription in self.feed_subscription_repository.list(limit=10_000):
            self.feed_service.upsert(subscription)
            sync_state = self.feed_subscription_repository.sync_state(subscription.subscription_id)
            if sync_state is None:
                continue
            entries = tuple(
                self.feed_entry_repository.list(
                    subscription_id=subscription.subscription_id,
                    limit=1_000,
                )
            )
            snapshot: FeedSnapshot | None = None
            if sync_state.last_success_at is not None or entries:
                collected_at = sync_state.last_success_at or max(
                    entry.first_seen_at for entry in entries
                )
                snapshot = FeedSnapshot(
                    subscription_id=subscription.subscription_id,
                    feed_url=subscription.feed_url,
                    title=subscription.title,
                    site_url=subscription.site_url,
                    entries=entries,
                    collected_at=collected_at,
                    etag=sync_state.etag,
                    last_modified=sync_state.last_modified,
                )
            self.feed_service.restore_state(sync_state, snapshot)

    def list_feed_subscriptions(
        self,
        *,
        enabled: bool | None = None,
        limit: int = 1_000,
    ) -> tuple[FeedSubscription, ...]:
        self._ensure_open()
        return tuple(self.feed_subscription_repository.list(enabled=enabled, limit=limit))

    def create_feed_subscription(
        self,
        *,
        title: str,
        feed_url: str,
        subscription_id: UUID | None = None,
        site_url: str | None = None,
        enabled: bool = True,
        refresh_minutes: int | None = None,
        include_terms: Iterable[str] = (),
        exclude_terms: Iterable[str] = (),
        digest_only: bool = False,
        allow_http: bool = False,
    ) -> FeedSubscription:
        self._ensure_open()
        normalized_url = feed_url.strip()
        reviewed_origin = (
            origin_for_url(normalized_url)
            if urlsplit(normalized_url).scheme.casefold() == "http" and allow_http
            else None
        )
        subscription = self.feed_subscription_repository.create(
            " ".join(title.split()),
            normalized_url,
            subscription_id=subscription_id,
            site_url=site_url,
            enabled=enabled,
            refresh_minutes=(
                self._settings.feed_refresh_minutes if refresh_minutes is None else refresh_minutes
            ),
            include_terms=tuple(include_terms),
            exclude_terms=tuple(exclude_terms),
            digest_only=digest_only,
            allow_http=allow_http,
            reviewed_origin=reviewed_origin,
        )
        self.feed_service.upsert(subscription)
        return subscription

    def update_feed_subscription(
        self,
        subscription_id: UUID | str,
        **changes: object,
    ) -> FeedSubscription | None:
        self._ensure_open()
        current = self.feed_subscription_repository.get(subscription_id)
        if current is None:
            return None
        values = current.model_dump(mode="python")
        values.update(changes)
        values["subscription_id"] = current.subscription_id
        if "include_terms" in values:
            values["include_terms"] = tuple(cast(Iterable[str], values["include_terms"]))
        if "exclude_terms" in values:
            values["exclude_terms"] = tuple(cast(Iterable[str], values["exclude_terms"]))
        feed_url = str(values["feed_url"]).strip()
        allow_http = bool(values.get("allow_http", False))
        values["reviewed_origin"] = (
            origin_for_url(feed_url)
            if urlsplit(feed_url).scheme.casefold() == "http" and allow_http
            else None
        )
        updated = FeedSubscription.model_validate(values)
        origin_changed = (
            str(current.feed_url) != str(updated.feed_url)
            or current.allow_http != updated.allow_http
        )
        if origin_changed:
            with suppress(KeyError):
                self.feed_service.unregister(current.subscription_id)
            self.feed_subscription_repository.delete(current.subscription_id)
            self.feed_subscription_repository.add(updated)
        else:
            self.feed_subscription_repository.add(updated)
        self.feed_service.upsert(updated)
        return updated

    def delete_feed_subscription(self, subscription_id: UUID | str) -> bool:
        self._ensure_open()
        deleted = self.feed_subscription_repository.delete(subscription_id)
        if deleted:
            with suppress(KeyError):
                self.feed_service.unregister(subscription_id)
        return deleted

    def set_feed_subscription_enabled(
        self,
        subscription_id: UUID | str,
        enabled: bool,
    ) -> FeedSubscription | None:
        self._ensure_open()
        updated = self.feed_subscription_repository.set_enabled(subscription_id, enabled)
        if updated is not None:
            self.feed_service.upsert(updated)
        return updated

    def list_feed_entries(
        self,
        *,
        subscription_id: UUID | str | None = None,
        state: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[FeedEntryRecord, ...]:
        self._ensure_open()
        if state not in {None, "read", "unread"}:
            raise ValueError("feed read state must be read or unread")
        records = self.feed_entry_repository.list_records(
            subscription_id=subscription_id,
            state=cast(Any, state),
            limit=limit,
            offset=offset,
        )
        subscriptions = {
            item.subscription_id: item
            for item in self.feed_subscription_repository.list(limit=10_000)
        }
        return tuple(
            record
            for record in records
            if (subscription := subscriptions.get(record.entry.subscription_id)) is not None
            and matches_filters(record.entry, subscription)
        )

    def set_feed_entry_read(
        self,
        entry_id: str,
        read: bool,
        *,
        at: datetime | None = None,
    ) -> FeedEntryRecord | None:
        self._ensure_open()
        try:
            if read:
                self.feed_entry_repository.mark_read(entry_id, at=at)
            else:
                self.feed_entry_repository.mark_unread(entry_id, at=at)
        except KeyError:
            return None
        return self.feed_entry_repository.get_record(entry_id)

    def feed_results(self) -> tuple[FeedResult, ...]:
        return self.feed_service.results()

    async def refresh_feeds(
        self,
        subscription_ids: Iterable[UUID | str] | None = None,
        *,
        force: bool = False,
        now: datetime | None = None,
    ) -> dict[UUID, FeedResult]:
        self._ensure_open()
        task = asyncio.current_task()
        if task is not None:
            self._refresh_tasks.add(task)
        try:
            results = await self.feed_service.refresh_all(
                subscription_ids,
                force=force,
                now=now,
            )
            for subscription_id, result in results.items():
                sync_state = self.feed_service.sync_state(subscription_id)
                if sync_state is not None:
                    self.feed_subscription_repository.save_sync_state(sync_state)
                if result.snapshot is not None and result.state in {"ready", "empty"}:
                    self.feed_entry_repository.upsert_many(
                        result.snapshot.entries,
                        seen_at=result.checked_at,
                    )
                self._diagnostics.record_source(_feed_source_result(result))
            return results
        finally:
            if task is not None:
                self._refresh_tasks.discard(task)

    def news_today_items(
        self,
        *,
        selected_subscription_ids: Iterable[UUID | str] | None = None,
        include_read: bool = False,
        limit: int = 100,
    ) -> tuple[TodayItem, ...]:
        selected = (
            None
            if selected_subscription_ids is None
            else {UUID(str(value)) for value in selected_subscription_ids}
        )
        subscriptions = {
            subscription.subscription_id: subscription
            for subscription in self.feed_subscription_repository.list(enabled=True, limit=10_000)
            if selected is None or subscription.subscription_id in selected
        }
        records = self.feed_entry_repository.list_records(
            state=None if include_read else "unread",
            limit=min(max(limit * 4, limit), 10_000),
        )
        filtered = tuple(
            record
            for record in records
            if (subscription := subscriptions.get(record.entry.subscription_id)) is not None
            and matches_filters(record.entry, subscription)
        )
        return project_feed_today(
            filtered,
            subscriptions,
            include_read=include_read,
            limit=limit,
        )

    def reconcile_news_digest(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> InboxItem | None:
        requested = now or datetime.now(UTC)
        if requested.tzinfo is None or requested.utcoffset() is None:
            raise ValueError("news digest timestamps must be timezone-aware")
        timestamp = requested.astimezone(UTC)
        local_date = timestamp.astimezone(ZoneInfo(self._settings.default_timezone)).date()
        items = self.news_today_items(limit=limit)
        if not items:
            return None
        deduplication_key = (
            f"news-digest:{self._settings.default_timezone}:{local_date.isoformat()}"
        )
        if not self.notification_repository.claim_key(
            deduplication_key,
            "digest",
            now=timestamp,
            safe_message="Daily news digest was claimed.",
        ):
            return None
        source_count = len({item.source.connection_id for item in items})
        inbox_item = InboxItem(
            item_id=f"news.digest.{local_date.strftime('%Y%m%d')}",
            kind="digest",
            title="News digest",
            safe_summary=(f"{len(items)} unread feed item(s) from {source_count} source(s)."),
            reason="Scheduled news digest",
            source=SourceRef(provider_id="rss", display_name="News feeds"),
            created_at=timestamp,
            severity="info",
            navigation_target="hub://watchlists/news",
            deduplication_key=deduplication_key,
            metadata={"item_count": len(items), "source_count": source_count},
        )
        return self.inbox_repository.add(inbox_item)

    # ------------------------------------------------------------------
    # Typed webpage watches and transactional alerts.

    def list_watches(
        self,
        *,
        enabled: bool | None = None,
        limit: int = 1_000,
    ) -> tuple[WatchDefinition, ...]:
        self._ensure_open()
        return tuple(self.watch_repository.list(enabled=enabled, limit=limit))

    def create_watch(
        self,
        *,
        name: str,
        kind: str,
        source_url: str,
        condition: WatchCondition | Mapping[str, object],
        watch_id: UUID | None = None,
        refresh_minutes: int | None = None,
        enabled: bool = True,
        notification_policy: str = "immediate",
        cooldown_minutes: int = 60,
        allow_http: bool = False,
        approved_redirect_origins: Iterable[str] = (),
        now: datetime | None = None,
    ) -> WatchDefinition:
        self._ensure_open()
        timestamp = now or datetime.now(UTC)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("watch timestamps must be timezone-aware")
        normalized_url = source_url.strip()
        watch = WatchDefinition(
            watch_id=watch_id or UUID(bytes=os.urandom(16), version=4),
            name=" ".join(name.split()),
            kind=kind,
            source_url=normalized_url,
            condition=(
                condition
                if isinstance(condition, WatchCondition)
                else WatchCondition.model_validate(condition)
            ),
            refresh_minutes=(
                self._settings.watch_refresh_minutes if refresh_minutes is None else refresh_minutes
            ),
            enabled=enabled,
            notification_policy=notification_policy,
            cooldown_minutes=cooldown_minutes,
            allow_http=allow_http,
            reviewed_origin=origin_for_url(normalized_url),
            approved_redirect_origins=tuple(approved_redirect_origins),
            created_at=timestamp,
            updated_at=timestamp,
        )
        return self.watch_repository.add(watch)

    def update_watch(
        self,
        watch_id: UUID | str,
        *,
        now: datetime | None = None,
        **changes: object,
    ) -> WatchDefinition | None:
        self._ensure_open()
        current = self.watch_repository.get(watch_id)
        if current is None:
            return None
        timestamp = now or datetime.now(UTC)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("watch timestamps must be timezone-aware")
        values = current.model_dump(mode="python")
        values.update(changes)
        values["watch_id"] = current.watch_id
        values["created_at"] = current.created_at
        values["updated_at"] = timestamp
        values["source_url"] = str(values["source_url"]).strip()
        values["reviewed_origin"] = origin_for_url(str(values["source_url"]))
        if "approved_redirect_origins" in values:
            values["approved_redirect_origins"] = tuple(
                cast(Iterable[str], values["approved_redirect_origins"])
            )
        updated = WatchDefinition.model_validate(values)
        reset_fields = {"kind", "source_url", "condition"}
        reset = any(
            current.model_dump(mode="json")[key] != updated.model_dump(mode="json")[key]
            for key in reset_fields
        )
        self.watch_repository.add(updated)
        if reset:
            self.watch_repository.reset_baseline(updated.watch_id)
        return updated

    def delete_watch(self, watch_id: UUID | str) -> bool:
        self._ensure_open()
        return self.watch_repository.delete(watch_id)

    def set_watch_enabled(
        self,
        watch_id: UUID | str,
        enabled: bool,
        *,
        now: datetime | None = None,
    ) -> WatchDefinition | None:
        if type(enabled) is not bool:
            raise TypeError("watch enabled state must be a bool")
        return self.update_watch(watch_id, enabled=enabled, now=now)

    def watch_observations(
        self,
        watch_id: UUID | str,
        *,
        limit: int = 100,
    ) -> tuple[WatchObservation, ...]:
        self._ensure_open()
        return tuple(self.watch_repository.list_observations(watch_id, limit=limit))

    def watch_alerts(
        self,
        *,
        watch_id: UUID | str | None = None,
        limit: int = 100,
    ) -> tuple[AlertEvent, ...]:
        self._ensure_open()
        return tuple(self.watch_repository.list_alerts(watch_id=watch_id, limit=limit))

    async def test_watch_now(
        self,
        watch: WatchDefinition | UUID | str,
        *,
        now: datetime | None = None,
    ) -> WatchObservation:
        self._ensure_open()
        selected = watch if isinstance(watch, WatchDefinition) else self.watch_repository.get(watch)
        if selected is None:
            raise KeyError("watch does not exist")
        return await self.watch_service.test_now(selected, now=now)

    async def test_watch_draft(
        self,
        *,
        name: str,
        kind: str,
        source_url: str,
        condition: WatchCondition | Mapping[str, object],
        refresh_minutes: int,
        enabled: bool = True,
        notification_policy: str = "immediate",
        cooldown_minutes: int = 60,
        allow_http: bool = False,
        approved_redirect_origins: Iterable[str] = (),
        now: datetime | None = None,
    ) -> WatchObservation:
        """Validate and test an unsaved authoring draft without durable side effects."""

        timestamp = now or datetime.now(UTC)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("watch timestamps must be timezone-aware")
        normalized_url = source_url.strip()
        draft = WatchDefinition(
            name=" ".join(name.split()),
            kind=kind,
            source_url=normalized_url,
            condition=(
                condition
                if isinstance(condition, WatchCondition)
                else WatchCondition.model_validate(condition)
            ),
            refresh_minutes=refresh_minutes,
            enabled=enabled,
            notification_policy=notification_policy,
            cooldown_minutes=cooldown_minutes,
            allow_http=allow_http,
            reviewed_origin=origin_for_url(normalized_url),
            approved_redirect_origins=tuple(approved_redirect_origins),
            created_at=timestamp,
            updated_at=timestamp,
        )
        return await self.watch_service.test_now(draft, now=timestamp)

    async def refresh_watches(
        self,
        watch_ids: Iterable[UUID | str] | None = None,
        *,
        force: bool = False,
        now: datetime | None = None,
    ) -> dict[UUID, WatchResult]:
        self._ensure_open()
        timestamp = now or datetime.now(UTC)
        task = asyncio.current_task()
        if task is not None:
            self._refresh_tasks.add(task)
        try:
            if watch_ids is None and not force:
                results = await self.watch_service.refresh_due(now=timestamp)
            else:
                selected = (
                    tuple(watch.watch_id for watch in self.list_watches(enabled=True, limit=10_000))
                    if watch_ids is None
                    else tuple(UUID(str(value)) for value in watch_ids)
                )
                values = await asyncio.gather(
                    *(
                        self.watch_service.refresh(
                            identifier,
                            force=force,
                            now=timestamp,
                        )
                        for identifier in selected
                    )
                )
                results = dict(zip(selected, values, strict=True))
            for result in results.values():
                definition = self.watch_repository.get(result.watch_id)
                if (
                    result.alert is not None
                    and definition is not None
                    and self._settings.notifications_enabled
                    and definition.notification_policy == "immediate"
                ):
                    self.notification_repository.claim_key(
                        result.alert.deduplication_key,
                        "windows",
                        now=result.checked_at,
                        safe_message="A watch alert is ready.",
                    )
                self.audit_repository.add(
                    AuditEvent(
                        event_type="watch.refresh",
                        source_type="web_watch",
                        source_id=str(result.watch_id),
                        outcome=("succeeded" if result.state == "ready" else "failed"),
                        occurred_at=result.checked_at,
                        metadata={
                            "state": result.state,
                            "alerted": result.alert is not None,
                        },
                    )
                )
            return results
        finally:
            if task is not None:
                self._refresh_tasks.discard(task)

    def watch_today_items(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> tuple[TodayItem, ...]:
        timestamp = now or datetime.now(UTC)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("watch Today timestamp must be timezone-aware")
        local_date = timestamp.astimezone(ZoneInfo(self._settings.default_timezone)).date()
        watches = {watch.watch_id: watch for watch in self.list_watches(limit=10_000)}
        items: list[TodayItem] = []
        for alert in self.watch_repository.list_alerts(limit=min(limit * 4, 1_000)):
            if (
                alert.observed_at.astimezone(ZoneInfo(self._settings.default_timezone)).date()
                != local_date
            ):
                continue
            watch = watches.get(alert.watch_id)
            if watch is None:
                continue
            items.append(
                TodayItem(
                    item_id=f"watch.alert.{alert.alert_id}",
                    kind="alert",
                    title=alert.title,
                    summary=alert.safe_summary,
                    starts_at=alert.observed_at,
                    severity="attention",
                    source=SourceRef(
                        provider_id="web-watch",
                        connection_id=str(alert.watch_id),
                        display_name=watch.name,
                    ),
                    navigation_target=f"hub://watchlists/{alert.watch_id}",
                    sensitive=True,
                )
            )
            if len(items) >= limit:
                break
        return tuple(items)

    # ------------------------------------------------------------------
    # Read-only attention-message summaries and explicit memory-only bodies.

    def register_message_adapter(self, source: MessageSource) -> None:
        self._ensure_open()
        config = self.message_repository.get_source(source.source_id)
        if config is not None and source.provider_id != config.provider:
            raise ValueError("message adapter provider does not match its source configuration")
        self.message_service.register(source)

    def create_message_source(
        self,
        *,
        source_id: str,
        provider: str,
        account_id: str,
        query: MessageQuery | Mapping[str, object],
        refresh_minutes: int | None = None,
        retention_days: int | None = None,
        enabled: bool = True,
        now: datetime | None = None,
    ) -> MessageSourceConfig:
        self._ensure_open()
        timestamp = now or datetime.now(UTC)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("message source timestamps must be timezone-aware")
        config = MessageSourceConfig(
            source_id=source_id,
            provider=provider,
            account_id=account_id,
            query=(
                query if isinstance(query, MessageQuery) else MessageQuery.model_validate(query)
            ),
            refresh_minutes=(
                self._settings.message_refresh_minutes
                if refresh_minutes is None
                else refresh_minutes
            ),
            retention_days=(
                self._settings.message_summary_retention_days
                if retention_days is None
                else retention_days
            ),
            enabled=enabled,
            created_at=timestamp,
            updated_at=timestamp,
        )
        return self.message_repository.add_source(config)

    def update_message_source(
        self,
        source_id: str,
        *,
        now: datetime | None = None,
        **changes: object,
    ) -> MessageSourceConfig | None:
        current = self.message_repository.get_source(source_id)
        if current is None:
            return None
        timestamp = now or datetime.now(UTC)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("message source timestamps must be timezone-aware")
        values = current.model_dump(mode="python")
        values.update(changes)
        values["source_id"] = current.source_id
        values["created_at"] = current.created_at
        values["updated_at"] = timestamp
        config = MessageSourceConfig.model_validate(values)
        if config.provider != current.provider or config.account_id != current.account_id:
            self.message_service.unregister(source_id)
        return self.message_repository.add_source(config)

    def delete_message_source(self, source_id: str) -> bool:
        self._ensure_open()
        self.message_service.unregister(source_id)
        return self.message_repository.delete_source(source_id)

    def list_message_sources(
        self,
        *,
        enabled: bool | None = None,
        limit: int = 1_000,
    ) -> tuple[MessageSourceConfig, ...]:
        self._ensure_open()
        return tuple(self.message_repository.list_sources(enabled=enabled, limit=limit))

    def list_message_summaries(
        self,
        *,
        source_id: str | None = None,
        unread: bool | None = None,
        category: MessageCategory | None = None,
        include_spam: bool = True,
        limit: int = 200,
    ) -> tuple[MessageSummary, ...]:
        self._ensure_open()
        return self.message_service.summaries(
            source_id=source_id,
            unread=unread,
            category=category,
            include_spam=include_spam,
            limit=limit,
        )

    async def refresh_messages(
        self,
        source_ids: Iterable[str] | None = None,
        *,
        force: bool = False,
        now: datetime | None = None,
    ) -> dict[str, MessageResult]:
        self._ensure_open()
        timestamp = now or datetime.now(UTC)
        selected = None if source_ids is None else tuple(source_ids)
        task = asyncio.current_task()
        if task is not None:
            self._refresh_tasks.add(task)
        try:
            results = await self.message_service.refresh_all(
                selected,
                force=force,
                now=timestamp,
            )
            for result in results.values():
                self.audit_repository.add(
                    AuditEvent(
                        event_type="message.refresh",
                        source_type=result.provider,
                        source_id=result.source_id,
                        outcome=("succeeded" if result.state in {"ready", "empty"} else "failed"),
                        occurred_at=result.checked_at,
                        metadata={
                            "state": result.state,
                            "item_count": (
                                0 if result.snapshot is None else len(result.snapshot.summaries)
                            ),
                        },
                    )
                )
            return results
        finally:
            if task is not None:
                self._refresh_tasks.discard(task)

    async def open_message_body(self, source_id: str, message_id: str) -> MessageBody:
        self._ensure_open()
        return await self.message_service.get_body(source_id, message_id)

    def clear_message_bodies(self, *, source_id: str | None = None) -> None:
        self.message_service.clear_bodies(source_id=source_id)

    def clear_message_summaries(self, source_id: str | None = None) -> int:
        self._ensure_open()
        return self.message_service.clear_summaries(source_id)

    def message_today_items(
        self,
        *,
        unread_only: bool = True,
        category: MessageCategory | None = "primary",
        limit: int = 100,
    ) -> tuple[TodayItem, ...]:
        summaries = self.list_message_summaries(
            unread=True if unread_only else None,
            category=category,
            include_spam=False,
            limit=limit,
        )
        return tuple(
            TodayItem(
                item_id=f"message.{summary.source_id}.{sha256(summary.message_id.encode()).hexdigest()[:24]}",
                kind="message",
                title=summary.title,
                summary=f"{summary.sender} — {summary.preview or 'No preview'}",
                starts_at=summary.received_at,
                severity=("attention" if summary.importance == "important" else "info"),
                source=SourceRef(
                    provider_id=summary.provider,
                    connection_id=summary.source_id,
                    resource_id=summary.message_id,
                    display_name=summary.account_id,
                ),
                navigation_target=f"hub://messages/{summary.source_id}/{sha256(summary.message_id.encode()).hexdigest()}",
                sensitive=True,
            )
            for summary in summaries
        )

    def reconcile_message_digest(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> InboxItem | None:
        timestamp = now or datetime.now(UTC)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("message digest timestamp must be timezone-aware")
        summaries = self.list_message_summaries(
            unread=True,
            include_spam=False,
            limit=limit,
        )
        if not summaries:
            return None
        local_date = timestamp.astimezone(ZoneInfo(self._settings.default_timezone)).date()
        key = f"message-digest:{self._settings.default_timezone}:{local_date.isoformat()}"
        if not self.notification_repository.claim_key(
            key,
            "digest",
            now=timestamp,
            safe_message="An attention-message digest was claimed.",
        ):
            return None
        sources = len({item.source_id for item in summaries})
        return self.inbox_repository.add(
            InboxItem(
                item_id=f"messages.digest.{local_date.strftime('%Y%m%d')}",
                kind="digest",
                title="Attention messages",
                safe_summary=f"{len(summaries)} unread message(s) from {sources} selected source(s).",
                reason="Scheduled message digest",
                source=SourceRef(provider_id="messages", display_name="Attention messages"),
                created_at=timestamp,
                severity="info",
                navigation_target="hub://inbox/messages",
                deduplication_key=key,
                metadata={"item_count": len(summaries), "source_count": sources},
            )
        )

    # ------------------------------------------------------------------
    # Source operations.

    async def connect_source(
        self,
        source_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> SourceResult:
        self._ensure_open()
        self.set_source_enabled(source_id, True)
        task = asyncio.current_task()
        if task is not None:
            self._refresh_tasks.add(task)
        try:
            result = await self.source_service.connect(
                source_id,
                timeout_seconds=timeout_seconds,
            )
            self._record_source_result(result)
            return result
        finally:
            if task is not None:
                self._refresh_tasks.discard(task)

    async def disconnect_source(
        self,
        source_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> SourceResult:
        self._ensure_open()
        task = asyncio.current_task()
        if task is not None:
            self._refresh_tasks.add(task)
        try:
            result = await self.source_service.disconnect(
                source_id,
                timeout_seconds=timeout_seconds,
            )
            if result.state is SourceState.DISCONNECTED:
                self.source_repository.delete(source_id)
            else:
                self._record_source_result(result)
            return result
        finally:
            if task is not None:
                self._refresh_tasks.discard(task)

    async def status_source(
        self,
        source_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> SourceResult:
        self._ensure_open()
        task = asyncio.current_task()
        if task is not None:
            self._refresh_tasks.add(task)
        try:
            result = await self.source_service.status(
                source_id,
                timeout_seconds=timeout_seconds,
            )
            self._record_source_result(result)
            return result
        finally:
            if task is not None:
                self._refresh_tasks.discard(task)

    async def refresh_source(
        self,
        source_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> SourceResult:
        self._ensure_open()
        task = asyncio.current_task()
        if task is not None:
            self._refresh_tasks.add(task)
        try:
            result = await self.source_service.refresh(
                source_id,
                timeout_seconds=timeout_seconds,
            )
            self._record_source_result(result)
            return result
        finally:
            if task is not None:
                self._refresh_tasks.discard(task)

    async def refresh_sources(
        self,
        source_ids: Iterable[str] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, SourceResult]:
        """Refresh selected sources independently through the bounded source service."""

        self._ensure_open()
        task = asyncio.current_task()
        if task is not None:
            self._refresh_tasks.add(task)
        try:
            selected = (
                tuple(item.source_id for item in self.list_sources(enabled_only=True))
                if source_ids is None
                else source_ids
            )
            results = await self.source_service.refresh_all(
                source_ids=selected,
                timeout_seconds=timeout_seconds,
            )
            for result in results.values():
                self._record_source_result(result)
            return results
        finally:
            if task is not None:
                self._refresh_tasks.discard(task)

    refresh_all = refresh_sources
    refresh = refresh_source

    # ------------------------------------------------------------------
    # Metadata-only observability.

    def audit_events(self, *, limit: int = 100) -> list[AuditEvent]:
        return self.audit_repository.list(limit=limit)

    audit = audit_events

    def diagnostics_report(self, *, generated_at: datetime | None = None) -> DiagnosticsReport:
        """Return a redacted report from current and persisted source health metadata."""

        for result in self.list_source_results():
            self._diagnostics.record_source(result)
        return self._diagnostics.report(generated_at=generated_at)

    diagnostics = diagnostics_report
    get_diagnostics = diagnostics_report

    def diagnostics_json(self, *, generated_at: datetime | None = None) -> str:
        return self.diagnostics_report(generated_at=generated_at).to_json()

    copyable_diagnostics = diagnostics_json

    # ------------------------------------------------------------------
    # Backup/restore and lifecycle.

    def backup_database(self, destination: Path | str | None = None) -> Path:
        self._ensure_open()
        selected = (
            Path(destination) if destination is not None else _default_backup_path(self.settings)
        )
        return backup_database(self.database, selected)

    backup = backup_database
    create_backup = backup_database

    def export_local_data(
        self,
        destination: Path | str,
        *,
        exported_at: datetime | None = None,
    ) -> Path:
        """Export bounded Hub data as JSON, excluding credentials and message bodies."""

        self._ensure_open()
        timestamp = exported_at or datetime.now(UTC)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("export timestamp must be timezone-aware")
        selected = Path(destination).expanduser().resolve(strict=False)
        if selected.exists() and selected.is_dir():
            raise ValueError("export destination must be a file")
        selected.parent.mkdir(parents=True, exist_ok=True)
        settings = self._settings.model_dump(mode="json", exclude={"data_dir", "database_name"})
        tasks = [
            task
            for state in ("open", "completed", "archived")
            for task in self.task_repository.list(state=state, limit=10_000)
        ]
        payload = {
            "format": "personal-hub-local-export-v1",
            "exported_at": timestamp.astimezone(UTC).isoformat(),
            "settings": settings,
            "source_connections": [
                item.model_dump(mode="json") for item in self.list_source_connections()
            ],
            "tasks": [item.model_dump(mode="json") for item in tasks],
            "inbox": [item.model_dump(mode="json") for item in self.list_inbox(limit=1_000)],
            "feed_subscriptions": [
                item.model_dump(mode="json") for item in self.list_feed_subscriptions(limit=1_000)
            ],
            "feed_entries": [
                item.model_dump(mode="json") for item in self.list_feed_entries(limit=10_000)
            ],
            "watches": [item.model_dump(mode="json") for item in self.list_watches(limit=1_000)],
            "watch_alerts": [
                item.model_dump(mode="json") for item in self.watch_alerts(limit=1_000)
            ],
            "message_sources": [
                item.model_dump(mode="json") for item in self.list_message_sources(limit=1_000)
            ],
            "message_summaries": [
                item.model_dump(mode="json") for item in self.list_message_summaries(limit=1_000)
            ],
        }
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{selected.name}.",
            suffix=".tmp",
            dir=selected.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, selected)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        self.audit_repository.add(
            AuditEvent(
                event_type="data.export",
                source_type="hub",
                source_id=None,
                outcome="succeeded",
                occurred_at=timestamp,
                metadata={
                    "task_count": len(tasks),
                    "inbox_count": len(payload["inbox"]),
                },
            )
        )
        return selected

    export = export_local_data

    def restore_database(
        self,
        source: Path | str,
        *,
        destination: Path | str | None = None,
    ) -> Path:
        self._ensure_open()
        restored = restore_database(self.database, source, destination=destination)
        if _same_path(restored, self.database.path):
            self._settings = _load_controller_settings(
                self.settings_repository,
                defaults=self._settings_defaults,
                database_path=self.database.path,
            )
            _set_service_offline(self.source_service, self._settings.offline_mode)
            _set_service_offline(self.calendar_service, self._settings.offline_mode)
            self.ical_feed_manager.set_offline(self._settings.offline_mode)
            self.ical_feed_manager.set_allow_http(self._settings.allow_insecure_feed_http)
            self.ical_feed_manager.reload()
            _set_service_offline(self.feed_service, self._settings.offline_mode)
            _set_service_offline(self.watch_service, self._settings.offline_mode)
            self._restore_feed_service(reset=True)
        return restored

    restore = restore_database
    restore_from_backup = restore_database

    async def cancel_refreshes(self) -> None:
        """Cancel controller-owned refresh tasks and wait for bounded cleanup."""

        current = asyncio.current_task()
        tasks = tuple(task for task in self._refresh_tasks if task is not current)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    cancel = cancel_refreshes

    async def shutdown(self, *, close_database: bool | None = None) -> None:
        """Stop refresh work and optionally close a database owned by this controller."""

        if not self._closed or self._refresh_tasks:
            await self.cancel_refreshes()
        self.message_service.clear_bodies()
        self._closed = True
        if close_database is None:
            close_database = self._owns_database
        if close_database:
            self.database.close()

    async def aclose(self) -> None:
        await self.shutdown()

    def close(self) -> None:
        """Synchronously mark closed and request cancellation.

        Qt shutdown handlers can call this without awaiting.  Async callers should prefer
        :meth:`shutdown`, which waits until cancelled source work has unwound.
        """

        self.message_service.clear_bodies()
        self._closed = True
        current = asyncio.current_task() if _has_running_loop() else None
        tasks = tuple(self._refresh_tasks)
        for task in tasks:
            if task is not current:
                task.cancel()
        if self._owns_database and not tasks:
            self.database.close()

    def __enter__(self) -> HubController:
        self._ensure_open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    async def __aenter__(self) -> HubController:
        self._ensure_open()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.shutdown()

    # ------------------------------------------------------------------
    # Internal safe projections.

    def _record_source_result(self, result: SourceResult) -> None:
        """Persist only source health metadata and a grouped safe failure item."""

        with suppress(Exception):
            self._persist_source_health(result)
        with suppress(Exception):
            self._diagnostics.record_source(result)
        if result.state in self._FAILURE_STATES:
            with suppress(Exception):
                self._enqueue_source_failure(result)
        with suppress(Exception):
            self._record_source_audit(result)

    def _persist_source_health(self, result: SourceResult) -> SourceSyncState:
        connection_id = self._ensure_source_connection(result.source_id, result.provider_id)
        previous = self.source_repository.get_sync_state(connection_id)
        success = result.state in self._SUCCESS_STATES
        offline = result.state is SourceState.OFFLINE
        collected_at = (
            result.snapshot.collected_at
            if result.snapshot is not None and success
            else previous.freshness.collected_at
            if previous is not None
            else None
        )
        if success and collected_at is None:
            collected_at = result.checked_at
        last_success_at = (
            result.last_success_at or collected_at
            if success
            else previous.last_success_at
            if previous is not None
            else result.last_success_at
        )
        if success:
            failure_count = 0
        elif offline:
            failure_count = previous.failure_count if previous is not None else 0
        else:
            failure_count = (previous.failure_count if previous is not None else 0) + 1
        last_attempt_at = (
            previous.last_attempt_at
            if offline and previous is not None
            else None
            if offline
            else result.checked_at
        )
        backoff_until = (
            None if success else previous.backoff_until if previous is not None else None
        )
        state = SourceSyncState(
            connection_id=connection_id,
            freshness=Freshness(
                state=result.state.value,
                collected_at=collected_at,
                safe_message=safe_state_message(result.state),
            ),
            last_attempt_at=last_attempt_at,
            last_success_at=last_success_at,
            failure_count=failure_count,
            backoff_until=backoff_until,
        )
        return self.source_repository.save_sync_state(state)

    def _ensure_source_connection(self, source_id: str, provider_id: str) -> str:
        existing = self.source_repository.get(source_id)
        if existing is not None:
            if existing.provider_id != provider_id:
                raise ValueError("source identity changed for persisted Hub connection")
            return existing.connection_id
        display_name = provider_id
        with suppress(KeyError):
            display_name = self.registry.get(source_id).definition.display_name
        now = datetime.now(UTC)
        connection = SourceConnection(
            connection_id=source_id,
            provider_id=provider_id,
            account_label=display_name,
            created_at=now,
            updated_at=now,
        )
        self.source_repository.save(connection)
        return connection.connection_id

    def _enqueue_source_failure(self, result: SourceResult) -> InboxItem:
        definition = None
        with suppress(KeyError):
            definition = self.registry.get(result.source_id).definition
        display_name = definition.display_name if definition is not None else result.provider_id
        item = InboxItem(
            kind="source_failure",
            title="Source needs attention",
            safe_summary="A source refresh needs attention.",
            reason=safe_state_message(result.state),
            source=SourceRef(
                provider_id=result.provider_id,
                connection_id=result.source_id,
                display_name=display_name,
            ),
            created_at=result.checked_at,
            severity="attention",
            navigation_target=f"hub://sources/{result.source_id}",
            deduplication_key=f"source_failure:{result.source_id}",
            metadata={"state": result.state.value, "provider_id": result.provider_id},
        )
        return self.inbox_repository.add(item)

    def _record_source_audit(self, result: SourceResult) -> AuditEvent:
        if result.state in self._SUCCESS_STATES:
            outcome = "succeeded"
        elif result.state is SourceState.OFFLINE:
            outcome = "skipped"
        else:
            outcome = "failed"
        event = AuditEvent(
            event_type="source.refresh",
            source_type=result.provider_id,
            source_id=result.source_id,
            outcome=outcome,
            occurred_at=result.checked_at,
            metadata={
                "duration_ms": round(result.duration_ms, 3),
                "item_count": result.item_count,
                "state": result.state.value,
            },
        )
        return self.audit_repository.append(event)

    def _result_from_persisted_state(
        self,
        source_id: str,
        state: SourceSyncState,
    ) -> SourceResult:
        connection = self.source_repository.get(source_id)
        provider_id = connection.provider_id if connection is not None else source_id
        display_name = connection.account_label if connection is not None else provider_id
        try:
            source_state = SourceState(state.freshness.state)
        except ValueError:
            source_state = SourceState.ERROR
        checked_at = state.last_attempt_at or state.last_success_at or state.freshness.collected_at
        checked_at = checked_at or datetime.now(UTC)
        return SourceResult(
            source_id=source_id,
            provider_id=provider_id,
            display_name=display_name,
            state=source_state,
            checked_at=checked_at,
            last_success_at=state.last_success_at,
            safe_message=safe_state_message(source_state),
        )

    def _audit_task_change(self, event_type: str, task: HubTask) -> None:
        with suppress(Exception):
            self.audit_repository.append(
                AuditEvent(
                    event_type=event_type,
                    source_type="local_tasks",
                    source_id=str(task.task_id),
                    outcome="succeeded",
                    metadata={
                        "state": task.state,
                        "priority": task.priority,
                        "has_due": task.due_at is not None,
                        "recurring": task.recurrence is not None,
                    },
                )
            )

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Personal Hub controller is closed")


def _container_values(
    value: HubContainerLike | HubSettings | HubDatabase | None,
) -> tuple[HubSettings | None, HubDatabase | None]:
    if value is None:
        return None, None
    if isinstance(value, HubSettings):
        return value, None
    if isinstance(value, HubDatabase):
        return None, value
    selected_settings = getattr(value, "settings", None)
    selected_database = getattr(value, "database", None)
    if not isinstance(selected_settings, HubSettings) or not isinstance(
        selected_database, HubDatabase
    ):
        raise TypeError("container must expose HubSettings settings and HubDatabase database")
    return selected_settings, selected_database


def _load_controller_settings(
    repository: SettingsRepository,
    *,
    defaults: HubSettings,
    database_path: Path,
) -> HubSettings:
    loaded = repository.load_settings(defaults=defaults)
    if _same_path(loaded.database_path, database_path):
        return loaded
    # A restored/copied database may contain settings from its original directory.  Keep
    # the active database location authoritative while restoring every other safe setting.
    values = cast(dict[str, Any], loaded.model_dump())
    values["data_dir"] = database_path.parent
    values["database_name"] = database_path.name
    return HubSettings.model_validate(values)


def _set_service_offline(service: SourceService, enabled: bool) -> None:
    setter = getattr(service, "set_offline", None)
    if callable(setter):
        setter(bool(enabled))


def _default_backup_path(settings: HubSettings) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = settings.backups_dir / f"personal_hub-{stamp}.sqlite"
    suffix = 1
    while destination.exists():
        destination = settings.backups_dir / f"personal_hub-{stamp}-{suffix}.sqlite"
        suffix += 1
    return destination


def _same_path(first: Path | str, second: Path | str) -> bool:
    """Compare paths after normalizing Windows aliases and existing symlinks."""

    try:
        first_resolved = Path(first).expanduser().resolve(strict=False)
        second_resolved = Path(second).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        first_resolved = Path(os.path.abspath(os.fspath(first)))
        second_resolved = Path(os.path.abspath(os.fspath(second)))
    return os.path.normcase(os.path.normpath(os.fspath(first_resolved))) == os.path.normcase(
        os.path.normpath(os.fspath(second_resolved))
    )


def _has_running_loop() -> bool:
    with suppress(RuntimeError):
        asyncio.get_running_loop()
        return True
    return False


def _calendar_selection_key(source_id: str, calendar_id: str) -> str:
    """Return the persisted, account-scoped identity used by calendar views."""

    return f"{source_id}/{calendar_id}"


def _calendar_selection_matches(
    source_id: str,
    calendar_id: str,
    selected: set[str],
) -> bool:
    """Honor source-level ``*`` selections as well as exact calendar keys."""

    return (
        _calendar_selection_key(source_id, calendar_id) in selected
        or _calendar_selection_key(source_id, "*") in selected
        or source_id in selected
    )


def _feed_source_result(result: FeedResult) -> SourceResult:
    return SourceResult(
        source_id=f"feed.{result.subscription_id.hex}",
        provider_id="rss",
        display_name="News feed",
        state=SourceState(result.state),
        checked_at=result.checked_at,
        snapshot=None,
        last_success_at=result.last_success_at,
        item_count=result.item_count,
        duration_ms=result.duration_ms,
        safe_message=safe_state_message(SourceState(result.state)),
    )


__all__ = ["HubContainerLike", "HubController"]
