"""Personal Hub-only composition root.

The container intentionally contains no Assistant router, planner, model, voice, skill,
or UI service. Later Hub features extend this root through Hub-owned contracts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from personal_hub import __version__
from personal_hub.calendar.ical import IcalFeedFetcher, IcalFeedManager
from personal_hub.calendar.local import CalendarImportRepository
from personal_hub.calendar.service import CalendarService
from personal_hub.config import HubSettings
from personal_hub.controller import HubController
from personal_hub.diagnostics import DiagnosticsAggregator
from personal_hub.feeds.fetcher import FeedFetcher
from personal_hub.feeds.repository import FeedEntryRepository, FeedSubscriptionRepository
from personal_hub.feeds.service import FeedRefreshService
from personal_hub.integrations.platforms import ExternalPlatformManager
from personal_hub.lifecycle import HubLifecycle
from personal_hub.messages import MessageRepository, MessageService
from personal_hub.sources import SourceRegistry, SourceService
from personal_hub.storage import (
    AuditRepository,
    HubDatabase,
    HubSecretVault,
    InboxRepository,
    NotificationRepository,
    SettingsRepository,
    SourceRepository,
    TaskRepository,
)
from personal_hub.watches import PageFetcher, WatchRepository, WatchService


@dataclass(slots=True)
class HubContainer:
    settings: HubSettings
    database: HubDatabase
    lifecycle: HubLifecycle
    settings_repository: SettingsRepository
    source_repository: SourceRepository
    inbox_repository: InboxRepository
    audit_repository: AuditRepository
    notification_repository: NotificationRepository
    task_repository: TaskRepository
    calendar_service: CalendarService
    calendar_import_repository: CalendarImportRepository
    ical_feed_manager: IcalFeedManager
    feed_subscription_repository: FeedSubscriptionRepository
    feed_entry_repository: FeedEntryRepository
    feed_service: FeedRefreshService
    watch_repository: WatchRepository
    watch_service: WatchService
    message_repository: MessageRepository
    message_service: MessageService
    secret_vault: HubSecretVault
    source_registry: SourceRegistry
    source_service: SourceService
    platform_connections: ExternalPlatformManager
    diagnostics: DiagnosticsAggregator
    controller: HubController
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.controller.close()
            self.database.close()
        finally:
            self.lifecycle.shutdown()

    def __enter__(self) -> HubContainer:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def build_hub_container(
    settings: HubSettings | None = None,
    *,
    acquire_instance_lock: bool = False,
) -> HubContainer:
    """Build an isolated Hub container and initialize its database.

    Tests and non-desktop tools normally leave ``acquire_instance_lock`` false. The real
    application enables it before creating any persistent directory, which preserves the
    single-instance startup order from the implementation plan.
    """

    resolved_settings = settings or HubSettings()
    lifecycle = HubLifecycle(resolved_settings)
    if acquire_instance_lock:
        lifecycle.start()

    database: HubDatabase | None = None
    try:
        for directory in (
            resolved_settings.data_dir,
            resolved_settings.secrets_dir,
            resolved_settings.logs_dir,
            resolved_settings.cache_dir,
            resolved_settings.backups_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        database = HubDatabase(resolved_settings.database_path)
        database.migrate()
        settings_repository = SettingsRepository(database)
        persisted_settings = settings_repository.load_settings(resolved_settings)
        if not settings_repository.all():
            settings_repository.save_settings(persisted_settings)
        relay_role = settings_repository.get("sync.provider_relay_role", "local")
        if relay_role == "consumer" and not persisted_settings.offline_mode:
            persisted_settings = persisted_settings.model_copy(update={"offline_mode": True})
            settings_repository.save_settings(persisted_settings)
        lifecycle.settings = persisted_settings

        source_repository = SourceRepository(database)
        inbox_repository = InboxRepository(database)
        audit_repository = AuditRepository(database)
        notification_repository = NotificationRepository(database)
        secret_vault = HubSecretVault(persisted_settings)
        task_repository = TaskRepository(database)
        calendar_service = CalendarService(offline=persisted_settings.offline_mode)
        calendar_import_repository = CalendarImportRepository(database)
        ical_feed_manager = IcalFeedManager(
            settings_repository,
            calendar_service,
            fetcher=IcalFeedFetcher(
                allow_http=persisted_settings.allow_insecure_feed_http,
                offline=persisted_settings.offline_mode,
            ),
        )
        feed_subscription_repository = FeedSubscriptionRepository(database)
        feed_entry_repository = FeedEntryRepository(
            database,
            max_entries=max(
                5_000,
                persisted_settings.feed_max_entries_per_subscription,
            ),
            max_entries_per_subscription=(persisted_settings.feed_max_entries_per_subscription),
            retention=timedelta(days=persisted_settings.feed_retention_days),
        )
        feed_service = FeedRefreshService(
            FeedFetcher(
                allow_http=persisted_settings.allow_insecure_feed_http,
                offline=persisted_settings.offline_mode,
            ),
            offline=persisted_settings.offline_mode,
        )
        watch_repository = WatchRepository(
            database,
            observation_retention=timedelta(
                days=persisted_settings.watch_observation_retention_days
            ),
            max_observations_per_watch=(persisted_settings.watch_max_observations_per_watch),
        )
        watch_service = WatchService(
            watch_repository,
            PageFetcher(
                allow_http=persisted_settings.allow_insecure_watch_http,
                offline=persisted_settings.offline_mode,
            ),
            offline=persisted_settings.offline_mode,
        )
        message_repository = MessageRepository(
            database,
            max_summaries_per_source=persisted_settings.message_max_summaries_per_source,
        )
        message_service = MessageService(
            message_repository,
            offline=persisted_settings.offline_mode,
        )
        source_registry = SourceRegistry()
        platform_connections = ExternalPlatformManager(
            settings_repository,
            secret_vault,
            source_registry,
            calendar_service,
            message_repository,
            message_service,
            task_repository,
            message_refresh_minutes=persisted_settings.message_refresh_minutes,
            message_retention_days=persisted_settings.message_summary_retention_days,
            default_timezone=persisted_settings.default_timezone,
            canvas_remove_completed_assignments=persisted_settings.canvas_remove_completed_assignments,
        )
        source_service = SourceService(
            source_registry,
            offline=persisted_settings.offline_mode,
        )
        diagnostics = DiagnosticsAggregator(
            application_version=__version__,
            schema_version=HubDatabase.CURRENT_SCHEMA_VERSION,
        )
        controller = HubController(
            settings=persisted_settings,
            database=database,
            source_service=source_service,
            registry=source_registry,
            settings_repository=settings_repository,
            source_repository=source_repository,
            inbox_repository=inbox_repository,
            audit_repository=audit_repository,
            notification_repository=notification_repository,
            task_repository=task_repository,
            calendar_service=calendar_service,
            calendar_import_repository=calendar_import_repository,
            ical_feed_manager=ical_feed_manager,
            feed_subscription_repository=feed_subscription_repository,
            feed_entry_repository=feed_entry_repository,
            feed_service=feed_service,
            watch_repository=watch_repository,
            watch_service=watch_service,
            message_repository=message_repository,
            message_service=message_service,
            diagnostics=diagnostics,
        )
        return HubContainer(
            settings=persisted_settings,
            database=database,
            lifecycle=lifecycle,
            settings_repository=settings_repository,
            source_repository=source_repository,
            inbox_repository=inbox_repository,
            audit_repository=audit_repository,
            notification_repository=notification_repository,
            task_repository=task_repository,
            calendar_service=calendar_service,
            calendar_import_repository=calendar_import_repository,
            ical_feed_manager=ical_feed_manager,
            feed_subscription_repository=feed_subscription_repository,
            feed_entry_repository=feed_entry_repository,
            feed_service=feed_service,
            watch_repository=watch_repository,
            watch_service=watch_service,
            message_repository=message_repository,
            message_service=message_service,
            secret_vault=secret_vault,
            source_registry=source_registry,
            source_service=source_service,
            platform_connections=platform_connections,
            diagnostics=diagnostics,
            controller=controller,
        )
    except BaseException:
        if database is not None:
            database.close()
        lifecycle.shutdown()
        raise


# A concise alias keeps the composition API familiar without importing Assistant bootstrap.
build_container = build_hub_container

__all__ = ["HubContainer", "build_container", "build_hub_container"]
