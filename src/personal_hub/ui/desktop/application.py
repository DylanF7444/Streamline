"""Desktop composition for the Personal Hub shell.

This module is the only Phase 1 layer that knows about both Qt view objects and the
Hub application controller. Provider refreshes run on a dedicated thread with their own
asyncio loop; the Qt thread only renders validated, bounded view data.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID
from zoneinfo import ZoneInfo

from PySide6.QtCore import QObject, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QInputDialog

from personal_hub.calendar.contracts import CalendarQuery, CalendarResult
from personal_hub.calendar.google_sync import CalendarSyncReport
from personal_hub.calendar.local import LOCAL_IMPORT_PROVIDER_ID
from personal_hub.calendar.native import LOCAL_CALENDAR_ID
from personal_hub.feeds.contracts import FeedResult, FeedSyncState
from personal_hub.integrations.canvas import canvas_assignment_url
from personal_hub.lifecycle import LifecycleEvent
from personal_hub.logging.contracts import LogFilter
from personal_hub.messages import (
    MESSAGE_CATEGORY_VALUES,
    MessageAttentionNotice,
    MessageBody,
    MessageCategory,
    MessageQuery,
    MessageResult,
    MessageSyncState,
    is_message_category,
)
from personal_hub.notifications import (
    NotificationPolicy,
    NotificationSeverity,
    QuietHoursSettings,
    decide_delivery,
)
from personal_hub.sources import SourceResult, SourceState, safe_state_message
from personal_hub.startup import WindowsStartupRegistration
from personal_hub.sync import SyncError
from personal_hub.tasks import Recurrence
from personal_hub.ui.desktop.calendar_item_dialog import CalendarItemDialog
from personal_hub.ui.desktop.connection_dialog import ExternalPlatformDialog
from personal_hub.ui.desktop.dashboard import LAYOUT_VERSION
from personal_hub.ui.desktop.ical_dialog import IcalFeedDialog
from personal_hub.ui.desktop.import_dialog import ImportDialog
from personal_hub.ui.desktop.logging_coordinator import LoggingCoordinator
from personal_hub.ui.desktop.main_window import (
    AttentionNotificationView,
    CalendarEventView,
    CalendarImportView,
    CalendarSourceView,
    CalendarView,
    DiagnosticsView,
    FeedEntryView,
    FeedSubscriptionView,
    IcalFeedView,
    InboxItemView,
    MessageBodyView,
    MessageSourceView,
    MessageSummaryView,
    MessagesView,
    NewsView,
    PersonalHubMainWindow,
    SettingsView,
    SourceFailureView,
    SourceView,
    TaskView,
    TodayItemView,
    TodaySectionView,
    TodayView,
    WatchAlertView,
    WebWatchesView,
    WebWatchView,
    create_application,
    create_main_window,
)
from personal_hub.watches import WatchObservation, WatchResult, WatchSyncState

if TYPE_CHECKING:
    from personal_hub.bootstrap import HubContainer


@dataclass(frozen=True, slots=True)
class _PendingSystemNotification:
    """One claimed notification waiting for the current refresh burst to settle."""

    deduplication_key: str
    title: str
    message: str
    at: datetime
    sensitive: bool
    suppress_native: bool


class _SourceRefreshThread(QThread):
    """Run one bounded source refresh without occupying the Qt event loop."""

    results_ready = Signal(object)
    refresh_failed = Signal()

    def __init__(
        self,
        container: HubContainer,
        parent: QObject | None = None,
        *,
        source_ids: tuple[str, ...] | None = None,
        action: str = "refresh",
    ) -> None:
        super().__init__(parent)
        self._container = container
        self._source_ids = source_ids
        self._action = action
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[dict[str, SourceResult]] | None = None

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            if self._action == "connect" and self._source_ids is not None:
                source_id = self._source_ids[0]
                operation = self._container.controller.connect_source(source_id)
            elif self._action == "disconnect" and self._source_ids is not None:
                source_id = self._source_ids[0]
                operation = self._container.controller.disconnect_source(source_id)
            else:
                operation = self._container.controller.refresh_sources(self._source_ids)
            self._task = loop.create_task(operation)
            results = loop.run_until_complete(self._task)
            if isinstance(results, SourceResult):
                self.results_ready.emit({results.source_id: results})
            else:
                self.results_ready.emit(results)
        except asyncio.CancelledError:
            return
        except Exception:
            # Adapter exception text must never cross into the presentation layer.
            self.refresh_failed.emit()
            return
        finally:
            self._task = None
            self._loop = None
            asyncio.set_event_loop(None)
            loop.close()

    def cancel(self) -> None:
        loop = self._loop
        task = self._task
        if loop is not None and task is not None:
            loop.call_soon_threadsafe(task.cancel)


class _MaintenanceThread(QThread):
    """Reconcile reminders and scheduled digests without blocking Qt."""

    completed = Signal(object)
    failed = Signal()

    def __init__(self, container: HubContainer, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._container = container
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[tuple[Any, ...]] | None = None

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            now = datetime.now(UTC)
            self._task = loop.create_task(
                self._container.controller.reconcile_task_reminders(now=now)
            )
            created: list[Any] = list(loop.run_until_complete(self._task))
            settings = self._container.controller.settings
            self._container.controller.cleanup_completed_tasks_after_due_date(now=now)
            local_now = now.astimezone(ZoneInfo(settings.default_timezone))
            digest_hour, digest_minute = (int(part) for part in settings.digest_time.split(":", 1))
            if settings.digest_enabled and local_now.timetz().replace(tzinfo=None) >= time(
                digest_hour, digest_minute
            ):
                for item in (
                    self._container.controller.reconcile_news_digest(now=now),
                    self._container.controller.reconcile_message_digest(now=now),
                ):
                    if item is not None:
                        created.append(item)
            cleanup_date = local_now.date().isoformat()
            cleanup: dict[str, int] = {}
            if (
                self._container.settings_repository.get("retention_cleanup_date", "")
                != cleanup_date
            ):
                cleanup = self._container.controller.run_retention_cleanup(now=now)
                self._container.settings_repository.set("retention_cleanup_date", cleanup_date)
            self.completed.emit({"created": tuple(created), "cleanup": cleanup})
        except asyncio.CancelledError:
            return
        except Exception:
            self.failed.emit()
        finally:
            self._task = None
            asyncio.set_event_loop(None)
            loop.close()

    def cancel(self) -> None:
        loop, task = self._loop, self._task
        if loop is not None and task is not None and not task.done():
            loop.call_soon_threadsafe(task.cancel)


class _CalendarRefreshThread(QThread):
    """Run one bounded multi-account calendar refresh off the Qt thread."""

    results_ready = Signal(object)
    sync_ready = Signal(object)
    refresh_failed = Signal()

    def __init__(
        self,
        container: HubContainer,
        query: CalendarQuery,
        *,
        source_ids: tuple[str, ...] | None = None,
        queries_by_source: Mapping[str, CalendarQuery] | None = None,
        sync_google: bool = False,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._container = container
        self._query = query
        self._source_ids = source_ids
        self._queries_by_source = dict(queries_by_source or {}) or None
        self._sync_google = sync_google
        self._sync_report: CalendarSyncReport | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[dict[str, CalendarResult]] | None = None

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            self._task = loop.create_task(self._refresh())
            results = loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            return
        except Exception:
            self.refresh_failed.emit()
            return
        finally:
            self._task = None
            self._loop = None
            asyncio.set_event_loop(None)
            loop.close()
        self.results_ready.emit(results)
        if self._sync_report is not None:
            self.sync_ready.emit(self._sync_report)

    async def _refresh(self) -> dict[str, CalendarResult]:
        if self._sync_google:
            self._sync_report = await self._container.platform_connections.sync_calendar_items(
                self._container.controller.local_calendar
            )
        return await self._container.controller.refresh_calendars(
            self._query, self._source_ids, queries_by_source=self._queries_by_source
        )

    def cancel(self) -> None:
        loop = self._loop
        task = self._task
        if loop is not None and task is not None:
            loop.call_soon_threadsafe(task.cancel)


class _FeedRefreshThread(QThread):
    """Run independent bounded feed refreshes off the Qt thread."""

    results_ready = Signal(object)
    refresh_failed = Signal()

    def __init__(
        self,
        container: HubContainer,
        *,
        subscription_ids: tuple[UUID, ...] | None = None,
        force: bool = False,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._container = container
        self._subscription_ids = subscription_ids
        self._force = force
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[dict[UUID, FeedResult]] | None = None

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            self._task = loop.create_task(
                self._container.controller.refresh_feeds(
                    self._subscription_ids,
                    force=self._force,
                )
            )
            results = loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            return
        except Exception:
            self.refresh_failed.emit()
            return
        finally:
            self._task = None
            self._loop = None
            asyncio.set_event_loop(None)
            loop.close()
        self.results_ready.emit(results)

    def cancel(self) -> None:
        loop = self._loop
        task = self._task
        if loop is not None and task is not None:
            loop.call_soon_threadsafe(task.cancel)


class _WatchRefreshThread(QThread):
    results_ready = Signal(object)
    refresh_failed = Signal()

    def __init__(
        self,
        container: HubContainer,
        *,
        watch_ids: tuple[UUID, ...] | None = None,
        force: bool = False,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._container = container
        self._watch_ids = watch_ids
        self._force = force
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[dict[UUID, WatchResult]] | None = None

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            self._task = loop.create_task(
                self._container.controller.refresh_watches(
                    self._watch_ids,
                    force=self._force,
                )
            )
            results = loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            return
        except Exception:
            self.refresh_failed.emit()
            return
        finally:
            self._task = None
            self._loop = None
            asyncio.set_event_loop(None)
            loop.close()
        self.results_ready.emit(results)

    def cancel(self) -> None:
        if self._loop is not None and self._task is not None:
            self._loop.call_soon_threadsafe(self._task.cancel)


class _WatchTestThread(QThread):
    result_ready = Signal(object)
    test_failed = Signal()

    def __init__(
        self,
        container: HubContainer,
        value: Mapping[object, object],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._container = container
        self._value = dict(value)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[WatchObservation] | None = None

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            watch_id = self._value.get("watch_id")
            if isinstance(watch_id, str):
                coroutine = self._container.controller.test_watch_now(watch_id)
            else:
                coroutine = self._container.controller.test_watch_draft(
                    **_watch_changes(self._value, include_identity=False)
                )
            self._task = loop.create_task(coroutine)
            result = loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            return
        except Exception:
            self.test_failed.emit()
            return
        finally:
            self._task = None
            self._loop = None
            asyncio.set_event_loop(None)
            loop.close()
        self.result_ready.emit(result)

    def cancel(self) -> None:
        if self._loop is not None and self._task is not None:
            self._loop.call_soon_threadsafe(self._task.cancel)


class _MessageRefreshThread(QThread):
    """Refresh configured message summaries without fetching bodies."""

    results_ready = Signal(object)
    refresh_failed = Signal()

    def __init__(
        self,
        container: HubContainer,
        *,
        force: bool = False,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._container = container
        self._force = force
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[dict[str, MessageResult]] | None = None

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            self._task = loop.create_task(
                self._container.controller.refresh_messages(force=self._force)
            )
            results = loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            return
        except Exception:
            self.refresh_failed.emit()
            return
        finally:
            self._task = None
            self._loop = None
            asyncio.set_event_loop(None)
            loop.close()
        self.results_ready.emit(results)

    def cancel(self) -> None:
        if self._loop is not None and self._task is not None:
            self._loop.call_soon_threadsafe(self._task.cancel)


class _MessageBodyThread(QThread):
    """Perform one explicit body read and return only its sanitized contract."""

    body_ready = Signal(object)
    body_failed = Signal()

    def __init__(
        self,
        container: HubContainer,
        source_id: str,
        message_id: str,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._container = container
        self._source_id = source_id
        self._message_id = message_id
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[MessageBody] | None = None

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            self._task = loop.create_task(
                self._container.controller.open_message_body(
                    self._source_id,
                    self._message_id,
                )
            )
            body = loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            return
        except Exception:
            self.body_failed.emit()
            return
        finally:
            self._task = None
            self._loop = None
            asyncio.set_event_loop(None)
            loop.close()
        self.body_ready.emit(body)

    def cancel(self) -> None:
        if self._loop is not None and self._task is not None:
            self._loop.call_soon_threadsafe(self._task.cancel)


class _GlobalRefreshThread(QThread):
    """Run one complete refresh cycle in one background event loop."""

    results_ready = Signal(object)
    refresh_failed = Signal(object)

    def __init__(
        self,
        container: HubContainer,
        *,
        query: CalendarQuery,
        calendar_source_ids: tuple[str, ...] | None = None,
        calendar_queries_by_source: Mapping[str, CalendarQuery] | None = None,
        include_sources: bool = True,
        include_calendars: bool = True,
        force: bool = False,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._container = container
        self._query = query
        self._calendar_source_ids = calendar_source_ids
        self._calendar_queries_by_source = dict(calendar_queries_by_source or {}) or None
        self._include_sources = include_sources
        self._include_calendars = include_calendars
        self._force = force
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[tuple[dict[str, object], tuple[str, ...]]] | None = None

    async def _refresh(self) -> tuple[dict[str, object], tuple[str, ...]]:
        results: dict[str, object] = {}
        failures: list[str] = []
        controller = self._container.controller
        operations: list[tuple[str, Callable[[], Awaitable[object]]]] = []
        if self._include_sources:
            operations.append(("sources", controller.refresh_sources))
        if self._include_calendars:
            operations.append(
                (
                    "calendars",
                    lambda: controller.refresh_calendars(
                        self._query,
                        self._calendar_source_ids,
                        queries_by_source=self._calendar_queries_by_source,
                    ),
                )
            )
        operations.extend(
            (
                ("feeds", lambda: controller.refresh_feeds(force=self._force)),
                ("watches", lambda: controller.refresh_watches(force=self._force)),
                ("messages", lambda: controller.refresh_messages(force=self._force)),
            )
        )
        for name, operation in operations:
            try:
                results[name] = await operation()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never expose an adapter exception or response body to the UI.
                failures.append(name)
        return results, tuple(failures)

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            self._task = loop.create_task(self._refresh())
            results, failures = loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            return
        except Exception:
            self.refresh_failed.emit(("sources", "calendars", "feeds", "watches", "messages"))
            return
        finally:
            self._task = None
            self._loop = None
            asyncio.set_event_loop(None)
            loop.close()
        self.results_ready.emit(results)
        if failures:
            self.refresh_failed.emit(failures)

    def cancel(self) -> None:
        if self._loop is not None and self._task is not None:
            self._loop.call_soon_threadsafe(self._task.cancel)


class HubDesktopCoordinator(QObject):
    """Bind one Hub container to one presentation-only main window."""

    lifecycle_event = Signal(object)

    def __init__(
        self,
        container: HubContainer,
        window: PersonalHubMainWindow,
        application: QApplication,
        *,
        startup_registration: object | None = None,
    ) -> None:
        super().__init__(window)
        self.container = container
        self.window = window
        self.application = application
        self._startup_registration = startup_registration or WindowsStartupRegistration()
        self._global_refresh_thread: _GlobalRefreshThread | None = None
        self._global_refresh_pending = False
        self._global_refresh_pending_force = False
        self._refresh_thread: _SourceRefreshThread | None = None
        self._source_operation_pending: tuple[tuple[str, ...], str] | None = None
        self._calendar_refresh_thread: _CalendarRefreshThread | None = None
        self._calendar_refresh_pending = False
        self._feed_refresh_thread: _FeedRefreshThread | None = None
        self._feed_refresh_pending = False
        self._feed_refresh_pending_force = False
        self._watch_refresh_thread: _WatchRefreshThread | None = None
        self._watch_refresh_pending = False
        self._watch_refresh_pending_force = False
        self._watch_test_thread: _WatchTestThread | None = None
        self._message_refresh_thread: _MessageRefreshThread | None = None
        self._message_refresh_pending = False
        self._message_refresh_pending_force = False
        self._message_body_thread: _MessageBodyThread | None = None
        self._maintenance_thread: _MaintenanceThread | None = None
        self._next_source_refresh_at: datetime | None = None
        self._next_calendar_refresh_at: datetime | None = None
        self._pending_system_notifications: dict[str, _PendingSystemNotification] = {}
        self._system_notification_batch_active = False
        self._system_notification_batch_suppress_native = False
        self._global_refresh_rendering = False
        self._closed = False
        self._unsubscribe_lifecycle = container.lifecycle.subscribe(self.lifecycle_event.emit)

        self.lifecycle_event.connect(self._handle_lifecycle_event)
        window.refresh_requested.connect(self.refresh_all)
        window.network_pause_changed.connect(self.set_network_paused)
        window.offline_mode_changed.connect(self.set_offline_mode)
        window.quit_requested.connect(application.quit)
        window.task_created.connect(self._task_created)
        window.task_updated.connect(self._task_updated)
        window.task_completed.connect(self._task_completed)
        window.task_archived.connect(self._task_archived)
        window.task_undo_requested.connect(self._task_undo)
        window.task_native_open_requested.connect(self._open_task_native)
        window.task_import_requested.connect(lambda: self._open_import_dialog("tasks"))
        window.task_list_create_requested.connect(self._task_list_create_requested)
        window.today_section_order_changed.connect(self._save_today_order)
        window.today_section_visibility_changed.connect(self._save_today_visibility)
        window.dashboard_layout_changed.connect(self._save_dashboard_layout)
        window.workspace_navigation_changed.connect(self._save_workspace_navigation)
        window.workspace_calendar_views_changed.connect(self._save_workspace_calendar_views)
        window.feed_subscription_create_requested.connect(self._feed_subscription_created)
        window.feed_subscription_update_requested.connect(self._feed_subscription_updated)
        window.feed_subscription_delete_requested.connect(self._feed_subscription_deleted)
        window.feed_subscription_enabled_changed.connect(self._feed_subscription_enabled)
        window.ical_feed_create_requested.connect(self._ical_feed_created)
        window.ical_feed_update_requested.connect(self._ical_feed_updated)
        window.ical_feed_delete_requested.connect(self._ical_feed_deleted)
        window.ical_feed_enabled_changed.connect(self._ical_feed_enabled)
        window.calendar_import_requested.connect(lambda: self._open_import_dialog("calendar"))
        window.calendar_import_delete_requested.connect(self._calendar_import_deleted)
        window.calendar_item_create_requested.connect(self._calendar_item_editor)
        window.calendar_item_edit_requested.connect(self._calendar_item_editor)
        window.calendar_item_delete_requested.connect(self._calendar_item_deleted)
        window.calendar_google_sync_requested.connect(
            lambda: self.refresh_calendars(sync_google=True)
        )
        window.news_entry_read_changed.connect(self._feed_entry_read_changed)
        window.news_filter_changed.connect(self._save_news_filters)
        window.news_subscription_selection_changed.connect(self._news_selection_changed)
        window.news_entry_open_requested.connect(self._open_news_entry)
        window.message_category_changed.connect(self._save_message_category)
        window.watch_create_requested.connect(self._watch_created)
        window.watch_update_requested.connect(self._watch_updated)
        window.watch_delete_requested.connect(self._watch_deleted)
        window.watch_test_requested.connect(self._watch_test_requested)
        window.watch_refresh_requested.connect(self._watch_refresh_requested)
        window.watch_enabled_changed.connect(self._watch_enabled)
        window.watch_open_requested.connect(self._open_watch)
        window.message_open_requested.connect(self._open_message_body)
        window.message_deep_link_requested.connect(self._open_message_link)
        window.message_clear_requested.connect(self._clear_message_summaries)
        window.inbox_state_changed.connect(self._inbox_state_changed)
        window.settings_save_requested.connect(self._settings_save_requested)
        window.backup_requested.connect(self._backup_requested)
        window.restore_requested.connect(self._restore_requested)
        window.export_requested.connect(self._export_requested)
        window.sync_configure_requested.connect(self._sync_configure_requested)
        window.sync_requested.connect(self._sync_requested)
        window.sync_disable_requested.connect(self._sync_disable_requested)
        window.sync_relay_role_changed.connect(self._sync_relay_role_changed)
        window.sync_relay_categories_changed.connect(self._sync_relay_categories_changed)
        window.source_refresh_requested.connect(self._source_refresh_requested)
        window.platform_configure_requested.connect(self._platform_configure_requested)
        window.source_reconnect_requested.connect(self._source_reconnect_requested)
        window.source_enabled_changed.connect(self._source_enabled_changed)
        window.source_disconnect_requested.connect(self._source_disconnect_requested)
        application.aboutToQuit.connect(self.shutdown)
        self.logging_coordinator = LoggingCoordinator(
            container.controller, window, refresh_home=self.render_task_surfaces
        )
        self.render_all()
        window.restore_workspace_mode(container.settings_repository.get("workspace_modes", None))
        window.calendar_mode_changed.connect(self._save_calendar_mode)
        window.calendar_period_changed.connect(self._calendar_period_changed)
        window.calendar_selection_changed.connect(self._calendar_selection_changed)
        # Selecting a calendar item only reveals its in-app details.  The
        # details card emits the separate deep-link request for native opening.
        window.calendar_deep_link_requested.connect(self._open_calendar_event)
        window.calendar_task_completion_changed.connect(self._calendar_task_completion_changed)
        window.calendar_slot_requested.connect(self._calendar_slot_requested)
        self._scheduler_timer = QTimer(self)
        self._scheduler_timer.setInterval(60_000)
        self._scheduler_timer.setSingleShot(False)
        self._scheduler_timer.timeout.connect(self._scheduled_tick)
        self._scheduler_timer.start()
        self._system_notification_timer = QTimer(self)
        self._system_notification_timer.setInterval(0)
        self._system_notification_timer.setSingleShot(True)
        self._system_notification_timer.timeout.connect(self._flush_system_notifications)

    def render_all(self) -> None:
        """Render the durable Phase 1 projections without performing provider I/O."""

        self.container.controller.remove_legacy_message_digests()
        self.window.set_workspace_navigation_preferences(
            self.container.settings_repository.get("workspace_navigation", None)
        )
        self.window.set_workspace_calendar_views(
            self.container.settings_repository.get("workspace_calendar_views", None)
        )
        settings = self.container.controller.settings
        self.window.render_settings(
            SettingsView(
                offline_mode=settings.offline_mode,
                privacy_masking=settings.privacy_masking,
                close_to_tray=settings.close_to_tray,
                start_at_login=settings.start_at_login,
                notifications_enabled=settings.notifications_enabled,
                quiet_hours_enabled=settings.notification_quiet_hours_enabled,
                quiet_hours_start=settings.notification_quiet_hours_start,
                quiet_hours_end=settings.notification_quiet_hours_end,
                default_timezone=settings.default_timezone,
                calendar_refresh_minutes=settings.calendar_refresh_minutes,
                digest_enabled=settings.digest_enabled,
                digest_time=settings.digest_time,
                inbox_retention_days=settings.inbox_retention_days,
                audit_retention_days=settings.audit_retention_days,
                feed_retention_days=settings.feed_retention_days,
                feed_refresh_minutes=settings.feed_refresh_minutes,
                watch_retention_days=settings.watch_observation_retention_days,
                watch_refresh_minutes=settings.watch_refresh_minutes,
                message_retention_days=settings.message_summary_retention_days,
                message_refresh_minutes=settings.message_refresh_minutes,
                remove_completed_tasks_after_due_date=(
                    settings.remove_completed_tasks_after_due_date
                ),
                text_scale=settings.ui_text_scale,
                high_contrast=settings.ui_high_contrast,
                reduced_motion=settings.ui_reduced_motion,
                ui_theme=settings.ui_theme,
                ui_accent_color=settings.ui_accent_color,
                ui_background_color=settings.ui_background_color,
                ui_surface_color=settings.ui_surface_color,
                network_refresh_paused=self.container.lifecycle.network_paused,
            )
        )
        self.window.render_sync_status(self.container.controller.sync_status())
        self.window.render_inbox(
            tuple(_inbox_view(item) for item in self.container.controller.list_inbox())
        )
        self.render_news()
        self.render_web_watches()
        self.render_messages()
        self._reconcile_message_attention_notice()
        self.render_task_surfaces()
        self.render_calendar()
        saved_calendar_mode = self.container.settings_repository.get("calendar_view_mode", "month")
        if saved_calendar_mode in {"month", "day", "week", "agenda"}:
            self.window.set_calendar_mode(saved_calendar_mode)
        self.render_sources()
        self.render_diagnostics()

    def render_task_surfaces(self) -> None:
        self.logging_coordinator.refresh()
        task_lists = self.container.controller.list_task_lists()
        self.window.set_workspace_task_lists(task_lists)
        lists = {str(task_list.list_id): task_list.name for task_list in task_lists}
        self.window.render_tasks(
            tuple(
                _task_view(task, lists=lists)
                for task in self.container.controller.list_tasks(limit=10_000)
            ),
            preserve_undo=True,
        )
        projection = self.container.controller.today_projection()
        section_titles = {
            "overdue": "Overdue",
            "today": "Due today",
            "upcoming": "Upcoming",
            "unscheduled": "Unscheduled",
        }
        task_sections = tuple(
            TodaySectionView(
                section_id=section.value,
                title=section_titles[section.value],
                visible=section.value != "unscheduled",
                items=tuple(_today_item_view(item) for item in items),
            )
            for section, items in projection.sections
        )
        saved_selection = _saved_calendar_selection(self.container)
        calendar_items = self.container.controller.calendar_today_items(
            day=projection.generated_at.astimezone(ZoneInfo(projection.timezone)).date(),
            timezone=projection.timezone,
            selected_calendar_keys=saved_selection,
        )
        news_selection = _saved_news_selection(self.container)
        news_items = self.container.controller.news_today_items(
            selected_subscription_ids=news_selection,
            limit=100,
        )
        watch_items = self.container.controller.watch_today_items(now=projection.generated_at)
        message_items = self.container.controller.message_today_items(limit=100)
        activity_day = projection.generated_at.astimezone(ZoneInfo(projection.timezone)).date()
        activity_facts = self.container.controller.activity_summary(
            LogFilter(start=activity_day, end=activity_day, timezone=projection.timezone)
        )
        activity_count = activity_facts["task_completion"] + activity_facts["manual"]
        sections = (
            TodaySectionView(
                section_id="logged_today",
                title="Logged today",
                visible=False,
                body=f"{activity_count} activity entries today. Open Activity to review.",
            ),
            TodaySectionView(
                section_id="schedule",
                title="Agenda",
                body="No calendar events are scheduled.",
                items=tuple(_today_item_view(item) for item in calendar_items),
            ),
            *task_sections,
            *self._dashboard_attention_sections(),
            TodaySectionView(
                section_id="inbox",
                title="Attention messages",
                body="No unread messages match the selected views.",
                items=tuple(_today_item_view(item) for item in message_items),
            ),
            TodaySectionView(
                section_id="news",
                title="News digest",
                body="No unread feed entries are available.",
                items=tuple(_today_item_view(item) for item in news_items),
            ),
            TodaySectionView(
                section_id="watch_alerts",
                title="Watch alerts",
                body="No watch alerts are available.",
                items=tuple(_today_item_view(item) for item in watch_items),
            ),
        )
        self.window.render_today(
            TodayView(
                sections=sections,
                local_date=projection.generated_at.astimezone(ZoneInfo(projection.timezone))
                .date()
                .isoformat(),
                timezone=projection.timezone,
                generated_at=projection.generated_at,
            )
        )
        saved_dashboard = self.container.settings_repository.get("today_dashboard_layout", None)
        saved_layout_version = (
            saved_dashboard.get("version") if isinstance(saved_dashboard, dict) else None
        )
        if (
            isinstance(saved_layout_version, int)
            and not isinstance(saved_layout_version, bool)
            and saved_layout_version in {1, 2, LAYOUT_VERSION}
        ):
            self.window.restore_dashboard_layout(saved_dashboard)
            if saved_layout_version < LAYOUT_VERSION:
                self._save_dashboard_layout(self.window.dashboard.export_layout())
            return
        saved_order = self.container.settings_repository.get("today_section_order", [])
        if (
            isinstance(saved_order, list)
            and saved_order
            and all(isinstance(item, str) for item in saved_order)
        ):
            self.window.set_today_section_order(tuple(saved_order))
        saved_visibility = self.container.settings_repository.get("today_section_visibility", {})
        if isinstance(saved_visibility, dict):
            for section_id, visible in saved_visibility.items():
                if isinstance(section_id, str) and isinstance(visible, bool):
                    self.window.set_today_section_visibility(section_id, visible)

    def _dashboard_attention_sections(self) -> tuple[TodaySectionView, ...]:
        """Bounded local projections; dashboard layout never stores content."""
        inbox = self.container.controller.list_inbox(state="unread", limit=30)
        watch_items = []
        offline = self.container.controller.settings.offline_mode
        for watch in self.container.controller.list_watches(limit=30):
            state = self.container.watch_repository.sync_state(watch.watch_id)
            status = "Not checked yet" if state is None else state.state.replace("_", " ")
            if not watch.enabled:
                status = "Paused"
            elif offline:
                status = "Offline · showing saved state"
            if state is not None and state.last_checked_at is not None:
                status += " · checked " + state.last_checked_at.astimezone(
                    ZoneInfo(self.container.controller.settings.default_timezone)
                ).strftime("%b %d, %H:%M")
            watch_items.append(
                TodayItemView(
                    item_id=str(watch.watch_id),
                    kind="alert",
                    title=watch.name,
                    summary=status,
                    source=watch.source_url.host or "Watchlists",
                    sensitive=True,
                )
            )
        return (
            TodaySectionView(
                section_id="attention",
                title="Inbox",
                body="You're all caught up.",
                items=tuple(
                    TodayItemView(
                        item_id=item.item_id,
                        kind="alert",
                        title=item.title,
                        summary=item.safe_summary,
                        severity=item.severity,
                        source=item.source.display_name if item.source else "Personal Hub",
                        sensitive=True,
                    )
                    for item in inbox
                ),
            ),
            TodaySectionView(
                section_id="watchlist",
                title="Watchlist",
                body="Add a watch to follow what changes.",
                items=tuple(watch_items),
            ),
        )

    def render_calendar(
        self,
        results: Mapping[str, CalendarResult] | None = None,
    ) -> None:
        """Render the last validated event snapshots without provider objects."""

        selected_results = {
            result.source_id: result for result in self.container.controller.calendar_results()
        }
        if results is not None:
            selected_results.update(
                (source_id, result)
                for source_id, result in results.items()
                if isinstance(result, CalendarResult)
            )
        explicit_selection = _saved_calendar_selection(self.container)
        events: list[CalendarEventView] = []
        calendars: list[CalendarSourceView] = []
        seen_events: set[tuple[str, str, str]] = set()
        seen_calendars: set[str] = set()
        linked_google_ids = (
            self.container.controller.local_calendar.remote_ids()
            if _calendar_selection_applies(
                LOCAL_IMPORT_PROVIDER_ID, str(LOCAL_CALENDAR_ID), explicit_selection
            )
            else frozenset()
        )
        for source_id in self.container.calendar_service.source_ids:
            result = selected_results.get(source_id)
            source = self.container.calendar_service.source(source_id)
            source_name = str(getattr(source, "display_name", source_id))
            if result is None or result.snapshot is None:
                calendar_key = _calendar_selection_key(source_id, "*")
                if calendar_key not in seen_calendars:
                    seen_calendars.add(calendar_key)
                    calendars.append(
                        CalendarSourceView(
                            calendar_id=calendar_key,
                            display_name=source_name,
                            source_id=source_id,
                            source_name=source_name,
                            selected=(
                                _calendar_selection_applies(
                                    source_id,
                                    "*",
                                    explicit_selection,
                                )
                            ),
                            status="disconnected" if result is None else result.state,
                        )
                    )
                continue
            snapshot = result.snapshot
            calendar_ids = set(snapshot.available_calendar_ids or snapshot.selected_calendar_ids)
            calendar_display_names = snapshot.calendar_display_names
            calendar_ids.update(event.calendar_id for event in snapshot.events)
            for calendar_id in sorted(calendar_ids):
                calendar_key = _calendar_selection_key(source_id, calendar_id)
                if calendar_key in seen_calendars:
                    continue
                seen_calendars.add(calendar_key)
                calendars.append(
                    CalendarSourceView(
                        calendar_id=calendar_key,
                        display_name=calendar_display_names.get(calendar_id, calendar_id),
                        source_id=source_id,
                        source_name=source_name,
                        account=_calendar_account_label(snapshot.events, calendar_id),
                        selected=_calendar_selection_applies(
                            source_id,
                            calendar_id,
                            explicit_selection,
                        ),
                        status=result.state,
                    )
                )
            for event in snapshot.events:
                if (
                    snapshot.provider_id == "google-calendar"
                    and (event.calendar_id, event.event_id) in linked_google_ids
                ):
                    continue
                identity = (source_id, event.calendar_id, event.event_id)
                if identity in seen_events:
                    continue
                seen_events.add(identity)
                calendar_key = _calendar_selection_key(source_id, event.calendar_id)
                events.append(
                    CalendarEventView(
                        event_id=_calendar_event_view_id(source_id, event.event_id),
                        title=event.title,
                        starts_at=event.starts_at,
                        ends_at=event.ends_at,
                        all_day=event.all_day,
                        timezone=event.timezone,
                        calendar_id=calendar_key,
                        calendar_name=calendar_display_names.get(
                            event.calendar_id,
                            event.calendar_id,
                        ),
                        source_id=source_id,
                        source_name=event.source.display_name,
                        account=event.source.connection_id or "",
                        location=event.location or "",
                        navigation_target=event.deep_link or "",
                        sensitive=event.sensitive,
                        external_event_id=event.event_id,
                        external_calendar_id=event.calendar_id,
                    )
                )
        query = self._calendar_query()
        local_import_records = self.container.controller.list_calendar_imports()
        local_import_names = {str(item.import_id): item.name for item in local_import_records}
        local_import_views = tuple(
            CalendarImportView(
                import_id=str(item.import_id),
                name=item.name,
                source_format=item.source_format,
                event_count=self.container.controller.count_imported_calendar_events(
                    item.import_id
                ),
            )
            for item in local_import_records
            if item.source_format != "manual"
        )
        local_events = self.container.controller.list_imported_calendar_events(
            starts_at=query.starts_at,
            ends_at=query.ends_at,
            limit=5_000,
        )
        for imported in local_import_records:
            import_id = str(imported.import_id)
            calendar_key = _calendar_selection_key(LOCAL_IMPORT_PROVIDER_ID, import_id)
            if calendar_key not in seen_calendars:
                seen_calendars.add(calendar_key)
                calendars.append(
                    CalendarSourceView(
                        calendar_id=calendar_key,
                        display_name=imported.name,
                        source_id=LOCAL_IMPORT_PROVIDER_ID,
                        source_name="Local calendar"
                        if imported.source_format == "manual"
                        else "Local import",
                        selected=_calendar_selection_applies(
                            LOCAL_IMPORT_PROVIDER_ID,
                            import_id,
                            explicit_selection,
                        ),
                        status="ready",
                        sensitive=True,
                    )
                )
        for imported in local_events:
            import_id = str(imported.import_id)
            identity = (LOCAL_IMPORT_PROVIDER_ID, import_id, str(imported.event_id))
            if identity in seen_events:
                continue
            seen_events.add(identity)
            calendar_key = _calendar_selection_key(LOCAL_IMPORT_PROVIDER_ID, import_id)
            events.append(
                CalendarEventView(
                    event_id=_calendar_event_view_id(
                        LOCAL_IMPORT_PROVIDER_ID,
                        str(imported.event_id),
                    ),
                    title=imported.title,
                    starts_at=imported.starts_at,
                    ends_at=imported.ends_at,
                    all_day=imported.all_day,
                    timezone=imported.timezone,
                    calendar_id=calendar_key,
                    calendar_name=local_import_names.get(import_id, "Local calendar import"),
                    source_id=LOCAL_IMPORT_PROVIDER_ID,
                    source_name="Local calendar"
                    if import_id == str(LOCAL_CALENDAR_ID)
                    else "Local import",
                    location=imported.location,
                    description=imported.description,
                    navigation_target=imported.deep_link,
                    sensitive=True,
                    external_event_id=str(imported.event_id),
                    external_calendar_id=import_id,
                )
            )
        due_tasks = self.container.controller.list_tasks(
            states=("open", "completed"),
            due_after=query.starts_at,
            due_before=query.ends_at,
            include_archived=False,
            limit=1_000,
        )
        events.extend(_calendar_task_view(task) for task in due_tasks if task.due_at is not None)
        state, detail, generated_at = _calendar_summary(
            tuple(selected_results.values()),
            has_sources=bool(self.container.calendar_service.source_ids or local_import_records),
        )
        if local_import_records and not selected_results:
            state = "ready" if local_events else "empty"
            detail = (
                "Showing local imported calendar items."
                if local_events
                else "Local calendar imports have no items in this period."
            )
        if due_tasks and state == "empty":
            state = "ready"
            detail = "Showing Personal Hub to-do due dates."
        if (
            self.container.controller.settings.offline_mode
            and self.container.calendar_service.source_ids
        ):
            state = "offline"
            detail = "Calendar refresh is paused by offline mode."
        ical_feeds = tuple(
            IcalFeedView(
                feed_id=str(feed.feed_id),
                title=feed.title,
                feed_url=str(feed.feed_url),
                enabled=feed.enabled,
                refresh_minutes=feed.refresh_minutes,
                status=(
                    "disconnected"
                    if (
                        sync_state := self.container.ical_feed_manager.repository.sync_state(
                            feed.feed_id
                        )
                    )
                    is None
                    else sync_state.state
                ),
                detail=(
                    "Feed has not been refreshed yet."
                    if sync_state is None
                    else sync_state.safe_error or ""
                ),
                last_success_at=(
                    None if sync_state is None else _format_timestamp(sync_state.last_success_at)
                ),
            )
            for feed in self.container.controller.list_ical_feeds()
        )
        selected_ids = (
            tuple(calendar.calendar_id for calendar in calendars)
            if explicit_selection is None
            else tuple(
                calendar.calendar_id
                for calendar in calendars
                if _calendar_selection_key_applies(calendar.calendar_id, explicit_selection)
            )
        )
        self.window.render_calendar(
            CalendarView(
                events=tuple(sorted(events, key=_calendar_event_sort_key)),
                calendars=tuple(calendars),
                selected_calendar_ids=selected_ids,
                timezone=self.container.controller.settings.default_timezone,
                freshness=state,
                detail=detail,
                generated_at=generated_at,
                cursor_date=self.window.current_calendar_date,
                ical_feeds=ical_feeds,
                local_imports=local_import_views,
            )
        )

    def render_news(self) -> None:
        subscriptions = self.container.controller.list_feed_subscriptions(limit=10_000)
        sync_states = {
            subscription.subscription_id: self.container.feed_subscription_repository.sync_state(
                subscription.subscription_id
            )
            for subscription in subscriptions
        }
        explicit_selection = _saved_news_selection(self.container)
        subscription_views = tuple(
            FeedSubscriptionView(
                subscription_id=str(subscription.subscription_id),
                title=subscription.title,
                feed_url=str(subscription.feed_url),
                site_url="" if subscription.site_url is None else str(subscription.site_url),
                enabled=subscription.enabled,
                refresh_minutes=subscription.refresh_minutes,
                include_terms=subscription.include_terms,
                exclude_terms=subscription.exclude_terms,
                digest_only=subscription.digest_only,
                status=(
                    "disconnected"
                    if sync_states[subscription.subscription_id] is None
                    else sync_states[subscription.subscription_id].state
                ),
                detail=(
                    "Feed has not been refreshed yet."
                    if sync_states[subscription.subscription_id] is None
                    else sync_states[subscription.subscription_id].safe_error or ""
                ),
                last_success_at=(
                    None
                    if sync_states[subscription.subscription_id] is None
                    else _format_timestamp(
                        sync_states[subscription.subscription_id].last_success_at
                    )
                ),
                unread_count=self.container.feed_entry_repository.count(
                    subscription_id=subscription.subscription_id,
                    state="unread",
                ),
                entry_count=self.container.feed_entry_repository.count(
                    subscription_id=subscription.subscription_id
                ),
                source_id=f"feed.{subscription.subscription_id.hex}",
                source_name="RSS/Atom",
                sensitive=True,
            )
            for subscription in subscriptions
        )
        subscription_map = {
            subscription.subscription_id: subscription for subscription in subscriptions
        }
        entry_views: list[FeedEntryView] = []
        for record in self.container.controller.list_feed_entries(limit=1_000):
            entry = record.entry
            subscription = subscription_map.get(entry.subscription_id)
            if subscription is None:
                continue
            entry_views.append(
                FeedEntryView(
                    entry_id=entry.entry_id,
                    subscription_id=str(entry.subscription_id),
                    title=entry.title,
                    url=str(entry.url),
                    author=entry.author or "",
                    published_at=entry.published_at,
                    summary_text=entry.summary_text or "",
                    first_seen_at=entry.first_seen_at,
                    read=record.state == "read",
                    source_name=subscription.title,
                    source_url=str(subscription.feed_url),
                    navigation_target=str(entry.url),
                    sensitive=True,
                )
            )
        state, detail, generated_at = _news_summary(
            tuple(value for value in sync_states.values() if value is not None),
            has_subscriptions=bool(subscriptions),
        )
        if self.container.controller.settings.offline_mode and subscriptions:
            state = "offline"
            detail = "Feed refresh is paused by offline mode."
        selected_ids = (
            tuple(
                str(subscription.subscription_id)
                for subscription in subscriptions
                if subscription.enabled
            )
            if explicit_selection is None
            else tuple(
                str(subscription.subscription_id)
                for subscription in subscriptions
                if str(subscription.subscription_id) in explicit_selection
            )
        )
        include_terms, exclude_terms, unread_only = _saved_news_filters(self.container)
        self.window.render_news(
            NewsView(
                subscriptions=subscription_views,
                entries=tuple(entry_views),
                selected_subscription_ids=selected_ids,
                selection_explicit=explicit_selection is not None,
                include_terms=include_terms,
                exclude_terms=exclude_terms,
                unread_only=unread_only,
                freshness=state,
                detail=detail,
                generated_at=generated_at,
                max_entries=100,
            )
        )

    def render_web_watches(self) -> None:
        definitions = self.container.controller.list_watches(limit=10_000)
        relay_enabled = self.container.controller.provider_relay_category_enabled("watches")
        watches: list[WebWatchView] = []
        states = []
        for definition in definitions:
            state = (
                self.container.watch_repository.sync_state(definition.watch_id)
                if relay_enabled
                else None
            )
            if state is not None:
                states.append(state)
            watches.append(
                WebWatchView(
                    watch_id=str(definition.watch_id),
                    name=definition.name,
                    kind=definition.kind,
                    source_url=str(definition.source_url),
                    condition_kind=definition.condition.kind,
                    threshold=definition.condition.threshold,
                    percentage=definition.condition.percentage,
                    term=definition.condition.term or "",
                    selector=definition.condition.selector or "",
                    refresh_minutes=definition.refresh_minutes,
                    enabled=definition.enabled,
                    notification_policy=definition.notification_policy,
                    cooldown_minutes=definition.cooldown_minutes,
                    allow_http=definition.allow_http,
                    status="stale" if state is None else state.state,
                    detail=(
                        "Watch snapshot sharing is disabled on this device."
                        if not relay_enabled
                        else "Watch has not been checked yet."
                        if state is None
                        else state.safe_error or ""
                    ),
                    last_checked_at=None if state is None else state.last_checked_at,
                    last_value="" if state is None else _watch_value_text(state.baseline_value),
                    sensitive=True,
                )
            )
        alerts = tuple(
            WatchAlertView(
                alert_id=str(alert.alert_id),
                watch_id=str(alert.watch_id),
                title=alert.title,
                summary=alert.safe_summary,
                reason_code=alert.reason_code,
                observed_at=alert.observed_at,
                sensitive=True,
            )
            for alert in self.container.controller.watch_alerts(limit=1_000)
        )
        state, detail = _watch_summary(states, has_watches=bool(definitions))
        if self.container.controller.settings.offline_mode and definitions:
            state, detail = "offline", "Watch refresh is paused by offline mode."
        self.window.render_web_watches(
            WebWatchesView(
                watches=tuple(watches),
                alerts=alerts,
                freshness=state,
                detail=detail,
            )
        )

    def render_messages(self) -> None:
        """Render configured-source summaries; never request message bodies."""

        configs = self.container.controller.list_message_sources(limit=1_000)
        states = {
            config.source_id: self.container.message_repository.sync_state(config.source_id)
            for config in configs
        }
        source_views = tuple(
            MessageSourceView(
                source_id=config.source_id,
                provider=config.provider,
                account=config.account_id,
                selection=_message_selection_text(config.query),
                status=(
                    "stale" if states[config.source_id] is None else states[config.source_id].state
                ),
                detail=(
                    "Message source has not been refreshed yet."
                    if states[config.source_id] is None
                    else states[config.source_id].safe_error or ""
                ),
                enabled=config.enabled,
                sensitive=True,
            )
            for config in configs
        )
        summary_views = tuple(
            MessageSummaryView(
                source_id=summary.source_id,
                message_id=summary.message_id,
                provider=summary.provider,
                account=summary.account_id,
                sender=summary.sender,
                title=summary.title,
                preview=summary.preview or "",
                received_at=summary.received_at,
                unread=summary.unread,
                importance=summary.importance,
                category=summary.category,
                deep_link=summary.deep_link or "",
                sensitive=True,
            )
            for summary in self.container.controller.list_message_summaries(limit=200)
        )
        state, detail = _message_summary(
            tuple(value for value in states.values() if value is not None),
            has_sources=bool(configs),
        )
        if self.container.controller.settings.offline_mode and configs:
            state, detail = "offline", "Message refresh is paused by offline mode."
        self.window.render_messages(
            MessagesView(
                sources=source_views,
                summaries=summary_views,
                selected_category=_saved_message_category(self.container),
                freshness=state,
                detail=detail,
            )
        )

    @Slot(object)
    def _watch_created(self, value: object) -> None:
        if not isinstance(value, Mapping):
            return
        try:
            watch = self.container.controller.create_watch(
                **_watch_changes(value, include_identity=False)
            )
        except (TypeError, ValueError):
            self.window.render_watch_test_result(
                {"success": False, "message": "The watch could not be saved safely."}
            )
            return
        self.render_web_watches()
        self.refresh_watches((watch.watch_id,), force=True)

    @Slot(object)
    def _watch_updated(self, value: object) -> None:
        if not isinstance(value, Mapping) or not isinstance(value.get("watch_id"), str):
            return
        identifier = str(value["watch_id"])
        try:
            watch = self.container.controller.update_watch(
                identifier,
                **_watch_changes(value, include_identity=False),
            )
        except (TypeError, ValueError):
            self.window.render_watch_test_result(
                {"success": False, "message": "The watch could not be updated safely."}
            )
            return
        if watch is not None:
            self.render_web_watches()
            self.refresh_watches((watch.watch_id,), force=True)

    @Slot(object)
    def _watch_deleted(self, value: object) -> None:
        if not isinstance(value, Mapping) or not isinstance(value.get("watch_id"), str):
            return
        with suppress(TypeError, ValueError):
            self.container.controller.delete_watch(str(value["watch_id"]))
        self.render_web_watches()
        self.render_task_surfaces()

    @Slot(object)
    def _watch_enabled(self, value: object) -> None:
        if not isinstance(value, Mapping):
            return
        identifier, enabled = value.get("watch_id"), value.get("enabled")
        if not isinstance(identifier, str) or not isinstance(enabled, bool):
            return
        with suppress(TypeError, ValueError):
            self.container.controller.set_watch_enabled(identifier, enabled)
        self.render_web_watches()

    @Slot(object)
    def _watch_test_requested(self, value: object) -> None:
        if self._closed or self._watch_test_thread is not None or not isinstance(value, Mapping):
            return
        if not self.container.lifecycle.network_refresh_allowed:
            self.window.render_watch_test_result(
                {"success": False, "message": "Watch testing is paused by offline mode."}
            )
            return
        worker = _WatchTestThread(self.container, value, self)
        self._watch_test_thread = worker
        worker.result_ready.connect(self._watch_test_completed)
        worker.test_failed.connect(self._watch_test_failed)
        worker.finished.connect(self._watch_test_finished)
        worker.start()

    @Slot(object)
    def _watch_test_completed(self, value: object) -> None:
        if self._closed:
            return
        if not isinstance(value, WatchObservation):
            self._watch_test_failed()
            return
        visible = (
            "Hidden by privacy settings"
            if self.window.privacy_masked
            else _watch_value_text(value.normalized_value)
        )
        self.window.render_watch_test_result(
            {"success": True, "message": "Extraction succeeded.", "value": visible}
        )

    @Slot()
    def _watch_test_failed(self) -> None:
        if self._closed:
            return
        self.window.render_watch_test_result(
            {"success": False, "message": "Watch extraction failed safely."}
        )

    @Slot()
    def _watch_test_finished(self) -> None:
        worker, self._watch_test_thread = self._watch_test_thread, None
        if worker is not None:
            worker.deleteLater()

    @Slot(object)
    def _watch_refresh_requested(self, value: object) -> None:
        identifier = value.get("watch_id") if isinstance(value, Mapping) else None
        try:
            selected = None if identifier is None else (UUID(str(identifier)),)
        except ValueError:
            return
        self.refresh_watches(selected, force=True)

    @Slot(object)
    def _open_watch(self, value: object) -> None:
        if not isinstance(value, WebWatchView):
            return
        target = QUrl(value.source_url)
        if target.isValid() and target.scheme().casefold() in {"http", "https"} and target.host():
            QDesktopServices.openUrl(target)

    @Slot(object)
    def _task_created(self, view: object) -> None:
        if not isinstance(view, TaskView):
            return
        with suppress(TypeError, ValueError):
            self.container.controller.create_task(**self._task_changes(view, include_identity=True))
        self.render_task_surfaces()
        self.render_calendar()

    @Slot(object)
    def _task_updated(self, view: object) -> None:
        if not isinstance(view, TaskView):
            return
        with suppress(TypeError, ValueError):
            self.container.controller.update_task(
                view.task_id,
                **self._task_changes(view, include_identity=False),
            )
        self.render_task_surfaces()
        self.render_calendar()

    @Slot(object)
    def _task_completed(self, view: object) -> None:
        if isinstance(view, TaskView):
            self.container.controller.complete_task(view.task_id, completed_at=view.completed_at)
        self.render_task_surfaces()
        self.render_calendar()

    @Slot(object)
    def _task_archived(self, view: object) -> None:
        if isinstance(view, TaskView):
            self.container.controller.archive_task(view.task_id, at=view.updated_at)
        self.render_task_surfaces()
        self.render_calendar()

    @Slot(object)
    def _task_undo(self, view: object) -> None:
        if isinstance(view, TaskView):
            self.container.controller.undo_task(view.task_id)
        self.render_task_surfaces()
        self.render_calendar()

    @Slot(object)
    def _open_task_native(self, value: object) -> None:
        if not isinstance(value, TaskView) or not value.external_url:
            return
        target = QUrl(value.external_url)
        if (
            target.isValid()
            and target.scheme().casefold() in {"http", "https"}
            and target.host()
            and not target.userName()
            and not target.password()
        ):
            QDesktopServices.openUrl(target)

    @Slot(object)
    def _save_dashboard_layout(self, layout: object) -> None:
        if isinstance(layout, Mapping):
            self.container.settings_repository.set(
                "today_dashboard_layout", self.window.dashboard.export_layout()
            )

    @Slot(object)
    def _save_today_order(self, order: object) -> None:
        if isinstance(order, (tuple, list)) and all(isinstance(item, str) for item in order):
            self.container.settings_repository.set("today_section_order", list(order))

    @Slot(object)
    def _save_today_visibility(self, change: object) -> None:
        if not isinstance(change, Mapping):
            return
        current = self.container.settings_repository.get("today_section_visibility", {})
        values = dict(current) if isinstance(current, dict) else {}
        for section_id, visible in change.items():
            if isinstance(section_id, str) and isinstance(visible, bool):
                values[section_id] = visible
        self.container.settings_repository.set("today_section_visibility", values)

    @Slot(object)
    def _feed_subscription_created(self, value: object) -> None:
        if not isinstance(value, Mapping):
            return
        try:
            subscription = self.container.controller.create_feed_subscription(
                **_feed_subscription_changes(value, include_identity=False)
            )
        except (TypeError, ValueError):
            self._feed_editor_error("The feed subscription could not be saved safely.")
            return
        selection = _saved_news_selection(self.container)
        if selection is not None and str(subscription.subscription_id) not in selection:
            self.container.settings_repository.set(
                "news_selected_subscription_ids",
                [*selection, str(subscription.subscription_id)],
            )
        self.render_news()
        self.render_task_surfaces()
        self.refresh_feeds((subscription.subscription_id,), force=True)

    @Slot(object)
    def _feed_subscription_updated(self, value: object) -> None:
        if not isinstance(value, Mapping):
            return
        identifier = value.get("subscription_id")
        if not isinstance(identifier, str):
            return
        try:
            updated = self.container.controller.update_feed_subscription(
                identifier,
                **_feed_subscription_changes(value, include_identity=False),
            )
        except (TypeError, ValueError):
            self._feed_editor_error("The feed subscription could not be updated safely.")
            return
        if updated is None:
            self._feed_editor_error("The selected feed no longer exists.")
            return
        self.render_news()
        self.render_task_surfaces()
        self.refresh_feeds((updated.subscription_id,), force=True)

    @Slot(object)
    def _feed_subscription_deleted(self, value: object) -> None:
        if not isinstance(value, Mapping):
            return
        identifier = value.get("subscription_id")
        if not isinstance(identifier, str):
            return
        with suppress(TypeError, ValueError):
            self.container.controller.delete_feed_subscription(identifier)
        selection = _saved_news_selection(self.container)
        if selection is not None:
            self.container.settings_repository.set(
                "news_selected_subscription_ids",
                [item for item in selection if item != identifier],
            )
        self.render_news()
        self.render_task_surfaces()

    @Slot(object)
    def _feed_subscription_enabled(self, value: object) -> None:
        if not isinstance(value, Mapping):
            return
        identifier = value.get("subscription_id")
        enabled = value.get("enabled")
        if not isinstance(identifier, str) or not isinstance(enabled, bool):
            return
        with suppress(TypeError, ValueError):
            self.container.controller.set_feed_subscription_enabled(identifier, enabled)
        self.render_news()
        self.render_task_surfaces()

    @Slot()
    def _ical_feed_created(self) -> None:
        dialog = IcalFeedDialog(parent=self.window)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        payload = dialog.feed_payload()
        if payload is None:
            return
        try:
            feed = self.container.controller.create_ical_feed(**payload)
        except (TypeError, ValueError, RuntimeError):
            self.window.calendar_status_label.setText(
                "The iCalendar feed could not be imported safely."
            )
            return
        selected = _saved_calendar_selection(self.container)
        if selected is not None:
            source_id = self.container.ical_feed_manager.source_id_for(feed.feed_id)
            wildcard = _calendar_selection_key(source_id, "*")
            if wildcard not in selected:
                self.container.settings_repository.set(
                    "calendar_selected_ids",
                    [*selected, wildcard],
                )
        self.render_calendar()
        self.render_task_surfaces()
        self.refresh_calendars()

    @Slot(str)
    def _ical_feed_updated(self, feed_id: str) -> None:
        current = self.container.controller.ical_feed_manager.get(feed_id)
        if current is None:
            self.window.calendar_status_label.setText(
                "The selected iCalendar feed no longer exists."
            )
            return
        dialog = IcalFeedDialog(current=current, parent=self.window)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        payload = dialog.feed_payload()
        if payload is None:
            return
        try:
            updated = self.container.controller.update_ical_feed(feed_id, **payload)
        except (TypeError, ValueError, RuntimeError):
            self.window.calendar_status_label.setText(
                "The iCalendar feed could not be updated safely."
            )
            return
        if updated is None:
            self.window.calendar_status_label.setText(
                "The selected iCalendar feed no longer exists."
            )
            return
        self.render_calendar()
        self.render_task_surfaces()
        self.refresh_calendars()

    @Slot(str)
    def _ical_feed_deleted(self, feed_id: str) -> None:
        try:
            deleted = self.container.controller.delete_ical_feed(feed_id)
        except (TypeError, ValueError, RuntimeError):
            deleted = False
        if not deleted:
            return
        source_id = self.container.ical_feed_manager.source_id_for(feed_id)
        selected = _saved_calendar_selection(self.container)
        if selected is not None:
            self.container.settings_repository.set(
                "calendar_selected_ids",
                [
                    item
                    for item in selected
                    if _parse_calendar_selection_key(item) is None
                    or _parse_calendar_selection_key(item)[0] != source_id
                ],
            )
        self.render_calendar()
        self.render_task_surfaces()

    @Slot(object)
    def _ical_feed_enabled(self, value: object) -> None:
        if not isinstance(value, Mapping):
            return
        feed_id = value.get("feed_id")
        enabled = value.get("enabled")
        if not isinstance(feed_id, str) or type(enabled) is not bool:
            return
        try:
            updated = self.container.controller.set_ical_feed_enabled(feed_id, enabled)
        except (TypeError, ValueError, RuntimeError):
            updated = None
        if updated is None:
            return
        self.render_calendar()
        self.render_task_surfaces()
        if updated.enabled:
            self.refresh_calendars()

    @Slot(str)
    def _open_import_dialog(self, default_target: str) -> None:
        dialog = ImportDialog(default_target=default_target, parent=self.window)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        payload = dialog.payload()
        if payload is None:
            return
        import_payload = dict(payload)
        content = import_payload.pop("content", None)
        if not isinstance(content, str):
            if default_target == "calendar":
                self.window.calendar_status_label.setText(
                    "The import did not contain readable text."
                )
            else:
                self.window.task_status_label.setText("The import did not contain readable text.")
            return
        import_payload["content_or_path"] = content
        try:
            result = self.container.controller.import_items(**import_payload)
        except (TypeError, ValueError, RuntimeError):
            message = "The import could not be validated or saved safely."
            if default_target == "calendar":
                self.window.calendar_status_label.setText(message)
            else:
                self.window.task_status_label.setText(message)
            return
        if result.target == "calendar" and result.import_id:
            selected = _saved_calendar_selection(self.container)
            if selected is not None:
                local_key = _calendar_selection_key(LOCAL_IMPORT_PROVIDER_ID, result.import_id)
                local_wildcard = _calendar_selection_key(LOCAL_IMPORT_PROVIDER_ID, "*")
                if (
                    local_key not in selected
                    and local_wildcard not in selected
                    and LOCAL_IMPORT_PROVIDER_ID not in selected
                ):
                    self.container.settings_repository.set(
                        "calendar_selected_ids",
                        [*selected, local_key],
                    )
        summary = _import_result_summary(result)
        if result.target == "calendar":
            self.render_calendar()
            self.render_task_surfaces()
            self.window.calendar_status_label.setText(summary)
        else:
            self.render_task_surfaces()
            self.render_calendar()
            self.window.task_status_label.setText(summary)

    @Slot(str)
    def _calendar_import_deleted(self, import_id: str) -> None:
        try:
            deleted = self.container.controller.delete_calendar_import(import_id)
        except (TypeError, ValueError, RuntimeError):
            deleted = False
        if not deleted:
            return
        selected = _saved_calendar_selection(self.container)
        if selected is not None:
            self.container.settings_repository.set(
                "calendar_selected_ids",
                [
                    item
                    for item in selected
                    if _parse_calendar_selection_key(item) is None
                    or _parse_calendar_selection_key(item) != (LOCAL_IMPORT_PROVIDER_ID, import_id)
                ],
            )
        self.render_calendar()
        self.render_task_surfaces()

    @Slot()
    @Slot(object)
    def _calendar_item_editor(self, value: object = None) -> None:
        if self._closed or self.window.locked:
            return
        if isinstance(value, CalendarEventView) and self.window.privacy_masked:
            self.window.calendar_status_label.setText(
                "Turn off privacy masking to edit this calendar item."
            )
            return
        local = self.container.controller.local_calendar
        current = None
        if isinstance(value, CalendarEventView):
            if value.external_calendar_id != str(LOCAL_CALENDAR_ID):
                return
            current = local.get(value.external_event_id)
            if current is None:
                return
        binding = local.binding(current.event_id) if current else None
        config = self.container.platform_connections.configuration("google")
        destinations: list[tuple[str, str]] = []
        if config and config.enabled and config.calendar_sync_enabled:
            for result in self.container.controller.calendar_results():
                if result.provider_id == "google-calendar" and result.snapshot:
                    destinations.extend(
                        (
                            identifier,
                            result.snapshot.calendar_display_names.get(identifier, identifier),
                        )
                        for identifier in result.snapshot.available_calendar_ids
                    )
            if not destinations:
                destinations.append(("primary", "Primary calendar"))
        dialog = CalendarItemDialog(
            day=self.window.current_calendar_date,
            timezone=self.container.controller.settings.default_timezone,
            current=current,
            binding=binding,
            google_calendars=tuple(destinations),
            parent=self.window,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        fields = dialog.payload()
        resolution = fields.pop("conflict_resolution", None)
        try:
            if current is None:
                event = self.container.controller.create_calendar_item(**fields)
            else:
                event = self.container.controller.update_calendar_item(current.event_id, **fields)
            if resolution is not None and binding is not None:
                local.save_binding(binding.model_copy(update={"resolution": resolution}))
            if fields.get("google_calendar_id") is None and binding is not None:
                local.stop_sync(event.event_id)
        except (TypeError, ValueError):
            self.window.calendar_status_label.setText(
                "The calendar item could not be saved. Check its dates and fields."
            )
            return
        selected = _saved_calendar_selection(self.container)
        key = _calendar_selection_key(LOCAL_IMPORT_PROVIDER_ID, str(event.import_id))
        if selected is not None and key not in selected:
            self.container.settings_repository.set("calendar_selected_ids", [*selected, key])
        self.render_calendar()
        self.render_task_surfaces()
        self.window.calendar_status_label.setText(
            "Calendar item saved locally. Choose Sync Google to sync selected items."
        )

    @Slot(object)
    def _calendar_item_deleted(self, value: object) -> None:
        if self._closed or self.window.locked:
            return
        if not isinstance(value, CalendarEventView) or value.external_calendar_id != str(
            LOCAL_CALENDAR_ID
        ):
            return
        self.container.controller.delete_calendar_item(value.external_event_id)
        self.render_calendar()
        self.render_task_surfaces()
        self.window.calendar_status_label.setText(
            "Calendar item removed locally. Its Google copy is retained."
        )

    @Slot(object)
    def _calendar_sync_completed(self, value: object) -> None:
        if not isinstance(value, CalendarSyncReport) or self._closed:
            return
        self.window.calendar_status_label.setText(
            f"{value.message} Sent {value.uploaded}, received {value.downloaded}, "
            f"conflicts {value.conflicts}, removed at Google {value.remote_deleted}, failed {value.failed}. "
            "Edit a conflicting local item to choose which version to use."
        )
        self.render_task_surfaces()

    @Slot(object)
    def _feed_entry_read_changed(self, value: object) -> None:
        if not isinstance(value, Mapping):
            return
        entry_id = value.get("entry_id")
        read = value.get("read")
        if not isinstance(entry_id, str) or not isinstance(read, bool):
            return
        self.container.controller.set_feed_entry_read(entry_id, read)
        self.render_news()
        self.render_task_surfaces()

    @Slot(object)
    def _save_news_filters(self, value: object) -> None:
        if not isinstance(value, Mapping):
            return
        include_terms = _bounded_text_sequence(value.get("include_terms"), max_items=64)
        exclude_terms = _bounded_text_sequence(value.get("exclude_terms"), max_items=64)
        unread_only = value.get("unread_only", False)
        if not isinstance(unread_only, bool):
            return
        self.container.settings_repository.set(
            "news_view_filters",
            {
                "include_terms": list(include_terms),
                "exclude_terms": list(exclude_terms),
                "unread_only": unread_only,
            },
        )

    @Slot(str)
    def _save_message_category(self, value: str) -> None:
        if value in MESSAGE_CATEGORY_VALUES:
            self.container.settings_repository.set("message_category_filter", value)

    @Slot(object)
    def _news_selection_changed(self, value: object) -> None:
        selected = _bounded_text_sequence(value, max_items=1_000)
        self.container.settings_repository.set(
            "news_selected_subscription_ids",
            list(selected),
        )
        self.render_task_surfaces()

    @Slot(object)
    def _open_news_entry(self, value: object) -> None:
        if not isinstance(value, FeedEntryView) or not value.navigation_target:
            return
        target = QUrl(value.navigation_target)
        if target.isValid() and target.scheme().casefold() in {"http", "https"} and target.host():
            QDesktopServices.openUrl(target)

    def _feed_editor_error(self, message: str) -> None:
        label = getattr(self.window, "news_subscription_editor_status", None)
        if label is not None:
            label.setText(message)

    def _task_changes(self, view: TaskView, *, include_identity: bool) -> dict[str, object]:
        values: dict[str, object] = {
            "title": view.title,
            "notes": view.notes,
            "external_url": view.external_url or None,
            "priority": view.priority,
            "due_at": view.due_at,
            "due_is_all_day": view.due_is_all_day,
            "timezone": (
                self.container.controller.settings.default_timezone
                if view.timezone in {"", "local"}
                else view.timezone
            ),
            "list_id": self._resolve_task_list(view.list_key or view.list_id),
            "tags": view.tags,
            "recurrence": _recurrence(view.recurrence),
            "reminder_offsets_minutes": view.reminder_offsets_minutes,
        }
        if include_identity:
            values.update(
                {
                    "task_id": UUID(view.task_id),
                    "created_at": view.created_at or datetime.now().astimezone(),
                    "updated_at": view.updated_at or datetime.now().astimezone(),
                }
            )
        return values

    @Slot()
    def _task_list_create_requested(self) -> None:
        name, accepted = QInputDialog.getText(
            self.window,
            "Create task list",
            "Task list name:",
        )
        if not accepted:
            return
        try:
            normalized = " ".join(name.split())
            if not normalized:
                raise ValueError("Task list name cannot be blank")
            task_list = next(
                (
                    item
                    for item in self.container.controller.list_task_lists()
                    if item.name.casefold() == normalized.casefold()
                ),
                None,
            )
            if task_list is None:
                task_list = self.container.controller.create_task_list(normalized)
        except (TypeError, ValueError, RuntimeError):
            self.window.task_status_label.setText(
                "That task list could not be created. Choose a different name."
            )
            return
        self.render_task_surfaces()
        self.window.open_workspace_collection("task-list", str(task_list.list_id))

    def _resolve_task_list(self, value: str | None) -> UUID | None:
        if not value:
            return None
        try:
            identifier = UUID(value)
        except ValueError:
            normalized = " ".join(value.split())
            for task_list in self.container.controller.list_task_lists():
                if task_list.name.casefold() == normalized.casefold():
                    return task_list.list_id
            return self.container.controller.create_task_list(normalized).list_id
        return identifier

    def render_sources(
        self,
        results: Mapping[str, SourceResult] | None = None,
    ) -> None:
        selected = dict(results or {})
        for result in self.container.controller.list_source_results():
            selected.setdefault(result.source_id, result)
        definitions = {
            definition.source_id: definition
            for definition in self.container.controller.list_sources()
        }
        source_ids = sorted(set(definitions) | set(selected))
        views: list[SourceView] = []
        for source_id in source_ids:
            result = selected.get(source_id)
            definition = definitions.get(source_id)
            if result is None:
                views.append(
                    SourceView(
                        source_id=source_id,
                        display_name=(
                            definition.display_name if definition is not None else source_id
                        ),
                        status="disconnected",
                        detail="Source has not been refreshed yet.",
                        enabled=definition.enabled if definition is not None else True,
                    )
                )
                continue
            views.append(
                _source_view(
                    result,
                    enabled=definition.enabled if definition is not None else True,
                )
            )
        self.window.render_sources(views)

    @Slot(str)
    def _source_refresh_requested(self, source_id: str) -> None:
        self.refresh_sources((source_id,))

    @Slot(str)
    def _platform_configure_requested(self, source_id: str) -> None:
        try:
            platform_id = self.container.platform_connections.platform_id(source_id)
            definition = next(
                item
                for item in self.container.platform_connections.definitions()
                if item.platform_id == platform_id
            )
        except (KeyError, StopIteration):
            self.window.set_sources_status("The selected source has no platform setup flow.")
            return
        dialog = ExternalPlatformDialog(
            definition,
            self.container.platform_connections.configuration(platform_id),
            self.window,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        payload = dialog.connection_payload()
        if payload is None:
            return
        try:
            self.container.platform_connections.configure(platform_id, payload)
            self.container.controller.set_source_enabled(source_id, True)
        except Exception:
            # Vault and validation details must not cross the presentation boundary.
            self.window.set_sources_status("Connection setup could not be saved safely.")
            self.render_sources()
            return
        if platform_id == "canvas":
            status_message = "Importing Canvas calendar items as due-dated to-dos…"
        elif platform_id == "slack":
            status_message = "Checking the read-only Slack connection…"
        else:
            status_message = "Connection started. Complete sign-in in your browser when prompted."
        self.window.set_sources_status(status_message)
        self.render_sources()
        self._start_source_operation((source_id,), action="connect")

    @Slot(str)
    def _source_reconnect_requested(self, source_id: str) -> None:
        self._start_source_operation((source_id,), action="connect")

    @Slot(object)
    def _source_enabled_changed(self, value: object) -> None:
        if (
            not isinstance(value, (tuple, list))
            or len(value) != 2
            or not isinstance(value[0], str)
            or type(value[1]) is not bool
        ):
            return
        with suppress(KeyError, TypeError, ValueError):
            self.container.controller.set_source_enabled(value[0], value[1])
            if value[0].startswith("platform-"):
                self.container.platform_connections.set_enabled(value[0], value[1])
        self.render_sources()

    @Slot(str)
    def _source_disconnect_requested(self, source_id: str) -> None:
        self._start_source_operation((source_id,), action="disconnect")

    def render_diagnostics(self) -> None:
        report = self.container.controller.diagnostics_report()
        failures = tuple(
            SourceFailureView(
                source_id=source.source_id,
                display_name=source.provider_id,
                message=source.safe_message,
                retryable=source.state
                not in {SourceState.PERMISSION_REQUIRED, SourceState.DISCONNECTED},
                last_success_at=_format_timestamp(source.last_success_at),
            )
            for source in report.sources
            if source.state
            in {
                SourceState.STALE,
                SourceState.PERMISSION_REQUIRED,
                SourceState.DISCONNECTED,
                SourceState.ERROR,
            }
        )
        self.window.render_diagnostics(
            DiagnosticsView(
                status="attention" if failures else "ready",
                detail=report.safe_summary,
                entries=(
                    f"Application {report.application_version}",
                    f"Database schema {report.schema_version}",
                    f"Sources {len(report.sources)}",
                    f"Notification records {len(report.notifications)}",
                ),
                source_failures=failures,
                generated_at=_format_timestamp(report.generated_at),
            )
        )

    @Slot(object)
    def _inbox_state_changed(self, value: object) -> None:
        if (
            not isinstance(value, (tuple, list))
            or len(value) != 2
            or not all(isinstance(item, str) for item in value)
        ):
            return
        item_id, state = value
        if state not in {"unread", "read", "dismissed", "archived"}:
            return
        self.container.controller.set_inbox_state(item_id, state)
        self.window.render_inbox(
            tuple(_inbox_view(item) for item in self.container.controller.list_inbox())
        )

    @Slot(object)
    def _settings_save_requested(self, value: object) -> None:
        if not isinstance(value, Mapping):
            return
        current = self.container.controller.settings
        try:
            desired = type(current).model_validate({**current.model_dump(), **dict(value)})
            if desired.start_at_login != current.start_at_login:
                setter = getattr(self._startup_registration, "set_enabled", None)
                if not callable(setter):
                    raise RuntimeError("Startup registration is unavailable")
                setter(desired.start_at_login)
            updated = self.container.controller.update_settings(desired)
        except (OSError, RuntimeError, TypeError, ValueError):
            message = (
                "Settings were not saved. Check timezone, time, retention, and startup values."
            )
            self.window.set_settings_status(message)
            self.window.set_settings_save_status(message)
            return
        self.container.settings = updated
        self.render_all()
        self.window.set_settings_status("Settings saved.")
        self.window.set_settings_save_status("Settings saved.")

    @Slot(object)
    def _save_workspace_navigation(self, value: object) -> None:
        if not isinstance(value, Mapping):
            return
        self.container.settings_repository.set("workspace_navigation", dict(value))

    @Slot(object)
    def _save_workspace_calendar_views(self, value: object) -> None:
        if not isinstance(value, Mapping):
            return
        self.container.settings_repository.set("workspace_calendar_views", dict(value))

    @Slot(str)
    def _sync_configure_requested(self, endpoint: str) -> None:
        try:
            self.container.controller.configure_sync(endpoint)
        except (OSError, RuntimeError, TypeError, ValueError):
            self.window.set_settings_status("The sync folder could not be configured safely.")
            return
        self.window.render_sync_status(self.container.controller.sync_status())
        self.window.set_settings_status(
            "Encrypted sync folder saved. Enter the same passphrase on each device."
        )

    @Slot(str)
    def _sync_requested(self, passphrase: str) -> None:
        if not self.window.logging_workspace.flush():
            return
        try:
            result = self.container.controller.sync_now(passphrase)
        except (OSError, RuntimeError, TypeError, ValueError, SyncError):
            self.window.clear_sync_passphrase()
            self.window.render_sync_status(self.container.controller.sync_status())
            self.window.set_settings_status(
                "Sync failed safely. Check the shared folder and passphrase."
            )
            return
        self.window.clear_sync_passphrase()
        self.render_all()
        sync_status = self.container.controller.sync_status()
        self.window.render_sync_status(sync_status)
        if sync_status.last_error:
            self.window.set_settings_status(
                f"Hub records synced. Provider relay needs attention: {sync_status.last_error}"
            )
            return
        role = self.container.controller.provider_relay_role()
        if role == "collector":
            message = (
                f"Sync complete. Published {result.relay_item_count} provider snapshot item(s) "
                f"and synced {result.record_count} Hub record(s)."
            )
        elif role == "consumer":
            message = (
                f"Sync complete. Received {result.relay_item_count} provider snapshot item(s) "
                f"and synced {result.record_count} Hub record(s)."
            )
        else:
            message = (
                f"Sync complete: {result.record_count} record(s) across "
                f"{result.remote_device_count + 1} device(s)."
            )
        self.window.set_settings_status(message)

    @Slot(str)
    def _sync_relay_role_changed(self, role: str) -> None:
        try:
            self.container.controller.set_provider_relay_role(role)  # type: ignore[arg-type]
        except (RuntimeError, TypeError, ValueError):
            self.window.set_settings_status("Provider relay role could not be changed safely.")
            self.window.render_sync_status(self.container.controller.sync_status())
            return
        self.container.settings = self.container.controller.settings
        if role == "consumer":
            self.set_offline_mode(True)
        else:
            self.window.set_offline_mode(self.container.settings.offline_mode, emit=False)
        self.window.render_sync_status(self.container.controller.sync_status())
        self.render_all()
        if role == "collector":
            self.window.set_settings_status(
                "This device will publish selected provider snapshots on Sync now."
            )
        elif role == "consumer":
            self.window.set_settings_status(
                "Provider access is off here. Sync now reads the collector's latest snapshot."
            )
        else:
            self.window.set_settings_status("This device uses its own connected sources.")

    @Slot(object)
    def _sync_relay_categories_changed(self, categories: object) -> None:
        if not isinstance(categories, (tuple, list)):
            return
        try:
            self.container.controller.set_provider_relay_categories(
                item for item in categories if isinstance(item, str)
            )
        except (RuntimeError, TypeError, ValueError):
            self.window.set_settings_status("Provider relay content selection could not be saved.")
        self.window.render_sync_status(self.container.controller.sync_status())
        self.render_all()

    @Slot()
    def _sync_disable_requested(self) -> None:
        try:
            self.container.controller.disable_sync()
        except (RuntimeError, TypeError, ValueError):
            self.window.set_settings_status("Sync could not be disabled safely.")
            return
        self.window.render_sync_status(self.container.controller.sync_status())
        self.window.set_settings_status("Cross-device sync is disabled.")

    @Slot()
    def _backup_requested(self) -> None:
        if not self.window.logging_workspace.flush():
            return
        default_path = self.container.settings.backups_dir / "personal-hub-backup.db"
        selected, _filter = QFileDialog.getSaveFileName(
            self.window,
            "Back up Personal Hub database",
            str(default_path),
            "SQLite database (*.db)",
        )
        if not selected:
            return
        try:
            self.container.controller.backup_database(Path(selected))
        except (OSError, RuntimeError, ValueError):
            self.window.set_settings_status("Database backup failed safely.")
            return
        self.window.set_settings_status("Database backup completed.")

    @Slot()
    def _restore_requested(self) -> None:
        if not self.window.logging_workspace.flush():
            return
        if any(
            worker is not None
            for worker in (
                self._global_refresh_thread,
                self._refresh_thread,
                self._calendar_refresh_thread,
                self._feed_refresh_thread,
                self._watch_refresh_thread,
                self._watch_test_thread,
                self._message_refresh_thread,
                self._message_body_thread,
                self._maintenance_thread,
            )
        ):
            self.window.set_settings_status(
                "Wait for active refreshes to finish before restoring a backup."
            )
            return
        selected, _filter = QFileDialog.getOpenFileName(
            self.window,
            "Restore Personal Hub database",
            str(self.container.settings.backups_dir),
            "SQLite database (*.db)",
        )
        if not selected:
            return
        try:
            self.container.controller.restore_database(Path(selected))
        except (OSError, RuntimeError, ValueError):
            self.window.set_settings_status("Database restore failed integrity checks.")
            return
        self.container.settings = self.container.controller.settings
        self.window.logging_workspace.reflection = None
        self.window.logging_workspace.dirty = False
        self.window.logging_workspace._load_editor()
        self.render_all()
        self.window.set_settings_status("Database restored and migrated successfully.")

    @Slot()
    def _export_requested(self) -> None:
        if not self.window.logging_workspace.flush():
            return
        default_path = self.container.settings.data_dir / "personal-hub-export.json"
        selected, _filter = QFileDialog.getSaveFileName(
            self.window,
            "Export Personal Hub local data",
            str(default_path),
            "JSON document (*.json)",
        )
        if not selected:
            return
        try:
            self.container.controller.export_local_data(Path(selected))
        except (OSError, RuntimeError, TypeError, ValueError):
            self.window.set_settings_status("Local data export failed safely.")
            return
        self.window.set_settings_status(
            "Local data exported without credentials or message bodies."
        )

    @Slot()
    def refresh_all(self) -> None:
        self._begin_system_notification_batch(suppress_native=True)
        self._start_global_refresh(force=True)
        self._maybe_finish_system_notification_batch()

    def refresh_startup(self) -> None:
        self._begin_system_notification_batch(suppress_native=True)
        self._start_global_refresh(force=False)
        now = datetime.now(UTC)
        self._next_source_refresh_at = now + timedelta(minutes=15)
        self._next_calendar_refresh_at = now + timedelta(
            minutes=self.container.controller.settings.calendar_refresh_minutes
        )
        self.run_maintenance()
        self._maybe_finish_system_notification_batch()

    @Slot()
    def _scheduled_tick(self) -> None:
        if self._closed:
            return
        self._begin_system_notification_batch()
        self.run_maintenance()
        if not self.container.lifecycle.network_refresh_allowed:
            self._maybe_finish_system_notification_batch()
            return
        now = datetime.now(UTC)
        refresh_sources = False
        refresh_calendars = False
        if self._next_source_refresh_at is None or now >= self._next_source_refresh_at:
            refresh_sources = True
            self._next_source_refresh_at = now + timedelta(minutes=15)
        interval = self.container.controller.settings.calendar_refresh_minutes
        if self._next_calendar_refresh_at is None or now >= self._next_calendar_refresh_at:
            refresh_calendars = True
            self._next_calendar_refresh_at = now + timedelta(minutes=interval)
        self._start_global_refresh(
            force=False,
            include_sources=refresh_sources,
            include_calendars=refresh_calendars,
        )
        self._maybe_finish_system_notification_batch()

    def _start_global_refresh(
        self,
        *,
        force: bool,
        include_sources: bool = True,
        include_calendars: bool = True,
    ) -> None:
        if self._closed:
            return
        if self._global_refresh_thread is not None:
            self._global_refresh_pending = True
            self._global_refresh_pending_force = self._global_refresh_pending_force or force
            return
        if any(
            worker is not None
            for worker in (
                self._refresh_thread,
                self._calendar_refresh_thread,
                self._feed_refresh_thread,
                self._watch_refresh_thread,
                self._message_refresh_thread,
            )
        ):
            self._global_refresh_pending = True
            self._global_refresh_pending_force = self._global_refresh_pending_force or force
            return
        if not self.container.lifecycle.network_refresh_allowed:
            self.render_all()
            return
        query, source_ids, queries_by_source = self._calendar_refresh_request()
        worker = _GlobalRefreshThread(
            self.container,
            query=query,
            calendar_source_ids=source_ids,
            calendar_queries_by_source=queries_by_source,
            include_sources=include_sources,
            include_calendars=include_calendars,
            force=force,
            parent=self,
        )
        self._global_refresh_thread = worker
        self.window.set_refresh_busy(True)
        worker.results_ready.connect(self._global_refresh_completed)
        worker.refresh_failed.connect(self._global_refresh_failed)
        worker.finished.connect(self._global_refresh_finished)
        worker.start()

    @Slot(object)
    def _global_refresh_completed(self, results: object) -> None:
        if self._closed or not isinstance(results, Mapping):
            return
        callbacks = {
            "sources": self._refresh_completed,
            "calendars": self._calendar_refresh_completed,
            "feeds": self._feed_refresh_completed,
            "watches": self._watch_refresh_completed,
            "messages": self._message_refresh_completed,
        }
        self._global_refresh_rendering = True
        try:
            for name, callback in callbacks.items():
                if name in results:
                    callback(results[name])
        finally:
            self._global_refresh_rendering = False
        # Calendar, feed, watch, and message callbacks all contribute to the
        # same Today projection. Render it once after the complete refresh
        # burst instead of exposing several intermediate dashboard layouts.
        if not self._closed:
            self.render_task_surfaces()

    @Slot(object)
    def _global_refresh_failed(self, failures: object) -> None:
        if self._closed:
            return
        selected = set(failures) if isinstance(failures, (tuple, list, set)) else set()
        callbacks = {
            "sources": self._refresh_failed,
            "calendars": self._calendar_refresh_failed,
            "feeds": self._feed_refresh_failed,
            "watches": self._watch_refresh_failed,
            "messages": self._message_refresh_failed,
        }
        for name, callback in callbacks.items():
            if name in selected:
                callback()

    @Slot()
    def _global_refresh_finished(self) -> None:
        worker, self._global_refresh_thread = self._global_refresh_thread, None
        if worker is not None:
            worker.deleteLater()
        self.window.set_refresh_busy(False)
        self._resume_deferred_refreshes()
        self._maybe_finish_system_notification_batch()

    def _resume_deferred_refreshes(self) -> None:
        if self._closed or self._global_refresh_thread is not None:
            return
        if any(
            worker is not None
            for worker in (
                self._refresh_thread,
                self._calendar_refresh_thread,
                self._feed_refresh_thread,
                self._watch_refresh_thread,
                self._message_refresh_thread,
            )
        ):
            return
        if self._source_operation_pending is not None:
            source_ids, action = self._source_operation_pending
            self._source_operation_pending = None
            self._start_source_operation(source_ids, action=action)
        elif self._global_refresh_pending:
            force = self._global_refresh_pending_force
            self._global_refresh_pending = False
            self._global_refresh_pending_force = False
            self._calendar_refresh_pending = False
            self._start_global_refresh(force=force)
        elif self._calendar_refresh_pending and not self._closed:
            self._calendar_refresh_pending = False
            self.refresh_calendars()

    def run_maintenance(self) -> None:
        if self._closed or self._maintenance_thread is not None:
            return
        self._begin_system_notification_batch()
        worker = _MaintenanceThread(self.container, self)
        self._maintenance_thread = worker
        worker.completed.connect(self._maintenance_completed)
        worker.failed.connect(self._maintenance_failed)
        worker.finished.connect(self._maintenance_finished)
        worker.start()

    @Slot(object)
    def _maintenance_completed(self, result: object) -> None:
        if self._closed:
            return
        created = result.get("created", ()) if isinstance(result, Mapping) else result
        if isinstance(created, (tuple, list)):
            for item in created:
                if isinstance(item, MessageAttentionNotice):
                    self.window.render_attention_notification(_message_attention_view(item))
                elif getattr(item, "kind", None) == "task_reminder":
                    self._deliver_system_notification(
                        deduplication_key=str(
                            getattr(item, "deduplication_key", None)
                            or getattr(item, "item_id", "task-reminder")
                        ),
                        title=str(getattr(item, "title", "Task reminder")),
                        message=str(getattr(item, "safe_summary", "A task reminder is due.")),
                        at=getattr(item, "created_at", datetime.now(UTC)),
                        severity=NotificationSeverity.ATTENTION,
                        sensitive=True,
                    )
        self.window.render_inbox(
            tuple(_inbox_view(item) for item in self.container.controller.list_inbox())
        )
        self.render_task_surfaces()
        self.render_diagnostics()

    @Slot()
    def _maintenance_failed(self) -> None:
        if self._closed:
            return
        self.window.post_source_failure(
            SourceFailureView(
                source_id="hub-maintenance",
                display_name="Personal Hub maintenance",
                message="Scheduled reminders and digests could not be reconciled.",
            )
        )

    @Slot()
    def _maintenance_finished(self) -> None:
        worker, self._maintenance_thread = self._maintenance_thread, None
        if worker is not None:
            worker.deleteLater()
        self._maybe_finish_system_notification_batch()

    @Slot()
    def refresh_sources(self, source_ids: tuple[str, ...] | None = None) -> None:
        self._start_source_operation(source_ids, action="refresh")

    def _start_source_operation(
        self,
        source_ids: tuple[str, ...] | None,
        *,
        action: str,
    ) -> None:
        if self._closed:
            return
        if self._refresh_thread is not None or self._global_refresh_thread is not None:
            if action in {"connect", "disconnect"} and source_ids:
                self._source_operation_pending = (source_ids, action)
            return
        if not self.container.lifecycle.network_refresh_allowed:
            self.render_sources()
            return
        worker = _SourceRefreshThread(
            self.container,
            self,
            source_ids=source_ids,
            action=action,
        )
        self._refresh_thread = worker
        worker.results_ready.connect(self._refresh_completed)
        worker.refresh_failed.connect(self._refresh_failed)
        worker.finished.connect(self._refresh_finished)
        worker.start()

    @Slot()
    def refresh_calendars(self, *, sync_google: bool = False) -> None:
        if self._closed:
            return
        if self._global_refresh_thread is not None:
            if sync_google:
                self.window.calendar_status_label.setText(
                    "Refresh is running. Choose Sync Google when it finishes."
                )
                return
            self._calendar_refresh_pending = True
            return
        if self._calendar_refresh_thread is not None:
            if sync_google:
                self.window.calendar_status_label.setText(
                    "Calendar refresh is running. Choose Sync Google when it finishes."
                )
                return
            self._calendar_refresh_pending = True
            return
        if not self.container.lifecycle.network_refresh_allowed:
            self.render_calendar()
            if sync_google:
                self.window.calendar_status_label.setText(
                    "Google sync is paused. Your calendar items are saved locally."
                )
            return
        query, source_ids, queries_by_source = self._calendar_refresh_request()
        worker = _CalendarRefreshThread(
            self.container,
            query,
            source_ids=source_ids,
            queries_by_source=queries_by_source,
            sync_google=sync_google,
            parent=self,
        )
        self._calendar_refresh_thread = worker
        worker.results_ready.connect(self._calendar_refresh_completed)
        worker.sync_ready.connect(self._calendar_sync_completed)
        worker.refresh_failed.connect(self._calendar_refresh_failed)
        worker.finished.connect(self._calendar_refresh_finished)
        worker.start()

    def _calendar_refresh_request(
        self,
    ) -> tuple[
        CalendarQuery,
        tuple[str, ...] | None,
        dict[str, CalendarQuery] | None,
    ]:
        query = self._calendar_query()
        selected = _saved_calendar_selection(self.container)
        source_ids: tuple[str, ...] | None = None
        queries_by_source: dict[str, CalendarQuery] | None = None
        if selected is not None:
            queries_by_source = {}
            grouped: dict[str, list[str]] = {}
            for key in selected:
                parsed = _parse_calendar_selection_key(key)
                if parsed is None:
                    continue
                source_id, calendar_id = parsed
                if source_id not in self.container.calendar_service.source_ids:
                    continue
                grouped.setdefault(source_id, [])
                if calendar_id != "*":
                    grouped[source_id].append(calendar_id)
            source_ids = tuple(grouped)
            for source_id, calendar_ids in grouped.items():
                queries_by_source[source_id] = CalendarQuery(
                    starts_at=query.starts_at,
                    ends_at=query.ends_at,
                    calendar_ids=tuple(dict.fromkeys(calendar_ids)),
                    timezone=query.timezone,
                    limit=query.limit,
                )
        return query, source_ids, queries_by_source

    def _calendar_query(self) -> CalendarQuery:
        timezone = self.container.controller.settings.default_timezone
        zone = ZoneInfo(timezone)
        cursor = self.window.current_calendar_date
        mode = self.window.calendar_mode_value
        if mode == "month":
            month_start = cursor.replace(day=1)
            starts_on = month_start - timedelta(days=(month_start.weekday() + 1) % 7)
            ends_on = starts_on + timedelta(days=42)
        elif mode == "day":
            starts_on = cursor
            ends_on = starts_on + timedelta(days=1)
        elif mode == "week":
            starts_on = cursor - timedelta(days=(cursor.weekday() + 1) % 7)
            ends_on = starts_on + timedelta(days=7)
        else:
            starts_on = cursor
            ends_on = starts_on + timedelta(days=31)
        return CalendarQuery(
            starts_at=datetime.combine(starts_on, time.min, tzinfo=zone).astimezone(UTC),
            ends_at=datetime.combine(ends_on, time.min, tzinfo=zone).astimezone(UTC),
            calendar_ids=(),
            timezone=timezone,
            limit=1_000,
        )

    @Slot(object)
    def _calendar_refresh_completed(self, results: object) -> None:
        if self._closed:
            return
        selected = results if isinstance(results, Mapping) else {}
        self.render_calendar(selected)
        if not self._global_refresh_rendering:
            self.render_task_surfaces()
        self.render_diagnostics()

    @Slot()
    def _calendar_refresh_failed(self) -> None:
        if self._closed:
            return
        self.window.render_calendar(
            CalendarView(
                timezone=self.container.controller.settings.default_timezone,
                freshness="error",
                detail="Calendar refresh failed safely.",
                cursor_date=self.window.current_calendar_date,
            )
        )

    @Slot()
    def _calendar_refresh_finished(self) -> None:
        worker, self._calendar_refresh_thread = self._calendar_refresh_thread, None
        if worker is not None:
            worker.deleteLater()
        if self._calendar_refresh_pending and not self._closed:
            self._calendar_refresh_pending = False
            self.refresh_calendars()
        self._resume_deferred_refreshes()
        self._maybe_finish_system_notification_batch()

    def refresh_feeds(
        self,
        subscription_ids: tuple[UUID, ...] | None = None,
        *,
        force: bool = True,
    ) -> None:
        if self._closed:
            return
        self._begin_system_notification_batch()
        if self._global_refresh_thread is not None:
            return
        if self._feed_refresh_thread is not None:
            self._feed_refresh_pending = True
            self._feed_refresh_pending_force = self._feed_refresh_pending_force or force
            return
        if not self.container.lifecycle.network_refresh_allowed:
            self.render_news()
            self._maybe_finish_system_notification_batch()
            return
        worker = _FeedRefreshThread(
            self.container,
            subscription_ids=subscription_ids,
            force=force,
            parent=self,
        )
        self._feed_refresh_thread = worker
        worker.results_ready.connect(self._feed_refresh_completed)
        worker.refresh_failed.connect(self._feed_refresh_failed)
        worker.finished.connect(self._feed_refresh_finished)
        worker.start()

    @Slot(object)
    def _feed_refresh_completed(self, results: object) -> None:
        if self._closed:
            return
        selected = results if isinstance(results, Mapping) else {}
        for result in selected.values():
            if not isinstance(result, FeedResult):
                continue
            if result.state in {
                "stale",
                "permission_required",
                "disconnected",
                "error",
            }:
                self.window.post_source_failure(
                    SourceFailureView(
                        source_id=f"feed.{result.subscription_id.hex}",
                        display_name=result.title,
                        message=result.safe_message,
                        retryable=result.state not in {"permission_required", "disconnected"},
                        last_success_at=_format_timestamp(result.last_success_at),
                    )
                )
        self.render_news()
        if not self._global_refresh_rendering:
            self.render_task_surfaces()
        self.window.render_inbox(
            tuple(_inbox_view(item) for item in self.container.controller.list_inbox())
        )
        if "platform-canvas" in selected:
            self.render_task_surfaces()
            self.render_calendar()
        self.render_diagnostics()

    @Slot()
    def _feed_refresh_failed(self) -> None:
        if self._closed:
            return
        self.window.post_source_failure(
            SourceFailureView(
                source_id="hub-feed-refresh",
                display_name="News feeds",
                message="Feed refresh failed safely.",
            )
        )

    @Slot()
    def _feed_refresh_finished(self) -> None:
        worker, self._feed_refresh_thread = self._feed_refresh_thread, None
        if worker is not None:
            worker.deleteLater()
        if self._feed_refresh_pending and not self._closed:
            force = self._feed_refresh_pending_force
            self._feed_refresh_pending = False
            self._feed_refresh_pending_force = False
            self.refresh_feeds(force=force)
        self._resume_deferred_refreshes()
        self._maybe_finish_system_notification_batch()

    def refresh_watches(
        self,
        watch_ids: tuple[UUID, ...] | None = None,
        *,
        force: bool = True,
    ) -> None:
        if self._closed:
            return
        self._begin_system_notification_batch()
        if self._global_refresh_thread is not None:
            return
        if self._watch_refresh_thread is not None:
            self._watch_refresh_pending = True
            self._watch_refresh_pending_force = self._watch_refresh_pending_force or force
            return
        if not self.container.lifecycle.network_refresh_allowed:
            self.render_web_watches()
            self._maybe_finish_system_notification_batch()
            return
        worker = _WatchRefreshThread(
            self.container,
            watch_ids=watch_ids,
            force=force,
            parent=self,
        )
        self._watch_refresh_thread = worker
        worker.results_ready.connect(self._watch_refresh_completed)
        worker.refresh_failed.connect(self._watch_refresh_failed)
        worker.finished.connect(self._watch_refresh_finished)
        worker.start()

    @Slot(object)
    def _watch_refresh_completed(self, results: object) -> None:
        if self._closed:
            return
        selected = results if isinstance(results, Mapping) else {}
        for result in selected.values():
            if not isinstance(result, WatchResult):
                continue
            self._deliver_watch_notification(result)
            if result.state == "ready":
                continue
            self.window.post_source_failure(
                SourceFailureView(
                    source_id=f"watch.{result.watch_id.hex}",
                    display_name=result.name,
                    message=result.safe_message,
                    retryable=result.state not in {"permission_required"},
                )
            )
        self.render_web_watches()
        if not self._global_refresh_rendering:
            self.render_task_surfaces()
        self.window.render_inbox(
            tuple(_inbox_view(item) for item in self.container.controller.list_inbox())
        )
        self.render_diagnostics()

    def _deliver_watch_notification(self, result: WatchResult) -> None:
        alert = result.alert
        if alert is None:
            return
        self._deliver_system_notification(
            deduplication_key=alert.deduplication_key,
            title=alert.title,
            message=alert.safe_summary,
            at=result.checked_at,
            severity=NotificationSeverity.ATTENTION,
            sensitive=True,
        )

    def _deliver_system_notification(
        self,
        *,
        deduplication_key: str,
        title: str,
        message: str,
        at: datetime,
        severity: NotificationSeverity,
        sensitive: bool,
    ) -> None:
        repository = self.container.notification_repository
        delivery = repository.get(deduplication_key, "windows")
        if delivery is None:
            repository.claim_key(
                deduplication_key,
                "windows",
                now=at,
                safe_message="A system notification is ready.",
            )
            delivery = repository.get(deduplication_key, "windows")
        if delivery is None or delivery.status != "pending":
            return
        settings = self.container.controller.settings
        local_now = at.astimezone(ZoneInfo(settings.default_timezone))
        decision = decide_delivery(
            NotificationPolicy.IMMEDIATE,
            now=local_now,
            quiet_hours=QuietHoursSettings(
                enabled=settings.notification_quiet_hours_enabled,
                start=settings.notification_quiet_hours_start,
                end=settings.notification_quiet_hours_end,
            ),
            severity=severity,
        )
        if not settings.notifications_enabled or not decision.deliver_system_notification:
            reason = (
                "Notifications are disabled."
                if not settings.notifications_enabled
                else ("Notification was suppressed by quiet hours.")
            )
            repository.mark_suppressed(
                deduplication_key,
                "windows",
                at=at,
                safe_message=reason,
            )
            return
        self._pending_system_notifications.setdefault(
            deduplication_key,
            _PendingSystemNotification(
                deduplication_key=deduplication_key,
                title=title,
                message=message,
                at=at,
                sensitive=sensitive,
                suppress_native=self._system_notification_batch_suppress_native,
            ),
        )
        self._schedule_system_notification_flush()

    def _begin_system_notification_batch(self, *, suppress_native: bool = False) -> None:
        if self._closed:
            return
        self._system_notification_batch_active = True
        self._system_notification_batch_suppress_native = (
            self._system_notification_batch_suppress_native or suppress_native
        )
        self._system_notification_timer.stop()

    def _maybe_finish_system_notification_batch(self) -> None:
        if self._closed or not self._system_notification_batch_active:
            return
        if any(
            worker is not None
            for worker in (
                self._global_refresh_thread,
                self._refresh_thread,
                self._calendar_refresh_thread,
                self._feed_refresh_thread,
                self._watch_refresh_thread,
                self._message_refresh_thread,
                self._maintenance_thread,
            )
        ):
            return
        if any(
            (
                self._global_refresh_pending,
                self._calendar_refresh_pending,
                self._feed_refresh_pending,
                self._watch_refresh_pending,
                self._message_refresh_pending,
            )
        ):
            return
        self._system_notification_batch_active = False
        self._system_notification_batch_suppress_native = False
        self._schedule_system_notification_flush()

    def _schedule_system_notification_flush(self) -> None:
        if (
            self._closed
            or self._system_notification_batch_active
            or not self._pending_system_notifications
        ):
            return
        if not self._system_notification_timer.isActive():
            self._system_notification_timer.start()

    @Slot()
    def _flush_system_notifications(self) -> None:
        """Present one native notification for all alerts in one refresh burst."""

        if self._closed or self._system_notification_batch_active:
            return
        pending, self._pending_system_notifications = (
            tuple(self._pending_system_notifications.values()),
            {},
        )
        if self._closed:
            return
        repository = self.container.notification_repository
        candidates = tuple(
            item
            for item in pending
            if (delivery := repository.get(item.deduplication_key, "windows")) is not None
            and delivery.status == "pending"
        )
        if not candidates:
            return

        refresh_suppressed = tuple(item for item in candidates if item.suppress_native)
        candidates = tuple(item for item in candidates if not item.suppress_native)
        for item in refresh_suppressed:
            repository.mark_suppressed(
                item.deduplication_key,
                "windows",
                at=item.at,
                safe_message="Refresh notifications are available in Personal Hub.",
            )
        if not candidates:
            return

        if self.window.isVisible() and not self.window.isMinimized():
            for item in candidates:
                repository.mark_suppressed(
                    item.deduplication_key,
                    "windows",
                    at=item.at,
                    safe_message="Personal Hub is already open.",
                )
            return

        settings = self.container.controller.settings
        masked = any(
            item.sensitive and (settings.privacy_masking or self.window.locked)
            for item in candidates
        )
        if len(candidates) == 1:
            item = candidates[0]
            title = "Personal Hub attention" if masked else item.title
            message = "Open Personal Hub to view this item." if masked else item.message
        else:
            title = "Personal Hub attention"
            message = f"{len(candidates)} attention items are ready. Open Personal Hub to review."
        try:
            shown = self.window.tray_controller.show_notification(title, message)
        except Exception:
            shown = False
        for item in candidates:
            marker = repository.mark_delivered if shown else repository.mark_failed
            marker(
                item.deduplication_key,
                "windows",
                at=item.at,
                **({"safe_message": "System notification was unavailable."} if not shown else {}),
            )

    @Slot()
    def _watch_refresh_failed(self) -> None:
        if self._closed:
            return
        self.window.post_source_failure(
            SourceFailureView(
                source_id="hub-watch-refresh",
                display_name="Web watches",
                message="Watch refresh failed safely.",
            )
        )

    @Slot()
    def _watch_refresh_finished(self) -> None:
        worker, self._watch_refresh_thread = self._watch_refresh_thread, None
        if worker is not None:
            worker.deleteLater()
        if self._watch_refresh_pending and not self._closed:
            force = self._watch_refresh_pending_force
            self._watch_refresh_pending = False
            self._watch_refresh_pending_force = False
            self.refresh_watches(force=force)
        self._resume_deferred_refreshes()
        self._maybe_finish_system_notification_batch()

    def refresh_messages(self, *, force: bool = True) -> None:
        if self._closed:
            return
        self._begin_system_notification_batch()
        if self._global_refresh_thread is not None:
            return
        if self._message_refresh_thread is not None:
            self._message_refresh_pending = True
            self._message_refresh_pending_force = self._message_refresh_pending_force or force
            return
        if not self.container.lifecycle.network_refresh_allowed:
            self.render_messages()
            self._reconcile_message_attention_notice()
            self._maybe_finish_system_notification_batch()
            return
        worker = _MessageRefreshThread(self.container, force=force, parent=self)
        self._message_refresh_thread = worker
        worker.results_ready.connect(self._message_refresh_completed)
        worker.refresh_failed.connect(self._message_refresh_failed)
        worker.finished.connect(self._message_refresh_finished)
        worker.start()

    @Slot(object)
    def _message_refresh_completed(self, results: object) -> None:
        if self._closed:
            return
        selected = results if isinstance(results, Mapping) else {}
        for result in selected.values():
            if not isinstance(result, MessageResult) or result.state in {"ready", "empty"}:
                continue
            self.window.post_source_failure(
                SourceFailureView(
                    source_id=f"message.{result.source_id}",
                    display_name=f"{result.provider.title()} messages",
                    message=result.safe_message,
                    retryable=result.state not in {"permission_required", "disconnected"},
                    last_success_at=_format_timestamp(result.last_success_at),
                )
            )
        self._reconcile_message_attention_notice()
        self.render_messages()
        if not self._global_refresh_rendering:
            self.render_task_surfaces()
        self.window.render_inbox(
            tuple(_inbox_view(item) for item in self.container.controller.list_inbox())
        )
        self.render_diagnostics()

    def _reconcile_message_attention_notice(self) -> None:
        notice = self.container.controller.reconcile_message_attention_notice()
        if notice is not None:
            self.window.render_attention_notification(_message_attention_view(notice))
            return
        if not self.container.controller.list_message_summaries(
            unread=True,
            include_spam=False,
            limit=1,
        ):
            self.window.clear_attention_notification()

    @Slot()
    def _message_refresh_failed(self) -> None:
        if self._closed:
            return
        self._reconcile_message_attention_notice()
        self.window.post_source_failure(
            SourceFailureView(
                source_id="hub-message-refresh",
                display_name="Attention messages",
                message="Message summary refresh failed safely.",
            )
        )

    @Slot()
    def _message_refresh_finished(self) -> None:
        worker, self._message_refresh_thread = self._message_refresh_thread, None
        if worker is not None:
            worker.deleteLater()
        if self._message_refresh_pending and not self._closed:
            force = self._message_refresh_pending_force
            self._message_refresh_pending = False
            self._message_refresh_pending_force = False
            self.refresh_messages(force=force)
        self._resume_deferred_refreshes()
        self._maybe_finish_system_notification_batch()

    @Slot(object)
    def _open_message_body(self, value: object) -> None:
        if (
            self._closed
            or not isinstance(value, MessageSummaryView)
            or self._message_body_thread is not None
            or not self.container.lifecycle.network_refresh_allowed
        ):
            return
        worker = _MessageBodyThread(
            self.container,
            value.source_id,
            value.message_id,
            parent=self,
        )
        self._message_body_thread = worker
        worker.body_ready.connect(self._message_body_ready)
        worker.body_failed.connect(self._message_body_failed)
        worker.finished.connect(self._message_body_finished)
        worker.start()

    @Slot(object)
    def _message_body_ready(self, value: object) -> None:
        if self._closed:
            return
        if not isinstance(value, MessageBody):
            return
        self.window.render_message_body(
            MessageBodyView(
                source_id=value.source_id,
                message_id=value.message_id,
                plain_text=value.plain_text,
                links=value.links,
                fetched_at=value.fetched_at,
                sensitive=True,
            )
        )

    @Slot()
    def _message_body_failed(self) -> None:
        if self._closed:
            return
        self.window.clear_message_body()
        self.window.post_source_failure(
            SourceFailureView(
                source_id="hub-message-body",
                display_name="Message body",
                message="The selected message body could not be opened safely.",
            )
        )

    @Slot()
    def _message_body_finished(self) -> None:
        worker, self._message_body_thread = self._message_body_thread, None
        if worker is not None:
            worker.deleteLater()

    @Slot(object)
    def _open_message_link(self, value: object) -> None:
        if not isinstance(value, str):
            return
        target = QUrl(value)
        if target.isValid() and target.scheme().casefold() in {"http", "https"} and target.host():
            QDesktopServices.openUrl(target)

    @Slot()
    def _clear_message_summaries(self) -> None:
        self.container.controller.clear_message_bodies()
        self.container.controller.clear_message_summaries()
        self.window.clear_message_body()
        self.window.clear_attention_notification()
        self.render_messages()
        self.render_task_surfaces()

    @Slot(str)
    def _save_calendar_mode(self, mode: str) -> None:
        if mode in {"month", "day", "week", "agenda"}:
            self.container.settings_repository.set("calendar_view_mode", mode)
            self.refresh_calendars()

    @Slot(object)
    def _calendar_period_changed(self, _cursor: object) -> None:
        self.refresh_calendars()

    @Slot(object)
    def _calendar_selection_changed(self, selection: object) -> None:
        if not isinstance(selection, (tuple, list)) or not all(
            isinstance(item, str) for item in selection
        ):
            return
        self.container.settings_repository.set("calendar_selected_ids", list(selection))
        self.render_task_surfaces()
        self.refresh_calendars()

    @Slot(object)
    def _open_calendar_event(self, value: object) -> None:
        if not isinstance(value, CalendarEventView) or not value.navigation_target:
            return
        target = QUrl(value.navigation_target)
        if (
            target.isValid()
            and target.scheme().casefold() in {"http", "https"}
            and target.host()
            and not target.userName()
            and not target.password()
        ):
            QDesktopServices.openUrl(target)

    @Slot(object)
    def _calendar_task_completion_changed(self, value: object) -> None:
        if self._closed or not isinstance(value, Mapping):
            return
        task_id = value.get("task_id")
        completed = value.get("completed")
        if not isinstance(task_id, str) or not isinstance(completed, bool):
            return
        task = self.container.controller.get_task(task_id)
        if task is None:
            self.window.calendar_status_label.setText("This to-do item no longer exists.")
            return
        try:
            if completed and task.state != "completed":
                self.container.controller.complete_task(task_id, completed_at=datetime.now(UTC))
            elif not completed and task.state == "completed":
                self.container.controller.undo_task(task_id)
        except (TypeError, ValueError, RuntimeError):
            self.window.calendar_status_label.setText("The to-do item could not be updated.")
            return
        self.render_task_surfaces()
        self.render_calendar()

    @Slot(object)
    def _calendar_slot_requested(self, value: object) -> None:
        """Start a local task at an empty hourly calendar slot."""

        if self._closed or not isinstance(value, Mapping):
            return
        selected_date = value.get("date")
        minutes = value.get("minutes")
        if isinstance(selected_date, datetime):
            selected_date = selected_date.date()
        if not isinstance(selected_date, date):
            return
        if not isinstance(minutes, int) or isinstance(minutes, bool) or not 0 <= minutes < 24 * 60:
            return
        timezone = self.container.controller.settings.default_timezone
        try:
            zone = ZoneInfo(timezone)
            due_at = datetime.combine(
                selected_date,
                time(hour=minutes // 60, minute=minutes % 60),
                tzinfo=zone,
            )
        except (TypeError, ValueError):
            return
        self.window.show_destination("Tasks")
        self.window.open_new_task_editor(due_at=due_at, timezone=timezone)

    @Slot(object)
    def _refresh_completed(self, results: object) -> None:
        if self._closed:
            return
        selected = results if isinstance(results, Mapping) else {}
        self.render_sources(selected)
        for result in selected.values():
            if not isinstance(result, SourceResult):
                continue
            if result.state in {
                SourceState.STALE,
                SourceState.PERMISSION_REQUIRED,
                SourceState.DISCONNECTED,
                SourceState.ERROR,
            }:
                self.window.post_source_failure(
                    SourceFailureView(
                        source_id=result.source_id,
                        display_name=result.display_name,
                        message=safe_state_message(result.state),
                        retryable=result.state
                        not in {SourceState.PERMISSION_REQUIRED, SourceState.DISCONNECTED},
                        last_success_at=_format_timestamp(result.last_success_at),
                    )
                )
        self.window.render_inbox(
            tuple(_inbox_view(item) for item in self.container.controller.list_inbox())
        )
        self.render_diagnostics()

    @Slot()
    def _refresh_failed(self) -> None:
        if self._closed:
            return
        self.window.post_source_failure(
            SourceFailureView(
                source_id="hub-refresh",
                display_name="Personal Hub sources",
                message="Source refresh failed safely.",
            )
        )

    @Slot()
    def _refresh_finished(self) -> None:
        worker, self._refresh_thread = self._refresh_thread, None
        if worker is not None:
            worker.deleteLater()
        self._resume_deferred_refreshes()
        self._maybe_finish_system_notification_batch()

    @Slot(bool)
    def set_network_paused(self, paused: bool) -> None:
        if self._closed:
            return
        self.container.lifecycle.set_network_paused(paused)
        if paused and self._global_refresh_thread is not None:
            self._global_refresh_thread.cancel()
        if paused and self._refresh_thread is not None:
            self._refresh_thread.cancel()
        if paused and self._calendar_refresh_thread is not None:
            self._calendar_refresh_thread.cancel()
        if paused and self._feed_refresh_thread is not None:
            self._feed_refresh_thread.cancel()
        if paused and self._watch_refresh_thread is not None:
            self._watch_refresh_thread.cancel()
        if paused and self._watch_test_thread is not None:
            self._watch_test_thread.cancel()
        if paused and self._message_refresh_thread is not None:
            self._message_refresh_thread.cancel()
        if paused and self._message_body_thread is not None:
            self._message_body_thread.cancel()
        if paused:
            self._feed_refresh_pending = False
            self._feed_refresh_pending_force = False
            self._watch_refresh_pending = False
            self._watch_refresh_pending_force = False
            self._message_refresh_pending = False
            self._message_refresh_pending_force = False

    @Slot(bool)
    def set_offline_mode(self, enabled: bool) -> None:
        if self._closed:
            return
        updated = self.container.controller.set_offline_mode(enabled)
        self.container.settings = updated
        self.window.set_offline_mode(updated.offline_mode, emit=False)
        if enabled and self._global_refresh_thread is not None:
            self._global_refresh_thread.cancel()
        if enabled and self._refresh_thread is not None:
            self._refresh_thread.cancel()
        if enabled and self._calendar_refresh_thread is not None:
            self._calendar_refresh_thread.cancel()
        if enabled and self._feed_refresh_thread is not None:
            self._feed_refresh_thread.cancel()
        if enabled and self._watch_refresh_thread is not None:
            self._watch_refresh_thread.cancel()
        if enabled and self._watch_test_thread is not None:
            self._watch_test_thread.cancel()
        if enabled and self._message_refresh_thread is not None:
            self._message_refresh_thread.cancel()
        if enabled and self._message_body_thread is not None:
            self._message_body_thread.cancel()
        if enabled:
            self._calendar_refresh_pending = False
            self._feed_refresh_pending = False
            self._feed_refresh_pending_force = False
            self._watch_refresh_pending = False
            self._watch_refresh_pending_force = False
            self._message_refresh_pending = False
            self._message_refresh_pending_force = False
        self.render_sources()
        self.render_calendar()
        self.render_news()
        self.render_web_watches()
        self.render_messages()

    @Slot(object)
    def _handle_lifecycle_event(self, event: object) -> None:
        if event == LifecycleEvent.SESSION_LOCKED:
            self.window.set_locked(True)
            self.container.controller.clear_message_bodies()
            if (
                self.container.settings.pause_network_while_locked
                and self._global_refresh_thread is not None
            ):
                self._global_refresh_thread.cancel()
            if (
                self.container.settings.pause_network_while_locked
                and self._refresh_thread is not None
            ):
                self._refresh_thread.cancel()
            if (
                self.container.settings.pause_network_while_locked
                and self._calendar_refresh_thread is not None
            ):
                self._calendar_refresh_thread.cancel()
            if (
                self.container.settings.pause_network_while_locked
                and self._feed_refresh_thread is not None
            ):
                self._feed_refresh_thread.cancel()
            if (
                self.container.settings.pause_network_while_locked
                and self._watch_refresh_thread is not None
            ):
                self._watch_refresh_thread.cancel()
            if (
                self.container.settings.pause_network_while_locked
                and self._watch_test_thread is not None
            ):
                self._watch_test_thread.cancel()
            if (
                self.container.settings.pause_network_while_locked
                and self._message_refresh_thread is not None
            ):
                self._message_refresh_thread.cancel()
            if self._message_body_thread is not None:
                self._message_body_thread.cancel()
        elif event == LifecycleEvent.SESSION_UNLOCKED:
            self.window.set_locked(False)
        elif event == LifecycleEvent.SLEEPING:
            if self._global_refresh_thread is not None:
                self._global_refresh_thread.cancel()
            if self._refresh_thread is not None:
                self._refresh_thread.cancel()
            if self._calendar_refresh_thread is not None:
                self._calendar_refresh_thread.cancel()
            if self._feed_refresh_thread is not None:
                self._feed_refresh_thread.cancel()
            if self._watch_refresh_thread is not None:
                self._watch_refresh_thread.cancel()
            if self._watch_test_thread is not None:
                self._watch_test_thread.cancel()
            if self._message_refresh_thread is not None:
                self._message_refresh_thread.cancel()
            if self._message_body_thread is not None:
                self._message_body_thread.cancel()
            self.container.controller.clear_message_bodies()
            self.window.clear_message_body()
        elif event == LifecycleEvent.STOPPING:
            self.shutdown()

    @Slot()
    def shutdown(self) -> None:
        self.window.logging_workspace.flush()
        if self._closed:
            return
        self._closed = True
        self._scheduler_timer.stop()
        self._system_notification_timer.stop()
        self._pending_system_notifications.clear()
        self._system_notification_batch_suppress_native = False
        self._unsubscribe_lifecycle()
        global_worker, self._global_refresh_thread = self._global_refresh_thread, None
        if global_worker is not None:
            global_worker.cancel()
            global_worker.wait(5_000)
            global_worker.deleteLater()
        maintenance_worker, self._maintenance_thread = self._maintenance_thread, None
        if maintenance_worker is not None:
            maintenance_worker.cancel()
            maintenance_worker.wait(5_000)
            maintenance_worker.deleteLater()
        worker, self._refresh_thread = self._refresh_thread, None
        if worker is not None:
            worker.cancel()
            worker.wait(5_000)
            worker.deleteLater()
        calendar_worker, self._calendar_refresh_thread = (
            self._calendar_refresh_thread,
            None,
        )
        if calendar_worker is not None:
            calendar_worker.cancel()
            calendar_worker.wait(5_000)
            calendar_worker.deleteLater()
        feed_worker, self._feed_refresh_thread = self._feed_refresh_thread, None
        if feed_worker is not None:
            feed_worker.cancel()
            feed_worker.wait(5_000)
            feed_worker.deleteLater()
        watch_worker, self._watch_refresh_thread = self._watch_refresh_thread, None
        if watch_worker is not None:
            watch_worker.cancel()
            watch_worker.wait(5_000)
            watch_worker.deleteLater()
        test_worker, self._watch_test_thread = self._watch_test_thread, None
        if test_worker is not None:
            test_worker.cancel()
            test_worker.wait(5_000)
            test_worker.deleteLater()
        message_worker, self._message_refresh_thread = self._message_refresh_thread, None
        if message_worker is not None:
            message_worker.cancel()
            message_worker.wait(5_000)
            message_worker.deleteLater()
        body_worker, self._message_body_thread = self._message_body_thread, None
        if body_worker is not None:
            body_worker.cancel()
            body_worker.wait(5_000)
            body_worker.deleteLater()
        self.container.controller.clear_message_bodies()
        self.window.clear_message_body()


def run_hub_desktop(
    container: HubContainer,
    argv: Sequence[str] | None = None,
    *,
    app: QApplication | None = None,
) -> int:
    """Create the Hub shell, bind its services, and run the Qt event loop."""

    application = app or create_application(argv)
    settings = container.controller.settings
    window = create_main_window(
        close_to_tray=settings.close_to_tray,
        show_tray=True,
    )
    coordinator = HubDesktopCoordinator(container, window, application)
    # Keep the coordinator alive through the event loop without adding a global singleton.
    window._hub_coordinator = coordinator  # type: ignore[attr-defined]
    if settings.start_in_tray:
        window.hide()
    else:
        window.show()
    coordinator.refresh_startup()
    return application.exec()


def _inbox_view(item: Any) -> InboxItemView:
    source = item.source.display_name if item.source is not None else ""
    return InboxItemView(
        item_id=item.item_id,
        title=item.title,
        summary=item.safe_summary,
        source=source,
        timestamp=_format_timestamp(item.created_at) or "",
        severity=item.severity,
        sensitive=item.kind in {"message", "digest"},
        unread=item.state == "unread",
        state=item.state,
    )


def _message_attention_view(notice: MessageAttentionNotice) -> AttentionNotificationView:
    return AttentionNotificationView(
        notice_id=notice.notice_id,
        title=notice.title,
        summary=notice.safe_summary,
        navigation_target=notice.navigation_target,
        sensitive=True,
    )


def _source_view(result: SourceResult, *, enabled: bool = True) -> SourceView:
    return SourceView(
        source_id=result.source_id,
        display_name=result.display_name,
        status=result.state.value,
        detail=safe_state_message(result.state),
        last_success_at=_format_timestamp(result.last_success_at) or "",
        sensitive=False,
        enabled=enabled,
    )


def _task_view(task: Any, *, lists: Mapping[str, str]) -> TaskView:
    recurrence = task.recurrence.frequency.capitalize() if task.recurrence is not None else None
    native_platform = "canvas" if "canvas" in task.tags else ""
    external_url = _task_native_url(task)
    return TaskView(
        task_id=str(task.task_id),
        title=task.title,
        notes=task.notes,
        external_url=external_url or "",
        native_platform=native_platform,
        state=task.state,
        priority=task.priority,
        due_at=task.due_at,
        due_is_all_day=task.due_is_all_day,
        timezone=task.timezone,
        list_id=lists.get(str(task.list_id)) if task.list_id is not None else None,
        list_key=str(task.list_id) if task.list_id is not None else None,
        tags=task.tags,
        recurrence=recurrence,
        reminder_offsets_minutes=task.reminder_offsets_minutes,
        created_at=task.created_at,
        updated_at=task.updated_at,
        completed_at=task.completed_at,
    )


def _calendar_task_view(task: Any) -> CalendarEventView:
    """Project one due-dated local task into the calendar presentation model."""

    due_at = task.due_at
    ends_at = due_at + timedelta(days=1) if task.due_is_all_day else None
    is_canvas = "canvas" in task.tags
    external_url = _task_native_url(task)
    return CalendarEventView(
        event_id=f"task:{task.task_id}",
        title=task.title,
        starts_at=due_at,
        ends_at=ends_at,
        all_day=task.due_is_all_day,
        timezone=task.timezone,
        calendar_name="Canvas assignments" if is_canvas else "To-do list",
        source_id="personal-hub-tasks",
        source_name="Canvas" if is_canvas else "Personal Hub",
        description=task.notes,
        navigation_target=external_url or "",
        native_platform="canvas" if is_canvas else "",
        sensitive=True,
        item_kind="task",
        task_id=str(task.task_id),
        is_completed=task.state == "completed",
        completed_at=task.completed_at,
        completion_state=task.state,
    )


def _task_native_url(task: Any) -> str | None:
    external_url = task.external_url or None
    if "canvas" in task.tags:
        return canvas_assignment_url(external_url)
    return external_url


def _today_item_view(item: Any) -> TodayItemView:
    return TodayItemView(
        item_id=item.item_id,
        kind=item.kind,
        title=item.title,
        summary=item.summary or "",
        starts_at=item.starts_at,
        due_at=item.due_at,
        severity=item.severity,
        source=item.source.display_name,
        navigation_target=item.navigation_target,
        sensitive=item.sensitive,
        due_is_all_day=item.due_is_all_day,
        is_completed=getattr(item, "is_completed", False),
        completed_at=getattr(item, "completed_at", None),
    )


def _saved_calendar_selection(container: HubContainer) -> tuple[str, ...] | None:
    value = container.settings_repository.get("calendar_selected_ids", None)
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > 100:
        return ()
    selected: list[str] = []
    for item in value:
        if not isinstance(item, str) or len(item) > 385:
            continue
        if _parse_calendar_selection_key(item) is not None and item not in selected:
            selected.append(item)
    return tuple(selected)


def _feed_subscription_changes(
    value: Mapping[object, object],
    *,
    include_identity: bool,
) -> dict[str, object]:
    title = value.get("title")
    feed_url = value.get("feed_url")
    if not isinstance(title, str) or not isinstance(feed_url, str):
        raise TypeError("feed title and URL must be strings")
    try:
        refresh_minutes = int(value.get("refresh_minutes", 30))
    except (TypeError, ValueError) as error:
        raise ValueError("feed refresh interval must be an integer") from error
    changes: dict[str, object] = {
        "title": title,
        "feed_url": feed_url,
        "refresh_minutes": refresh_minutes,
        "include_terms": _bounded_text_sequence(value.get("include_terms"), max_items=64),
        "exclude_terms": _bounded_text_sequence(value.get("exclude_terms"), max_items=64),
        "digest_only": value.get("digest_only", False),
        "enabled": value.get("enabled", True),
        "allow_http": value.get("allow_http", False),
    }
    for key in ("digest_only", "enabled", "allow_http"):
        if not isinstance(changes[key], bool):
            raise TypeError(f"{key} must be a bool")
    site_url = value.get("site_url")
    if site_url not in {None, ""}:
        if not isinstance(site_url, str):
            raise TypeError("feed site URL must be a string")
        changes["site_url"] = site_url
    if include_identity:
        identifier = value.get("subscription_id")
        if identifier is not None:
            changes["subscription_id"] = UUID(str(identifier))
    return changes


def _watch_changes(
    value: Mapping[object, object],
    *,
    include_identity: bool,
) -> dict[str, object]:
    name, kind, source_url = value.get("name"), value.get("kind"), value.get("source_url")
    condition = value.get("condition")
    if not all(isinstance(item, str) for item in (name, kind, source_url)):
        raise TypeError("watch name, kind, and URL must be strings")
    if not isinstance(condition, Mapping):
        raise TypeError("watch condition must be a mapping")
    try:
        refresh_minutes = int(value.get("refresh_minutes", 60))
        cooldown_minutes = int(value.get("cooldown_minutes", 60))
    except (TypeError, ValueError) as error:
        raise ValueError("watch timing values must be integers") from error
    enabled, allow_http = value.get("enabled", True), value.get("allow_http", False)
    if not isinstance(enabled, bool) or not isinstance(allow_http, bool):
        raise TypeError("watch flags must be booleans")
    changes: dict[str, object] = {
        "name": name,
        "kind": kind,
        "source_url": source_url,
        "condition": dict(condition),
        "refresh_minutes": refresh_minutes,
        "cooldown_minutes": cooldown_minutes,
        "notification_policy": value.get("notification_policy", "immediate"),
        "enabled": enabled,
        "allow_http": allow_http,
        "approved_redirect_origins": _bounded_text_sequence(
            value.get("approved_redirect_origins"), max_items=8
        ),
    }
    if include_identity and value.get("watch_id") is not None:
        changes["watch_id"] = UUID(str(value["watch_id"]))
    return changes


def _watch_value_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Available" if value else "Unavailable"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return " ".join(str(value).split())[:160]


def _watch_summary(
    states: Sequence[WatchSyncState],
    *,
    has_watches: bool,
) -> tuple[str, str]:
    if not states:
        return (
            ("stale", "Watches have not been checked yet.")
            if has_watches
            else ("empty", "Add a typed webpage watch to track changes.")
        )
    priority = (
        "permission_required",
        "disconnected",
        "error",
        "offline",
        "stale",
        "ready",
    )
    present = {state.state for state in states}
    selected = next(candidate for candidate in priority if candidate in present)
    fallback = {
        "permission_required": "A watch needs permission review.",
        "disconnected": "A watched origin is unavailable.",
        "error": "A watch check failed safely.",
        "offline": "Watch refresh is paused by offline mode.",
        "stale": "Some watch observations may be stale.",
        "ready": "Watches are up to date.",
    }
    errors = tuple(dict.fromkeys(state.safe_error for state in states if state.safe_error))
    return selected, " ".join(errors)[:512] if errors else fallback[selected]


def _message_selection_text(query: MessageQuery) -> str:
    values: list[str] = []
    if query.folders:
        values.append("Folders: " + ", ".join(query.folders))
    if query.channels:
        values.append("Channels: " + ", ".join(query.channels))
    if query.query:
        values.append("Query: " + query.query)
    return " · ".join(values)[:500]


def _message_summary(
    states: Sequence[MessageSyncState],
    *,
    has_sources: bool,
) -> tuple[str, str]:
    if not states:
        return (
            ("stale", "Message sources have not been refreshed yet.")
            if has_sources
            else ("empty", "Configure a selected folder, channel, or saved query.")
        )
    priority = (
        "permission_required",
        "disconnected",
        "error",
        "offline",
        "stale",
        "ready",
        "empty",
    )
    present = {state.state for state in states}
    selected = next(candidate for candidate in priority if candidate in present)
    fallback = {
        "permission_required": "A message source requires read permission.",
        "disconnected": "A message source is disconnected.",
        "error": "A message summary refresh failed safely.",
        "offline": "Message refresh is paused by offline mode.",
        "stale": "Some message summaries may be stale.",
        "ready": "Message summaries are up to date.",
        "empty": "No messages match the selected views.",
    }
    errors = tuple(dict.fromkeys(state.safe_error for state in states if state.safe_error))
    return selected, " ".join(errors)[:512] if errors else fallback[selected]


def _bounded_text_sequence(value: object, *, max_items: int) -> tuple[str, ...]:
    if isinstance(value, str):
        candidates: Sequence[object] = tuple(
            part for line in value.splitlines() for part in line.split(",")
        )
    elif isinstance(value, (tuple, list)):
        candidates = value
    elif value is None:
        return ()
    else:
        raise TypeError("text values must be a string or sequence")
    if len(candidates) > max_items:
        raise ValueError("too many text values")
    normalized: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, str):
            raise TypeError("text sequence values must be strings")
        item = " ".join(candidate.split())
        if not item:
            continue
        if len(item) > 128:
            raise ValueError("text values must be bounded")
        if item.casefold() not in {existing.casefold() for existing in normalized}:
            normalized.append(item)
    return tuple(normalized)


def _saved_news_selection(container: HubContainer) -> tuple[str, ...] | None:
    value = container.settings_repository.get("news_selected_subscription_ids", None)
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > 1_000:
        return ()
    selected: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        try:
            normalized = str(UUID(item))
        except ValueError:
            continue
        if normalized not in selected:
            selected.append(normalized)
    return tuple(selected)


def _saved_news_filters(
    container: HubContainer,
) -> tuple[tuple[str, ...], tuple[str, ...], bool]:
    value = container.settings_repository.get("news_view_filters", {})
    if not isinstance(value, Mapping):
        return (), (), False
    try:
        include_terms = _bounded_text_sequence(value.get("include_terms"), max_items=64)
        exclude_terms = _bounded_text_sequence(value.get("exclude_terms"), max_items=64)
    except (TypeError, ValueError):
        return (), (), False
    unread_only = value.get("unread_only", False)
    return include_terms, exclude_terms, unread_only if isinstance(unread_only, bool) else False


def _saved_message_category(container: HubContainer) -> MessageCategory:
    value = container.settings_repository.get("message_category_filter", "primary")
    return cast(MessageCategory, value) if is_message_category(value) else "primary"


def _news_summary(
    states: tuple[FeedSyncState, ...],
    *,
    has_subscriptions: bool,
) -> tuple[str, str, datetime | None]:
    if not states:
        if has_subscriptions:
            return "disconnected", "Feeds have not been refreshed yet.", None
        return "empty", "Add an RSS or Atom feed to see news.", None
    priority = (
        "permission_required",
        "disconnected",
        "error",
        "offline",
        "stale",
        "ready",
        "empty",
    )
    present = {state.state for state in states}
    selected = next(candidate for candidate in priority if candidate in present)
    fallback = {
        "permission_required": "A feed needs permission review.",
        "disconnected": "A feed is disconnected.",
        "error": "A feed refresh failed safely.",
        "offline": "Feed refresh is paused by offline mode.",
        "stale": "Some feed data may be stale.",
        "ready": "Feeds are up to date.",
        "empty": "No feed entries are available.",
    }
    messages = tuple(dict.fromkeys(state.safe_error for state in states if state.safe_error))
    detail = " ".join(messages)[:512] if messages else fallback[selected]
    checked = tuple(state.last_checked_at for state in states if state.last_checked_at is not None)
    return selected, detail, max(checked) if checked else None


def _calendar_selection_key(source_id: str, calendar_id: str) -> str:
    return f"{source_id}/{calendar_id}"


def _parse_calendar_selection_key(value: str) -> tuple[str, str] | None:
    if "/" not in value or any(ord(character) < 32 for character in value):
        return None
    source_id, calendar_id = value.split("/", 1)
    if not source_id or not calendar_id or len(source_id) > 128 or len(calendar_id) > 256:
        return None
    return source_id, calendar_id


def _calendar_selection_applies(
    source_id: str,
    calendar_id: str,
    selected: tuple[str, ...] | None,
) -> bool:
    if selected is None:
        return True
    selected_ids = set(selected)
    return (
        _calendar_selection_key(source_id, calendar_id) in selected_ids
        or _calendar_selection_key(source_id, "*") in selected_ids
        or source_id in selected_ids
    )


def _calendar_selection_key_applies(
    calendar_key: str,
    selected: tuple[str, ...] | None,
) -> bool:
    parsed = _parse_calendar_selection_key(calendar_key)
    return parsed is not None and _calendar_selection_applies(*parsed, selected)


def _calendar_account_label(events: Sequence[Any], calendar_id: str) -> str:
    for event in events:
        if event.calendar_id == calendar_id and event.source.connection_id:
            return event.source.connection_id
    return ""


def _calendar_event_view_id(source_id: str, event_id: str) -> str:
    value = f"{source_id}:{event_id}"
    if len(value) <= 220:
        return value
    return f"calendar:{sha256(value.encode('utf-8')).hexdigest()}"


def _calendar_event_sort_key(event: CalendarEventView) -> tuple[object, ...]:
    fallback = datetime.max.replace(tzinfo=UTC)
    starts_at = event.starts_at if isinstance(event.starts_at, datetime) else fallback
    ends_at = event.ends_at if isinstance(event.ends_at, datetime) else fallback
    return (
        starts_at,
        0 if event.all_day else 1,
        ends_at,
        event.title.casefold(),
        event.event_id,
    )


def _calendar_summary(
    results: tuple[CalendarResult, ...],
    *,
    has_sources: bool,
) -> tuple[str, str, datetime | None]:
    if not results:
        if has_sources:
            return "disconnected", "Calendars have not been refreshed yet.", None
        return "empty", "Connect a read-only calendar to see events.", None
    priority = (
        "permission_required",
        "disconnected",
        "error",
        "offline",
        "stale",
        "ready",
        "empty",
    )
    states = {result.state for result in results}
    state = next(candidate for candidate in priority if candidate in states)
    messages = tuple(dict.fromkeys(result.safe_message for result in results))
    detail = " ".join(messages)[:512]
    generated_at = max(result.checked_at for result in results)
    return state, detail, generated_at


def _import_result_summary(result: object) -> str:
    """Format a bounded, non-sensitive import outcome for the desktop status label."""

    imported = int(getattr(result, "imported_count", 0) or 0)
    updated = int(getattr(result, "updated_count", 0) or 0)
    issues = tuple(getattr(result, "issues", ()) or ())
    parts = [f"Imported {imported} item(s)"]
    if updated:
        parts.append(f"updated {updated}")
    if issues:
        parts.append(f"skipped {len(issues)} invalid row(s)")
    return "; ".join(parts) + "."


def _recurrence(value: str | None) -> Recurrence | None:
    if not value:
        return None
    normalized = value.strip().casefold()
    if normalized not in {"daily", "weekly", "monthly", "yearly"}:
        raise ValueError("unsupported task recurrence")
    return Recurrence(frequency=normalized)  # type: ignore[arg-type]


def _format_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat(timespec="seconds")


__all__ = ["HubDesktopCoordinator", "run_hub_desktop"]
