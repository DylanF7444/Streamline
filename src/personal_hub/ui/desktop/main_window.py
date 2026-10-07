"""Independent Personal Hub desktop shell and read-only view surfaces.

The UI owns presentation only.  Inbox, source, settings, diagnostics, tray,
and failure APIs accept small Hub-owned view objects (or plain mappings) and
emit signals for the composition root to handle.  No provider, database,
network, lifecycle, or conversational UI code is imported here.
"""

from __future__ import annotations

import re
import sys
from calendar import monthrange
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, tzinfo
from html import unescape
from typing import Any, Final
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from PySide6.QtCore import QDateTime, QEvent, QObject, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFontMetrics, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QSystemTrayIcon,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from personal_hub.calendar.schedule import build_hourly_schedule_layout, event_local_date
from personal_hub.messages.categories import (
    MESSAGE_CATEGORY_LABELS,
    MESSAGE_CATEGORY_VALUES,
    is_message_category,
    message_category_label,
)
from personal_hub.ui.desktop.appearance import AppearancePanel, theme_stylesheet
from personal_hub.ui.desktop.dashboard import DashboardBoard
from personal_hub.ui.desktop.hourly_calendar import HourlyCalendarGrid
from personal_hub.ui.desktop.mini_calendar import MiniCalendar
from personal_hub.ui.desktop.motion import PageTransition

APPLICATION_NAME: Final = "Personal Hub"
HOME_DESTINATION: Final = "Home"
# Retain the public constant and Today view APIs for existing callers and saved links.
TODAY_DESTINATION: Final = HOME_DESTINATION
NAVIGATION_DESTINATIONS: Final[tuple[str, ...]] = (
    TODAY_DESTINATION,
    "Calendar",
    "Tasks",
    "Watchlists",
    "Inbox",
    "Sources",
    "Settings",
)
NAVIGATION_LABELS: Final[tuple[str, ...]] = NAVIGATION_DESTINATIONS
PLANNED_NAVIGATION_DESTINATIONS: Final[tuple[str, ...]] = NAVIGATION_DESTINATIONS[1:]

_NAVIGATION_OBJECT_NAMES: Final[dict[str, str]] = {
    HOME_DESTINATION: "todayButton",
    "Calendar": "calendarButton",
    "Tasks": "tasksButton",
    "Watchlists": "watchlistsButton",
    "Inbox": "inboxButton",
    "Sources": "sourcesButton",
    "Settings": "settingsButton",
}
_MAX_TEXT_LENGTH: Final = 500
_MAX_ID_LENGTH: Final = 128


@dataclass(frozen=True, slots=True)
class InboxItemView:
    """Safe display data for one Inbox row.

    This is intentionally a presentation contract, not a storage or provider
    model.  Message-like rows default to sensitive so privacy masking is safe
    when a caller omits the flag.
    """

    item_id: str
    title: str
    summary: str = ""
    source: str = ""
    timestamp: str = ""
    severity: str = "info"
    sensitive: bool = True
    unread: bool = True
    state: str = "unread"


@dataclass(frozen=True, slots=True)
class AttentionNotificationView:
    """Ephemeral count-only notice shown above the page content."""

    notice_id: str
    title: str = "Attention messages"
    summary: str = ""
    action_label: str = "Review messages"
    navigation_target: str = "hub://inbox/messages"
    sensitive: bool = True


@dataclass(frozen=True, slots=True)
class SourceView:
    """Safe display data for one configured source."""

    source_id: str
    display_name: str
    status: str = "disconnected"
    detail: str = ""
    last_success_at: str = ""
    sensitive: bool = False
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class SettingsView:
    """The small set of settings needed by the Phase 1 shell."""

    offline_mode: bool = False
    privacy_masking: bool = False
    close_to_tray: bool = True
    start_at_login: bool = False
    notifications_enabled: bool = True
    quiet_hours_enabled: bool = False
    quiet_hours_start: str = "22:00"
    quiet_hours_end: str = "07:00"
    default_timezone: str = "UTC"
    calendar_refresh_minutes: int = 15
    digest_enabled: bool = True
    digest_time: str = "08:00"
    inbox_retention_days: int = 90
    audit_retention_days: int = 30
    feed_retention_days: int = 30
    feed_refresh_minutes: int = 30
    watch_retention_days: int = 90
    watch_refresh_minutes: int = 60
    message_retention_days: int = 7
    message_refresh_minutes: int = 10
    canvas_remove_completed_assignments: bool = False
    text_scale: float = 1.0
    high_contrast: bool = False
    reduced_motion: bool = False
    ui_theme: str = "navy"
    ui_accent_color: str = ""
    ui_background_color: str = ""
    ui_surface_color: str = ""
    network_refresh_paused: bool = False


@dataclass(frozen=True, slots=True)
class SyncView:
    """Safe presentation data for the encrypted shared-folder sync card."""

    state: str = "not_configured"
    enabled: bool = False
    endpoint: str = ""
    profile_paired: bool = False
    last_sync_at: str = ""
    last_error: str = ""
    remote_device_count: int = 0
    record_count: int = 0


@dataclass(frozen=True, slots=True)
class SourceFailureView:
    """A bounded, user-safe source failure summary."""

    source_id: str
    message: str
    display_name: str = ""
    retryable: bool = True
    last_success_at: str | None = None


@dataclass(frozen=True, slots=True)
class DiagnosticsView:
    """Safe diagnostic status for the Settings diagnostics skeleton."""

    status: str = "ready"
    detail: str = ""
    entries: tuple[str, ...] = ()
    source_failures: tuple[SourceFailureView, ...] = ()
    generated_at: str | None = None


@dataclass(frozen=True, slots=True)
class TaskView:
    """Presentation-only view of one Hub task.

    The desktop surface deliberately uses this small plain dataclass instead
    of importing the Hub repository or task domain.  Callers may also provide
    mappings or structural objects with the same field names to keep the UI
    replaceable while the Phase 2 composition root is being assembled.
    """

    task_id: str
    title: str
    notes: str = ""
    state: str = "open"
    priority: str = "normal"
    due_at: datetime | date | str | None = None
    due_is_all_day: bool = False
    timezone: str = "local"
    list_id: str | None = None
    tags: tuple[str, ...] = ()
    recurrence: str | None = None
    reminder_offsets_minutes: tuple[int, ...] = ()
    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None
    external_url: str = ""
    native_platform: str = ""

    @property
    def id(self) -> str:
        """Convenience alias for view consumers that use generic IDs."""

        return self.task_id

    @property
    def all_day(self) -> bool:
        """Convenience alias for editor and mapping-oriented callers."""

        return self.due_is_all_day

    @property
    def list_name(self) -> str:
        """Return the displayable list identifier when one is available."""

        return self.list_id or ""

    @property
    def reminders(self) -> tuple[int, ...]:
        """Convenience alias for reminder offsets."""

        return self.reminder_offsets_minutes

    @property
    def due(self) -> datetime | date | str | None:
        return self.due_at

    @property
    def status(self) -> str:
        return self.state

    @property
    def navigation_target(self) -> str:
        """Compatibility alias for the source link used by navigation views."""

        return self.external_url


# Explicit aliases make the contract discoverable without tying callers to a
# domain model name.  They are all the same immutable, presentation-only view.
HubTaskView = TaskView
TaskItemView = TaskView


@dataclass(frozen=True, slots=True)
class TodayItemView:
    """Safe display data for an item inside a Today section."""

    item_id: str
    kind: str = "task"
    title: str = ""
    summary: str = ""
    starts_at: datetime | date | str | None = None
    due_at: datetime | date | str | None = None
    severity: str = "info"
    source: str = ""
    navigation_target: str = ""
    sensitive: bool = False
    due_is_all_day: bool = False
    is_completed: bool = False
    completed_at: datetime | date | str | None = None

    @property
    def completed(self) -> bool:
        return self.is_completed

    @property
    def done(self) -> bool:
        return self.is_completed

    @property
    def is_done(self) -> bool:
        return self.is_completed


@dataclass(frozen=True, slots=True)
class TodaySectionView:
    """One ordered, independently hideable Today section."""

    section_id: str
    title: str
    body: str = ""
    items: tuple[object, ...] = ()
    visible: bool = True

    @property
    def id(self) -> str:
        return self.section_id

    @property
    def entries(self) -> tuple[object, ...]:
        return self.items


@dataclass(frozen=True, slots=True)
class TodayView:
    """Presentation data for the ordered Today projection."""

    sections: tuple[TodaySectionView, ...] = ()
    local_date: str = ""
    timezone: str = ""
    generated_at: datetime | date | str | None = None


HubTodayItemView = TodayItemView
HubTodaySectionView = TodaySectionView
HubTodayView = TodayView


@dataclass(frozen=True, slots=True)
class CalendarEventView:
    """Provider event or due-task data rendered on the calendar."""

    event_id: str
    title: str
    starts_at: datetime | date | str | None = None
    ends_at: datetime | date | str | None = None
    all_day: bool = False
    timezone: str = "local"
    calendar_id: str | None = None
    calendar_name: str = ""
    source_id: str | None = None
    source_name: str = ""
    account: str = ""
    location: str = ""
    description: str = ""
    navigation_target: str = ""
    sensitive: bool = True
    item_kind: str = "event"
    task_id: str = ""
    is_completed: bool = False
    completed_at: datetime | date | str | None = None
    completion_state: str = "open"
    external_event_id: str = ""
    external_calendar_id: str = ""
    native_platform: str = ""

    @property
    def id(self) -> str:
        return self.event_id

    @property
    def start_at(self) -> datetime | date | str | None:
        return self.starts_at

    @property
    def end_at(self) -> datetime | date | str | None:
        return self.ends_at

    @property
    def due_is_all_day(self) -> bool:
        return self.all_day

    @property
    def completed(self) -> bool:
        return self.is_completed

    @property
    def done(self) -> bool:
        return self.is_completed

    @property
    def locally_completed(self) -> bool:
        return self.is_completed

    @property
    def is_done(self) -> bool:
        return self.is_completed

    @property
    def is_task(self) -> bool:
        return self.item_kind == "task" and bool(self.task_id)


@dataclass(frozen=True, slots=True)
class CalendarSourceView:
    """A selected-calendar/account indicator shown above the views."""

    calendar_id: str
    display_name: str
    source_id: str = ""
    source_name: str = ""
    account: str = ""
    selected: bool = True
    status: str = "ready"
    sensitive: bool = False

    @property
    def id(self) -> str:
        return self.calendar_id


@dataclass(frozen=True, slots=True)
class CalendarImportView:
    """Presentation data for one explicitly local calendar import batch."""

    import_id: str
    name: str
    source_format: str = "csv"
    event_count: int = 0

    @property
    def id(self) -> str:
        return self.import_id

    @property
    def title(self) -> str:
        return self.name


@dataclass(frozen=True, slots=True)
class CalendarView:
    """Bounded presentation data for a read-only calendar projection."""

    events: tuple[CalendarEventView, ...] = ()
    calendars: tuple[CalendarSourceView, ...] = ()
    selected_calendar_ids: tuple[str, ...] = ()
    timezone: str = ""
    freshness: str = "ready"
    detail: str = ""
    generated_at: datetime | date | str | None = None
    cursor_date: date | str | None = None
    ical_feeds: tuple[IcalFeedView, ...] = ()
    local_imports: tuple[CalendarImportView, ...] = ()

    @property
    def sources(self) -> tuple[CalendarSourceView, ...]:
        return self.calendars


HubCalendarEventView = CalendarEventView
HubCalendarSourceView = CalendarSourceView
HubCalendarView = CalendarView
HubCalendarImportView = CalendarImportView


@dataclass(frozen=True, slots=True)
class IcalFeedView:
    """Presentation data for one locally imported iCalendar feed."""

    feed_id: str
    title: str
    feed_url: str = ""
    enabled: bool = True
    refresh_minutes: int = 30
    status: str = "disconnected"
    detail: str = ""
    last_success_at: str | None = None

    @property
    def id(self) -> str:
        return self.feed_id


@dataclass(frozen=True, slots=True)
class FeedSubscriptionView:
    """Presentation data for one RSS or Atom subscription.

    The desktop layer deliberately keeps the URL and status as bounded plain
    values.  It never fetches a feed or persists a subscription; callers
    provide this view from the Hub feed service and consume the emitted
    selection/filter signals in the composition root.
    """

    subscription_id: str
    title: str
    feed_url: str = ""
    site_url: str = ""
    enabled: bool = True
    refresh_minutes: int = 30
    include_terms: tuple[str, ...] = ()
    exclude_terms: tuple[str, ...] = ()
    digest_only: bool = False
    status: str = "ready"
    detail: str = ""
    last_success_at: str | None = None
    unread_count: int = 0
    entry_count: int = 0
    source_id: str = ""
    source_name: str = ""
    account: str = ""
    sensitive: bool = False

    @property
    def id(self) -> str:
        return self.subscription_id

    @property
    def url(self) -> str:
        return self.feed_url


@dataclass(frozen=True, slots=True)
class FeedEntryView:
    """Safe, plain-text presentation data for one feed entry."""

    entry_id: str
    subscription_id: str
    title: str
    url: str = ""
    author: str = ""
    published_at: datetime | date | str | None = None
    summary_text: str = ""
    first_seen_at: datetime | date | str | None = None
    read: bool = False
    source_name: str = ""
    source_url: str = ""
    navigation_target: str = ""
    sensitive: bool = False

    @property
    def id(self) -> str:
        return self.entry_id

    @property
    def is_read(self) -> bool:
        return self.read

    @property
    def unread(self) -> bool:
        return not self.read

    @property
    def summary(self) -> str:
        return self.summary_text


@dataclass(frozen=True, slots=True)
class NewsView:
    """Bounded feed projection used by News and the Watchlists page."""

    subscriptions: tuple[FeedSubscriptionView, ...] = ()
    entries: tuple[FeedEntryView, ...] = ()
    selected_subscription_ids: tuple[str, ...] = ()
    selection_explicit: bool = False
    include_terms: tuple[str, ...] = ()
    exclude_terms: tuple[str, ...] = ()
    unread_only: bool = False
    freshness: str = "ready"
    detail: str = ""
    generated_at: datetime | date | str | None = None
    max_entries: int = 100

    @property
    def feeds(self) -> tuple[FeedSubscriptionView, ...]:
        return self.subscriptions

    @property
    def feed_entries(self) -> tuple[FeedEntryView, ...]:
        return self.entries

    @property
    def selected_feed_ids(self) -> tuple[str, ...]:
        return self.selected_subscription_ids


@dataclass(frozen=True, slots=True)
class WebWatchView:
    """Bounded presentation data for one typed webpage watch."""

    watch_id: str
    name: str
    kind: str
    source_url: str
    condition_kind: str = "changed"
    threshold: float | None = None
    percentage: float | None = None
    term: str = ""
    selector: str = ""
    refresh_minutes: int = 60
    enabled: bool = True
    notification_policy: str = "immediate"
    cooldown_minutes: int = 60
    allow_http: bool = False
    status: str = "stale"
    detail: str = ""
    last_checked_at: datetime | date | str | None = None
    last_value: str = ""
    sensitive: bool = True


@dataclass(frozen=True, slots=True)
class WatchAlertView:
    alert_id: str
    watch_id: str
    title: str
    summary: str
    reason_code: str
    observed_at: datetime | date | str | None = None
    sensitive: bool = True


@dataclass(frozen=True, slots=True)
class WebWatchesView:
    watches: tuple[WebWatchView, ...] = ()
    alerts: tuple[WatchAlertView, ...] = ()
    freshness: str = "ready"
    detail: str = ""


@dataclass(frozen=True, slots=True)
class MessageSourceView:
    """Configured read-only message source without credentials or message content."""

    source_id: str
    provider: str
    account: str = ""
    selection: str = ""
    status: str = "stale"
    detail: str = ""
    enabled: bool = True
    sensitive: bool = True


@dataclass(frozen=True, slots=True)
class MessageSummaryView:
    """Bounded summary shown before an explicit body fetch."""

    source_id: str
    message_id: str
    provider: str
    account: str
    sender: str
    title: str
    preview: str = ""
    received_at: datetime | date | str | None = None
    unread: bool = True
    importance: str = "normal"
    deep_link: str = ""
    sensitive: bool = True
    category: str = "primary"


@dataclass(frozen=True, slots=True)
class MessageBodyView:
    """Sanitized, memory-only body projection for the explicit body panel."""

    source_id: str
    message_id: str
    plain_text: str = ""
    links: tuple[str, ...] = ()
    fetched_at: datetime | date | str | None = None
    sensitive: bool = True


@dataclass(frozen=True, slots=True)
class MessagesView:
    sources: tuple[MessageSourceView, ...] = ()
    summaries: tuple[MessageSummaryView, ...] = ()
    freshness: str = "ready"
    detail: str = ""
    selected_category: str = "primary"
    category_counts: tuple[tuple[str, int], ...] = ()


# Explicit aliases make it possible for the Phase 4 service/controller to use
# either domain terminology without making the view import that service.
FeedView = NewsView
NewsFeedView = NewsView
WatchlistView = NewsView
HubFeedSubscriptionView = FeedSubscriptionView
HubFeedEntryView = FeedEntryView
HubNewsView = NewsView
HubFeedView = NewsView


_NEWS_FRESHNESS_STATES: Final[frozenset[str]] = frozenset(
    {
        "ready",
        "empty",
        "stale",
        "offline",
        "permission_required",
        "disconnected",
        "error",
        "refreshing",
    }
)
_NEWS_FRESHNESS_MESSAGES: Final[dict[str, str]] = {
    "ready": "Ready",
    "empty": "No new entries",
    "stale": "Stale · showing the last successful entries",
    "offline": "Offline · refresh is paused",
    "permission_required": "Permission required",
    "disconnected": "Disconnected",
    "error": "Error · feed entries are unavailable",
    "refreshing": "Refreshing",
}
_MAX_NEWS_ENTRIES: Final = 200


_CALENDAR_FRESHNESS_STATES: Final[frozenset[str]] = frozenset(
    {
        "ready",
        "empty",
        "stale",
        "offline",
        "permission_required",
        "disconnected",
        "error",
        "refreshing",
    }
)
_CALENDAR_FRESHNESS_MESSAGES: Final[dict[str, str]] = {
    "ready": "Ready",
    "empty": "No events in this view",
    "stale": "Stale · showing the last successful snapshot",
    "offline": "Offline · refresh is paused",
    "permission_required": "Permission required",
    "disconnected": "Disconnected",
    "error": "Error · calendar data is unavailable",
    "refreshing": "Refreshing",
}

# These are deliberately dark, high-contrast accents that work on the Hub's
# navy surface.  They identify a source visually while the source name remains
# present in the accessible label and event details.
_CALENDAR_EVENT_COLORS: Final[tuple[str, ...]] = (
    "#2f80ed",  # blue
    "#159570",  # green
    "#c77700",  # amber
    "#8056c7",  # purple
    "#c24b7f",  # pink
    "#128c98",  # teal
    "#c34c4c",  # red
    "#657f3c",  # olive
)


def _safe_text(value: object, *, fallback: str = "", limit: int = _MAX_TEXT_LENGTH) -> str:
    """Convert view data to bounded plain text suitable for Qt labels."""

    if value is None:
        return fallback
    text = str(value).replace("\x00", "").strip()
    if not text:
        return fallback
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _timezone_names() -> tuple[str, ...]:
    """Return the supported IANA time zones in a stable, user-selectable order."""

    try:
        names = available_timezones()
    except OSError:
        # ``tzdata`` is a runtime dependency, but keep the UI usable if a platform
        # timezone database is temporarily unavailable.
        names = set()
    filtered = {
        name
        for name in names
        if name
        and not name.startswith(("posix/", "right/"))
        and name not in {"Factory", "localtime"}
    }
    filtered.add("UTC")
    return ("UTC", *sorted((name for name in filtered if name != "UTC"), key=str.casefold))


_TIMEZONE_ITEMS: Final[tuple[tuple[str, str], ...]] = tuple(
    (name, name) for name in _timezone_names()
)


class TimezoneComboBox(QComboBox):
    """Non-editable timezone selector backed by IANA timezone identifiers."""

    def __init__(
        self,
        *,
        include_local: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._fallback_timezone = "local" if include_local else "UTC"
        self.setEditable(False)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.setDuplicatesEnabled(False)
        self.setMaxVisibleItems(20)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(18)
        if include_local:
            self.addItem("Local time", "local")
        self.addItems([label for label, _value in _TIMEZONE_ITEMS])
        for index, (_label, value) in enumerate(_TIMEZONE_ITEMS):
            self.setItemData(index + (1 if include_local else 0), value)
        self.set_timezone(self._fallback_timezone)

    def set_timezone(self, value: object) -> None:
        """Select a timezone value, safely falling back to the default option."""

        normalized = _safe_text(value, fallback=self._fallback_timezone, limit=128)
        index = self.findData(normalized)
        if index < 0:
            try:
                ZoneInfo(normalized)
            except (TypeError, ValueError, ZoneInfoNotFoundError):
                normalized = self._fallback_timezone
                index = self.findData(normalized)
            else:
                # Preserve a valid timezone from an older or externally supplied
                # setting without making the control editable.
                self.addItem(normalized, normalized)
                index = self.findData(normalized)
        if index >= 0:
            self.setCurrentIndex(index)

    def timezone(self) -> str:
        """Return the stored timezone identifier for the selected option."""

        value = self.currentData()
        return _safe_text(value, fallback=self.currentText(), limit=128)

    # Keep the old line-edit-shaped methods as a small compatibility bridge for
    # callers that populate view controls programmatically.
    def setText(self, value: object) -> None:  # noqa: N802 - Qt compatibility name
        self.set_timezone(value)

    def text(self) -> str:  # noqa: N802 - Qt compatibility name
        return self.timezone()

    def setCurrentText(self, text: str) -> None:  # noqa: N802 - Qt API name
        index = self.findText(text)
        if index < 0:
            index = self.findData(text)
        if index >= 0:
            self.setCurrentIndex(index)


def _field(value: object, name: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _task_identifier(value: object) -> str:
    return _safe_text(
        _field(value, "task_id", _field(value, "id", value)),
        limit=_MAX_ID_LENGTH,
    )


def _bool_value(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes", "on", "enabled"}:
            return True
        if normalized in {"false", "0", "no", "off", "disabled"}:
            return False
    return bool(value)


def _int_value(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _iter_values(value: object) -> tuple[object, ...]:
    if value is None:
        return ()
    if isinstance(value, (Mapping, str)):
        return (value,)
    try:
        return tuple(value)  # type: ignore[arg-type]
    except TypeError:
        return (value,)


def _source_display_name(value: object) -> str:
    if isinstance(value, Mapping):
        return _safe_text(
            value.get("display_name", value.get("name", value.get("provider", ""))),
            limit=180,
        )
    return _safe_text(value, limit=180)


def _coerce_inbox_item(value: object, index: int) -> InboxItemView:
    if isinstance(value, InboxItemView):
        return value
    item_id = _safe_text(
        _field(value, "item_id", _field(value, "id", f"inbox-{index}")),
        fallback=f"inbox-{index}",
        limit=_MAX_ID_LENGTH,
    )
    severity = _safe_text(_field(value, "severity", "info"), fallback="info", limit=32).casefold()
    if severity not in {"info", "attention", "urgent"}:
        severity = "info"
    source = _field(value, "source", _field(value, "source_name", ""))
    state = _safe_text(_field(value, "state", ""), limit=24).casefold()
    if state not in {"unread", "read", "dismissed", "archived"}:
        state = "unread" if _bool_value(_field(value, "unread", True), default=True) else "read"
    return InboxItemView(
        item_id=item_id,
        title=_safe_text(_field(value, "title", "Attention item"), fallback="Attention item"),
        summary=_safe_text(_field(value, "summary", _field(value, "preview", ""))),
        source=_source_display_name(source),
        timestamp=_safe_text(
            _field(
                value, "timestamp", _field(value, "received_at", _field(value, "observed_at", ""))
            ),
            limit=120,
        ),
        severity=severity,
        sensitive=_bool_value(_field(value, "sensitive", True), default=True),
        unread=state == "unread",
        state=state,
    )


def _coerce_attention_notification(value: object, index: int = 0) -> AttentionNotificationView:
    if isinstance(value, AttentionNotificationView):
        return value
    notice_id = _safe_text(
        _field(value, "notice_id", _field(value, "notification_id", f"attention-{index}")),
        fallback=f"attention-{index}",
        limit=_MAX_ID_LENGTH,
    )
    unread_count = _int_value(_field(value, "unread_count", 0), 0)
    source_count = _int_value(_field(value, "source_count", 0), 0)
    summary = _safe_text(
        _field(value, "safe_summary", _field(value, "summary", _field(value, "message", "")))
    )
    if not summary and unread_count > 0:
        source_label = (
            f"{source_count} selected source"
            if source_count == 1
            else (f"{source_count} selected sources")
        )
        summary = f"{unread_count} unread message(s) from {source_label}."
    return AttentionNotificationView(
        notice_id=notice_id,
        title=_safe_text(
            _field(value, "title", "Attention messages"),
            fallback="Attention messages",
            limit=120,
        ),
        summary=summary,
        action_label=_safe_text(
            _field(value, "action_label", "Review messages"),
            fallback="Review messages",
            limit=64,
        ),
        navigation_target=_safe_text(
            _field(value, "navigation_target", "hub://inbox/messages"),
            fallback="hub://inbox/messages",
            limit=256,
        ),
        sensitive=_bool_value(_field(value, "sensitive", True), default=True),
    )


def _coerce_source(value: object, index: int) -> SourceView:
    if isinstance(value, SourceView):
        return value
    source_id = _safe_text(
        _field(value, "source_id", _field(value, "id", f"source-{index}")),
        fallback=f"source-{index}",
        limit=_MAX_ID_LENGTH,
    )
    return SourceView(
        source_id=source_id,
        display_name=_safe_text(
            _field(value, "display_name", _field(value, "name", "Source")),
            fallback="Source",
            limit=180,
        ),
        status=_safe_text(
            _field(value, "status", _field(value, "health", "disconnected")),
            fallback="disconnected",
            limit=48,
        ),
        detail=_safe_text(_field(value, "detail", _field(value, "message", ""))),
        last_success_at=_safe_text(
            _field(value, "last_success_at", _field(value, "last_sync", "")),
            limit=120,
        ),
        sensitive=_bool_value(_field(value, "sensitive", False)),
        enabled=_bool_value(_field(value, "enabled", True), default=True),
    )


def _coerce_sync(value: SyncView | Mapping[str, object] | None) -> SyncView:
    if isinstance(value, SyncView):
        return value
    data: object = value or {}
    return SyncView(
        state=_safe_text(
            _field(data, "state", "not_configured"), fallback="not_configured", limit=32
        ),
        enabled=_bool_value(_field(data, "enabled", False)),
        endpoint=_safe_text(_field(data, "endpoint", ""), limit=1_024),
        profile_paired=_bool_value(_field(data, "profile_paired", False)),
        last_sync_at=_safe_text(_field(data, "last_sync_at", ""), limit=120),
        last_error=_safe_text(_field(data, "last_error", ""), limit=240),
        remote_device_count=max(0, _int_value(_field(data, "remote_device_count", 0), 0)),
        record_count=max(0, _int_value(_field(data, "record_count", 0), 0)),
    )


def _coerce_settings(value: SettingsView | Mapping[str, object] | None) -> SettingsView:
    if isinstance(value, SettingsView):
        return value
    data: object = value or {}
    try:
        text_scale = float(_field(data, "text_scale", _field(data, "ui_text_scale", 1.0)) or 1.0)
    except (TypeError, ValueError):
        text_scale = 1.0
    return SettingsView(
        offline_mode=_bool_value(_field(data, "offline_mode", _field(data, "offline", False))),
        privacy_masking=_bool_value(
            _field(data, "privacy_masking", _field(data, "hide_sensitive_display", False))
        ),
        close_to_tray=_bool_value(_field(data, "close_to_tray", True), default=True),
        start_at_login=_bool_value(_field(data, "start_at_login", False)),
        notifications_enabled=_bool_value(
            _field(data, "notifications_enabled", True), default=True
        ),
        quiet_hours_enabled=_bool_value(
            _field(
                data, "quiet_hours_enabled", _field(data, "notification_quiet_hours_enabled", False)
            )
        ),
        quiet_hours_start=_safe_text(
            _field(
                data, "quiet_hours_start", _field(data, "notification_quiet_hours_start", "22:00")
            ),
            fallback="22:00",
            limit=5,
        ),
        quiet_hours_end=_safe_text(
            _field(data, "quiet_hours_end", _field(data, "notification_quiet_hours_end", "07:00")),
            fallback="07:00",
            limit=5,
        ),
        default_timezone=_safe_text(
            _field(data, "default_timezone", "UTC"), fallback="UTC", limit=64
        ),
        calendar_refresh_minutes=_int_value(_field(data, "calendar_refresh_minutes", 15), 15),
        digest_enabled=_bool_value(_field(data, "digest_enabled", True), default=True),
        digest_time=_safe_text(_field(data, "digest_time", "08:00"), fallback="08:00", limit=5),
        inbox_retention_days=_int_value(_field(data, "inbox_retention_days", 90), 90),
        audit_retention_days=_int_value(_field(data, "audit_retention_days", 30), 30),
        feed_retention_days=_int_value(_field(data, "feed_retention_days", 30), 30),
        feed_refresh_minutes=_int_value(_field(data, "feed_refresh_minutes", 30), 30),
        watch_retention_days=_int_value(
            _field(
                data,
                "watch_retention_days",
                _field(data, "watch_observation_retention_days", 90),
            ),
            90,
        ),
        watch_refresh_minutes=_int_value(_field(data, "watch_refresh_minutes", 60), 60),
        message_retention_days=_int_value(
            _field(
                data,
                "message_retention_days",
                _field(data, "message_summary_retention_days", 7),
            ),
            7,
        ),
        message_refresh_minutes=_int_value(_field(data, "message_refresh_minutes", 10), 10),
        canvas_remove_completed_assignments=_bool_value(
            _field(data, "canvas_remove_completed_assignments", False)
        ),
        text_scale=text_scale,
        high_contrast=_bool_value(
            _field(data, "high_contrast", _field(data, "ui_high_contrast", False))
        ),
        reduced_motion=_bool_value(
            _field(data, "reduced_motion", _field(data, "ui_reduced_motion", False))
        ),
        ui_theme=_safe_text(_field(data, "ui_theme", "navy"), limit=16),
        ui_accent_color=_safe_text(_field(data, "ui_accent_color", ""), limit=7),
        ui_background_color=_safe_text(_field(data, "ui_background_color", ""), limit=7),
        ui_surface_color=_safe_text(_field(data, "ui_surface_color", ""), limit=7),
        network_refresh_paused=_bool_value(
            _field(data, "network_refresh_paused", _field(data, "refresh_paused", False))
        ),
    )


def _coerce_failure(
    source: SourceFailureView | Mapping[str, object] | str,
    message: str | None = None,
    *,
    retryable: bool = True,
    last_success_at: str | None = None,
) -> SourceFailureView:
    if isinstance(source, SourceFailureView):
        return source
    if isinstance(source, Mapping):
        source_id = _safe_text(
            source.get("source_id", source.get("id", source.get("provider", "source"))),
            fallback="source",
            limit=_MAX_ID_LENGTH,
        )
        failure_message = _safe_text(
            source.get("message", source.get("safe_message", message or "Source refresh failed")),
            fallback="Source refresh failed",
        )
        display_name = _safe_text(
            source.get("display_name", source.get("name", source_id)),
            fallback=source_id,
            limit=180,
        )
        return SourceFailureView(
            source_id=source_id,
            message=failure_message,
            display_name=display_name,
            retryable=_bool_value(source.get("retryable", retryable), default=retryable),
            last_success_at=_safe_text(
                source.get("last_success_at", source.get("last_sync", last_success_at)),
                limit=120,
            )
            or None,
        )
    source_id = _safe_text(source, fallback="source", limit=_MAX_ID_LENGTH)
    return SourceFailureView(
        source_id=source_id,
        message=_safe_text(message, fallback="Source refresh failed"),
        display_name=source_id,
        retryable=retryable,
        last_success_at=_safe_text(last_success_at, limit=120) or None,
    )


def _coerce_diagnostics(value: DiagnosticsView | Mapping[str, object] | None) -> DiagnosticsView:
    if isinstance(value, DiagnosticsView):
        return value
    data: object = value or {}
    raw_entries = _field(data, "entries", _field(data, "events", ()))
    entries: list[str] = []
    for entry in _iter_values(raw_entries):
        if isinstance(entry, Mapping):
            key = _safe_text(
                entry.get("label", entry.get("event", entry.get("name", "Entry"))),
                fallback="Entry",
                limit=100,
            )
            detail = _safe_text(entry.get("detail", entry.get("message", "")), limit=260)
            entries.append(f"{key}: {detail}" if detail else key)
        else:
            entries.append(_safe_text(entry, fallback="Entry", limit=360))
    failures = tuple(
        _coerce_failure(item)
        for item in _iter_values(_field(data, "source_failures", _field(data, "failures", ())))
    )
    return DiagnosticsView(
        status=_safe_text(_field(data, "status", "ready"), fallback="ready", limit=48),
        detail=_safe_text(_field(data, "detail", _field(data, "message", ""))),
        entries=tuple(entries),
        source_failures=failures,
        generated_at=_safe_text(
            _field(data, "generated_at", _field(data, "collected_at", "")),
            limit=120,
        )
        or None,
    )


def _coerce_datetime(value: object) -> datetime | None:
    """Best-effort conversion for display and local filter calculations."""

    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    text = _safe_text(value, limit=128)
    if not text:
        return None
    normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        try:
            return datetime.combine(date.fromisoformat(text), time.min)
        except ValueError:
            return None


def _qt_datetime(value: datetime) -> QDateTime:
    """Create a Qt datetime without requiring a Qt/domain conversion layer."""

    return QDateTime(
        value.year,
        value.month,
        value.day,
        value.hour,
        value.minute,
        value.second,
        value.microsecond // 1_000_000,
    )


def _task_editor_timezone(value: object) -> tzinfo:
    """Resolve the timezone used for wall-clock values entered in the task form."""

    normalized = _safe_text(value, fallback="local", limit=128)
    if normalized.casefold() == "local":
        return datetime.now().astimezone().tzinfo or ZoneInfo("UTC")
    try:
        return ZoneInfo(normalized)
    except (TypeError, ValueError, ZoneInfoNotFoundError) as error:
        raise ValueError("Task timezone is not recognized") from error


def _task_editor_due_datetime(value: object, timezone: object) -> datetime | None:
    """Interpret a task editor's wall-clock value in its selected timezone."""

    due = _coerce_datetime(value)
    if due is None or (due.tzinfo is not None and due.utcoffset() is not None):
        return due
    return due.replace(tzinfo=_task_editor_timezone(timezone))


def _coerce_text_tuple(value: object, *, limit: int = 80, max_items: int = 32) -> tuple[str, ...]:
    if isinstance(value, str):
        values: Iterable[object] = value.split(",")
    else:
        values = _iter_values(value)
    result: list[str] = []
    for item in values:
        text = _safe_text(item, limit=limit)
        if text and text not in result:
            result.append(text)
        if len(result) >= max_items:
            break
    return tuple(result)


def _coerce_int_tuple(value: object, *, max_items: int = 16) -> tuple[int, ...]:
    values: Iterable[object] = value.split(",") if isinstance(value, str) else _iter_values(value)
    result: list[int] = []
    for item in values:
        try:
            number = int(str(item).strip())
        except (TypeError, ValueError):
            continue
        if number not in result:
            result.append(number)
        if len(result) >= max_items:
            break
    return tuple(result)


def _coerce_task(value: object, index: int) -> TaskView:
    if isinstance(value, TaskView):
        return value
    task_id = _safe_text(
        _field(value, "task_id", _field(value, "id", f"task-{index}")),
        fallback=f"task-{index}",
        limit=_MAX_ID_LENGTH,
    )
    state = _safe_text(
        _field(value, "state", _field(value, "status", "open")),
        fallback="open",
        limit=32,
    ).casefold()
    if state not in {"open", "completed", "archived"}:
        state = "open"
    priority = _safe_text(
        _field(value, "priority", "normal"),
        fallback="normal",
        limit=32,
    ).casefold()
    if priority not in {"none", "low", "normal", "high"}:
        priority = "normal"
    list_value = _field(
        value,
        "list_id",
        _field(value, "list_name", _field(value, "list", "")),
    )
    recurrence_value = _field(value, "recurrence", "")
    return TaskView(
        task_id=task_id,
        title=_safe_text(_field(value, "title", "Untitled task"), fallback="Untitled task"),
        notes=_safe_text(_field(value, "notes", _field(value, "description", ""))),
        external_url=_safe_http_target(
            _field(
                value,
                "external_url",
                _field(value, "navigation_target", _field(value, "deep_link", "")),
            ),
            limit=1_024,
        ),
        native_platform=_safe_text(
            _field(value, "native_platform", _field(value, "platform", "")),
            limit=64,
        ).casefold(),
        state=state,
        priority=priority,
        due_at=_coerce_datetime(
            _field(value, "due_at", _field(value, "due", _field(value, "due_date", None)))
        ),
        due_is_all_day=_bool_value(
            _field(value, "due_is_all_day", _field(value, "all_day", False))
        ),
        timezone=_safe_text(_field(value, "timezone", "local"), fallback="local", limit=128),
        list_id=_safe_text(list_value, limit=128) or None,
        tags=_coerce_text_tuple(_field(value, "tags", ())),
        recurrence=_safe_text(recurrence_value, limit=128) or None,
        reminder_offsets_minutes=_coerce_int_tuple(
            _field(
                value,
                "reminder_offsets_minutes",
                _field(value, "reminders", _field(value, "reminder_offsets", ())),
            )
        ),
        created_at=_coerce_datetime(_field(value, "created_at", None)),
        updated_at=_coerce_datetime(_field(value, "updated_at", None)),
        completed_at=_coerce_datetime(_field(value, "completed_at", None)),
    )


def _coerce_today_item(value: object, index: int) -> TodayItemView:
    if isinstance(value, TodayItemView):
        return value
    item_id = _safe_text(
        _field(value, "item_id", _field(value, "id", f"today-item-{index}")),
        fallback=f"today-item-{index}",
        limit=_MAX_ID_LENGTH,
    )
    kind = _safe_text(_field(value, "kind", "task"), fallback="task", limit=48).casefold()
    if kind not in {"event", "task", "message", "alert", "news", "source_problem"}:
        kind = "task"
    severity = _safe_text(_field(value, "severity", "info"), fallback="info", limit=32).casefold()
    if severity not in {"info", "attention", "urgent"}:
        severity = "info"
    source = _field(value, "source", _field(value, "source_name", ""))
    return TodayItemView(
        item_id=item_id,
        kind=kind,
        title=_safe_text(_field(value, "title", "Today item"), fallback="Today item"),
        summary=_safe_text(_field(value, "summary", _field(value, "body", ""))),
        starts_at=_coerce_datetime(_field(value, "starts_at", _field(value, "start_at", None))),
        due_at=_coerce_datetime(_field(value, "due_at", _field(value, "due", None))),
        severity=severity,
        source=_source_display_name(source),
        navigation_target=_safe_text(_field(value, "navigation_target", ""), limit=240),
        sensitive=_bool_value(_field(value, "sensitive", False)),
        due_is_all_day=_bool_value(
            _field(value, "due_is_all_day", _field(value, "all_day", False))
        ),
        is_completed=_bool_value(
            _field(value, "is_completed", _field(value, "completed", _field(value, "done", False)))
        ),
        completed_at=_coerce_datetime(_field(value, "completed_at", None)),
    )


def _coerce_today_section(value: object, index: int) -> TodaySectionView:
    if isinstance(value, TodaySectionView):
        return TodaySectionView(
            section_id=_safe_text(
                value.section_id, fallback=f"section-{index}", limit=80
            ).casefold(),
            title=_safe_text(value.title, fallback="Today", limit=180),
            body=_safe_text(value.body),
            items=tuple(_iter_values(value.items)),
            visible=value.visible,
        )
    if isinstance(value, (tuple, list)) and len(value) == 2:
        section_id = _safe_text(value[0], fallback=f"section-{index}", limit=80).casefold()
        return TodaySectionView(
            section_id=section_id,
            title=section_id.replace("_", " ").title(),
            items=tuple(_iter_values(value[1])),
        )
    section_id = _safe_text(
        _field(value, "section_id", _field(value, "id", f"section-{index}")),
        fallback=f"section-{index}",
        limit=80,
    ).casefold()
    title = _safe_text(
        _field(value, "title", _field(value, "label", section_id.replace("_", " ").title())),
        fallback=section_id.replace("_", " ").title(),
        limit=180,
    )
    raw_items = _field(value, "items", _field(value, "entries", ()))
    return TodaySectionView(
        section_id=section_id,
        title=title,
        body=_safe_text(_field(value, "body", _field(value, "summary", ""))),
        items=tuple(_iter_values(raw_items)),
        visible=_bool_value(_field(value, "visible", True), default=True),
    )


def _coerce_today_view(value: object) -> TodayView:
    if isinstance(value, TodayView):
        return TodayView(
            sections=tuple(
                _coerce_today_section(item, index)
                for index, item in enumerate(_iter_values(value.sections))
            ),
            local_date=_safe_text(value.local_date, limit=64),
            timezone=_safe_text(value.timezone, limit=128),
            generated_at=_coerce_datetime(value.generated_at),
        )
    data: object = value or {}
    raw_sections = _field(data, "sections", None)
    if raw_sections is None:
        # The local Today projection often arrives as four named sections.
        projection_sections: list[dict[str, object]] = []
        for section_id, title in (
            ("overdue", "Overdue tasks"),
            ("today", "Due today"),
            ("upcoming", "Upcoming"),
            ("unscheduled", "Unscheduled"),
        ):
            raw_items = _field(data, section_id, None)
            if raw_items is not None:
                projection_sections.append(
                    {"section_id": section_id, "title": title, "items": raw_items}
                )
        raw_sections = projection_sections
    elif isinstance(raw_sections, Mapping):
        raw_sections = tuple(
            {
                "section_id": section_id,
                "title": _field(section, "title", str(section_id).replace("_", " ").title()),
                "body": _field(section, "body", _field(section, "summary", "")),
                "items": _field(section, "items", _field(section, "entries", section)),
                "visible": _field(section, "visible", True),
            }
            for section_id, section in raw_sections.items()
        )
    sections = tuple(
        _coerce_today_section(item, index) for index, item in enumerate(_iter_values(raw_sections))
    )
    generated = _field(data, "generated_at", _field(data, "refreshed_at", None))
    return TodayView(
        sections=sections,
        local_date=_safe_text(_field(data, "local_date", _field(data, "date", "")), limit=64),
        timezone=_safe_text(_field(data, "timezone", ""), limit=128),
        generated_at=_coerce_datetime(generated),
    )


def _coerce_calendar_event(value: object, index: int) -> CalendarEventView:
    if isinstance(value, CalendarEventView):
        return CalendarEventView(
            event_id=_safe_text(value.event_id, fallback=f"event-{index}", limit=_MAX_ID_LENGTH),
            title=_safe_text(value.title, fallback="Calendar event", limit=240),
            starts_at=_coerce_datetime(value.starts_at),
            ends_at=_coerce_datetime(value.ends_at),
            all_day=value.all_day,
            timezone=_safe_text(value.timezone, fallback="local", limit=128),
            calendar_id=_safe_text(value.calendar_id, limit=_MAX_ID_LENGTH) or None,
            calendar_name=_safe_text(value.calendar_name, limit=180),
            source_id=_safe_text(value.source_id, limit=_MAX_ID_LENGTH) or None,
            source_name=_safe_text(value.source_name, limit=180),
            account=_safe_text(value.account, limit=180),
            location=_safe_text(value.location, limit=240),
            description=_safe_text(value.description),
            navigation_target=_safe_text(value.navigation_target, limit=512),
            native_platform=_safe_text(value.native_platform, limit=64).casefold(),
            sensitive=value.sensitive,
            item_kind=("task" if value.item_kind == "task" else "event"),
            task_id=_safe_text(value.task_id, limit=_MAX_ID_LENGTH),
            is_completed=value.is_completed,
            completed_at=_coerce_datetime(value.completed_at),
            completion_state=_safe_text(value.completion_state, fallback="open", limit=32),
            external_event_id=_safe_text(
                value.external_event_id or value.event_id,
                limit=_MAX_ID_LENGTH,
            ),
            external_calendar_id=_safe_text(
                value.external_calendar_id or value.calendar_id or "",
                limit=_MAX_ID_LENGTH,
            ),
        )
    raw_calendar = _field(value, "calendar_id", _field(value, "calendar", ""))
    calendar_id = _safe_text(
        _field(raw_calendar, "calendar_id", _field(raw_calendar, "id", raw_calendar)),
        limit=_MAX_ID_LENGTH,
    )
    raw_source = _field(value, "source_id", _field(value, "source", ""))
    source_id = _safe_text(
        _field(raw_source, "source_id", _field(raw_source, "id", raw_source)),
        limit=_MAX_ID_LENGTH,
    )
    raw_event_id = _field(
        value,
        "event_id",
        _field(value, "id", f"event-{index}"),
    )
    external_event_id = _field(
        value,
        "external_event_id",
        _field(value, "provider_event_id", _field(value, "remote_event_id", raw_event_id)),
    )
    completion_state = _safe_text(
        _field(value, "completion_state", _field(value, "local_state", "open")),
        fallback="open",
        limit=32,
    )
    return CalendarEventView(
        event_id=_safe_text(
            raw_event_id,
            fallback=f"event-{index}",
            limit=_MAX_ID_LENGTH,
        ),
        title=_safe_text(
            _field(value, "title", _field(value, "summary", "Calendar event")),
            fallback="Calendar event",
            limit=240,
        ),
        starts_at=_coerce_datetime(
            _field(value, "starts_at", _field(value, "start_at", _field(value, "start", None)))
        ),
        ends_at=_coerce_datetime(
            _field(value, "ends_at", _field(value, "end_at", _field(value, "end", None)))
        ),
        all_day=_bool_value(_field(value, "all_day", _field(value, "is_all_day", False))),
        timezone=_safe_text(_field(value, "timezone", "local"), fallback="local", limit=128),
        calendar_id=calendar_id or None,
        calendar_name=_safe_text(
            _field(
                raw_calendar,
                "display_name",
                _field(raw_calendar, "name", _field(value, "calendar_name", "")),
            ),
            limit=180,
        ),
        source_id=source_id or None,
        source_name=_safe_text(
            _field(
                raw_source,
                "display_name",
                _field(raw_source, "name", _field(value, "source_name", "")),
            ),
            limit=180,
        ),
        account=_safe_text(
            _field(
                value, "account", _field(value, "account_name", _field(value, "account_label", ""))
            ),
            limit=180,
        ),
        location=_safe_text(_field(value, "location", _field(value, "where", "")), limit=240),
        description=_safe_text(_field(value, "description", _field(value, "body", ""))),
        navigation_target=_safe_text(
            _field(
                value, "navigation_target", _field(value, "deep_link", _field(value, "url", ""))
            ),
            limit=512,
        ),
        native_platform=_safe_text(
            _field(value, "native_platform", _field(value, "platform", "")),
            limit=64,
        ).casefold(),
        sensitive=_bool_value(_field(value, "sensitive", True), default=True),
        item_kind=(
            "task"
            if _safe_text(_field(value, "item_kind", _field(value, "kind", "event"))).casefold()
            == "task"
            else "event"
        ),
        task_id=_safe_text(_field(value, "task_id", ""), limit=_MAX_ID_LENGTH),
        is_completed=_bool_value(
            _field(
                value,
                "is_completed",
                _field(
                    value,
                    "completed",
                    _field(
                        value,
                        "done",
                        completion_state in {"completed", "external_deleted"},
                    ),
                ),
            )
        ),
        completed_at=_coerce_datetime(_field(value, "completed_at", None)),
        completion_state=completion_state,
        external_event_id=_safe_text(external_event_id, limit=_MAX_ID_LENGTH),
        external_calendar_id=_safe_text(
            _field(value, "external_calendar_id", calendar_id),
            limit=_MAX_ID_LENGTH,
        ),
    )


def _coerce_calendar_source(value: object, index: int) -> CalendarSourceView:
    if isinstance(value, CalendarSourceView):
        return CalendarSourceView(
            calendar_id=_safe_text(
                value.calendar_id, fallback=f"calendar-{index}", limit=_MAX_ID_LENGTH
            ),
            display_name=_safe_text(value.display_name, fallback="Calendar", limit=180),
            source_id=_safe_text(value.source_id, limit=_MAX_ID_LENGTH),
            source_name=_safe_text(value.source_name, limit=180),
            account=_safe_text(value.account, limit=180),
            selected=value.selected,
            status=_safe_text(value.status, fallback="ready", limit=48),
            sensitive=value.sensitive,
        )
    raw_source = _field(value, "source_id", _field(value, "source", ""))
    return CalendarSourceView(
        calendar_id=_safe_text(
            _field(value, "calendar_id", _field(value, "id", f"calendar-{index}")),
            fallback=f"calendar-{index}",
            limit=_MAX_ID_LENGTH,
        ),
        display_name=_safe_text(
            _field(value, "display_name", _field(value, "name", "Calendar")),
            fallback="Calendar",
            limit=180,
        ),
        source_id=_safe_text(
            _field(raw_source, "source_id", _field(raw_source, "id", raw_source)),
            limit=_MAX_ID_LENGTH,
        ),
        source_name=_safe_text(
            _field(
                raw_source,
                "display_name",
                _field(raw_source, "name", _field(value, "source_name", "")),
            ),
            limit=180,
        ),
        account=_safe_text(
            _field(
                value, "account", _field(value, "account_name", _field(value, "account_label", ""))
            ),
            limit=180,
        ),
        selected=_bool_value(
            _field(value, "selected", _field(value, "enabled", True)), default=True
        ),
        status=_safe_text(_field(value, "status", "ready"), fallback="ready", limit=48),
        sensitive=_bool_value(_field(value, "sensitive", False)),
    )


def _coerce_ical_feed(value: object, index: int) -> IcalFeedView:
    if isinstance(value, IcalFeedView):
        return IcalFeedView(
            feed_id=_safe_text(value.feed_id, fallback=f"ical-feed-{index}", limit=_MAX_ID_LENGTH),
            title=_safe_text(value.title, fallback="iCal feed", limit=180),
            feed_url=_safe_text(value.feed_url, limit=512),
            enabled=value.enabled,
            refresh_minutes=max(15, min(int(value.refresh_minutes), 1_440)),
            status=_safe_text(value.status, fallback="disconnected", limit=48),
            detail=_safe_text(value.detail, limit=240),
            last_success_at=_safe_text(value.last_success_at, limit=64),
        )
    try:
        refresh_minutes = int(_field(value, "refresh_minutes", 30))
    except (TypeError, ValueError):
        refresh_minutes = 30
    return IcalFeedView(
        feed_id=_safe_text(
            _field(value, "feed_id", _field(value, "id", f"ical-feed-{index}")),
            fallback=f"ical-feed-{index}",
            limit=_MAX_ID_LENGTH,
        ),
        title=_safe_text(
            _field(value, "title", _field(value, "name", "iCal feed")),
            fallback="iCal feed",
            limit=180,
        ),
        feed_url=_safe_text(_field(value, "feed_url", _field(value, "url", "")), limit=512),
        enabled=_bool_value(_field(value, "enabled", True), default=True),
        refresh_minutes=max(15, min(refresh_minutes, 1_440)),
        status=_safe_text(
            _field(value, "status", "disconnected"), fallback="disconnected", limit=48
        ),
        detail=_safe_text(_field(value, "detail", ""), limit=240),
        last_success_at=_safe_text(_field(value, "last_success_at", ""), limit=64),
    )


def _coerce_calendar_import(value: object, index: int) -> CalendarImportView:
    if isinstance(value, CalendarImportView):
        source_format = _safe_text(value.source_format, limit=12).casefold()
        return CalendarImportView(
            import_id=_safe_text(
                value.import_id,
                fallback=f"calendar-import-{index}",
                limit=_MAX_ID_LENGTH,
            ),
            name=_safe_text(value.name, fallback="Local calendar import", limit=180),
            source_format="text" if source_format == "text" else "csv",
            event_count=max(0, min(_int_value(value.event_count, 0), 5_000)),
        )
    try:
        event_count = int(_field(value, "event_count", _field(value, "count", 0)))
    except (TypeError, ValueError):
        event_count = 0
    source_format = _safe_text(_field(value, "source_format", "csv"), limit=12).casefold()
    return CalendarImportView(
        import_id=_safe_text(
            _field(value, "import_id", _field(value, "id", f"calendar-import-{index}")),
            fallback=f"calendar-import-{index}",
            limit=_MAX_ID_LENGTH,
        ),
        name=_safe_text(
            _field(value, "name", _field(value, "title", "Local calendar import")),
            fallback="Local calendar import",
            limit=180,
        ),
        source_format="text" if source_format == "text" else "csv",
        event_count=max(0, min(event_count, 5_000)),
    )


def _coerce_calendar_view(value: object) -> CalendarView:
    if isinstance(value, CalendarView):
        events = tuple(
            _coerce_calendar_event(item, index)
            for index, item in enumerate(_iter_values(value.events))
        )
        calendars = tuple(
            _coerce_calendar_source(item, index)
            for index, item in enumerate(_iter_values(value.calendars))
        )
        ical_feeds = tuple(
            _coerce_ical_feed(item, index)
            for index, item in enumerate(_iter_values(value.ical_feeds))
        )
        local_imports = tuple(
            _coerce_calendar_import(item, index)
            for index, item in enumerate(_iter_values(value.local_imports))
        )
        selected = _coerce_text_tuple(value.selected_calendar_ids, limit=_MAX_ID_LENGTH)
        return _calendar_view_with_derived_sources(
            CalendarView(
                events=events,
                calendars=calendars,
                selected_calendar_ids=selected,
                timezone=_safe_text(value.timezone, limit=128),
                freshness=_calendar_freshness(value.freshness),
                detail=_safe_text(value.detail),
                generated_at=_coerce_datetime(value.generated_at),
                cursor_date=_coerce_date_value(value.cursor_date),
                ical_feeds=ical_feeds,
                local_imports=local_imports,
            )
        )
    data: object = value or {}
    raw_events = _field(data, "events", _field(data, "items", ()))
    events = tuple(
        _coerce_calendar_event(item, index)
        for index, item in enumerate(
            raw_events.values() if isinstance(raw_events, Mapping) else _iter_values(raw_events)
        )
    )
    raw_calendars = _field(data, "calendars", _field(data, "sources", ()))
    if isinstance(raw_calendars, Mapping):
        raw_calendars = tuple(
            {
                "calendar_id": calendar_id,
                "display_name": _field(
                    calendar, "display_name", _field(calendar, "name", calendar_id)
                ),
                "source": _field(calendar, "source", ""),
                "account": _field(calendar, "account", ""),
                "selected": _field(calendar, "selected", True),
                "status": _field(calendar, "status", "ready"),
            }
            for calendar_id, calendar in raw_calendars.items()
        )
    calendars = tuple(
        _coerce_calendar_source(item, index)
        for index, item in enumerate(_iter_values(raw_calendars))
    )
    raw_ical_feeds = _field(data, "ical_feeds", _field(data, "imported_ical_feeds", ()))
    ical_feeds = tuple(
        _coerce_ical_feed(item, index) for index, item in enumerate(_iter_values(raw_ical_feeds))
    )
    raw_local_imports = _field(
        data,
        "local_imports",
        _field(data, "calendar_imports", _field(data, "imported_calendars", ())),
    )
    local_imports = tuple(
        _coerce_calendar_import(item, index)
        for index, item in enumerate(_iter_values(raw_local_imports))
    )
    selected_raw = _field(
        data,
        "selected_calendar_ids",
        _field(data, "selected_calendars", ()),
    )
    selected = _coerce_text_tuple(selected_raw, limit=_MAX_ID_LENGTH)
    return _calendar_view_with_derived_sources(
        CalendarView(
            events=events,
            calendars=calendars,
            selected_calendar_ids=selected,
            timezone=_safe_text(_field(data, "timezone", ""), limit=128),
            freshness=_calendar_freshness(
                _field(data, "freshness", _field(data, "status", _field(data, "state", "ready")))
            ),
            detail=_safe_text(_field(data, "detail", _field(data, "message", ""))),
            generated_at=_coerce_datetime(
                _field(data, "generated_at", _field(data, "refreshed_at", None))
            ),
            cursor_date=_coerce_date_value(
                _field(data, "cursor_date", _field(data, "date", _field(data, "range_start", None)))
            ),
            ical_feeds=ical_feeds,
            local_imports=local_imports,
        )
    )


def _calendar_freshness(value: object) -> str:
    state = _safe_text(value, fallback="ready", limit=48).casefold()
    return state if state in _CALENDAR_FRESHNESS_STATES else "error"


def _safe_feed_content(value: object, *, limit: int = _MAX_TEXT_LENGTH) -> str:
    """Return bounded plain text for untrusted RSS/Atom content.

    Feed services should sanitize before creating a view, but the UI keeps a
    second defensive boundary so a malformed or hostile fixture cannot become
    rich text in a label or row.  No HTML is ever handed to Qt as markup.
    """

    if value is None:
        return ""
    text = unescape(str(value).replace("\x00", ""))
    text = re.sub(r"(?is)<script\b[^>]*>.*?</script\s*>", " ", text)
    text = re.sub(r"(?is)<style\b[^>]*>.*?</style\s*>", " ", text)
    text = re.sub(r"(?s)<[^>]{0,512}>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return _safe_text(text, limit=limit)


def _safe_http_target(value: object, *, limit: int = 512) -> str:
    """Allow only ordinary HTTP(S) links from provider-backed content."""

    target = _safe_text(value, limit=limit)
    if not target:
        return ""
    try:
        parsed = urlsplit(target)
    except ValueError:
        return ""
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        return ""
    return target


def _feed_origin_label(value: object) -> str:
    """Expose only a host origin in a subscription indicator."""

    target = _safe_http_target(value, limit=512)
    if not target:
        return ""
    try:
        return _safe_text(urlsplit(target).hostname, limit=180)
    except ValueError:
        return ""


def _coerce_feed_subscription(value: object, index: int) -> FeedSubscriptionView:
    if isinstance(value, FeedSubscriptionView):
        return FeedSubscriptionView(
            subscription_id=_safe_text(
                value.subscription_id, fallback=f"feed-{index}", limit=_MAX_ID_LENGTH
            ),
            title=_safe_feed_content(value.title, limit=180) or "Feed",
            feed_url=_safe_http_target(value.feed_url),
            site_url=_safe_http_target(value.site_url),
            enabled=bool(value.enabled),
            refresh_minutes=max(1, min(int(value.refresh_minutes or 30), 1440)),
            include_terms=_coerce_text_tuple(value.include_terms, limit=80),
            exclude_terms=_coerce_text_tuple(value.exclude_terms, limit=80),
            digest_only=bool(value.digest_only),
            status=_news_freshness(value.status),
            detail=_safe_feed_content(value.detail),
            last_success_at=_safe_text(value.last_success_at, limit=120) or None,
            unread_count=max(0, min(int(value.unread_count or 0), _MAX_NEWS_ENTRIES)),
            entry_count=max(0, min(int(value.entry_count or 0), _MAX_NEWS_ENTRIES)),
            source_id=_safe_text(value.source_id, limit=_MAX_ID_LENGTH),
            source_name=_safe_feed_content(value.source_name, limit=180),
            account=_safe_feed_content(value.account, limit=180),
            sensitive=bool(value.sensitive),
        )
    raw_terms = _field(value, "include_terms", _field(value, "include_keywords", ()))
    raw_exclude = _field(value, "exclude_terms", _field(value, "exclude_keywords", ()))
    try:
        refresh_minutes = int(_field(value, "refresh_minutes", _field(value, "refresh", 30)) or 30)
    except (TypeError, ValueError):
        refresh_minutes = 30
    try:
        unread_count = int(_field(value, "unread_count", _field(value, "unread", 0)) or 0)
    except (TypeError, ValueError):
        unread_count = 0
    try:
        entry_count = int(_field(value, "entry_count", _field(value, "count", 0)) or 0)
    except (TypeError, ValueError):
        entry_count = 0
    return FeedSubscriptionView(
        subscription_id=_safe_text(
            _field(value, "subscription_id", _field(value, "id", f"feed-{index}")),
            fallback=f"feed-{index}",
            limit=_MAX_ID_LENGTH,
        ),
        title=_safe_feed_content(_field(value, "title", _field(value, "name", "Feed")), limit=180)
        or "Feed",
        feed_url=_safe_http_target(
            _field(value, "feed_url", _field(value, "url", _field(value, "origin", "")))
        ),
        site_url=_safe_http_target(_field(value, "site_url", _field(value, "homepage", ""))),
        enabled=_bool_value(_field(value, "enabled", True), default=True),
        refresh_minutes=max(1, min(refresh_minutes, 1440)),
        include_terms=_coerce_text_tuple(raw_terms, limit=80),
        exclude_terms=_coerce_text_tuple(raw_exclude, limit=80),
        digest_only=_bool_value(_field(value, "digest_only", False)),
        status=_news_freshness(
            _field(value, "status", _field(value, "freshness", _field(value, "state", "ready")))
        ),
        detail=_safe_feed_content(_field(value, "detail", _field(value, "message", ""))),
        last_success_at=_safe_text(
            _field(value, "last_success_at", _field(value, "last_sync", "")), limit=120
        )
        or None,
        unread_count=max(0, min(unread_count, _MAX_NEWS_ENTRIES)),
        entry_count=max(0, min(entry_count, _MAX_NEWS_ENTRIES)),
        source_id=_safe_text(
            _field(value, "source_id", _field(value, "source", "")), limit=_MAX_ID_LENGTH
        ),
        source_name=_safe_feed_content(
            _field(value, "source_name", _field(value, "provider", "")), limit=180
        ),
        account=_safe_feed_content(
            _field(value, "account", _field(value, "account_label", "")), limit=180
        ),
        sensitive=_bool_value(_field(value, "sensitive", False)),
    )


def _coerce_feed_entry(value: object, index: int) -> FeedEntryView:
    if isinstance(value, FeedEntryView):
        return FeedEntryView(
            entry_id=_safe_text(value.entry_id, fallback=f"entry-{index}", limit=_MAX_ID_LENGTH),
            subscription_id=_safe_text(value.subscription_id, limit=_MAX_ID_LENGTH),
            title=_safe_feed_content(value.title, limit=240) or "Feed entry",
            url=_safe_http_target(value.url),
            author=_safe_feed_content(value.author, limit=180),
            published_at=_coerce_datetime(value.published_at),
            summary_text=_safe_feed_content(value.summary_text),
            first_seen_at=_coerce_datetime(value.first_seen_at),
            read=bool(value.read),
            source_name=_safe_feed_content(value.source_name, limit=180),
            source_url=_safe_http_target(value.source_url),
            navigation_target=_safe_http_target(value.navigation_target or value.url),
            sensitive=bool(value.sensitive),
        )
    raw_subscription = _field(value, "subscription_id", _field(value, "subscription", ""))
    subscription_id = _safe_text(
        _field(
            raw_subscription, "subscription_id", _field(raw_subscription, "id", raw_subscription)
        ),
        limit=_MAX_ID_LENGTH,
    )
    raw_url = _field(value, "url", _field(value, "link", _field(value, "canonical_url", "")))
    read = _field(value, "read", _field(value, "is_read", None))
    if read is None:
        read = not _bool_value(_field(value, "unread", False))
    return FeedEntryView(
        entry_id=_safe_text(
            _field(value, "entry_id", _field(value, "id", _field(value, "guid", f"entry-{index}"))),
            fallback=f"entry-{index}",
            limit=_MAX_ID_LENGTH,
        ),
        subscription_id=subscription_id,
        title=_safe_feed_content(
            _field(value, "title", _field(value, "headline", "Feed entry")), limit=240
        )
        or "Feed entry",
        url=_safe_http_target(raw_url),
        author=_safe_feed_content(_field(value, "author", _field(value, "creator", "")), limit=180),
        published_at=_coerce_datetime(
            _field(
                value, "published_at", _field(value, "published", _field(value, "updated_at", None))
            )
        ),
        summary_text=_safe_feed_content(
            _field(
                value, "summary_text", _field(value, "summary", _field(value, "description", ""))
            )
        ),
        first_seen_at=_coerce_datetime(
            _field(value, "first_seen_at", _field(value, "first_seen", None))
        ),
        read=_bool_value(read),
        source_name=_safe_feed_content(
            _field(value, "source_name", _field(value, "feed_title", "")), limit=180
        ),
        source_url=_safe_http_target(_field(value, "source_url", _field(value, "feed_url", ""))),
        navigation_target=_safe_http_target(
            _field(value, "navigation_target", _field(value, "deep_link", raw_url))
        ),
        sensitive=_bool_value(_field(value, "sensitive", False)),
    )


def _news_freshness(value: object) -> str:
    state = _safe_text(value, fallback="ready", limit=48).casefold()
    return state if state in _NEWS_FRESHNESS_STATES else "error"


def _coerce_news_view(value: object) -> NewsView:
    if isinstance(value, NewsView):
        subscriptions = tuple(
            _coerce_feed_subscription(item, index) for index, item in enumerate(value.subscriptions)
        )
        entries = tuple(_coerce_feed_entry(item, index) for index, item in enumerate(value.entries))
        return NewsView(
            subscriptions=subscriptions,
            entries=entries,
            selected_subscription_ids=_coerce_text_tuple(
                value.selected_subscription_ids, limit=_MAX_ID_LENGTH
            ),
            selection_explicit=bool(value.selection_explicit),
            include_terms=_coerce_text_tuple(value.include_terms, limit=80),
            exclude_terms=_coerce_text_tuple(value.exclude_terms, limit=80),
            unread_only=bool(value.unread_only),
            freshness=_news_freshness(value.freshness),
            detail=_safe_feed_content(value.detail),
            generated_at=_coerce_datetime(value.generated_at),
            max_entries=max(1, min(int(value.max_entries or 100), _MAX_NEWS_ENTRIES)),
        )
    data: object = value or {}
    raw_subscriptions = _field(
        data, "subscriptions", _field(data, "feeds", _field(data, "sources", ()))
    )
    raw_entries = _field(data, "entries", _field(data, "items", _field(data, "feed_entries", ())))
    subscriptions = tuple(
        _coerce_feed_subscription(item, index)
        for index, item in enumerate(
            raw_subscriptions.values()
            if isinstance(raw_subscriptions, Mapping)
            else _iter_values(raw_subscriptions)
        )
    )
    entries = tuple(
        _coerce_feed_entry(item, index)
        for index, item in enumerate(
            raw_entries.values() if isinstance(raw_entries, Mapping) else _iter_values(raw_entries)
        )
    )
    selected = _field(
        data,
        "selected_subscription_ids",
        _field(data, "selected_feed_ids", _field(data, "selected_feeds", ())),
    )
    try:
        max_entries = int(_field(data, "max_entries", _field(data, "limit", 100)) or 100)
    except (TypeError, ValueError):
        max_entries = 100
    return NewsView(
        subscriptions=subscriptions,
        entries=entries,
        selected_subscription_ids=_coerce_text_tuple(selected, limit=_MAX_ID_LENGTH),
        selection_explicit=_bool_value(_field(data, "selection_explicit", False)),
        include_terms=_coerce_text_tuple(
            _field(data, "include_terms", _field(data, "include_keywords", ())), limit=80
        ),
        exclude_terms=_coerce_text_tuple(
            _field(data, "exclude_terms", _field(data, "exclude_keywords", ())), limit=80
        ),
        unread_only=_bool_value(_field(data, "unread_only", _field(data, "unread", False))),
        freshness=_news_freshness(
            _field(data, "freshness", _field(data, "status", _field(data, "state", "ready")))
        ),
        detail=_safe_feed_content(_field(data, "detail", _field(data, "message", ""))),
        generated_at=_coerce_datetime(
            _field(
                data,
                "generated_at",
                _field(data, "refreshed_at", _field(data, "collected_at", None)),
            )
        ),
        max_entries=max(1, min(max_entries, _MAX_NEWS_ENTRIES)),
    )


def _coerce_date_value(value: object) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _safe_text(value, limit=64)
    try:
        return date.fromisoformat(text)
    except ValueError:
        parsed = _coerce_datetime(text)
        return parsed.date() if parsed is not None else None


def _calendar_view_with_derived_sources(view: CalendarView) -> CalendarView:
    calendars = view.calendars
    if not calendars:
        derived: list[CalendarSourceView] = []
        seen: set[str] = set()
        for index, event in enumerate(view.events):
            calendar_id = event.calendar_id or event.source_id or f"calendar-{index}"
            if calendar_id in seen:
                continue
            seen.add(calendar_id)
            derived.append(
                CalendarSourceView(
                    calendar_id=calendar_id,
                    display_name=event.calendar_name or "Calendar",
                    source_id=event.source_id or "",
                    source_name=event.source_name,
                    account=event.account,
                )
            )
        calendars = tuple(derived)
    if view.selected_calendar_ids:
        selected = view.selected_calendar_ids
    else:
        selected = tuple(calendar.calendar_id for calendar in calendars if calendar.selected)
    timezone = view.timezone
    if not timezone:
        timezone = next(
            (
                event.timezone
                for event in view.events
                if event.timezone and event.timezone != "local"
            ),
            "local" if view.events else "",
        )
    return replace(
        view,
        calendars=calendars,
        selected_calendar_ids=selected,
        timezone=timezone,
    )


def _calendar_source_id(calendar_key: str) -> str:
    return calendar_key.split("/", 1)[0] if "/" in calendar_key else ""


def _calendar_source_wildcard(calendar_key: str) -> str:
    source_id = _calendar_source_id(calendar_key)
    return f"{source_id}/*" if source_id else ""


def _calendar_event_date(
    event: CalendarEventView,
    timezone: str = "local",
) -> date | None:
    """Return an event's day in the calendar display timezone."""

    try:
        return event_local_date(event, timezone=timezone)
    except (TypeError, ValueError):
        starts_at = _coerce_datetime(event.starts_at)
        return None if starts_at is None else starts_at.date()


def _calendar_display_timezone(value: object) -> str:
    """Return a bounded, loadable timezone for calendar geometry."""

    normalized = _safe_text(value, fallback="local", limit=128)
    if normalized.casefold() == "local":
        return "local"
    try:
        ZoneInfo(normalized)
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return "local"
    return normalized


_DEFAULT_TODAY_SECTIONS: Final[tuple[TodaySectionView, ...]] = (
    TodaySectionView("mini_calendar", "Mini calendar"),
    TodaySectionView("schedule", "Agenda", "No calendar events are scheduled."),
    TodaySectionView("today", "Due today", "No tasks are due today."),
    TodaySectionView("attention", "Inbox", "You're all caught up."),
    TodaySectionView("watchlist", "Watchlist", "Add a watch to follow what changes."),
    TodaySectionView("overdue", "Overdue tasks", "Nothing is overdue."),
    TodaySectionView("upcoming", "Upcoming", "No upcoming tasks or events.", visible=False),
    TodaySectionView("inbox", "Unread messages", "No unread messages.", visible=False),
    TodaySectionView("watch_alerts", "Watch alerts", "No watch alerts.", visible=False),
    TodaySectionView("news", "News digest", "No news digest is available.", visible=False),
    TodaySectionView("source_problems", "Source problems", "No source problems.", visible=False),
)


def _accessible_stylesheet(
    stylesheet: str,
    *,
    text_scale: float,
    high_contrast: bool,
) -> str:
    """Scale point sizes and optionally apply a high-contrast override."""

    scale = max(1.0, min(float(text_scale), 1.75))

    def scale_font(match: re.Match[str]) -> str:
        points = float(match.group(1)) * scale
        rendered = f"{points:.2f}".rstrip("0").rstrip(".")
        return f"font-size: {rendered}pt"

    scaled = re.sub(r"font-size:\s*([0-9]+(?:\.[0-9]+)?)pt", scale_font, stylesheet)
    if not high_contrast:
        return scaled
    return (
        scaled
        + """
QMainWindow, QWidget { background: #000000; color: #ffffff; }
QLabel { color: #ffffff; }
QPushButton[hubNavigation="true"] { color: #ffffff; border: 2px solid #ffffff; }
QPushButton[hubNavigation="true"]:checked { background: #003b5c; color: #ffffff; border-color: #00ffff; }
QPushButton[hubNavigation="true"]:disabled { background: #000000; color: #8c8c8c; border-color: #666666; }
QPushButton[hubNavigation="true"]:focus, QListWidget:focus { border: 3px solid #00ffff; }
QListWidget, QFrame#todayCard, QFrame#todaySection, QFrame#taskQuickAdd,
QFrame#taskEditorPanel, QFrame#taskRow, QFrame#calendarMonthCell,
QFrame#calendarWeekColumn, QFrame#calendarSourcesPanel,
QFrame#calendarSettingsPanel, QFrame#inboxRow, QFrame#messageSummaryRow,
QFrame#sourceFailurePanel, QFrame#attentionNotificationPanel, QFrame#diagnosticsView,
QFrame#newsSubscriptionsPanel, QFrame#newsEntryRow,
QFrame#newsFiltersPanel, QFrame#newsSubscriptionEditor,
QFrame#newsSubscriptionRow {
    background: #000000; color: #ffffff; border: 2px solid #ffffff;
}
"""
    )


_BASE_STYLESHEET: Final = """
QMainWindow, QWidget {
    background: #0b1421;
    color: #e7eef7;
}

#personalHubNavigation {
    background: #08101b;
    border-right: 1px solid #22334a;
}

#brandTitle {
    color: #f7fbff;
    font-size: 16pt;
    font-weight: 650;
}

#pageSubtitle, #statusLabel, #settingsValue {
    color: #9aabc0;
}

#navigationLabel, #pageEyebrow {
    color: #7792b3;
    font-size: 8pt;
    font-weight: 700;
    letter-spacing: 1px;
}

QPushButton[hubNavigation="true"] {
    background: transparent;
    border: 1px solid transparent;
    border-radius: 7px;
    color: #b8c8dc;
    min-height: 38px;
    padding: 0 12px;
    text-align: left;
}

QPushButton[hubNavigation="true"]:hover:enabled {
    background: #14253a;
    border-color: #294766;
    color: #f7fbff;
}

QPushButton[hubNavigation="true"]:focus {
    border: 2px solid #70b5ff;
}

QPushButton[hubNavigation="true"]:checked {
    background: #1c3c61;
    border-color: #5f9edc;
    color: #ffffff;
    font-weight: 650;
}

QPushButton[hubNavigation="true"]:disabled {
    background: #0b1421;
    border-color: transparent;
    color: #586a80;
}

#todayPage, #calendarPage, #tasksPage, #watchlistsPage, #inboxPage, #sourcesPage, #settingsPage {
    background: #0b1421;
}

#attentionNotificationPanel {
    background: #173b5d;
    border: 1px solid #70b5ff;
    border-radius: 10px;
}

#attentionNotificationTitle {
    color: #ffffff;
    font-weight: 650;
}

#attentionNotificationBody {
    color: #dce7f3;
}

#todayCard, #todaySection, #taskQuickAdd, #taskEditorPanel,
#calendarMonthCell, #calendarWeekColumn, #calendarSourcesPanel,
#calendarEventDetailsPanel, #sourceFailurePanel, #diagnosticsView, #newsSubscriptionsPanel,
#newsEntryRow, #newsFiltersPanel {
    background: #101f32;
    border: 1px solid #294563;
    border-radius: 14px;
}

#taskQuickAdd, #taskEditorPanel {
    border-radius: 9px;
}

#pageHeader, #calendarToolbar, #calendarSourcesPanel, #taskViewBar {
    background: transparent;
}

#calendarSettingsPanel {
    background: #0d1a2b;
    border: 1px solid #263b55;
    border-radius: 9px;
}

#calendarContentSplitter, #taskContentSplitter, #messageContentSplitter {
    background: transparent;
    border: 0;
}

QSplitter::handle:horizontal {
    background: #22334a;
    width: 1px;
    margin: 8px 0;
}

#newsToolbar {
    background: transparent;
}

#newsFiltersPanel, #newsSubscriptionsPanel {
    background: #0d1a2b;
    border: 1px solid #263b55;
    border-radius: 9px;
}

#newsSubscriptionEditor {
    background: #0d1a2b;
    border: 1px solid #263b55;
    border-radius: 9px;
}

#newsFilterLabel, #newsFilterStatusLabel, #newsFreshnessLabel,
#newsStatusLabel, #newsSubscriptionsTitle, #newsSubscriptionsEmpty,
#newsEntriesStatusLabel, #newsEntryMetadata, #newsEditorLabel,
#newsSubscriptionEditorStatus {
    color: #9aabc0;
}

#newsEditorTitle {
    color: #ecf4fc;
    font-weight: 600;
}

#newsIncludeKeywordsInput, #newsExcludeKeywordsInput,
#newsSubscriptionTitleInput, #newsSubscriptionUrlInput,
#newsSubscriptionIntervalInput, #newsSubscriptionIncludeInput,
#newsSubscriptionExcludeInput {
    background: #101f32;
    border: 1px solid #385371;
    border-radius: 6px;
    color: #e7eef7;
    padding: 6px;
}

#newsEntriesList {
    background: transparent;
    border: 0;
    color: #dce7f3;
}

#newsEntriesList::item {
    padding: 2px;
}

#newsEntryRow {
    background: #101f32;
    border: 1px solid #294563;
    border-radius: 9px;
}

#newsEntryTitle {
    color: #ecf4fc;
    font-weight: 600;
}

#newsEntrySummary {
    color: #b9c8d9;
}

#newsEntryReadButton, #newsEntryOpenButton {
    background: #1a4164;
    border: 1px solid #376c98;
    border-radius: 4px;
    color: #e7eef7;
    padding: 4px 8px;
}

#newsEntryReadButton:hover, #newsEntryReadButton:focus,
#newsEntryOpenButton:hover, #newsEntryOpenButton:focus {
    background: #265a84;
    border: 2px solid #70b5ff;
}

#calendarPeriodLabel {
    color: #f7fbff;
    font-size: 13pt;
    font-weight: 600;
}

#calendarFreshnessLabel, #calendarStatusLabel, #calendarTimezoneLabel,
#calendarSourcesTitle, #calendarSourcesEmpty, #calendarWeekdayLabel,
#calendarMoreLabel, #calendarEmptyLabel {
    color: #9aabc0;
}

#calendarMonthCell, #calendarWeekColumn {
    background: #0d1a2b;
    border: 1px solid #263b55;
    border-radius: 7px;
}

#calendarHourlyGrid {
    background: #0d1a2b;
    border: 1px solid #263b55;
    border-radius: 9px;
}

#calendarTimeSlotButton {
    background: transparent;
    border: 0;
    border-radius: 0;
    min-width: 0;
    min-height: 0;
    padding: 0;
}

#calendarTimeSlotButton:hover, #calendarTimeSlotButton:focus {
    background: #142e4a;
    border: 1px solid #376c98;
}

#calendarTimeSlotButton:pressed {
    background: #1c3d66;
}

#calendarDayScroll, #calendarWeekScroll {
    background: transparent;
    border: 0;
}

#calendarEventRow {
    background: transparent;
    border: 0;
}

#calendarTaskDoneCheckBox {
    min-width: 18px;
    max-width: 18px;
    padding: 0;
}

#calendarEventColorSwatch {
    min-width: 4px;
    max-width: 4px;
    border: 0;
}

#calendarSourceColorIndicator {
    font-weight: 700;
}

#calendarAgendaView {
    background: #0d1a2b;
    border: 1px solid #263b55;
    border-radius: 9px;
    color: #dce7f3;
    padding: 8px;
}

#calendarAgendaView::item {
    padding: 9px;
    border-bottom: 1px solid #20334d;
}

#calendarAgendaView::item:selected {
    background: #1c3d66;
    color: #ffffff;
}

#calendarEventButton {
    background: #1a4164;
    border: 1px solid #376c98;
    border-radius: 4px;
    color: #e7eef7;
    min-width: 0;
    min-height: 24px;
    max-height: 28px;
    padding: 3px 5px;
    text-align: left;
}

#calendarEventButton:hover, #calendarEventButton:focus {
    background: #265a84;
    border: 2px solid #70b5ff;
}

#calendarEventDetailsPanel {
    background: #0d1a2b;
    border: 1px solid #376c98;
    border-left: 4px solid #70b5ff;
    border-radius: 9px;
}

#calendarEventDetailsTitle {
    color: #f7fbff;
    font-size: 12pt;
    font-weight: 650;
}

#calendarEventDetailsWhen, #calendarEventDetailsLocation,
#calendarEventDetailsSource, #calendarEventDetailsStatus {
    color: #b9c8d9;
}

#calendarEventDetailsDescription {
    background: #101f32;
    border: 1px solid #263b55;
    border-radius: 6px;
    color: #dce7f3;
    padding: 6px;
}

#calendarEventNativeOpenButton, #calendarEventDetailsCloseButton {
    background: #1a4164;
    border: 1px solid #376c98;
    border-radius: 4px;
    color: #e7eef7;
    padding: 4px 8px;
}

#calendarEventNativeOpenButton:hover, #calendarEventNativeOpenButton:focus,
#calendarEventDetailsCloseButton:hover, #calendarEventDetailsCloseButton:focus {
    background: #265a84;
    border: 2px solid #70b5ff;
}

#calendarEventNativeOpenButton:disabled {
    background: #101f32;
    border-color: #263b55;
    color: #687d94;
}

#taskQuickAddInput, #taskSearchInput, #taskEditorPanel QLineEdit,
#taskEditorPanel QPlainTextEdit, #taskEditorPanel QDateTimeEdit,
#taskEditorPanel QComboBox, #taskFilters QComboBox, #settingsPage QComboBox {
    background: #0d1a2b;
    border: 1px solid #385371;
    border-radius: 6px;
    color: #e7eef7;
    padding: 6px;
}

#taskQuickViewButton, #taskFiltersButton, #taskUndoButton,
#calendarSettingsButton, #taskOverflowButton, #messageCategorySelector {
    background: #101f32;
    border: 1px solid #294563;
    border-radius: 6px;
    color: #dce7f3;
    padding: 5px 9px;
}

#taskQuickViewButton:hover, #taskQuickViewButton:focus,
#taskFiltersButton:hover, #taskFiltersButton:focus,
#taskUndoButton:hover, #taskUndoButton:focus,
#calendarSettingsButton:hover, #calendarSettingsButton:focus,
#taskOverflowButton:hover, #taskOverflowButton:focus,
#messageCategorySelector:hover, #messageCategorySelector:focus {
    background: #1a4164;
    border-color: #70b5ff;
}

#taskRow {
    background: transparent;
    border: 0;
    border-bottom: 1px solid #20334d;
    border-radius: 0;
}

#taskRow:hover {
    background: #101f32;
}

#taskCompleteButton {
    background: #1a4164;
    border: 1px solid #376c98;
    border-radius: 16px;
    color: #e7eef7;
    font-size: 12pt;
    font-weight: 700;
}

#taskCompleteButton:hover, #taskCompleteButton:focus {
    background: #265a84;
    border: 2px solid #70b5ff;
}

#taskNativeOpenButton {
    padding: 4px 8px;
}

#taskRowTitle, #todaySectionTitle, #taskEditorTitle {
    color: #ecf4fc;
    font-weight: 600;
}

#taskRowDetails, #todaySectionBody, #taskStatusLabel, #taskEditorStatus,
#todayDateLabel, #todayTimezoneLabel, #todayRefreshStatus, #taskFieldLabel {
    color: #9aabc0;
}

#tasksList, #todaySectionItems {
    background: transparent;
    border: 0;
    color: #dce7f3;
}

#tasksList::item, #todaySectionItems::item {
    padding: 2px;
}

#pageTitle {
    color: #f7fbff;
    font-size: 20pt;
    font-weight: 650;
}

#pageSubtitle {
    min-height: 24px;
}

#sourceFailureTitle, #diagnosticsTitle {
    color: #ecf4fc;
    font-size: 13pt;
    font-weight: 600;
}

#sourceFailureBody, #diagnosticsBody {
    color: #a9b9cc;
    font-size: 10pt;
}

#inboxList, #messageSummaryList, #sourcesList, #diagnosticsList {
    background: #0d1a2b;
    border: 1px solid #263b55;
    border-radius: 9px;
    color: #dce7f3;
    padding: 8px;
}

#inboxList::item, #messageSummaryList::item, #sourcesList::item, #diagnosticsList::item {
    padding: 9px;
    border-bottom: 1px solid #20334d;
}

#inboxList::item:selected, #messageSummaryList::item:selected,
#sourcesList::item:selected, #diagnosticsList::item:selected {
    background: #1c3d66;
    color: #ffffff;
}

#inboxModeTabs::pane {
    border: 0;
    background: transparent;
}

#inboxModeTabs QTabBar::tab {
    background: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
    color: #9aabc0;
    padding: 7px 12px;
    margin-right: 4px;
}

#inboxModeTabs QTabBar::tab:selected {
    background: #1c3c61;
    border-color: #5f9edc;
    color: #ffffff;
    font-weight: 650;
}

#inboxModeTabs QTabBar::tab:hover:!selected {
    background: #14253a;
    color: #f7fbff;
}

#messageCategoryBar {
    background: transparent;
}

#inboxRow, #messageSummaryRow {
    background: transparent;
    border: 0;
    border-bottom: 1px solid #20334d;
}

#inboxRow:hover, #messageSummaryRow:hover {
    background: #101f32;
}

#inboxRowTitle, #messageSummaryTitle {
    color: #ecf4fc;
    font-weight: 600;
}

#inboxRowDetails, #messageSummaryMetadata, #messageCategoryStatus {
    color: #9aabc0;
}

#inboxUnreadIndicator, #messageUnreadIndicator {
    color: #70b5ff;
    font-size: 9pt;
}

#messageSummaryRow[important="true"] #messageUnreadIndicator {
    color: #f7c66c;
    font-weight: 700;
}

#privacyOverlay {
    background: #0b1421;
    border: 2px solid #5f9edc;
    border-radius: 12px;
}

#privacyOverlayTitle {
    color: #f7fbff;
    font-size: 16pt;
    font-weight: 650;
}

#privacyOverlayBody {
    color: #b9c8d9;
}

#statusLabel {
    border-top: 1px solid #1c2c40;
    padding: 8px 16px;
}

#settingsRow {
    border-bottom: 1px solid #1c2c40;
    padding: 7px 0;
}
"""


class CalendarEventButton(QPushButton):
    """A compact calendar chip that elides only its visual label.

    A normal ``QPushButton`` includes its complete text in its minimum size
    hint.  Calendar titles are provider content and can be arbitrarily long
    within the bounded contract, so using that hint lets one event resize an
    entire week column.  This button deliberately ignores the text size hint
    and recalculates an elided label whenever its width or font changes.  The
    unabridged event description remains available through the tooltip and
    accessible name supplied by the calendar renderer.
    """

    def __init__(
        self,
        text: str,
        *,
        parent: QWidget | None = None,
    ) -> None:
        self._calendar_label = text
        super().__init__(text, parent)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

    def resizeEvent(self, event: object) -> None:
        super().resizeEvent(event)  # type: ignore[arg-type]
        self._update_elided_label()

    def changeEvent(self, event: object) -> None:
        super().changeEvent(event)  # type: ignore[arg-type]
        if getattr(event, "type", lambda: None)() in {
            QEvent.Type.FontChange,
            QEvent.Type.StyleChange,
        }:
            self._update_elided_label()

    def _update_elided_label(self) -> None:
        available_width = max(0, self.contentsRect().width() - 12)
        if available_width < 8:
            # Keep a visible affordance during the brief zero-width phase
            # while a stacked/grid layout is being activated.
            rendered = "…" if self._calendar_label else ""
        else:
            rendered = QFontMetrics(self.font()).elidedText(
                self._calendar_label,
                Qt.TextElideMode.ElideRight,
                available_width,
            )
        if self.text() != rendered:
            self.setText(rendered)


def _application_icon() -> QIcon:
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#4b8fd8"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(4, 4, 56, 56, 16, 16)
    painter.setPen(QColor("#ffffff"))
    font = painter.font()
    font.setBold(True)
    font.setPointSize(25)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "P")
    painter.end()
    return QIcon(pixmap)


def _configure_application(application: QApplication) -> QApplication:
    application.setApplicationName(APPLICATION_NAME)
    application.setApplicationDisplayName(APPLICATION_NAME)
    application.setQuitOnLastWindowClosed(True)
    application.setStyle("Fusion")
    application.setWindowIcon(_application_icon())
    application.setStyleSheet(_BASE_STYLESHEET)
    return application


def create_application(argv: Sequence[str] | None = None) -> QApplication:
    """Return a configured QApplication without starting its event loop."""

    application = QApplication.instance()
    if application is None:
        arguments = list(argv) if argv is not None else sys.argv
        application = QApplication(arguments)
    return _configure_application(application)


class PersonalHubTrayController(QObject):
    """Hub-owned tray actions that emit work requests to the composition root."""

    open_requested = Signal()
    today_requested = Signal()
    inbox_requested = Signal()
    refresh_requested = Signal()
    network_pause_changed = Signal(bool)
    offline_mode_changed = Signal(bool)
    quit_requested = Signal()

    # Semantic aliases keep integrations readable while retaining one signal
    # source for each tray control.
    pause_changed = network_pause_changed
    offline_toggled = offline_mode_changed
    refresh_sources_requested = refresh_requested

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        icon: QIcon | None = None,
        on_open: Callable[[], object] | None = None,
        on_today: Callable[[], object] | None = None,
        on_inbox: Callable[[], object] | None = None,
        on_refresh: Callable[[], object] | None = None,
        on_quit: Callable[[], object] | None = None,
    ) -> None:
        super().__init__(parent)
        self._icon = icon or _application_icon()
        self._network_refresh_paused = False
        self._offline_mode = False
        self._visible = False

        self.menu = QMenu(parent if isinstance(parent, QWidget) else None)
        self.open_action = QAction("Open Hub", self)
        self.today_action = QAction("Open Home", self)
        self.inbox_action = QAction("Open Inbox", self)
        self.refresh_action = QAction("Refresh enabled sources", self)
        self.network_pause_action = QAction("Pause network refresh", self)
        self.offline_mode_action = QAction("Offline mode", self)
        self.offline_mode_action.setCheckable(True)
        self.quit_action = QAction("Quit", self)
        self.menu.addAction(self.open_action)
        self.menu.addAction(self.today_action)
        self.menu.addAction(self.inbox_action)
        self.menu.addSeparator()
        self.menu.addAction(self.refresh_action)
        self.menu.addAction(self.network_pause_action)
        self.menu.addAction(self.offline_mode_action)
        self.menu.addSeparator()
        self.menu.addAction(self.quit_action)

        self.open_action.triggered.connect(self.open_requested)
        self.today_action.triggered.connect(self.today_requested)
        self.inbox_action.triggered.connect(self.inbox_requested)
        self.refresh_action.triggered.connect(self.refresh_requested)
        self.network_pause_action.triggered.connect(self._toggle_network_pause)
        self.offline_mode_action.toggled.connect(self._set_offline_from_action)
        self.quit_action.triggered.connect(self.quit_requested)
        if on_open is not None:
            self.open_requested.connect(on_open)
        if on_today is not None:
            self.today_requested.connect(on_today)
        if on_inbox is not None:
            self.inbox_requested.connect(on_inbox)
        if on_refresh is not None:
            self.refresh_requested.connect(on_refresh)
        if on_quit is not None:
            self.quit_requested.connect(on_quit)

        self.tray_icon: QSystemTrayIcon | None = None
        self.is_available = False
        try:
            self.is_available = QSystemTrayIcon.isSystemTrayAvailable()
            self.tray_icon = QSystemTrayIcon(
                self._icon,
                parent if isinstance(parent, QWidget) else None,
            )
            self.tray_icon.setToolTip(APPLICATION_NAME)
            self.tray_icon.setContextMenu(self.menu)
            self.tray_icon.activated.connect(self._tray_activated)
        except (RuntimeError, TypeError):
            # A headless/offscreen test environment may not provide a native
            # tray.  The QAction menu remains available for deterministic tests.
            self.tray_icon = None

        # Concise aliases make integration code independent of action naming.
        self.pause_action = self.network_pause_action
        self.offline_action = self.offline_mode_action

    @property
    def network_refresh_paused(self) -> bool:
        return self._network_refresh_paused

    @property
    def offline_mode(self) -> bool:
        return self._offline_mode

    @property
    def actions(self) -> dict[str, QAction]:
        return {
            "open": self.open_action,
            "today": self.today_action,
            "inbox": self.inbox_action,
            "refresh": self.refresh_action,
            "pause": self.network_pause_action,
            "offline": self.offline_mode_action,
            "quit": self.quit_action,
        }

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in {
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        }:
            self.open_requested.emit()

    def _toggle_network_pause(self) -> None:
        self.set_network_refresh_paused(not self._network_refresh_paused)

    def _set_offline_from_action(self, enabled: bool) -> None:
        self.set_offline_mode(enabled)

    def set_network_refresh_paused(self, paused: bool, *, emit: bool = True) -> None:
        paused = bool(paused)
        changed = paused != self._network_refresh_paused
        self._network_refresh_paused = paused
        self.network_pause_action.setText(
            "Resume network refresh" if paused else "Pause network refresh"
        )
        self.network_pause_action.setChecked(paused)
        if changed and emit:
            self.network_pause_changed.emit(paused)

    set_network_paused = set_network_refresh_paused

    def set_offline_mode(self, enabled: bool, *, emit: bool = True) -> None:
        enabled = bool(enabled)
        changed = enabled != self._offline_mode
        self._offline_mode = enabled
        self.offline_mode_action.blockSignals(True)
        self.offline_mode_action.setChecked(enabled)
        self.offline_mode_action.blockSignals(False)
        if changed and emit:
            self.offline_mode_changed.emit(enabled)

    def show(self) -> None:
        self._visible = True
        if self.tray_icon is not None:
            self.tray_icon.show()

    def hide(self) -> None:
        self._visible = False
        if self.tray_icon is not None:
            self.tray_icon.hide()

    def show_notification(
        self,
        title: str,
        message: str,
        *,
        timeout_ms: int = 10_000,
    ) -> bool:
        """Show one bounded native notification when a system tray is available."""

        if self.tray_icon is None or not self.is_available:
            return False
        safe_title = _safe_text(title, fallback=APPLICATION_NAME, limit=120)
        safe_message = _safe_text(message, fallback="New Hub attention item.", limit=300)
        self.tray_icon.showMessage(safe_title, safe_message, self._icon, timeout_ms)
        return True

    @property
    def is_visible(self) -> bool:
        return self._visible

    def trigger(self, action: str) -> None:
        """Trigger a named action; useful for headless integration tests."""

        actions = self.actions
        if action not in actions:
            raise KeyError(action)
        actions[action].trigger()


class _InboxRowWidget(QFrame):
    """Quiet two-line presentation row for one durable Hub attention item."""

    def __init__(
        self,
        item: InboxItemView,
        title: str,
        details: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("inboxRow")
        self.setProperty("unread", bool(item.unread))
        self.setAccessibleName(f"Inbox item: {title}")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(9)

        indicator = QLabel("●" if item.unread else "○", self)
        indicator.setObjectName("inboxUnreadIndicator")
        indicator.setAccessibleName("Unread" if item.unread else "Read")
        indicator.setTextFormat(Qt.TextFormat.PlainText)
        indicator.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        indicator.setFixedWidth(14)
        layout.addWidget(indicator)

        body = QWidget(self)
        body.setObjectName("inboxRowBody")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(2)

        title_label = QLabel(title, body)
        title_label.setObjectName("inboxRowTitle")
        title_label.setTextFormat(Qt.TextFormat.PlainText)
        title_label.setWordWrap(True)
        body_layout.addWidget(title_label)

        details_label = QLabel(details, body)
        details_label.setObjectName("inboxRowDetails")
        details_label.setTextFormat(Qt.TextFormat.PlainText)
        details_label.setWordWrap(True)
        details_label.setVisible(bool(details))
        body_layout.addWidget(details_label)
        layout.addWidget(body, 1)


class _MessageSummaryRowWidget(QFrame):
    """Structured two-line message row that avoids a long delimiter string."""

    def __init__(
        self,
        summary: MessageSummaryView,
        title: str,
        metadata: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("messageSummaryRow")
        self.setProperty("unread", bool(summary.unread))
        self.setProperty("important", summary.importance == "important")
        self.setAccessibleName(f"Message: {title}")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(9)

        indicator_text = "!" if summary.importance == "important" else "●"
        if not summary.unread and summary.importance != "important":
            indicator_text = "○"
        indicator = QLabel(indicator_text, self)
        indicator.setObjectName("messageUnreadIndicator")
        indicator.setAccessibleName(
            "Important unread"
            if summary.importance == "important" and summary.unread
            else "Unread"
            if summary.unread
            else "Read"
        )
        indicator.setTextFormat(Qt.TextFormat.PlainText)
        indicator.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        indicator.setFixedWidth(14)
        layout.addWidget(indicator)

        body = QWidget(self)
        body.setObjectName("messageSummaryRowBody")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(2)

        title_label = QLabel(title, body)
        title_label.setObjectName("messageSummaryTitle")
        title_label.setTextFormat(Qt.TextFormat.PlainText)
        title_label.setWordWrap(True)
        body_layout.addWidget(title_label)

        metadata_label = QLabel(metadata, body)
        metadata_label.setObjectName("messageSummaryMetadata")
        metadata_label.setTextFormat(Qt.TextFormat.PlainText)
        metadata_label.setWordWrap(True)
        metadata_label.setVisible(bool(metadata))
        body_layout.addWidget(metadata_label)
        layout.addWidget(body, 1)


class TaskRowWidget(QFrame):
    """Keyboard-friendly presentation row for one in-memory task view."""

    complete_requested = Signal(str)
    edit_requested = Signal(str)
    archive_requested = Signal(str)
    undo_requested = Signal(str)
    native_open_requested = Signal(object)

    def __init__(self, task: TaskView, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.task = task
        self.setObjectName("taskRow")
        self.setProperty("taskPriority", task.priority)
        self.setProperty("taskState", task.state)
        self.setAccessibleName(f"Task: {task.title}")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFrameShape(QFrame.Shape.NoFrame)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 8, 8)
        layout.setSpacing(9)

        complete_label = "✓" if task.state == "open" else "↶"
        complete_name = "Complete" if task.state == "open" else "Undo completion"
        if task.state == "archived":
            complete_label = "↶"
            complete_name = "Undo archive"
        self.complete_button = QToolButton(self)
        self.complete_button.setText(complete_label)
        self.complete_button.setObjectName("taskCompleteButton")
        self.complete_button.setAccessibleName(complete_name)
        self.complete_button.setToolTip(complete_name)
        self.complete_button.setAutoRaise(True)
        self.complete_button.setFixedSize(32, 32)
        self.complete_button.clicked.connect(self._complete_or_undo)
        layout.addWidget(self.complete_button, 0, Qt.AlignmentFlag.AlignTop)

        body = QWidget(self)
        body.setObjectName("taskRowBody")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(2)

        self.title_label = QLabel(task.title, body)
        self.title_label.setObjectName("taskRowTitle")
        self.title_label.setAccessibleName("Task title")
        self.title_label.setTextFormat(Qt.TextFormat.PlainText)
        self.title_label.setWordWrap(True)
        body_layout.addWidget(self.title_label)

        details: list[str] = []
        if task.priority != "normal":
            details.append(task.priority.title())
        if task.due_at is not None:
            details.append(_task_due_label(task))
        else:
            details.append("No date")
        if task.list_id:
            details.append(task.list_id)
        if task.tags:
            details.append("#" + " #".join(task.tags))
        self.details_label = QLabel(" · ".join(details), body)
        self.details_label.setObjectName("taskRowDetails")
        self.details_label.setAccessibleName("Task details")
        self.details_label.setTextFormat(Qt.TextFormat.PlainText)
        self.details_label.setWordWrap(True)
        body_layout.addWidget(self.details_label)
        layout.addWidget(body, 1)

        self.native_open_button: QPushButton | None = None
        native_target = _safe_http_target(task.external_url, limit=1_024)
        if native_target:
            native_label = _task_native_open_label(task)
            self.native_open_button = QPushButton(native_label, self)
            self.native_open_button.setObjectName("taskNativeOpenButton")
            self.native_open_button.setAccessibleName(f"{native_label}: {task.title}")
            self.native_open_button.setAccessibleDescription(
                "Open this task in its source platform."
            )
            self.native_open_button.setAutoDefault(False)
            self.native_open_button.clicked.connect(
                lambda _checked=False: self.native_open_requested.emit(self.task)
            )
            layout.addWidget(self.native_open_button)
        self.task_native_open_button = self.native_open_button
        self.open_button = self.native_open_button

        # Keep the button attributes for the presentation contract and existing
        # callers, while exposing secondary actions through one compact menu.
        self.edit_button = QPushButton("Edit", self)
        self.edit_button.setObjectName("taskEditButton")
        self.edit_button.setAccessibleName("Edit task")
        self.edit_button.setAutoDefault(False)
        self.edit_button.hide()
        self.edit_button.clicked.connect(lambda: self.edit_requested.emit(self.task.task_id))

        archive_label = "Undo archive" if task.state == "archived" else "Archive"
        self.archive_button = QPushButton(archive_label, self)
        self.archive_button.setObjectName("taskArchiveButton")
        self.archive_button.setAccessibleName(archive_label)
        self.archive_button.setAutoDefault(False)
        self.archive_button.hide()
        self.archive_button.clicked.connect(self._archive_or_undo)

        self.action_button = QToolButton(self)
        self.action_button.setObjectName("taskOverflowButton")
        self.action_button.setText("…")
        self.action_button.setAccessibleName("More task actions")
        self.action_button.setToolTip("More task actions")
        self.action_button.setAutoRaise(True)
        self.action_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.action_menu = QMenu(self.action_button)
        edit_action = self.action_menu.addAction("Edit task")
        edit_action.triggered.connect(lambda _checked=False: self.edit_button.click())
        self.action_menu.addSeparator()
        archive_action = self.action_menu.addAction(archive_label)
        archive_action.triggered.connect(lambda _checked=False: self.archive_button.click())
        self.action_button.setMenu(self.action_menu)
        layout.addWidget(self.action_button, 0, Qt.AlignmentFlag.AlignTop)

    def _complete_or_undo(self) -> None:
        if self.task.state == "open":
            self.complete_requested.emit(self.task.task_id)
        else:
            self.undo_requested.emit(self.task.task_id)

    def _archive_or_undo(self) -> None:
        if self.task.state == "archived":
            self.undo_requested.emit(self.task.task_id)
        else:
            self.archive_requested.emit(self.task.task_id)

    def keyPressEvent(self, event: Any) -> None:
        key = event.key()
        if key in {Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space}:
            self._complete_or_undo()
            event.accept()
            return
        if key in {Qt.Key.Key_E, Qt.Key.Key_F2}:
            self.edit_requested.emit(self.task.task_id)
            event.accept()
            return
        if key == Qt.Key.Key_A:
            self._archive_or_undo()
            event.accept()
            return
        if key == Qt.Key.Key_O and self.native_open_button is not None:
            self.native_open_requested.emit(self.task)
            event.accept()
            return
        super().keyPressEvent(event)


class TaskListWidget(QListWidget):
    """Route task keyboard commands whether focus is on the list or a row."""

    def keyPressEvent(self, event: Any) -> None:
        current = self.currentItem()
        row = self.itemWidget(current) if current is not None else None
        if row is not None and event.key() in {
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
            Qt.Key.Key_Space,
            Qt.Key.Key_E,
            Qt.Key.Key_F2,
            Qt.Key.Key_A,
            Qt.Key.Key_O,
        }:
            row.keyPressEvent(event)
            return
        super().keyPressEvent(event)


def _task_due_label(task: TaskView) -> str:
    if task.due_at is None:
        return "Unscheduled"
    due = _coerce_datetime(task.due_at)
    if due is None:
        return "Unscheduled"
    label = due.date().isoformat()
    if not task.due_is_all_day:
        label += " " + due.strftime("%H:%M")
    if task.timezone and task.timezone != "local":
        label += f" ({task.timezone})"
    return label


def _task_native_open_label(task: TaskView) -> str:
    if task.native_platform == "canvas" or "canvas" in task.tags:
        return "Open in Canvas"
    return "Open source"


class _UiSignalBridge(QObject):
    source_failure = Signal(object)


class PersonalHubMainWindow(QMainWindow):
    """Responsive, accessible presentation shell for Personal Hub.

    Tasks and Today are intentionally local, in-memory view workflows.  The
    composition root can subscribe to the request signals and provide durable
    data later without making this module aware of repositories, providers, or
    lifecycle services.
    """

    WINDOW_TITLE: Final = APPLICATION_NAME
    MINIMUM_SIZE: Final[tuple[int, int]] = (720, 460)
    RESPONSIVE_BREAKPOINT: Final = 840

    destination_changed = Signal(str)
    refresh_requested = Signal()
    network_pause_changed = Signal(bool)
    offline_mode_changed = Signal(bool)
    quit_requested = Signal()
    source_failure_received = Signal(object)
    attention_notification_open_requested = Signal(object)
    attention_notification_dismissed = Signal(object)
    closed_to_tray = Signal()
    task_created = Signal(object)
    task_updated = Signal(object)
    task_completed = Signal(object)
    task_archived = Signal(object)
    task_undo_requested = Signal(object)
    task_native_open_requested = Signal(object)
    task_edit_requested = Signal(object)
    task_import_requested = Signal()
    task_filters_changed = Signal(object)
    today_changed = Signal(object)
    today_section_order_changed = Signal(object)
    today_section_visibility_changed = Signal(object)
    dashboard_layout_changed = Signal(object)
    calendar_changed = Signal(object)
    calendar_mode_changed = Signal(str)
    calendar_period_changed = Signal(object)
    calendar_selection_changed = Signal(object)
    calendar_event_open_requested = Signal(object)
    calendar_task_completion_changed = Signal(object)
    calendar_slot_requested = Signal(object)
    calendar_deep_link_requested = Signal(object)
    calendar_import_requested = Signal()
    calendar_import_delete_requested = Signal(str)
    ical_feed_create_requested = Signal()
    ical_feed_update_requested = Signal(str)
    ical_feed_delete_requested = Signal(str)
    ical_feed_enabled_changed = Signal(object)
    news_changed = Signal(object)
    news_filter_changed = Signal(object)
    news_subscription_selection_changed = Signal(object)
    feed_subscription_selection_changed = news_subscription_selection_changed
    news_entry_read_changed = Signal(object)
    feed_entry_read_changed = news_entry_read_changed
    news_entry_open_requested = Signal(object)
    feed_entry_open_requested = news_entry_open_requested
    news_deep_link_requested = news_entry_open_requested
    feed_deep_link_requested = news_entry_open_requested
    feed_subscription_create_requested = Signal(object)
    feed_subscription_update_requested = Signal(object)
    feed_subscription_delete_requested = Signal(object)
    feed_subscription_enabled_changed = Signal(object)
    watch_create_requested = Signal(object)
    watch_update_requested = Signal(object)
    watch_delete_requested = Signal(object)
    watch_test_requested = Signal(object)
    watch_refresh_requested = Signal(object)
    watch_enabled_changed = Signal(object)
    watch_open_requested = Signal(object)
    message_open_requested = Signal(object)
    message_deep_link_requested = Signal(object)
    message_clear_requested = Signal()
    message_category_changed = Signal(str)
    message_filter_changed = message_category_changed
    inbox_mode_changed = Signal(str)
    inbox_state_changed = Signal(object)
    settings_save_requested = Signal(object)
    backup_requested = Signal()
    restore_requested = Signal()
    export_requested = Signal()
    sync_configure_requested = Signal(str)
    sync_requested = Signal(str)
    sync_disable_requested = Signal()
    source_refresh_requested = Signal(str)
    platform_configure_requested = Signal(str)
    source_reconnect_requested = Signal(str)
    source_enabled_changed = Signal(object)
    source_disconnect_requested = Signal(str)
    news_subscription_create_requested = feed_subscription_create_requested
    news_subscription_update_requested = feed_subscription_update_requested
    news_subscription_delete_requested = feed_subscription_delete_requested
    news_subscription_enabled_changed = feed_subscription_enabled_changed
    create_subscription_requested = feed_subscription_create_requested
    update_subscription_requested = feed_subscription_update_requested
    delete_subscription_requested = feed_subscription_delete_requested

    calendar_source_selection_changed = calendar_selection_changed
    calendar_source_selected = calendar_selection_changed
    event_open_requested = calendar_event_open_requested
    event_deep_link_requested = calendar_deep_link_requested
    calendar_task_done_changed = calendar_task_completion_changed
    calendar_time_slot_requested = calendar_slot_requested

    # Semantic aliases keep the presentation contract easy to discover while
    # preserving a single signal per action.
    attention_notice_open_requested = attention_notification_open_requested
    attention_notice_dismissed = attention_notification_dismissed
    task_completion_requested = task_completed
    task_archive_requested = task_archived
    task_added = task_created
    task_deep_link_requested = task_native_open_requested
    task_changed = task_updated
    task_completed_requested = task_completed
    task_archived_requested = task_archived
    task_undo = task_undo_requested
    today_sections_changed = today_changed

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        close_to_tray: bool = True,
        show_tray: bool = False,
        text_scale: float = 1.0,
        high_contrast: bool = False,
        reduced_motion: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("personalHubWindow")
        self.setAccessibleName("Personal Hub main window")
        self.setWindowTitle(self.WINDOW_TITLE)
        self.setWindowIcon(_application_icon())
        self.setMinimumSize(*self.MINIMUM_SIZE)
        self.resize(1280, 820)

        self._active_destination = TODAY_DESTINATION
        self._close_to_tray = bool(close_to_tray)
        self._quitting = False
        self._locked = False
        self._privacy_masked = False
        self._network_refresh_paused = False
        self._offline_mode = False
        self._text_scale = max(1.0, min(float(text_scale), 1.75))
        self._high_contrast = bool(high_contrast)
        self._reduced_motion = bool(reduced_motion)
        self._appearance = {
            "ui_theme": "navy",
            "ui_accent_color": "",
            "ui_background_color": "",
            "ui_surface_color": "",
        }
        self._compact_navigation = False
        self._inbox_items: tuple[InboxItemView, ...] = ()
        self._sources: tuple[SourceView, ...] = ()
        self._settings_view = SettingsView()
        self._sync_view = SyncView()
        self._diagnostics_view = DiagnosticsView()
        self._source_failures: dict[str, SourceFailureView] = {}
        self._attention_notification: AttentionNotificationView | None = None
        self._tasks: dict[str, TaskView] = {}
        self._task_undo_stack: list[tuple[str, TaskView]] = []
        self._task_state_filter_value = "all"
        self._task_due_filter_value = "all"
        self._task_sort_value = "due"
        self._task_search_value = ""
        self._today_view = TodayView()
        self._today_sections: dict[str, TodaySectionView] = {
            section.section_id: section for section in _DEFAULT_TODAY_SECTIONS
        }
        self._today_section_order: tuple[str, ...] = tuple(
            section.section_id for section in _DEFAULT_TODAY_SECTIONS
        )
        self._calendar_view = CalendarView()
        self._calendar_events: tuple[CalendarEventView, ...] = ()
        self._calendar_sources: tuple[CalendarSourceView, ...] = ()
        self._calendar_ical_feeds: tuple[IcalFeedView, ...] = ()
        self._calendar_local_imports: tuple[CalendarImportView, ...] = ()
        self._calendar_selected_ids: set[str] = set()
        self._calendar_agenda_rendering = False
        self._calendar_mode_value = "month"
        self._calendar_cursor = date.today()
        self._calendar_color_by_key: dict[str, str] = {}
        self._calendar_selected_event_id: str | None = None
        self._editing_new_task = False
        self._news_view = NewsView()
        self._news_subscriptions: tuple[FeedSubscriptionView, ...] = ()
        self._news_entries: tuple[FeedEntryView, ...] = ()
        self._news_selected_ids: set[str] = set()
        self._news_include_terms: tuple[str, ...] = ()
        self._news_exclude_terms: tuple[str, ...] = ()
        self._news_unread_only = False
        self._news_selection_explicit = False
        self._news_subscription_editor_id: str | None = None
        self._web_watches_view = WebWatchesView()
        self._web_watches: tuple[WebWatchView, ...] = ()
        self._watch_alerts: tuple[WatchAlertView, ...] = ()
        self._watch_editor_id: str | None = None
        self._messages_view = MessagesView()
        self._message_sources: tuple[MessageSourceView, ...] = ()
        self._message_all_summaries: tuple[MessageSummaryView, ...] = ()
        self._message_summaries: tuple[MessageSummaryView, ...] = ()
        self._message_category_value = "primary"
        self._inbox_mode_value = "alerts"
        self.message_category_buttons: dict[str, QPushButton] = {}
        self._message_body: MessageBodyView | None = None
        self.navigation_buttons: dict[str, QPushButton] = {}
        self.destination_buttons = self.navigation_buttons
        self.nav_buttons = self.navigation_buttons
        self.page_widgets: dict[str, QWidget] = {}

        self._ui_bridge = _UiSignalBridge(self)
        self._ui_bridge.source_failure.connect(self._receive_source_failure)
        self._build_shell()
        self.page_transition = PageTransition(self.pages)

        self.tray_controller = PersonalHubTrayController(self, icon=self.windowIcon())
        self.tray = self.tray_controller.tray_icon
        self.system_tray = self.tray_controller
        self.tray_actions = self.tray_controller.actions
        self.tray_controller.open_requested.connect(self.show_from_tray)
        self.tray_controller.today_requested.connect(self.open_today_from_tray)
        self.tray_controller.inbox_requested.connect(self.open_inbox_from_tray)
        self.tray_controller.refresh_requested.connect(self._request_refresh)
        self.tray_controller.network_pause_changed.connect(self._handle_tray_pause)
        self.tray_controller.offline_mode_changed.connect(self._handle_tray_offline)
        self.tray_controller.quit_requested.connect(self.request_quit)

        self.apply_accessibility_preferences(
            text_scale=self._text_scale,
            high_contrast=self._high_contrast,
            reduced_motion=self._reduced_motion,
        )
        if show_tray:
            self.tray_controller.show()

    def _build_shell(self) -> None:
        root = QWidget(self)
        root.setObjectName("personalHubRoot")
        root.setAccessibleName("Personal Hub application content")
        self.setCentralWidget(root)

        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.navigation = self._build_navigation()
        root_layout.addWidget(self.navigation)

        self.pages = QStackedWidget()
        self.pages.setObjectName("personalHubPages")
        self.pages.setAccessibleName("Personal Hub page content")
        self.content_stack = self.pages
        self.page_widgets[TODAY_DESTINATION] = self._build_today_page()
        self.pages.addWidget(self.page_widgets[TODAY_DESTINATION])
        for destination in NAVIGATION_DESTINATIONS[1:]:
            if destination == "Calendar":
                page = self._build_calendar_page()
            elif destination == "Tasks":
                page = self._build_tasks_page()
            elif destination == "Watchlists":
                page = self._build_watchlists_page()
            elif destination == "Inbox":
                page = self._build_inbox_page()
            elif destination == "Sources":
                page = self._build_sources_page()
            else:
                page = self._build_settings_page()
            self.page_widgets[destination] = page
            self.pages.addWidget(page)
        content = QWidget()
        content.setObjectName("personalHubContent")
        content.setAccessibleName("Personal Hub page area")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        (
            self.attention_notification_panel,
            self.attention_notification_title,
            self.attention_notification_body,
            self.attention_notification_open_button,
            self.attention_notification_dismiss_button,
        ) = self._build_attention_notification_panel()
        self.attention_notice_panel = self.attention_notification_panel
        self.attention_notice_title = self.attention_notification_title
        self.attention_notice_body = self.attention_notification_body
        self.attention_notice_open_button = self.attention_notification_open_button
        self.attention_notice_dismiss_button = self.attention_notification_dismiss_button
        content_layout.addWidget(self.attention_notification_panel)
        (
            self.source_failure_panel,
            self.source_failure_title,
            self.source_failure_body,
        ) = self._build_source_failure_panel()
        self.source_failure_label = self.source_failure_body
        self.source_failure_message = self.source_failure_body
        content_layout.addWidget(self.source_failure_panel)
        content_layout.addWidget(self.pages, 1)
        root_layout.addWidget(content, 1)

        self.status_label = QLabel("Home · ready")
        self.status_label.setObjectName("statusLabel")
        self.status_label.setAccessibleName("Personal Hub status")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.statusBar().addPermanentWidget(self.status_label, 1)
        self.statusBar().setAccessibleName("Personal Hub status bar")
        self.statusBar().setSizeGripEnabled(False)

        self.privacy_overlay = self._build_privacy_overlay(self.pages)
        self._set_responsive_mode(self.width() < self.RESPONSIVE_BREAKPOINT)
        self._set_active_destination(TODAY_DESTINATION, emit=False)
        self._focus_first_available_control()

    def _build_navigation(self) -> QFrame:
        navigation = QFrame()
        navigation.setObjectName("personalHubNavigation")
        navigation.setAccessibleName("Personal Hub navigation")
        navigation.setMinimumWidth(190)
        navigation.setMaximumWidth(260)
        navigation.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)

        layout = QVBoxLayout(navigation)
        self.navigation_layout = layout
        layout.setContentsMargins(18, 22, 18, 18)
        layout.setSpacing(6)

        brand = QLabel(APPLICATION_NAME)
        brand.setObjectName("brandTitle")
        brand.setAccessibleName(APPLICATION_NAME)
        brand.setTextFormat(Qt.TextFormat.PlainText)
        self.brand_title = brand
        layout.addWidget(brand)

        layout.addSpacing(20)
        navigation_label = QLabel("NAVIGATION")
        navigation_label.setObjectName("navigationLabel")
        navigation_label.setAccessibleName("Navigation destinations")
        navigation_label.setTextFormat(Qt.TextFormat.PlainText)
        self.navigation_caption = navigation_label
        layout.addWidget(navigation_label)

        self._navigation_group = QButtonGroup(navigation)
        self._navigation_group.setExclusive(True)
        for destination in NAVIGATION_DESTINATIONS:
            button = QPushButton(destination)
            button.setObjectName(_NAVIGATION_OBJECT_NAMES[destination])
            button.setProperty("hubNavigation", True)
            button.setAccessibleName(destination)
            button.setAccessibleDescription(f"Open the {destination} page.")
            button.setCheckable(True)
            button.setAutoDefault(False)
            button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            button.setMinimumHeight(38)
            self._navigation_group.addButton(button)
            self.navigation_buttons[destination] = button
            setattr(self, f"{destination.casefold().replace(' ', '_')}_button", button)
            button.clicked.connect(
                lambda _checked=False, value=destination: self._set_active_destination(value)
            )
            layout.addWidget(button)

        self.today_button = self.navigation_buttons[HOME_DESTINATION]

        layout.addStretch(1)

        for first, second in zip(
            (self.navigation_buttons[destination] for destination in NAVIGATION_DESTINATIONS),
            (self.navigation_buttons[destination] for destination in NAVIGATION_DESTINATIONS[1:]),
            strict=False,
        ):
            self.setTabOrder(first, second)
        return navigation

    @staticmethod
    def _new_page(
        object_name: str,
        accessible_name: str,
        *,
        scrollable: bool = False,
    ) -> tuple[QWidget, QVBoxLayout]:
        if scrollable:
            page = QScrollArea()
            page.setObjectName(object_name)
            page.setAccessibleName(accessible_name)
            page.setFrameShape(QFrame.Shape.NoFrame)
            page.setWidgetResizable(True)
            page.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

            content = QWidget()
            content.setObjectName(f"{object_name}Content")
            content.setAccessibleName(f"{accessible_name} scrollable content")
            content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
            layout = QVBoxLayout(content)
            layout.setContentsMargins(24, 18, 24, 24)
            layout.setSpacing(10)
            layout.setSizeConstraint(QVBoxLayout.SizeConstraint.SetMinimumSize)
            page.setWidget(content)
            return page, layout

        page = QWidget()
        page.setObjectName(object_name)
        page.setAccessibleName(accessible_name)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 18, 24, 24)
        layout.setSpacing(10)
        return page, layout

    @staticmethod
    def _add_page_header(
        layout: QVBoxLayout,
        destination: str,
        description: str,
    ) -> tuple[QLabel, QLabel, QLabel]:
        header = QWidget()
        header.setObjectName("pageHeader")
        header.setAccessibleName(f"{destination} page header")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(12)

        eyebrow = QLabel(destination.upper())
        eyebrow.setObjectName("pageEyebrow")
        eyebrow.setAccessibleName(f"{destination} section")
        eyebrow.setTextFormat(Qt.TextFormat.PlainText)
        # Keep the semantic section label in the object tree for accessibility
        # and tests, but avoid spending a full visual line on a duplicate title.
        eyebrow.setVisible(False)
        header_layout.addWidget(eyebrow)

        title = QLabel(destination)
        title.setObjectName("pageTitle")
        title.setAccessibleName(f"{destination} page title")
        title.setTextFormat(Qt.TextFormat.PlainText)
        header_layout.addWidget(title)

        subtitle = QLabel(description)
        subtitle.setObjectName("pageSubtitle")
        subtitle.setAccessibleName(f"{destination} page description")
        subtitle.setTextFormat(Qt.TextFormat.PlainText)
        subtitle.setWordWrap(True)
        subtitle.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        header_layout.addWidget(subtitle, 1)
        layout.addWidget(header)
        return eyebrow, title, subtitle

    def _build_today_page(self) -> QWidget:
        page, layout = self._new_page("todayPage", "Home page", scrollable=True)
        self.today_page = page
        self._today_layout = layout
        self.today_eyebrow, self.today_title, self.today_subtitle = self._add_page_header(
            layout,
            TODAY_DESTINATION,
            "Schedule, tasks, alerts, news, and source status.",
        )
        layout.addSpacing(10)

        controls = QWidget()
        controls.setObjectName("todayControls")
        controls_layout = QGridLayout(controls)
        self.today_controls_layout = controls_layout
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(10)
        self.today_date_label = QLabel(date.today().isoformat())
        self.today_date_label.setObjectName("todayDateLabel")
        self.today_date_label.setAccessibleName("Today local date")
        self.today_date_label.setTextFormat(Qt.TextFormat.PlainText)
        self.today_date_label.setWordWrap(True)
        controls_layout.addWidget(self.today_date_label, 0, 0)
        self.today_timezone_label = QLabel("")
        self.today_timezone_label.setObjectName("todayTimezoneLabel")
        self.today_timezone_label.setAccessibleName("Today timezone")
        self.today_timezone_label.setTextFormat(Qt.TextFormat.PlainText)
        self.today_timezone_label.setWordWrap(True)
        controls_layout.addWidget(self.today_timezone_label, 0, 1)
        self.today_last_refresh_label = QLabel("Not refreshed yet")
        self.today_last_refresh_label.setObjectName("todayRefreshStatus")
        self.today_last_refresh_label.setAccessibleName("Today refresh status")
        self.today_last_refresh_label.setTextFormat(Qt.TextFormat.PlainText)
        self.today_last_refresh_label.setWordWrap(True)
        controls_layout.addWidget(self.today_last_refresh_label, 0, 2)
        self.today_refresh_button = QPushButton("Refresh")
        self.today_refresh_button.setObjectName("todayRefreshButton")
        self.today_refresh_button.setAccessibleName("Refresh Home")
        self.today_refresh_button.setAccessibleDescription(
            "Request a refresh of available Hub data."
        )
        self.today_refresh_button.setAutoDefault(False)
        self.today_refresh_button.clicked.connect(self._request_refresh)
        controls_layout.addWidget(self.today_refresh_button, 0, 3)
        self.today_appearance_button = QPushButton("Appearance")
        self.today_appearance_button.setAccessibleName("Customize Hub appearance")
        self.today_appearance_button.clicked.connect(lambda: self.show_destination("Settings"))
        controls_layout.addWidget(self.today_appearance_button, 0, 4)
        layout.addWidget(controls)

        self.today_sections_scroll = page
        self.dashboard = DashboardBoard()
        self.today_sections_container = self.dashboard
        self.today_sections_layout = self.dashboard.layout()
        layout.addWidget(self.today_sections_container, 1)
        self.today_section_widgets: dict[str, QFrame] = {}
        self.today_section_item_lists: dict[str, QListWidget] = {}
        self._today_section_controls: dict[str, tuple[QLabel, QLabel, QListWidget]] = {}
        self.dashboard.layout_changed.connect(self._dashboard_changed)
        self.dashboard.open_requested.connect(self.show_destination)
        self._render_today_sections()
        return page

    def _today_section_summary(self, section: TodaySectionView) -> str:
        summary = section.body
        if section.items:
            summary = f"{len(section.items)} item{'s' if len(section.items) != 1 else ''}"
            if self._offline_mode and section.section_id in {
                "schedule",
                "inbox",
                "watchlist",
                "watch_alerts",
                "news",
            }:
                summary += " · Offline · showing saved data"
        return summary

    def _update_today_section_widget(
        self,
        section: TodaySectionView,
        controls: tuple[QLabel, QLabel, QListWidget],
    ) -> None:
        """Update one section body without replacing or hiding its widgets."""

        title, body, items = controls
        if section.section_id == "mini_calendar":
            body.hide()
            items.hide()
            return
        title.setText(section.title)
        title.setAccessibleName(f"{section.title} heading")
        body.setAccessibleName(f"{section.title} summary")
        body.setText(self._today_section_summary(section))
        body.setVisible(bool(body.text()))

        # QListWidget.clear() otherwise exposes a transient empty frame on the
        # native Windows paint path. Keep updates and signals blocked while
        # the bounded rows are replaced, then let Qt paint the final state once.
        previous_updates = items.updatesEnabled()
        previous_signals_blocked = items.signalsBlocked()
        previous_scroll = items.verticalScrollBar().value()
        items.setUpdatesEnabled(False)
        items.blockSignals(True)
        try:
            items.clear()
            for index, item in enumerate(section.items):
                items.addItem(QListWidgetItem(self._render_today_item_text(item, index)))
            if section.items:
                items.setMinimumHeight(min(120, 30 + (len(section.items) * 28)))
                items.setMaximumHeight(16777215)
                items.setVisible(True)
            else:
                items.setMinimumHeight(0)
                items.setMaximumHeight(16777215)
                items.setVisible(False)
            items.verticalScrollBar().setValue(previous_scroll)
        finally:
            items.blockSignals(previous_signals_blocked)
            items.setUpdatesEnabled(previous_updates)

    def _build_today_section_widget(self, section: TodaySectionView) -> QFrame:
        card = QFrame(self.dashboard)
        card.setObjectName("todaySection")
        card.setProperty("todaySectionId", section.section_id)
        card.setAccessibleName(f"Today section: {section.title}")
        card.setFrameShape(QFrame.Shape.StyledPanel)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 13, 16, 13)
        card_layout.setSpacing(5)

        title = QLabel(section.title)
        title.setObjectName("todaySectionTitle")
        title.setAccessibleName(f"{section.title} heading")
        title.setTextFormat(Qt.TextFormat.PlainText)
        card_layout.addWidget(title)
        title.hide()  # The dashboard wrapper supplies the visible, draggable heading.

        body = QLabel(card)
        body.setObjectName("todaySectionBody")
        body.setAccessibleName(f"{section.title} summary")
        body.setTextFormat(Qt.TextFormat.PlainText)
        body.setWordWrap(True)
        card_layout.addWidget(body)

        items = QListWidget(card)
        items.setObjectName("todaySectionItems")
        items.setAccessibleName(f"{section.title} items")
        items.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        items.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        items.setWordWrap(True)
        items.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        card_layout.addWidget(items)
        controls = (title, body, items)
        self._today_section_controls[section.section_id] = controls
        self.today_section_item_lists[section.section_id] = items
        self._update_today_section_widget(section, controls)
        if section.section_id == "mini_calendar":
            self.mini_calendar = MiniCalendar(card)
            self.mini_calendar.date_requested.connect(self._open_mini_calendar_date)
            self.mini_calendar.month_changed.connect(self._browse_mini_calendar_month)
            card_layout.addWidget(self.mini_calendar)
        return card

    def _render_today_item_text(self, item: object, index: int) -> str:
        if isinstance(item, (str, int, float)):
            return _safe_text(item, fallback=f"Item {index + 1}")
        view = _coerce_today_item(item, index)
        rendered = view.title or "Today item"
        if view.is_completed:
            rendered = "Done locally · " + rendered
        details: list[str] = []
        if view.starts_at is not None:
            starts = _coerce_datetime(view.starts_at)
            if starts is not None:
                try:
                    timezone = ZoneInfo(self._today_view.timezone or "UTC")
                    starts = starts.astimezone(timezone)
                except (ZoneInfoNotFoundError, ValueError):
                    pass
                details.append(starts.strftime("%a %H:%M"))
        if view.summary:
            details.append(view.summary)
        if view.source:
            details.append(view.source)
        if view.due_at is not None:
            due = _coerce_datetime(view.due_at)
            if due is not None:
                details.append(due.date().isoformat())
        if details:
            rendered += " — " + " · ".join(details)
        return self._display_text(rendered, sensitive=view.sensitive, fallback="Today item")

    def _render_today_sections(self) -> None:
        old_widgets = self.today_section_widgets
        old_controls = self._today_section_controls
        widgets: dict[str, QFrame] = {}
        item_lists: dict[str, QListWidget] = {}
        controls_by_section: dict[str, tuple[QLabel, QLabel, QListWidget]] = {}
        sections: list[tuple[str, str, QFrame, bool]] = []
        for section_id in self._today_section_order:
            section = self._today_sections[section_id]
            widget = old_widgets.get(section_id)
            controls = old_controls.get(section_id)
            if widget is None or controls is None:
                widget = self._build_today_section_widget(section)
                controls = self._today_section_controls[section_id]
            else:
                self._update_today_section_widget(section, controls)
            widgets[section_id] = widget
            item_lists[section_id] = controls[2]
            controls_by_section[section_id] = controls
            sections.append((section_id, section.title, widget, section.visible))
        self.today_section_widgets = widgets
        self.today_section_item_lists = item_lists
        self._today_section_controls = controls_by_section
        self.dashboard.set_sections(sections)
        self._sync_dashboard_sections(self.dashboard.export_layout())
        self._render_mini_calendar()

    def _render_mini_calendar(self) -> None:
        if not hasattr(self, "mini_calendar"):
            return
        timezone = _calendar_display_timezone(self._calendar_view.timezone)
        today = _coerce_date_value(self._today_view.local_date)
        local_today = (
            datetime.now().date()
            if timezone == "local"
            else datetime.now(ZoneInfo(timezone)).date()
        )
        self.mini_calendar.set_today(today or local_today)
        first = date(self.mini_calendar.grid.yearShown(), self.mini_calendar.grid.monthShown(), 1)
        start = first - timedelta(days=(first.weekday() + 1) % 7)
        events = self._calendar_events_for_selected()
        counts: dict[date, int] = {}
        # Reuse the full calendar's timezone and multi-day clipping semantics.
        for week in range(6):
            schedule = build_hourly_schedule_layout(
                events, start_date=start + timedelta(days=7 * week), day_count=7, timezone=timezone
            )
            for placement in (*schedule.timed, *schedule.all_day):
                counts[placement.day] = counts.get(placement.day, 0) + 1
        freshness = "offline" if self._offline_mode else self._calendar_view.freshness
        self.mini_calendar.set_activity(counts, status=_CALENDAR_FRESHNESS_MESSAGES[freshness])
        self.dashboard.refresh_geometry()

    def _browse_mini_calendar_month(self, year: int, month: int) -> None:
        self._calendar_cursor = date(year, month, 1)
        if hasattr(self, "calendar_view_stack"):
            self.set_calendar_mode("month")
            self._render_calendar_views()
            self.calendar_period_changed.emit(self._calendar_cursor)
        else:
            self._render_mini_calendar()

    def _open_mini_calendar_date(self, value: date) -> None:
        self._calendar_cursor = value
        self.set_calendar_mode("day")
        self._render_calendar_views()
        self.calendar_period_changed.emit(value)
        self.show_destination("Calendar")

    def _dashboard_changed(self, layout: object) -> None:
        if not isinstance(layout, Mapping):
            return
        self._sync_dashboard_sections(layout)
        self.dashboard_layout_changed.emit(dict(layout))

    def _sync_dashboard_sections(self, layout: Mapping[str, object]) -> None:
        order = layout.get("order", [])
        hidden = layout.get("hidden", [])
        if not isinstance(order, list) or not isinstance(hidden, list):
            return
        self._today_section_order = tuple(item for item in order if item in self._today_sections)
        for section_id, section in self._today_sections.items():
            self._today_sections[section_id] = replace(section, visible=section_id not in hidden)
        self._sync_today_view()

    def restore_dashboard_layout(self, layout: Mapping[str, object]) -> None:
        with QSignalBlocker(self.dashboard):
            self.dashboard.restore_layout(layout)
        self._sync_dashboard_sections(self.dashboard.export_layout())

    def _build_calendar_page(self) -> QWidget:
        page, layout = self._new_page("calendarPage", "Calendar page")
        self.calendar_page = page
        self._calendar_layout = layout
        self._add_page_header(
            layout,
            "Calendar",
            "View selected calendars and event details.",
        )

        toolbar = QFrame()
        toolbar.setObjectName("calendarToolbar")
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(0, 0, 0, 0)
        toolbar_layout.setSpacing(8)
        self.calendar_previous_button = QPushButton("Previous")
        self.calendar_previous_button.setObjectName("calendarPreviousButton")
        self.calendar_previous_button.setAccessibleName("Previous calendar period")
        self.calendar_previous_button.setAutoDefault(False)
        self.calendar_previous_button.clicked.connect(self.previous_calendar_period)
        toolbar_layout.addWidget(self.calendar_previous_button)
        self.calendar_today_button = QPushButton("Today")
        self.calendar_today_button.setObjectName("calendarTodayButton")
        self.calendar_today_button.setAccessibleName("Go to today")
        self.calendar_today_button.setAutoDefault(False)
        self.calendar_today_button.clicked.connect(self.go_to_calendar_today)
        toolbar_layout.addWidget(self.calendar_today_button)
        self.calendar_next_button = QPushButton("Next")
        self.calendar_next_button.setObjectName("calendarNextButton")
        self.calendar_next_button.setAccessibleName("Next calendar period")
        self.calendar_next_button.setAutoDefault(False)
        self.calendar_next_button.clicked.connect(self.next_calendar_period)
        toolbar_layout.addWidget(self.calendar_next_button)
        self.calendar_period_label = QLabel()
        self.calendar_period_label.setObjectName("calendarPeriodLabel")
        self.calendar_period_label.setAccessibleName("Calendar period")
        self.calendar_period_label.setTextFormat(Qt.TextFormat.PlainText)
        toolbar_layout.addWidget(self.calendar_period_label, 1)
        self.calendar_mode_combo = QComboBox()
        self.calendar_mode_combo.setObjectName("calendarModeCombo")
        self.calendar_mode_combo.setAccessibleName("Calendar view mode")
        for label, value in (
            ("Month", "month"),
            ("Day", "day"),
            ("Week", "week"),
            ("Agenda", "agenda"),
        ):
            self.calendar_mode_combo.addItem(label, value)
        self.calendar_mode_combo.currentIndexChanged.connect(self._calendar_mode_from_combo)
        toolbar_layout.addWidget(self.calendar_mode_combo)
        self.calendar_timezone_label = QLabel()
        self.calendar_timezone_label.setObjectName("calendarTimezoneLabel")
        self.calendar_timezone_label.setAccessibleName("Calendar timezone")
        self.calendar_timezone_label.setTextFormat(Qt.TextFormat.PlainText)
        toolbar_layout.addWidget(self.calendar_timezone_label)
        self.calendar_settings_button = QToolButton()
        self.calendar_settings_button.setObjectName("calendarSettingsButton")
        self.calendar_settings_button.setText("Calendars")
        self.calendar_settings_button.setAccessibleName(
            "Show calendar sources, feeds, and local imports"
        )
        self.calendar_settings_button.setToolTip(
            "Show or hide selected calendars, iCalendar feeds, and local imports"
        )
        self.calendar_settings_button.setCheckable(True)
        self.calendar_settings_button.setAutoRaise(True)
        self.calendar_settings_button.toggled.connect(self._toggle_calendar_settings)
        toolbar_layout.addWidget(self.calendar_settings_button)
        layout.addWidget(toolbar)
        self.calendar_mode = self.calendar_mode_combo
        self.calendar_previous = self.calendar_previous_button
        self.calendar_next = self.calendar_next_button

        freshness_row = QWidget()
        freshness_layout = QHBoxLayout(freshness_row)
        freshness_layout.setContentsMargins(0, 0, 0, 0)
        freshness_layout.setSpacing(8)
        self.calendar_freshness_label = QLabel()
        self.calendar_freshness_label.setObjectName("calendarFreshnessLabel")
        self.calendar_freshness_label.setAccessibleName("Calendar freshness")
        self.calendar_freshness_label.setTextFormat(Qt.TextFormat.PlainText)
        freshness_layout.addWidget(self.calendar_freshness_label)
        self.calendar_status_label = QLabel()
        self.calendar_status_label.setObjectName("calendarStatusLabel")
        self.calendar_status_label.setAccessibleName("Calendar status details")
        self.calendar_status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.calendar_status_label.setWordWrap(True)
        freshness_layout.addWidget(self.calendar_status_label, 1)
        layout.addWidget(freshness_row)

        self.calendar_sources_panel = QFrame()
        self.calendar_sources_panel.setObjectName("calendarSourcesPanel")
        self.calendar_sources_panel.setAccessibleName("Selected calendars and accounts")
        self.calendar_sources_layout = QHBoxLayout(self.calendar_sources_panel)
        self.calendar_sources_layout.setContentsMargins(0, 0, 0, 0)
        self.calendar_sources_layout.setSpacing(8)
        self.calendar_sources_title = QLabel("Calendars")
        self.calendar_sources_title.setObjectName("calendarSourcesTitle")
        self.calendar_sources_title.setAccessibleName("Calendar sources heading")
        self.calendar_sources_title.setTextFormat(Qt.TextFormat.PlainText)
        self.calendar_sources_layout.addWidget(self.calendar_sources_title)
        self.calendar_sources_layout.addStretch(1)
        self.calendar_source_checkboxes: dict[str, QCheckBox] = {}
        self.calendar_sources = self.calendar_source_checkboxes

        self.calendar_ical_panel = QFrame()
        self.calendar_ical_panel.setObjectName("calendarIcalPanel")
        self.calendar_ical_panel.setAccessibleName("Imported iCalendar feeds")
        ical_panel_layout = QVBoxLayout(self.calendar_ical_panel)
        ical_panel_layout.setContentsMargins(12, 8, 12, 8)
        ical_panel_layout.setSpacing(6)
        ical_header = QHBoxLayout()
        self.calendar_ical_title = QLabel("Imported iCal feeds")
        self.calendar_ical_title.setObjectName("calendarIcalTitle")
        self.calendar_ical_title.setAccessibleName("Imported iCalendar feeds heading")
        self.calendar_ical_title.setTextFormat(Qt.TextFormat.PlainText)
        ical_header.addWidget(self.calendar_ical_title)
        ical_header.addStretch(1)
        self.calendar_ical_import_button = QPushButton("Import iCal feed")
        self.calendar_ical_import_button.setObjectName("calendarIcalImportButton")
        self.calendar_ical_import_button.setAccessibleName("Import iCalendar feed")
        self.calendar_ical_import_button.setAccessibleDescription(
            "Add a read-only HTTP or HTTPS iCalendar feed to Personal Hub."
        )
        self.calendar_ical_import_button.setAutoDefault(False)
        self.calendar_ical_import_button.clicked.connect(
            lambda: self.ical_feed_create_requested.emit()
        )
        ical_header.addWidget(self.calendar_ical_import_button)
        ical_panel_layout.addLayout(ical_header)
        self.calendar_ical_feeds_layout = QVBoxLayout()
        self.calendar_ical_feeds_layout.setContentsMargins(0, 0, 0, 0)
        self.calendar_ical_feeds_layout.setSpacing(4)
        ical_panel_layout.addLayout(self.calendar_ical_feeds_layout)
        self.calendar_ical_feed_checkboxes: dict[str, QCheckBox] = {}
        self.calendar_ical_feed_edit_buttons: dict[str, QPushButton] = {}
        self.calendar_ical_feed_remove_buttons: dict[str, QPushButton] = {}

        self.calendar_local_imports_panel = QFrame()
        self.calendar_local_imports_panel.setObjectName("calendarLocalImportsPanel")
        self.calendar_local_imports_panel.setAccessibleName("Local CSV and text calendar imports")
        local_imports_layout = QVBoxLayout(self.calendar_local_imports_panel)
        local_imports_layout.setContentsMargins(12, 8, 12, 8)
        local_imports_layout.setSpacing(6)
        local_imports_header = QHBoxLayout()
        self.calendar_local_imports_title = QLabel("Local calendar imports")
        self.calendar_local_imports_title.setObjectName("calendarLocalImportsTitle")
        self.calendar_local_imports_title.setAccessibleName("Local calendar imports heading")
        self.calendar_local_imports_title.setTextFormat(Qt.TextFormat.PlainText)
        local_imports_header.addWidget(self.calendar_local_imports_title)
        local_imports_header.addStretch(1)
        self.calendar_local_import_button = QPushButton("Import CSV or list")
        self.calendar_local_import_button.setObjectName("calendarLocalImportButton")
        self.calendar_local_import_button.setAccessibleName("Import local calendar items")
        self.calendar_local_import_button.setAccessibleDescription(
            "Add read-only calendar items from a CSV file or one item per line."
        )
        self.calendar_local_import_button.setAutoDefault(False)
        self.calendar_local_import_button.clicked.connect(
            lambda: self.calendar_import_requested.emit()
        )
        local_imports_header.addWidget(self.calendar_local_import_button)
        local_imports_layout.addLayout(local_imports_header)
        self.calendar_local_imports_layout = QVBoxLayout()
        self.calendar_local_imports_layout.setContentsMargins(0, 0, 0, 0)
        self.calendar_local_imports_layout.setSpacing(4)
        local_imports_layout.addLayout(self.calendar_local_imports_layout)
        self.calendar_local_import_remove_buttons: dict[str, QPushButton] = {}

        self.calendar_settings_panel = QFrame()
        self.calendar_settings_panel.setObjectName("calendarSettingsPanel")
        self.calendar_settings_panel.setAccessibleName("Calendar source settings")
        settings_layout = QVBoxLayout(self.calendar_settings_panel)
        settings_layout.setContentsMargins(10, 8, 10, 8)
        settings_layout.setSpacing(8)
        settings_layout.addWidget(self.calendar_sources_panel)
        settings_layout.addWidget(self.calendar_ical_panel)
        settings_layout.addWidget(self.calendar_local_imports_panel)
        layout.addWidget(self.calendar_settings_panel)
        self.calendar_settings_panel.hide()

        self.calendar_view_stack = QStackedWidget()
        self.calendar_view_stack.setObjectName("calendarViewStack")
        self.calendar_view_stack.setAccessibleName("Calendar views")
        self.calendar_month_view = QWidget()
        self.calendar_month_view.setObjectName("calendarMonthView")
        self.calendar_month_view.setAccessibleName("Calendar month view")
        self.calendar_month_layout = QGridLayout(self.calendar_month_view)
        self.calendar_month_layout.setContentsMargins(0, 0, 0, 0)
        self.calendar_month_layout.setSpacing(4)
        self.calendar_day_view = QWidget()
        self.calendar_day_view.setObjectName("calendarDayView")
        self.calendar_day_view.setAccessibleName("Calendar day view")
        day_layout = QVBoxLayout(self.calendar_day_view)
        day_layout.setContentsMargins(0, 0, 0, 0)
        day_layout.setSpacing(0)
        self.calendar_day_scroll = QScrollArea(self.calendar_day_view)
        self.calendar_day_scroll.setObjectName("calendarDayScroll")
        self.calendar_day_scroll.setAccessibleName("Calendar day time grid")
        self.calendar_day_scroll.setWidgetResizable(True)
        self.calendar_day_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.calendar_day_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.calendar_day_grid = HourlyCalendarGrid(
            self.calendar_day_scroll,
            column_object_name="calendarDayColumn",
        )
        self.calendar_day_grid.slot_requested.connect(self.calendar_slot_requested.emit)
        self.calendar_day_scroll.setWidget(self.calendar_day_grid)
        day_layout.addWidget(self.calendar_day_scroll)

        self.calendar_week_view = QWidget()
        self.calendar_week_view.setObjectName("calendarWeekView")
        self.calendar_week_view.setAccessibleName("Calendar week view")
        self.calendar_week_layout = QVBoxLayout(self.calendar_week_view)
        self.calendar_week_layout.setContentsMargins(0, 0, 0, 0)
        self.calendar_week_layout.setSpacing(0)
        self.calendar_week_scroll = QScrollArea(self.calendar_week_view)
        self.calendar_week_scroll.setObjectName("calendarWeekScroll")
        self.calendar_week_scroll.setAccessibleName("Calendar week time grid")
        self.calendar_week_scroll.setWidgetResizable(True)
        self.calendar_week_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.calendar_week_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.calendar_week_grid_widget = HourlyCalendarGrid(
            self.calendar_week_scroll,
            column_object_name="calendarWeekColumn",
        )
        self.calendar_week_grid_widget.slot_requested.connect(self.calendar_slot_requested.emit)
        self.calendar_week_scroll.setWidget(self.calendar_week_grid_widget)
        self.calendar_week_layout.addWidget(self.calendar_week_scroll)
        self.calendar_agenda_view = QListWidget()
        self.calendar_agenda_view.setObjectName("calendarAgendaView")
        self.calendar_agenda_view.setAccessibleName("Calendar agenda")
        self.calendar_agenda_view.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.calendar_agenda_view.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.calendar_agenda_view.itemClicked.connect(self._open_calendar_list_item)
        self.calendar_agenda_view.itemDoubleClicked.connect(self._open_calendar_list_item)
        self.calendar_agenda_view.itemChanged.connect(self._calendar_agenda_item_changed)
        self.calendar_view_stack.addWidget(self.calendar_month_view)
        self.calendar_view_stack.addWidget(self.calendar_day_view)
        self.calendar_view_stack.addWidget(self.calendar_week_view)
        self.calendar_view_stack.addWidget(self.calendar_agenda_view)
        self.calendar_views = {
            "month": self.calendar_month_view,
            "day": self.calendar_day_view,
            "week": self.calendar_week_view,
            "agenda": self.calendar_agenda_view,
        }
        self.calendar_month_grid = self.calendar_month_view
        self.calendar_week_grid = self.calendar_week_grid_widget
        self.calendar_agenda_list = self.calendar_agenda_view
        self.calendar_month_cells: dict[date, QFrame] = {}
        self.calendar_week_columns: dict[date, QFrame] = {}
        self.calendar_day_columns: dict[date, QFrame] = {}
        self.calendar_time_slots: tuple[QPushButton, ...] = ()
        self.calendar_day_time_slots: tuple[QPushButton, ...] = ()

        self.calendar_event_details_panel = QFrame()
        self.calendar_event_details_panel.setObjectName("calendarEventDetailsPanel")
        self.calendar_event_details_panel.setAccessibleName("Selected calendar event details")
        self.calendar_event_details_panel.setMinimumWidth(280)
        self.calendar_event_details_panel.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Expanding,
        )
        details_layout = QVBoxLayout(self.calendar_event_details_panel)
        details_layout.setContentsMargins(12, 10, 12, 10)
        details_layout.setSpacing(5)
        details_header = QHBoxLayout()
        details_header.setContentsMargins(0, 0, 0, 0)
        details_header.setSpacing(6)
        self.calendar_event_details_color_indicator = QLabel("●")
        self.calendar_event_details_color_indicator.setObjectName(
            "calendarEventDetailsColorIndicator"
        )
        self.calendar_event_details_color_indicator.setAccessibleName(
            "Selected calendar color indicator"
        )
        self.calendar_event_details_color_indicator.setTextFormat(Qt.TextFormat.PlainText)
        self.calendar_event_details_color_indicator.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.calendar_event_details_color_indicator.setFixedWidth(14)
        details_header.addWidget(self.calendar_event_details_color_indicator)
        self.calendar_event_details_title = QLabel()
        self.calendar_event_details_title.setObjectName("calendarEventDetailsTitle")
        self.calendar_event_details_title.setAccessibleName("Selected calendar event title")
        self.calendar_event_details_title.setTextFormat(Qt.TextFormat.PlainText)
        self.calendar_event_details_title.setWordWrap(True)
        details_header.addWidget(self.calendar_event_details_title, 1)
        self.calendar_event_details_close_button = QPushButton("Close")
        self.calendar_event_details_close_button.setObjectName("calendarEventDetailsCloseButton")
        self.calendar_event_details_close_button.setAccessibleName("Close calendar event details")
        self.calendar_event_details_close_button.setAutoDefault(False)
        self.calendar_event_details_close_button.clicked.connect(
            lambda _checked=False: self.clear_calendar_event_details()
        )
        details_header.addWidget(self.calendar_event_details_close_button)
        details_layout.addLayout(details_header)

        self.calendar_event_details_when = QLabel()
        self.calendar_event_details_when.setObjectName("calendarEventDetailsWhen")
        self.calendar_event_details_when.setAccessibleName("Selected calendar event date and time")
        self.calendar_event_details_when.setTextFormat(Qt.TextFormat.PlainText)
        self.calendar_event_details_when.setWordWrap(True)
        details_layout.addWidget(self.calendar_event_details_when)
        self.calendar_event_details_location = QLabel()
        self.calendar_event_details_location.setObjectName("calendarEventDetailsLocation")
        self.calendar_event_details_location.setAccessibleName("Selected calendar event location")
        self.calendar_event_details_location.setTextFormat(Qt.TextFormat.PlainText)
        self.calendar_event_details_location.setWordWrap(True)
        details_layout.addWidget(self.calendar_event_details_location)
        self.calendar_event_details_source = QLabel()
        self.calendar_event_details_source.setObjectName("calendarEventDetailsSource")
        self.calendar_event_details_source.setAccessibleName("Selected calendar event source")
        self.calendar_event_details_source.setTextFormat(Qt.TextFormat.PlainText)
        self.calendar_event_details_source.setWordWrap(True)
        details_layout.addWidget(self.calendar_event_details_source)
        self.calendar_event_details_description = QLabel()
        self.calendar_event_details_description.setObjectName("calendarEventDetailsDescription")
        self.calendar_event_details_description.setAccessibleName(
            "Selected calendar event description"
        )
        self.calendar_event_details_description.setTextFormat(Qt.TextFormat.PlainText)
        self.calendar_event_details_description.setWordWrap(True)
        details_layout.addWidget(self.calendar_event_details_description)
        self.calendar_event_details_status = QLabel()
        self.calendar_event_details_status.setObjectName("calendarEventDetailsStatus")
        self.calendar_event_details_status.setAccessibleName("Selected calendar event local status")
        self.calendar_event_details_status.setTextFormat(Qt.TextFormat.PlainText)
        self.calendar_event_details_status.setWordWrap(True)
        details_layout.addWidget(self.calendar_event_details_status)
        details_actions = QHBoxLayout()
        details_actions.setContentsMargins(0, 2, 0, 0)
        details_actions.addStretch(1)
        self.calendar_event_native_open_button = QPushButton("Open in native calendar")
        self.calendar_event_native_open_button.setObjectName("calendarEventNativeOpenButton")
        self.calendar_event_native_open_button.setAccessibleName(
            "Open selected event in native calendar"
        )
        self.calendar_event_native_open_button.setAccessibleDescription(
            "Open the selected event through its saved HTTP or HTTPS calendar link."
        )
        self.calendar_event_native_open_button.setAutoDefault(False)
        self.calendar_event_native_open_button.clicked.connect(
            lambda _checked=False: self._open_selected_calendar_event_native()
        )
        details_actions.addWidget(self.calendar_event_native_open_button)
        details_layout.addLayout(details_actions)
        self.calendar_event_details_panel.hide()
        self.calendar_event_details = self.calendar_event_details_panel
        self.calendar_event_open_button = self.calendar_event_native_open_button

        self.calendar_content_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.calendar_content_splitter.setObjectName("calendarContentSplitter")
        self.calendar_content_splitter.setAccessibleName("Calendar and selected event")
        self.calendar_content_splitter.setChildrenCollapsible(True)
        self.calendar_content_splitter.addWidget(self.calendar_view_stack)
        self.calendar_content_splitter.addWidget(self.calendar_event_details_panel)
        self.calendar_content_splitter.setStretchFactor(0, 1)
        self.calendar_content_splitter.setStretchFactor(1, 0)
        layout.addWidget(self.calendar_content_splitter, 1)

        self._render_calendar_sources()
        self._render_ical_feeds()
        self._render_calendar_views()
        self.calendar_freshness_label.setText(_CALENDAR_FRESHNESS_MESSAGES["ready"])
        self.calendar_status_label.clear()
        return page

    def _toggle_calendar_settings(self, visible: bool) -> None:
        """Reveal source-management controls only when the user asks for them."""

        if not hasattr(self, "calendar_settings_panel"):
            return
        self.calendar_settings_panel.setVisible(bool(visible))
        self.calendar_settings_button.setText("Hide calendars" if visible else "Calendars")
        self.calendar_settings_button.setAccessibleName(
            "Hide calendar sources and feeds" if visible else "Show calendar sources and feeds"
        )

    @staticmethod
    def _clear_layout(layout: QGridLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _calendar_color_for_key(self, key: object) -> str:
        """Return a stable accent for a calendar within this window."""

        normalized = _safe_text(key, limit=_MAX_ID_LENGTH).casefold()
        if not normalized:
            normalized = "personal-hub-calendar"
        color = self._calendar_color_by_key.get(normalized)
        if color is None:
            color = _CALENDAR_EVENT_COLORS[
                len(self._calendar_color_by_key) % len(_CALENDAR_EVENT_COLORS)
            ]
            self._calendar_color_by_key[normalized] = color
        return color

    def _calendar_color_for_event(self, event: CalendarEventView) -> str:
        """Resolve an event to the accent used by its selected calendar."""

        for source in self._calendar_sources:
            same_calendar = bool(
                event.calendar_id
                and (
                    source.calendar_id == event.calendar_id
                    or source.calendar_id.rsplit("/", 1)[-1] == event.calendar_id
                    or source.calendar_id.rsplit(":", 1)[-1] == event.calendar_id
                )
            )
            same_name = bool(
                event.calendar_name
                and source.display_name
                and event.calendar_name.casefold() == source.display_name.casefold()
            )
            if same_calendar or same_name:
                return self._calendar_color_for_key(source.calendar_id)
        if event.source_id:
            for source in self._calendar_sources:
                if source.source_id == event.source_id:
                    return self._calendar_color_for_key(source.calendar_id)
        return self._calendar_color_for_key(
            event.calendar_id
            or event.source_id
            or event.calendar_name
            or event.source_name
            or "personal-hub-calendar"
        )

    def _calendar_event_chip_label(self, event: CalendarEventView) -> str:
        """Keep the grid label focused on the title; details stay in the tooltip."""

        title = self._calendar_event_title(event)
        return f"✓ {title}" if event.is_task and event.is_completed else title

    def _build_calendar_event_button(
        self,
        event: CalendarEventView,
        *,
        hourly: bool = False,
    ) -> QWidget:
        row = QWidget()
        row.setObjectName("calendarEventRow")
        kind = "task" if event.is_task else "event"
        row.setAccessibleName(f"Calendar {kind} row: {self._calendar_event_title(event)}")
        row.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding if hourly else QSizePolicy.Policy.Fixed,
        )
        row.setMinimumWidth(0)
        row.setMaximumHeight(16777215 if hourly else 28)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(4)
        if event.is_task:
            done = QCheckBox()
            done.setObjectName("calendarTaskDoneCheckBox")
            done.setAccessibleName(f"Mark to-do complete: {self._calendar_event_title(event)}")
            done.setAccessibleDescription("Completes or reopens this Personal Hub to-do list item.")
            done.setToolTip("Mark this to-do list item complete")
            done.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            done.setFixedWidth(18)
            done.setChecked(event.is_completed)
            done.toggled.connect(
                lambda checked, value=event: self._calendar_task_completion_toggled(value, checked)
            )
            row_layout.addWidget(done)

        color = self._calendar_color_for_event(event)
        color_swatch = QFrame()
        color_swatch.setObjectName("calendarEventColorSwatch")
        color_swatch.setProperty("calendarColor", color)
        color_swatch.setAccessibleName(
            f"Calendar color indicator for {self._calendar_event_title(event)}"
        )
        color_swatch.setAccessibleDescription(
            "Visual calendar source indicator; the source name is available in event details."
        )
        color_swatch.setFixedSize(4, 16)
        color_swatch.setStyleSheet(f"background-color: {color}; border-radius: 2px;")
        row_layout.addWidget(color_swatch)

        button = CalendarEventButton(self._calendar_event_chip_label(event))
        button.setObjectName("calendarEventButton")
        button.setProperty("calendarColor", color)
        button.setProperty("calendarCompleted", event.is_task and event.is_completed)
        button.setAccessibleName(f"Calendar {kind}: {self._calendar_event_title(event)}")
        button.setAccessibleDescription(
            "Compact due-task chip with a completion control."
            if event.is_task
            else "Compact read-only calendar event chip with source details."
        )
        button.setToolTip(self._calendar_event_label(event, include_date=True))
        button.setAutoDefault(False)
        button.setMinimumHeight(24)
        button.setMaximumHeight(16777215 if hourly else 28)
        if hourly:
            button.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        button.clicked.connect(
            lambda _checked=False, value=event.event_id: self.open_calendar_event(value)
        )
        row_layout.addWidget(button, 1)
        return row

    def _calendar_task_completion_toggled(
        self,
        event: CalendarEventView,
        completed: bool,
    ) -> None:
        if event.is_task:
            self.calendar_task_completion_changed.emit(
                {"task_id": event.task_id, "completed": bool(completed)}
            )

    def _calendar_event_title(self, event: CalendarEventView) -> str:
        return self._display_text(event.title, sensitive=event.sensitive, fallback="Calendar event")

    def _calendar_event_location(self, event: CalendarEventView) -> str:
        return self._display_text(event.location, sensitive=event.sensitive)

    def _calendar_event_when_label(self, event: CalendarEventView) -> str:
        """Build a bounded plain-text date/time line for the detail card."""

        start = _coerce_datetime(event.starts_at)
        end = _coerce_datetime(event.ends_at)
        if start is None:
            return "When: Time not set"
        if event.all_day:
            label = f"When: {start.date().isoformat()} · All day"
        else:
            label = f"When: {start.date().isoformat()} · {start.strftime('%H:%M')}"
            if end is not None:
                label += f"–{end.strftime('%H:%M')}"
        if event.timezone and event.timezone != "local":
            label += f" · {event.timezone}"
        return label

    def _calendar_event_source_label(self, event: CalendarEventView) -> str:
        """Describe the source without exposing provider identifiers."""

        if event.sensitive and (self._privacy_masked or self._locked):
            return "Calendar: Hidden by privacy settings"
        parts: list[str] = []
        for value in (
            self._display_text(event.calendar_name, sensitive=event.sensitive),
            self._display_text(event.source_name, sensitive=event.sensitive),
            self._display_text(event.account, sensitive=event.sensitive),
        ):
            if value and value not in parts:
                parts.append(value)
        return f"Calendar: {' · '.join(parts) if parts else 'Source not provided'}"

    def _calendar_event_native_target(self, event: CalendarEventView) -> str:
        """Return only a safe HTTP(S) target eligible for external navigation."""

        if event.sensitive and (self._privacy_masked or self._locked):
            return ""
        return _safe_http_target(event.navigation_target)

    def _render_calendar_event_details(self, event: CalendarEventView) -> None:
        """Show one selected event while keeping the grid chip compact."""

        self._calendar_selected_event_id = event.event_id
        color = self._calendar_color_for_event(event)
        title = self._calendar_event_title(event)
        location = self._calendar_event_location(event)
        description = self._display_text(event.description, sensitive=event.sensitive)
        self.calendar_event_details_panel.setProperty("calendarColor", color)
        self.calendar_event_details_color_indicator.setProperty("calendarColor", color)
        self.calendar_event_details_color_indicator.setStyleSheet(
            f"color: {color}; font-size: 10pt;"
        )
        self.calendar_event_details_title.setText(title)
        self.calendar_event_details_when.setText(self._calendar_event_when_label(event))
        self.calendar_event_details_location.setText(
            f"Location: {location or 'No location provided'}"
        )
        self.calendar_event_details_source.setText(self._calendar_event_source_label(event))
        self.calendar_event_details_description.setText(
            f"Description: {description or 'No description provided'}"
        )
        if event.is_task:
            status = "Status: Completed to-do" if event.is_completed else "Status: Open to-do"
        else:
            status = "Status: Read-only calendar event"
        self.calendar_event_details_status.setText(status)
        target = self._calendar_event_native_target(event)
        native_label = self._calendar_event_native_open_label(event)
        self.calendar_event_native_open_button.setText(native_label)
        self.calendar_event_native_open_button.setAccessibleName(f"{native_label}: {title}")
        self.calendar_event_native_open_button.setEnabled(bool(target))
        self.calendar_event_native_open_button.setVisible(bool(target))
        self.calendar_event_native_open_button.setProperty("navigationAvailable", bool(target))
        self.calendar_event_native_open_button.setToolTip(
            "Open this event in the source calendar."
            if target
            else "This event does not have a safe HTTP(S) link available."
        )
        self.calendar_event_details_panel.setVisible(True)

    @staticmethod
    def _calendar_event_native_open_label(event: CalendarEventView) -> str:
        if event.native_platform == "canvas":
            return "Open in Canvas"
        return "Open in native calendar"

    def _sync_calendar_event_details(self) -> None:
        """Refresh or dismiss the detail card after calendar data changes."""

        selected_id = self._calendar_selected_event_id
        if not selected_id:
            self.calendar_event_details_panel.hide()
            return
        event = next(
            (candidate for candidate in self._calendar_events if candidate.event_id == selected_id),
            None,
        )
        if event is None or event not in self._calendar_events_for_selected():
            self.clear_calendar_event_details()
            return
        self._render_calendar_event_details(event)

    def clear_calendar_event_details(self) -> None:
        """Close the selected-event detail card without changing calendar data."""

        self._calendar_selected_event_id = None
        if hasattr(self, "calendar_event_details_panel"):
            self.calendar_event_details_panel.hide()

    @property
    def selected_calendar_event(self) -> CalendarEventView | None:
        selected_id = self._calendar_selected_event_id
        if not selected_id:
            return None
        return next(
            (event for event in self._calendar_events if event.event_id == selected_id),
            None,
        )

    def _open_selected_calendar_event_native(self) -> CalendarEventView | None:
        """Request external navigation for the event currently shown in the card."""

        event = self.selected_calendar_event
        if event is None or not self._calendar_event_native_target(event):
            return None
        self.calendar_deep_link_requested.emit(event)
        return event

    def _calendar_event_label(self, event: CalendarEventView, *, include_date: bool) -> str:
        start = _coerce_datetime(event.starts_at)
        end = _coerce_datetime(event.ends_at)
        if event.all_day:
            time_label = "All day"
        elif start is None:
            time_label = "Time not set"
        else:
            time_label = start.strftime("%H:%M")
            if end is not None:
                time_label += f"–{end.strftime('%H:%M')}"
        parts: list[str] = []
        if event.is_task and event.is_completed:
            parts.append("Completed")
        if include_date and start is not None:
            parts.append(start.date().isoformat())
        parts.append(time_label)
        if event.timezone and event.timezone != "local":
            parts.append(event.timezone)
        parts.append(self._calendar_event_title(event))
        location = self._calendar_event_location(event)
        if location:
            parts.append(location)
        source = event.calendar_name or event.source_name
        if source:
            parts.append(source)
        return " · ".join(parts)

    def _calendar_events_for_selected(self) -> tuple[CalendarEventView, ...]:
        if not getattr(self, "_calendar_selection_explicit", False):
            return self._calendar_events
        if not self._calendar_selected_ids:
            return ()
        selected = self._calendar_selected_ids
        return tuple(
            event
            for event in self._calendar_events
            if not event.calendar_id
            or event.calendar_id in selected
            or (event.source_id and event.source_id in selected)
            or (event.source_id and f"{event.source_id}/*" in selected)
        )

    def _events_for_calendar_date(self, value: date) -> tuple[CalendarEventView, ...]:
        timezone = _calendar_display_timezone(self._calendar_view.timezone)
        return tuple(
            event
            for event in self._calendar_events_for_selected()
            if event_local_date(event, timezone=timezone) == value
        )

    def _render_calendar_month(self) -> None:
        self._clear_layout(self.calendar_month_layout)
        self.calendar_month_cells.clear()
        weekdays = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")
        for column, name in enumerate(weekdays):
            label = QLabel(name)
            label.setObjectName("calendarWeekdayLabel")
            label.setAccessibleName(f"{name} weekday")
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setMinimumWidth(0)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            self.calendar_month_layout.addWidget(label, 0, column)
            self.calendar_month_layout.setColumnMinimumWidth(column, 0)
            self.calendar_month_layout.setColumnStretch(column, 1)
        first = self._calendar_cursor.replace(day=1)
        grid_start = first - timedelta(days=(first.weekday() + 1) % 7)
        for week in range(6):
            self.calendar_month_layout.setRowStretch(week + 1, 1)
            for column in range(7):
                current = grid_start + timedelta(days=week * 7 + column)
                cell = QFrame()
                cell.setObjectName("calendarMonthCell")
                cell.setProperty("calendarDate", current.isoformat())
                cell.setAccessibleName(f"Calendar day {current.isoformat()}")
                cell.setMinimumWidth(0)
                cell.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
                cell_layout = QVBoxLayout(cell)
                cell_layout.setContentsMargins(5, 4, 5, 4)
                cell_layout.setSpacing(2)
                day_label = QLabel(str(current.day))
                day_label.setObjectName("calendarDayLabel")
                day_label.setAccessibleName(f"Day {current.isoformat()}")
                day_label.setTextFormat(Qt.TextFormat.PlainText)
                day_label.setAlignment(Qt.AlignmentFlag.AlignRight)
                cell_layout.addWidget(day_label)
                events = self._events_for_calendar_date(current)
                for event in events[:5]:
                    cell_layout.addWidget(self._build_calendar_event_button(event))
                if len(events) > 5:
                    more = QLabel(f"+ {len(events) - 5} more")
                    more.setObjectName("calendarMoreLabel")
                    more.setAccessibleName(f"{len(events) - 5} more events")
                    more.setTextFormat(Qt.TextFormat.PlainText)
                    cell_layout.addWidget(more)
                cell_layout.addStretch(1)
                self.calendar_month_layout.addWidget(cell, week + 1, column)
                self.calendar_month_cells[current] = cell

    def _render_calendar_hourly(
        self,
        grid: HourlyCalendarGrid,
        *,
        start_date: date,
        day_count: int,
    ) -> None:
        """Render timed and all-day items into the shared hourly grid."""

        timezone = _calendar_display_timezone(self._calendar_view.timezone)
        events = self._calendar_events_for_selected()
        schedule = build_hourly_schedule_layout(
            events,
            start_date=start_date,
            day_count=day_count,
            timezone=timezone,
            max_events=1_000,
        )
        by_id = {event.event_id: event for event in events}
        timed_widgets: dict[tuple[str, int], QWidget] = {}
        for placement in schedule.timed:
            event = by_id.get(placement.event_id)
            if event is None:
                continue
            widget = self._build_calendar_event_button(event, hourly=True)
            widget.setProperty("calendarPlacementKey", (placement.event_id, placement.day_index))
            timed_widgets[(placement.event_id, placement.day_index)] = widget
        all_day_widgets: dict[tuple[str, int], QWidget] = {}
        for placement in schedule.all_day:
            event = by_id.get(placement.event_id)
            if event is None:
                continue
            widget = self._build_calendar_event_button(event, hourly=True)
            widget.setProperty("calendarPlacementKey", (placement.event_id, placement.day_index))
            all_day_widgets[(placement.event_id, placement.day_index)] = widget
        grid.render(
            schedule,
            timed_widgets=timed_widgets,
            all_day_widgets=all_day_widgets,
        )

    def _render_calendar_day(self) -> None:
        self._render_calendar_hourly(
            self.calendar_day_grid,
            start_date=self._calendar_cursor,
            day_count=1,
        )
        self.calendar_day_columns = self.calendar_day_grid.day_columns
        self.calendar_day_time_slots = self.calendar_day_grid.slot_buttons

    def _render_calendar_week(self) -> None:
        week_start = self._calendar_cursor - timedelta(
            days=(self._calendar_cursor.weekday() + 1) % 7
        )
        self._render_calendar_hourly(
            self.calendar_week_grid_widget,
            start_date=week_start,
            day_count=7,
        )
        self.calendar_week_columns = self.calendar_week_grid_widget.day_columns
        self.calendar_time_slots = self.calendar_week_grid_widget.slot_buttons

    def _render_calendar_agenda(self) -> None:
        self._calendar_agenda_rendering = True
        try:
            self.calendar_agenda_view.clear()
            events = sorted(
                self._calendar_events_for_selected(),
                key=lambda event: (
                    _coerce_datetime(event.starts_at) is None,
                    (
                        _coerce_datetime(event.starts_at).timestamp()
                        if _coerce_datetime(event.starts_at) is not None
                        else float("inf")
                    ),
                    event.title.casefold(),
                ),
            )
            for event in events:
                color = self._calendar_color_for_event(event)
                item = QListWidgetItem(f"●  {self._calendar_event_label(event, include_date=True)}")
                item.setData(Qt.ItemDataRole.UserRole, event.event_id)
                item.setData(Qt.ItemDataRole.UserRole + 1, color)
                item.setToolTip(self._calendar_event_label(event, include_date=True))
                if not self._high_contrast:
                    item.setForeground(QColor(color))
                flags = item.flags() | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
                if event.is_task:
                    flags |= Qt.ItemFlag.ItemIsUserCheckable
                else:
                    flags &= ~Qt.ItemFlag.ItemIsUserCheckable
                item.setFlags(flags)
                if event.is_task:
                    item.setCheckState(
                        Qt.CheckState.Checked if event.is_completed else Qt.CheckState.Unchecked
                    )
                self.calendar_agenda_view.addItem(item)
            if not events:
                empty = QListWidgetItem("No events in this view.")
                empty.setData(Qt.ItemDataRole.UserRole, "")
                self.calendar_agenda_view.addItem(empty)
        finally:
            self._calendar_agenda_rendering = False

    def _calendar_agenda_item_changed(self, item: QListWidgetItem) -> None:
        if self._calendar_agenda_rendering:
            return
        identifier = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(identifier, str) or not identifier:
            return
        event = next(
            (candidate for candidate in self._calendar_events if candidate.event_id == identifier),
            None,
        )
        if event is None or not event.is_task:
            return
        self.calendar_task_completion_changed.emit(
            {
                "task_id": event.task_id,
                "completed": item.checkState() == Qt.CheckState.Checked,
            }
        )

    def _render_calendar_views(self) -> None:
        self._render_calendar_month()
        self._render_calendar_day()
        self._render_calendar_week()
        self._render_calendar_agenda()
        self._update_calendar_period_label()
        self.calendar_view_stack.setCurrentWidget(self.calendar_views[self._calendar_mode_value])
        self._sync_calendar_event_details()
        self._render_mini_calendar()

    def _update_calendar_period_label(self) -> None:
        if self._calendar_mode_value == "month":
            label = self._calendar_cursor.strftime("%B %Y")
        elif self._calendar_mode_value == "day":
            label = (
                f"{self._calendar_cursor.strftime('%A')}, "
                f"{self._calendar_cursor.strftime('%B')} {self._calendar_cursor.day}, "
                f"{self._calendar_cursor.year}"
            )
        elif self._calendar_mode_value == "week":
            start = self._calendar_cursor - timedelta(
                days=(self._calendar_cursor.weekday() + 1) % 7
            )
            end = start + timedelta(days=6)
            if start.year == end.year and start.month == end.month:
                label = f"{start.strftime('%B')} {start.day}–{end.day}, {end.year}"
            else:
                label = f"{start.strftime('%b')} {start.day} – {end.strftime('%b')} {end.day}, {end.year}"
        else:
            label = f"Agenda · {self._calendar_cursor.strftime('%B %Y')}"
        self.calendar_period_label.setText(label)

    def _calendar_mode_from_combo(self, _index: int) -> None:
        self.set_calendar_mode(str(self.calendar_mode_combo.currentData() or "month"))

    def set_calendar_mode(self, mode: str) -> bool:
        normalized = _safe_text(mode, fallback="month", limit=32).casefold()
        if normalized not in self.calendar_views:
            return False
        self._calendar_mode_value = normalized
        index = self.calendar_mode_combo.findData(normalized)
        if index >= 0 and self.calendar_mode_combo.currentIndex() != index:
            self.calendar_mode_combo.blockSignals(True)
            self.calendar_mode_combo.setCurrentIndex(index)
            self.calendar_mode_combo.blockSignals(False)
        self.calendar_view_stack.setCurrentWidget(self.calendar_views[normalized])
        self._update_calendar_period_label()
        self.calendar_mode_changed.emit(normalized)
        return True

    def show_calendar_month(self) -> bool:
        return self.set_calendar_mode("month")

    def show_calendar_day(self) -> bool:
        return self.set_calendar_mode("day")

    def show_calendar_week(self) -> bool:
        return self.set_calendar_mode("week")

    def show_calendar_agenda(self) -> bool:
        return self.set_calendar_mode("agenda")

    @property
    def calendar_mode_value(self) -> str:
        return self._calendar_mode_value

    @property
    def current_calendar_date(self) -> date:
        return self._calendar_cursor

    @property
    def calendar_date(self) -> date:
        return self._calendar_cursor

    def _shift_calendar_period(self, amount: int) -> date:
        if self._calendar_mode_value == "month":
            month = self._calendar_cursor.month - 1 + amount
            year = self._calendar_cursor.year + month // 12
            month = month % 12 + 1
            day = min(self._calendar_cursor.day, monthrange(year, month)[1])
            self._calendar_cursor = date(year, month, day)
        else:
            self._calendar_cursor += timedelta(
                days=amount * (7 if self._calendar_mode_value == "week" else 1)
            )
        self._render_calendar_views()
        self.calendar_period_changed.emit(self._calendar_cursor)
        return self._calendar_cursor

    def previous_calendar_period(self) -> date:
        return self._shift_calendar_period(-1)

    def next_calendar_period(self) -> date:
        return self._shift_calendar_period(1)

    def go_to_calendar_today(self) -> date:
        self._calendar_cursor = date.today()
        self._render_calendar_views()
        self.calendar_period_changed.emit(self._calendar_cursor)
        return self._calendar_cursor

    calendar_previous_period = previous_calendar_period
    calendar_next_period = next_calendar_period
    calendar_today = go_to_calendar_today

    @property
    def calendar_view(self) -> CalendarView:
        return self._calendar_view

    @property
    def calendar_events(self) -> tuple[CalendarEventView, ...]:
        return self._calendar_events

    @property
    def selected_calendar_ids(self) -> tuple[str, ...]:
        return tuple(
            source.calendar_id
            for source in self._calendar_sources
            if source.calendar_id in self._calendar_selected_ids
            or _calendar_source_wildcard(source.calendar_id) in self._calendar_selected_ids
            or _calendar_source_id(source.calendar_id) in self._calendar_selected_ids
        )

    def _render_calendar_sources(self) -> None:
        while self.calendar_sources_layout.count() > 1:
            item = self.calendar_sources_layout.takeAt(1)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.calendar_source_checkboxes.clear()
        if not self._calendar_sources:
            empty = QLabel("No calendars selected.")
            empty.setObjectName("calendarSourcesEmpty")
            empty.setAccessibleName("No calendar sources")
            empty.setTextFormat(Qt.TextFormat.PlainText)
            self.calendar_sources_layout.insertWidget(1, empty)
            self.calendar_sources_layout.addStretch(1)
            return
        for source in self._calendar_sources:
            color = self._calendar_color_for_key(source.calendar_id)
            name = self._display_text(
                source.display_name, sensitive=source.sensitive, fallback="Calendar"
            )
            indicators = [name]
            if source.source_name:
                indicators.append(
                    self._display_text(source.source_name, sensitive=source.sensitive)
                )
            if source.account:
                indicators.append(self._display_text(source.account, sensitive=source.sensitive))
            if source.status and source.status != "ready":
                indicators.append(source.status)
            checkbox = QCheckBox(" · ".join(part for part in indicators if part))
            checkbox.setObjectName("calendarSourceCheckBox")
            checkbox.setAccessibleName(f"Select calendar {name}")
            checkbox.setAccessibleDescription(
                "Include this calendar in month, day, week, and agenda views."
            )
            checkbox.setChecked(source.calendar_id in self._calendar_selected_ids)
            checkbox.toggled.connect(
                lambda enabled, calendar_id=source.calendar_id: self._calendar_source_toggled(
                    calendar_id, enabled
                )
            )
            color_indicator = QLabel("●")
            color_indicator.setObjectName("calendarSourceColorIndicator")
            color_indicator.setProperty("calendarColor", color)
            color_indicator.setAccessibleName(f"Color indicator for calendar {name}")
            color_indicator.setAccessibleDescription(
                "Visual source indicator; the calendar name and account are shown beside it."
            )
            color_indicator.setTextFormat(Qt.TextFormat.PlainText)
            color_indicator.setAlignment(Qt.AlignmentFlag.AlignCenter)
            color_indicator.setFixedWidth(14)
            color_indicator.setStyleSheet(f"color: {color}; font-size: 10pt;")
            self.calendar_sources_layout.addWidget(color_indicator)
            self.calendar_sources_layout.addWidget(checkbox)
            self.calendar_source_checkboxes[source.calendar_id] = checkbox
        self.calendar_sources_layout.addStretch(1)

    def _render_ical_feeds(self) -> None:
        while self.calendar_ical_feeds_layout.count():
            item = self.calendar_ical_feeds_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.calendar_ical_feed_checkboxes.clear()
        self.calendar_ical_feed_edit_buttons.clear()
        self.calendar_ical_feed_remove_buttons.clear()
        if not self._calendar_ical_feeds:
            empty = QLabel("No direct iCal feeds imported.")
            empty.setObjectName("calendarIcalFeedsEmpty")
            empty.setAccessibleName("No imported iCalendar feeds")
            empty.setTextFormat(Qt.TextFormat.PlainText)
            self.calendar_ical_feeds_layout.addWidget(empty)
            return
        for feed in self._calendar_ical_feeds:
            row = QFrame()
            row.setObjectName("calendarIcalFeedRow")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(6)
            checkbox = QCheckBox(
                self._display_text(feed.title, sensitive=True, fallback="iCal feed")
            )
            checkbox.setObjectName("calendarIcalFeedEnabledCheckBox")
            checkbox.setAccessibleName(f"Enable iCalendar feed {feed.title}")
            checkbox.setChecked(feed.enabled)
            checkbox.toggled.connect(
                lambda enabled, feed_id=feed.feed_id: self.ical_feed_enabled_changed.emit(
                    {"feed_id": feed_id, "enabled": enabled}
                )
            )
            row_layout.addWidget(checkbox)
            url_label = QLabel(self._display_text(feed.feed_url, sensitive=True))
            url_label.setObjectName("calendarIcalFeedUrl")
            url_label.setAccessibleName(f"iCalendar feed URL for {feed.title}")
            url_label.setTextFormat(Qt.TextFormat.PlainText)
            url_label.setToolTip("The URL is used only for read-only calendar refreshes.")
            row_layout.addWidget(url_label, 1)
            status_parts = [feed.status]
            if feed.detail:
                status_parts.append(feed.detail)
            status = QLabel(" · ".join(status_parts))
            status.setObjectName("calendarIcalFeedStatus")
            status.setAccessibleName(f"iCalendar feed status for {feed.title}")
            status.setTextFormat(Qt.TextFormat.PlainText)
            row_layout.addWidget(status)
            edit_button = QPushButton("Edit")
            edit_button.setObjectName("calendarIcalFeedEditButton")
            edit_button.setAccessibleName(f"Edit iCalendar feed {feed.title}")
            edit_button.setAutoDefault(False)
            edit_button.clicked.connect(
                lambda _checked=False, feed_id=feed.feed_id: self.ical_feed_update_requested.emit(
                    feed_id
                )
            )
            row_layout.addWidget(edit_button)
            remove_button = QPushButton("Remove")
            remove_button.setObjectName("calendarIcalFeedRemoveButton")
            remove_button.setAccessibleName(f"Remove iCalendar feed {feed.title}")
            remove_button.setAutoDefault(False)
            remove_button.clicked.connect(
                lambda _checked=False, feed_id=feed.feed_id: self.ical_feed_delete_requested.emit(
                    feed_id
                )
            )
            row_layout.addWidget(remove_button)
            self.calendar_ical_feeds_layout.addWidget(row)
            self.calendar_ical_feed_checkboxes[feed.feed_id] = checkbox
            self.calendar_ical_feed_edit_buttons[feed.feed_id] = edit_button
            self.calendar_ical_feed_remove_buttons[feed.feed_id] = remove_button

    def _render_calendar_imports(self) -> None:
        while self.calendar_local_imports_layout.count():
            item = self.calendar_local_imports_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.calendar_local_import_remove_buttons.clear()
        if not self._calendar_local_imports:
            empty = QLabel("No local CSV or text calendar imports.")
            empty.setObjectName("calendarLocalImportsEmpty")
            empty.setAccessibleName("No local calendar imports")
            empty.setTextFormat(Qt.TextFormat.PlainText)
            self.calendar_local_imports_layout.addWidget(empty)
            return
        for imported in self._calendar_local_imports:
            row = QFrame()
            row.setObjectName("calendarLocalImportRow")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(6)
            title = self._display_text(
                imported.name,
                sensitive=True,
                fallback="Local calendar import",
            )
            label = QLabel(
                f"{title} · {imported.event_count} item(s) · {imported.source_format.upper()}"
            )
            label.setObjectName("calendarLocalImportLabel")
            label.setAccessibleName(f"Local calendar import {title}")
            label.setTextFormat(Qt.TextFormat.PlainText)
            row_layout.addWidget(label, 1)
            remove_button = QPushButton("Remove")
            remove_button.setObjectName("calendarLocalImportRemoveButton")
            remove_button.setAccessibleName(f"Remove local calendar import {title}")
            remove_button.setAccessibleDescription(
                "Remove this local import and its calendar items from Personal Hub."
            )
            remove_button.setAutoDefault(False)
            remove_button.clicked.connect(
                lambda _checked=False, import_id=imported.import_id: (
                    self.calendar_import_delete_requested.emit(import_id)
                )
            )
            row_layout.addWidget(remove_button)
            self.calendar_local_imports_layout.addWidget(row)
            self.calendar_local_import_remove_buttons[imported.import_id] = remove_button

    def _calendar_source_toggled(self, calendar_id: str, selected: bool) -> None:
        if selected:
            self._calendar_selected_ids.add(calendar_id)
        else:
            self._calendar_selected_ids.discard(calendar_id)
        self._calendar_selection_explicit = True
        self._render_calendar_views()
        self.calendar_selection_changed.emit(self.selected_calendar_ids)

    def set_calendar_selection(self, selected: Iterable[str]) -> tuple[str, ...]:
        requested = set(_coerce_text_tuple(selected, limit=_MAX_ID_LENGTH))
        known = {source.calendar_id for source in self._calendar_sources}
        self._calendar_selected_ids = {
            calendar_id
            for calendar_id in known
            if calendar_id in requested
            or _calendar_source_wildcard(calendar_id) in requested
            or _calendar_source_id(calendar_id) in requested
        }
        self._calendar_selection_explicit = True
        for calendar_id, checkbox in self.calendar_source_checkboxes.items():
            checkbox.blockSignals(True)
            checkbox.setChecked(calendar_id in self._calendar_selected_ids)
            checkbox.blockSignals(False)
        self._render_calendar_views()
        self.calendar_selection_changed.emit(self.selected_calendar_ids)
        return self.selected_calendar_ids

    select_calendars = set_calendar_selection

    def _open_calendar_list_item(self, item: QListWidgetItem) -> None:
        self.open_calendar_event(item.data(Qt.ItemDataRole.UserRole))

    def open_calendar_event(self, event_id: object) -> CalendarEventView | None:
        if isinstance(event_id, CalendarEventView):
            identifier = event_id.event_id
        else:
            identifier = _safe_text(
                _field(event_id, "event_id", _field(event_id, "id", event_id)),
                limit=_MAX_ID_LENGTH,
            )
        if not identifier:
            return None
        event = next((item for item in self._calendar_events if item.event_id == identifier), None)
        if event is None:
            return None
        self._render_calendar_event_details(event)
        self.calendar_event_open_requested.emit(event)
        return event

    def open_calendar_deep_link(self, event_id: object) -> CalendarEventView | None:
        """Compatibility helper that selects an event and requests its safe link."""

        event = self.open_calendar_event(event_id)
        if event is not None:
            self._open_selected_calendar_event_native()
        return event

    show_calendar_event = open_calendar_event

    def render_calendar(
        self,
        view: CalendarView | Mapping[str, object] | Iterable[object] | None,
    ) -> None:
        """Render bounded read-only calendar data without provider or network work."""

        if view is None:
            calendar_view = CalendarView()
        elif isinstance(view, (CalendarView, Mapping)):
            calendar_view = _coerce_calendar_view(view)
        else:
            calendar_view = _coerce_calendar_view({"events": view})
        self._calendar_view = calendar_view
        self._calendar_events = calendar_view.events
        self._calendar_sources = calendar_view.calendars
        self._calendar_ical_feeds = calendar_view.ical_feeds
        self._calendar_local_imports = calendar_view.local_imports
        self._calendar_selected_ids = set(calendar_view.selected_calendar_ids)
        self._calendar_selection_explicit = bool(self._calendar_sources)
        if calendar_view.cursor_date is not None:
            cursor = _coerce_date_value(calendar_view.cursor_date)
            if cursor is not None:
                self._calendar_cursor = cursor
        self.calendar_timezone_label.setText(
            f"· {calendar_view.timezone}" if calendar_view.timezone else ""
        )
        self._render_calendar_sources()
        self._render_ical_feeds()
        self._render_calendar_imports()
        self._render_calendar_views()
        freshness_text = _CALENDAR_FRESHNESS_MESSAGES[calendar_view.freshness]
        self.calendar_freshness_label.setText(freshness_text)
        detail = calendar_view.detail
        if calendar_view.generated_at is not None:
            generated = _coerce_datetime(calendar_view.generated_at)
            if generated is not None:
                detail = (
                    f"{detail} · Updated {generated.isoformat(timespec='minutes')}"
                    if detail
                    else f"Updated {generated.isoformat(timespec='minutes')}"
                )
        self.calendar_status_label.setText(detail)
        self.calendar_changed.emit(calendar_view)

    update_calendar = render_calendar
    set_calendar_view = render_calendar

    @property
    def calendar_freshness(self) -> str:
        return self._calendar_view.freshness

    def _render_calendar_from_state(self) -> None:
        if hasattr(self, "calendar_view_stack"):
            self._render_calendar_sources()
            self._render_ical_feeds()
            self._render_calendar_imports()
            self._render_calendar_views()
            self.calendar_freshness_label.setText(
                _CALENDAR_FRESHNESS_MESSAGES[self._calendar_view.freshness]
            )

    def _build_tasks_page(self) -> QWidget:
        page, layout = self._new_page("tasksPage", "Tasks page")
        self.tasks_page = page
        self._tasks_layout = layout
        self._add_page_header(
            layout,
            "Tasks",
            "Add and manage local tasks.",
        )

        quick_add = QFrame()
        quick_add.setObjectName("taskQuickAdd")
        quick_layout = QHBoxLayout(quick_add)
        quick_layout.setContentsMargins(12, 10, 12, 10)
        quick_layout.setSpacing(8)
        self.task_quick_add_input = QLineEdit()
        self.task_quick_add_input.setObjectName("taskQuickAddInput")
        self.task_quick_add_input.setAccessibleName("Quick add task")
        self.task_quick_add_input.setAccessibleDescription(
            "Type a task title and press Enter to add it."
        )
        self.task_quick_add_input.setPlaceholderText("Quick add a task")
        self.task_quick_add_input.returnPressed.connect(self._quick_add_task)
        quick_layout.addWidget(self.task_quick_add_input, 1)
        self.task_quick_add_button = QPushButton("Add task")
        self.task_quick_add_button.setObjectName("taskQuickAddButton")
        self.task_quick_add_button.setAccessibleName("Add task")
        self.task_quick_add_button.setAutoDefault(False)
        self.task_quick_add_button.clicked.connect(self._quick_add_task)
        quick_layout.addWidget(self.task_quick_add_button)
        self.task_import_button = QPushButton("Import")
        self.task_import_button.setObjectName("taskImportButton")
        self.task_import_button.setAccessibleName("Import tasks")
        self.task_import_button.setAccessibleDescription(
            "Import tasks from a CSV file or one task title per line."
        )
        self.task_import_button.setAutoDefault(False)
        self.task_import_button.clicked.connect(lambda: self.task_import_requested.emit())
        quick_layout.addWidget(self.task_import_button)
        layout.addWidget(quick_add)
        # Short aliases make the quick-add surface easy to discover in tests
        # and by composition code without adding another abstraction layer.
        self.task_quick_add = self.task_quick_add_input
        self.quick_add_input = self.task_quick_add_input
        self.quick_add_button = self.task_quick_add_button

        task_view_bar = QWidget()
        task_view_bar.setObjectName("taskViewBar")
        task_view_layout = QHBoxLayout(task_view_bar)
        task_view_layout.setContentsMargins(0, 0, 0, 0)
        task_view_layout.setSpacing(8)

        self.task_quick_view_button = QToolButton()
        self.task_quick_view_button.setObjectName("taskQuickViewButton")
        self.task_quick_view_button.setAccessibleName("Task quick view")
        self.task_quick_view_button.setToolTip("Choose a task view")
        self.task_quick_view_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.task_quick_view_menu = QMenu(self.task_quick_view_button)
        self.task_quick_view_actions: dict[str, QAction] = {}
        for label, value in (
            ("All tasks", "all"),
            ("Due today", "today"),
            ("Upcoming", "upcoming"),
            ("Overdue", "overdue"),
            ("No date", "unscheduled"),
        ):
            action = self.task_quick_view_menu.addAction(label)
            action.setCheckable(True)
            action.setData(value)
            action.triggered.connect(
                lambda _checked=False, selected=value: self.set_task_quick_view(selected)
            )
            self.task_quick_view_actions[value] = action
        self.task_quick_view_button.setMenu(self.task_quick_view_menu)
        task_view_layout.addWidget(self.task_quick_view_button)

        self.task_state_filter = QComboBox()
        self.task_state_filter.setObjectName("taskStateFilter")
        self.task_state_filter.setAccessibleName("Task state filter")
        for label, value in (
            ("All states", "all"),
            ("Open", "open"),
            ("Completed", "completed"),
            ("Archived", "archived"),
        ):
            self.task_state_filter.addItem(label, value)
        self.task_state_filter.currentIndexChanged.connect(self._task_filters_changed)
        self.task_status_filter = self.task_state_filter

        self.task_due_filter = QComboBox()
        self.task_due_filter.setObjectName("taskDueFilter")
        self.task_due_filter.setAccessibleName("Task due-date filter")
        for label, value in (
            ("Any due date", "all"),
            ("Overdue", "overdue"),
            ("Due today", "today"),
            ("Upcoming", "upcoming"),
            ("Unscheduled", "unscheduled"),
        ):
            self.task_due_filter.addItem(label, value)
        self.task_due_filter.currentIndexChanged.connect(self._task_filters_changed)
        self.task_date_filter = self.task_due_filter

        self.task_sort_combo = QComboBox()
        self.task_sort_combo.setObjectName("taskSortFilter")
        self.task_sort_combo.setAccessibleName("Task sort order")
        for label, value in (
            ("Due date", "due"),
            ("Priority", "priority"),
            ("Title", "title"),
            ("Recently updated", "updated"),
        ):
            self.task_sort_combo.addItem(label, value)
        self.task_sort_combo.currentIndexChanged.connect(self._task_filters_changed)
        self.task_sort_filter = self.task_sort_combo

        self.task_filter_input = QLineEdit()
        self.task_filter_input.setObjectName("taskSearchInput")
        self.task_filter_input.setAccessibleName("Filter tasks")
        self.task_filter_input.setPlaceholderText("Search tasks")
        self.task_filter_input.textChanged.connect(self._task_filters_changed)
        task_view_layout.addWidget(self.task_filter_input, 1)
        self.task_search_input = self.task_filter_input
        self.task_filter = self.task_filter_input

        self.task_filters_button = QToolButton()
        self.task_filters_button.setObjectName("taskFiltersButton")
        self.task_filters_button.setText("Filters")
        self.task_filters_button.setAccessibleName("Show task filters")
        self.task_filters_button.setToolTip("Show state, due-date, and sort filters")
        self.task_filters_button.setCheckable(True)
        self.task_filters_button.setAutoRaise(True)
        self.task_filters_button.toggled.connect(self._toggle_task_filters)
        task_view_layout.addWidget(self.task_filters_button)

        self.task_undo_button = QToolButton()
        self.task_undo_button.setObjectName("taskUndoButton")
        self.task_undo_button.setText("Undo")
        self.task_undo_button.setAccessibleName("Undo last task completion or archive")
        self.task_undo_button.setToolTip("Undo last completion or archive")
        self.task_undo_button.setAutoRaise(True)
        self.task_undo_button.clicked.connect(lambda _checked=False: self.undo_task())
        self.task_undo_button.setVisible(False)
        task_view_layout.addWidget(self.task_undo_button)
        self.task_undo_action = self.task_undo_button
        layout.addWidget(task_view_bar)

        filters = QFrame()
        filters.setObjectName("taskFilters")
        filters_layout = QHBoxLayout(filters)
        filters_layout.setContentsMargins(10, 8, 10, 8)
        filters_layout.setSpacing(8)
        filters_layout.addWidget(self.task_state_filter)
        filters_layout.addWidget(self.task_due_filter)
        filters_layout.addWidget(self.task_sort_combo)
        filters.setVisible(False)
        self.task_filters_panel = filters
        layout.addWidget(filters)

        self.task_status_label = QLabel("No tasks yet.")
        self.task_status_label.setObjectName("taskStatusLabel")
        self.task_status_label.setAccessibleName("Task list status")
        self.task_status_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.task_status_label)

        self.tasks_list = TaskListWidget()
        self.tasks_list.setObjectName("tasksList")
        self.tasks_list.setAccessibleName("Tasks")
        self.tasks_list.setSpacing(6)
        self.tasks_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.tasks_list.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.task_list = self.tasks_list
        self.task_list_widget = self.tasks_list

        self.task_editor_panel = self._build_task_editor()
        self.task_editor = self.task_editor_panel
        self.task_content_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.task_content_splitter.setObjectName("taskContentSplitter")
        self.task_content_splitter.setAccessibleName("Tasks and task editor")
        self.task_content_splitter.setChildrenCollapsible(True)
        self.task_content_splitter.addWidget(self.tasks_list)
        self.task_content_splitter.addWidget(self.task_editor_panel)
        self.task_content_splitter.setStretchFactor(0, 1)
        self.task_content_splitter.setStretchFactor(1, 0)
        layout.addWidget(self.task_content_splitter, 1)
        self._sync_task_quick_view()
        self._render_task_list()
        return page

    def _build_task_editor(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("taskEditorPanel")
        panel.setAccessibleName("Task editor")
        panel.setFrameShape(QFrame.Shape.StyledPanel)
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(16, 13, 16, 13)
        outer.setSpacing(8)

        heading = QLabel("Edit task")
        heading.setObjectName("taskEditorTitle")
        heading.setAccessibleName("Task editor heading")
        heading.setTextFormat(Qt.TextFormat.PlainText)
        self.task_editor_title_label = heading
        outer.addWidget(heading)
        self.task_editor_status_label = QLabel("")
        self.task_editor_status_label.setObjectName("taskEditorStatus")
        self.task_editor_status_label.setAccessibleName("Task editor status")
        self.task_editor_status_label.setTextFormat(Qt.TextFormat.PlainText)
        outer.addWidget(self.task_editor_status_label)

        form = QGridLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(6)
        outer.addLayout(form)

        def add_label(text: str, row: int, column: int = 0) -> None:
            label = QLabel(text)
            label.setObjectName("taskFieldLabel")
            label.setAccessibleName(f"{text} field")
            label.setTextFormat(Qt.TextFormat.PlainText)
            form.addWidget(label, row, column)

        add_label("Title", 0)
        self.task_title_input = QLineEdit()
        self.task_title_input.setObjectName("taskTitleInput")
        self.task_title_input.setAccessibleName("Task title")
        form.addWidget(self.task_title_input, 0, 1, 1, 3)

        add_label("Notes", 1)
        self.task_notes_input = QPlainTextEdit()
        self.task_notes_input.setObjectName("taskNotesInput")
        self.task_notes_input.setAccessibleName("Task notes")
        self.task_notes_input.setMaximumHeight(72)
        form.addWidget(self.task_notes_input, 1, 1, 1, 3)
        self.task_notes_editor = self.task_notes_input
        self.task_notes_edit = self.task_notes_input
        self.task_notes = self.task_notes_input

        add_label("Due", 2)
        self.task_due_input = QDateTimeEdit(QDateTime.currentDateTime())
        self.task_due_input.setObjectName("taskDueInput")
        self.task_due_input.setAccessibleName("Task due date and time")
        self.task_due_input.setCalendarPopup(True)
        self.task_due_input.calendarWidget().setFirstDayOfWeek(Qt.DayOfWeek.Sunday)
        form.addWidget(self.task_due_input, 2, 1)
        self.task_due_edit = self.task_due_input
        self.task_due_date_input = self.task_due_input
        self.task_due = self.task_due_input
        self.task_has_due_input = QCheckBox("Scheduled")
        self.task_has_due_input.setObjectName("taskHasDueInput")
        self.task_has_due_input.setAccessibleName("Task has due date")
        form.addWidget(self.task_has_due_input, 2, 2)
        self.task_all_day_input = QCheckBox("All day")
        self.task_all_day_input.setObjectName("taskAllDayInput")
        self.task_all_day_input.setAccessibleName("Task all-day due date")
        form.addWidget(self.task_all_day_input, 2, 3)
        self.task_all_day_checkbox = self.task_all_day_input
        self.task_all_day = self.task_all_day_input

        add_label("Timezone", 3)
        self.task_timezone_input = TimezoneComboBox(include_local=True)
        self.task_timezone_input.setObjectName("taskTimezoneInput")
        self.task_timezone_input.setAccessibleName("Task timezone")
        form.addWidget(self.task_timezone_input, 3, 1)
        self.task_timezone_edit = self.task_timezone_input
        self.task_timezone = self.task_timezone_input
        add_label("Priority", 3, 2)
        self.task_priority_input = QComboBox()
        self.task_priority_input.setObjectName("taskPriorityInput")
        self.task_priority_input.setAccessibleName("Task priority")
        for label, value in (
            ("None", "none"),
            ("Low", "low"),
            ("Normal", "normal"),
            ("High", "high"),
        ):
            self.task_priority_input.addItem(label, value)
        form.addWidget(self.task_priority_input, 3, 3)
        self.task_priority_combo = self.task_priority_input
        self.task_priority = self.task_priority_input

        add_label("List", 4)
        self.task_list_input = QLineEdit()
        self.task_list_input.setObjectName("taskListInput")
        self.task_list_input.setAccessibleName("Task list")
        form.addWidget(self.task_list_input, 4, 1)
        self.task_list_edit = self.task_list_input
        self.task_list_field = self.task_list_input
        add_label("Tags", 4, 2)
        self.task_tags_input = QLineEdit()
        self.task_tags_input.setObjectName("taskTagsInput")
        self.task_tags_input.setAccessibleName("Task tags")
        self.task_tags_input.setPlaceholderText("work, errands")
        form.addWidget(self.task_tags_input, 4, 3)
        self.task_tags_edit = self.task_tags_input
        self.task_tags = self.task_tags_input

        add_label("Recurrence", 5)
        self.task_recurrence_input = QComboBox()
        self.task_recurrence_input.setObjectName("taskRecurrenceInput")
        self.task_recurrence_input.setAccessibleName("Task recurrence")
        self.task_recurrence_input.setEditable(True)
        self.task_recurrence_input.addItems(("", "Daily", "Weekly", "Monthly", "Yearly"))
        form.addWidget(self.task_recurrence_input, 5, 1)
        self.task_recurrence_combo = self.task_recurrence_input
        self.task_recurrence = self.task_recurrence_input
        add_label("Reminders", 5, 2)
        self.task_reminders_input = QLineEdit()
        self.task_reminders_input.setObjectName("taskRemindersInput")
        self.task_reminders_input.setAccessibleName("Task reminders")
        self.task_reminders_input.setPlaceholderText("minutes before, comma separated")
        form.addWidget(self.task_reminders_input, 5, 3)
        self.task_reminders_edit = self.task_reminders_input
        self.task_reminders = self.task_reminders_input

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.task_cancel_button = QPushButton("Cancel")
        self.task_cancel_button.setObjectName("taskCancelButton")
        self.task_cancel_button.setAccessibleName("Cancel task editing")
        self.task_cancel_button.setAutoDefault(False)
        self.task_cancel_button.clicked.connect(self._cancel_task_edit)
        buttons.addWidget(self.task_cancel_button)
        self.task_save_button = QPushButton("Save task")
        self.task_save_button.setObjectName("taskSaveButton")
        self.task_save_button.setAccessibleName("Save task")
        self.task_save_button.setAutoDefault(False)
        self.task_save_button.clicked.connect(self._save_task_edit)
        buttons.addWidget(self.task_save_button)
        outer.addLayout(buttons)
        panel.hide()
        return panel

    @property
    def today_view(self) -> TodayView:
        return self._today_view

    @property
    def today_sections(self) -> tuple[TodaySectionView, ...]:
        return tuple(self._today_sections[section_id] for section_id in self._today_section_order)

    @property
    def today_section_order(self) -> tuple[str, ...]:
        return self._today_section_order

    @property
    def today_section_visibility(self) -> dict[str, bool]:
        return {
            section_id: self._today_sections[section_id].visible
            for section_id in self._today_section_order
        }

    def render_today(
        self, view: TodayView | Mapping[str, object] | Iterable[object] | None
    ) -> None:
        """Render Today sections from Hub-owned view data only."""

        if view is None:
            today = TodayView()
        elif isinstance(view, (TodayView, Mapping)):
            today = _coerce_today_view(view)
        else:
            today = TodayView(
                sections=tuple(
                    _coerce_today_section(item, index)
                    for index, item in enumerate(_iter_values(view))
                )
            )
        if today.sections:
            self._today_sections = {section.section_id: section for section in today.sections}
            self._today_section_order = tuple(self._today_sections)
        else:
            self._today_sections = {
                section.section_id: section for section in _DEFAULT_TODAY_SECTIONS
            }
            self._today_section_order = tuple(self._today_sections)
        self._today_view = today
        if "mini_calendar" not in self._today_sections:
            self._today_sections = {
                "mini_calendar": _DEFAULT_TODAY_SECTIONS[0],
                **self._today_sections,
            }
            self._today_section_order = ("mini_calendar", *self._today_section_order)
        if today.local_date:
            self.today_date_label.setText(today.local_date)
        else:
            self.today_date_label.setText(date.today().isoformat())
        self.today_timezone_label.setText(f"· {today.timezone}" if today.timezone else "")
        if today.generated_at is not None:
            generated = _coerce_datetime(today.generated_at)
            self.today_last_refresh_label.setText(
                generated.isoformat(sep=" ", timespec="minutes")
                if generated is not None
                else _safe_text(today.generated_at, limit=80)
            )
        else:
            self.today_last_refresh_label.setText("Not refreshed yet")
        self._render_today_sections()
        self.today_changed.emit(today)

    update_today = render_today
    set_today_view = render_today

    def render_today_sections(self, sections: Iterable[object] | None) -> None:
        """Convenience API for callers that only have ordered sections."""

        self.render_today(
            TodayView(
                sections=tuple(
                    _coerce_today_section(item, index)
                    for index, item in enumerate(_iter_values(sections))
                )
            )
        )

    set_today_sections = render_today_sections

    def set_today_section_order(self, order: Sequence[str]) -> tuple[str, ...]:
        """Apply a stable section order, appending omitted known sections."""

        requested = _coerce_text_tuple(order, limit=80, max_items=64)
        known = set(self._today_sections)
        ordered = [section_id for section_id in requested if section_id in known]
        ordered.extend(
            section_id
            for section_id in self._today_section_order
            if section_id not in ordered and section_id in known
        )
        ordered.extend(
            section_id for section_id in self._today_sections if section_id not in ordered
        )
        self._today_section_order = tuple(ordered)
        self._sync_today_view()
        self._render_today_sections()
        self.restore_dashboard_layout({**self.dashboard.export_layout(), "order": ordered})
        self.today_section_order_changed.emit(self._today_section_order)
        self.today_changed.emit(self._today_view)
        return self._today_section_order

    reorder_today_sections = set_today_section_order
    set_section_order = set_today_section_order

    def set_today_section_visibility(self, section_id: str, visible: bool) -> bool:
        section_id = _safe_text(section_id, limit=80).casefold()
        section = self._today_sections.get(section_id)
        if section is None:
            return False
        updated = replace(section, visible=bool(visible))
        self._today_sections[section_id] = updated
        self._sync_today_view()
        self._render_today_sections()
        dashboard_layout = self.dashboard.export_layout()
        hidden = set(dashboard_layout.get("hidden", []))
        if updated.visible:
            hidden.discard(section_id)
        else:
            hidden.add(section_id)
        self.restore_dashboard_layout({**dashboard_layout, "hidden": sorted(hidden)})
        payload = {section_id: updated.visible}
        self.today_section_visibility_changed.emit(payload)
        self.today_changed.emit(self._today_view)
        return True

    set_section_visibility = set_today_section_visibility

    def _sync_today_view(self) -> None:
        self._today_view = replace(
            self._today_view,
            sections=tuple(
                self._today_sections[section_id] for section_id in self._today_section_order
            ),
        )

    @property
    def tasks(self) -> tuple[TaskView, ...]:
        return tuple(self._tasks.values())

    @property
    def task_views(self) -> tuple[TaskView, ...]:
        return self.tasks

    @property
    def task_filters(self) -> dict[str, str]:
        return {
            "state": self._task_state_filter_value,
            "due": self._task_due_filter_value,
            "sort": self._task_sort_value,
            "search": self._task_search_value,
        }

    def _toggle_task_filters(self, visible: bool) -> None:
        """Keep advanced task filters available without dominating the toolbar."""

        self.task_filters_panel.setVisible(bool(visible))
        self.task_filters_button.setText("Hide filters" if visible else "Filters")
        self.task_filters_button.setAccessibleName(
            "Hide task filters" if visible else "Show task filters"
        )

    def set_task_quick_view(self, value: str) -> dict[str, str]:
        """Apply one of the compact task views exposed in the toolbar menu."""

        normalized = _safe_text(value, fallback="all", limit=32).casefold()
        if normalized not in {"all", "today", "upcoming", "overdue", "unscheduled"}:
            normalized = "all"
        return self.set_task_filters(due=normalized)

    def _sync_task_quick_view(self) -> None:
        """Reflect the current due filter in the compact view menu."""

        if not hasattr(self, "task_quick_view_actions"):
            return
        value = self._task_due_filter_value
        labels = {
            "all": "All tasks",
            "today": "Due today",
            "upcoming": "Upcoming",
            "overdue": "Overdue",
            "unscheduled": "No date",
        }
        self.task_quick_view_button.setText(f"View: {labels.get(value, 'All tasks')}")
        for action_value, action in self.task_quick_view_actions.items():
            action.setChecked(action_value == value)

    def render_tasks(
        self,
        tasks: Iterable[TaskView | Mapping[str, object]] | None,
        *,
        preserve_undo: bool = False,
    ) -> None:
        """Replace the local task view cache and repaint the Tasks page."""

        if isinstance(tasks, Mapping):
            tasks = tasks.get("tasks", tasks.get("items", tasks.values()))  # type: ignore[assignment]
        self._tasks = {
            task.task_id: task
            for index, value in enumerate(_iter_values(tasks))
            for task in (_coerce_task(value, index),)
        }
        if not preserve_undo:
            self._task_undo_stack.clear()
        self._render_task_list()

    update_tasks = render_tasks
    set_tasks = render_tasks

    def _task_filters_changed(self, *_args: object) -> None:
        self._task_state_filter_value = str(self.task_state_filter.currentData() or "all")
        self._task_due_filter_value = str(self.task_due_filter.currentData() or "all")
        self._task_sort_value = str(self.task_sort_combo.currentData() or "due")
        self._task_search_value = self.task_filter_input.text().strip().casefold()
        self._sync_task_quick_view()
        self._render_task_list()
        self.task_filters_changed.emit(self.task_filters)

    def set_task_filters(
        self,
        *,
        state: str | None = None,
        due: str | None = None,
        sort: str | None = None,
        search: str | None = None,
    ) -> dict[str, str]:
        """Set state, due, sort, and text filters without external work."""

        if state is not None:
            self._set_combo_data(self.task_state_filter, state, fallback="all")
        if due is not None:
            self._set_combo_data(self.task_due_filter, due, fallback="all")
        if sort is not None:
            self._set_combo_data(self.task_sort_combo, sort, fallback="due")
        if search is not None:
            self.task_filter_input.setText(_safe_text(search, limit=240))
        self._task_filters_changed()
        return self.task_filters

    set_task_filter = set_task_filters
    filter_tasks = set_task_filters

    def set_task_state_filter(self, value: str) -> dict[str, str]:
        return self.set_task_filters(state=value)

    def set_task_due_filter(self, value: str) -> dict[str, str]:
        return self.set_task_filters(due=value)

    def set_task_sort(self, value: str) -> dict[str, str]:
        return self.set_task_filters(sort=value)

    @staticmethod
    def _set_combo_data(combo: QComboBox, value: str, *, fallback: str) -> None:
        normalized = _safe_text(value, fallback=fallback, limit=64).casefold()
        index = combo.findData(normalized)
        combo.setCurrentIndex(index if index >= 0 else combo.findData(fallback))

    def _task_matches(self, task: TaskView) -> bool:
        if self._task_state_filter_value != "all" and task.state != self._task_state_filter_value:
            return False
        due = _coerce_datetime(task.due_at)
        due_filter = self._task_due_filter_value
        if due_filter == "unscheduled" and due is not None:
            return False
        if due_filter != "unscheduled" and due_filter != "all" and due is None:
            return False
        if due is not None and due_filter in {"overdue", "today", "upcoming"}:
            local_due = due.astimezone().date() if due.tzinfo and due.utcoffset() else due.date()
            local_today = date.today()
            if due_filter == "overdue" and local_due >= local_today:
                return False
            if due_filter == "today" and local_due != local_today:
                return False
            if due_filter == "upcoming" and local_due <= local_today:
                return False
        if self._task_search_value:
            searchable = " ".join(
                (
                    task.title,
                    task.notes,
                    task.list_id or "",
                    " ".join(task.tags),
                    task.recurrence or "",
                )
            ).casefold()
            if self._task_search_value not in searchable:
                return False
        return True

    def _task_sort_key(self, task: TaskView) -> tuple[object, ...]:
        if self._task_sort_value == "priority":
            priority = {"high": 0, "normal": 1, "low": 2, "none": 3}.get(task.priority, 1)
            return (priority, task.title.casefold(), task.task_id)
        if self._task_sort_value == "title":
            return (task.title.casefold(), task.task_id)
        if self._task_sort_value == "updated":
            updated = _coerce_datetime(task.updated_at)
            return (-(updated.timestamp() if updated is not None else 0.0), task.title.casefold())
        due = _coerce_datetime(task.due_at)
        if due is None:
            return (1, datetime.max, task.title.casefold(), task.task_id)
        return (
            0,
            due.year,
            due.month,
            due.day,
            due.hour,
            due.minute,
            due.second,
            due.microsecond,
            task.title.casefold(),
            task.task_id,
        )

    def _render_task_list(self) -> None:
        if not hasattr(self, "tasks_list"):
            return
        visible_tasks = [task for task in self._tasks.values() if self._task_matches(task)]
        visible_tasks.sort(key=self._task_sort_key)
        selected_id = None
        selected = self.tasks_list.currentItem()
        if selected is not None:
            selected_id = selected.data(Qt.ItemDataRole.UserRole)
        self.tasks_list.clear()
        if hasattr(self, "task_undo_button"):
            self.task_undo_button.setVisible(bool(self._task_undo_stack))
        if not self._tasks:
            self.task_status_label.setText("No tasks yet.")
        elif not visible_tasks:
            self.task_status_label.setText("No tasks match the selected filters.")
        else:
            self.task_status_label.setText(
                f"{len(visible_tasks)} of {len(self._tasks)} task"
                f"{'s' if len(self._tasks) != 1 else ''}"
            )
        for task in visible_tasks:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, task.task_id)
            row = TaskRowWidget(task)
            row.complete_requested.connect(self.complete_task)
            row.edit_requested.connect(self.open_task_editor)
            row.archive_requested.connect(self.archive_task)
            row.undo_requested.connect(self.undo_task)
            row.native_open_requested.connect(self.task_native_open_requested.emit)
            self.tasks_list.addItem(item)
            self.tasks_list.setItemWidget(item, row)
            item.setSizeHint(row.sizeHint())
            if selected_id == task.task_id:
                self.tasks_list.setCurrentItem(item)

    def _quick_add_task(self) -> TaskView | None:
        title = _safe_text(self.task_quick_add_input.text(), limit=240)
        if not title:
            self.task_status_label.setText("Enter a task title to add it.")
            self.task_quick_add_input.setFocus()
            return None
        task = self.add_task(title)
        self.task_quick_add_input.clear()
        return task

    def quick_add_task(self, title: str) -> TaskView | None:
        """Add a title through the same bounded path as the quick-add field."""

        self.task_quick_add_input.setText(_safe_text(title, limit=240))
        return self._quick_add_task()

    def add_task(
        self,
        title: str | TaskView | Mapping[str, object],
        **fields: object,
    ) -> TaskView:
        """Add an in-memory task and emit a composition-root-friendly signal."""

        now = datetime.now().astimezone()
        if isinstance(title, TaskView):
            task = title
        elif isinstance(title, Mapping):
            data = dict(title)
            data.update(fields)
            task = _coerce_task(data, len(self._tasks))
        else:
            data = dict(fields)
            data["title"] = title
            data.setdefault("task_id", str(uuid4()))
            data.setdefault("created_at", now)
            data.setdefault("updated_at", now)
            task = _coerce_task(data, len(self._tasks))
        if not task.task_id or task.task_id in self._tasks:
            task = replace(task, task_id=str(uuid4()))
        self._tasks[task.task_id] = task
        self._render_task_list()
        self.task_created.emit(task)
        return task

    def open_task_editor(self, task_id: str) -> TaskView | None:
        task = self._tasks.get(_task_identifier(task_id))
        if task is None:
            return None
        self._editing_task_id = task.task_id
        self._editing_new_task = False
        self.task_editor_title_label.setText("Edit task")
        self._populate_task_editor(task)
        self.task_editor_panel.show()
        self.task_editor_status_label.setText("Edit fields, then save or cancel.")
        self.task_title_input.setFocus()
        self.task_edit_requested.emit(task)
        return task

    edit_task_in_editor = open_task_editor

    def open_new_task_editor(
        self,
        *,
        due_at: datetime | None = None,
        due_is_all_day: bool = False,
        timezone: str = "local",
    ) -> None:
        """Open the task editor with an optional calendar slot prefilled."""

        self._editing_task_id = None
        self._editing_new_task = True
        self.task_editor_title_label.setText("Add scheduled task")
        self.task_title_input.clear()
        self.task_notes_input.clear()
        self.task_has_due_input.setChecked(due_at is not None)
        if due_at is not None:
            self.task_due_input.setDateTime(_qt_datetime(due_at))
        self.task_all_day_input.setChecked(bool(due_is_all_day))
        self.task_timezone_input.set_timezone(_safe_text(timezone, fallback="local", limit=128))
        self._set_combo_data(self.task_priority_input, "normal", fallback="normal")
        self.task_list_input.clear()
        self.task_tags_input.clear()
        self.task_recurrence_input.setCurrentText("")
        self.task_reminders_input.clear()
        self.task_editor_panel.show()
        self.task_editor_status_label.setText(
            "Add a local task for this calendar time, then save it to place it on the grid."
        )
        self.task_title_input.setFocus()

    def _populate_task_editor(self, task: TaskView) -> None:
        self.task_title_input.setText(task.title)
        self.task_notes_input.setPlainText(task.notes)
        due = _coerce_datetime(task.due_at)
        self.task_has_due_input.setChecked(due is not None)
        if due is not None:
            self.task_due_input.setDateTime(_qt_datetime(due))
        self.task_all_day_input.setChecked(task.due_is_all_day)
        self.task_timezone_input.set_timezone(task.timezone)
        self._set_combo_data(self.task_priority_input, task.priority, fallback="normal")
        self.task_list_input.setText(task.list_id or "")
        self.task_tags_input.setText(", ".join(task.tags))
        self.task_recurrence_input.setCurrentText(task.recurrence or "")
        self.task_reminders_input.setText(
            ", ".join(str(item) for item in task.reminder_offsets_minutes)
        )

    def _save_task_edit(self) -> TaskView | None:
        task_id = getattr(self, "_editing_task_id", None)
        is_new = bool(getattr(self, "_editing_new_task", False))
        if not task_id and not is_new:
            return None
        title = _safe_text(self.task_title_input.text(), limit=240)
        if not title:
            self.task_editor_status_label.setText("A task title is required.")
            self.task_title_input.setFocus()
            return None
        try:
            due_value = self.task_due_input.dateTime()
            due_wall_clock = datetime(
                due_value.date().year(),
                due_value.date().month(),
                due_value.date().day(),
                due_value.time().hour(),
                due_value.time().minute(),
                due_value.time().second(),
                due_value.time().msec() * 1_000,
            )
            due = (
                _task_editor_due_datetime(
                    due_wall_clock,
                    self.task_timezone_input.timezone(),
                )
                if self.task_has_due_input.isChecked()
                else None
            )
        except ValueError:
            self.task_editor_status_label.setText("Enter a recognized task timezone.")
            self.task_timezone_input.setFocus()
            return None
        changes = {
            "title": title,
            "notes": self.task_notes_input.toPlainText(),
            "due_at": due,
            "due_is_all_day": self.task_all_day_input.isChecked(),
            "timezone": self.task_timezone_input.timezone(),
            "priority": self.task_priority_input.currentData() or "normal",
            "list_id": self.task_list_input.text(),
            "tags": self.task_tags_input.text(),
            "recurrence": self.task_recurrence_input.currentText(),
            "reminder_offsets_minutes": self.task_reminders_input.text(),
        }
        updated = self.add_task(**changes) if is_new else self.edit_task(task_id, **changes)
        if updated is not None:
            self.task_editor_panel.hide()
            self._editing_task_id = None
            self._editing_new_task = False
            self.task_editor_title_label.setText("Edit task")
        return updated

    def _cancel_task_edit(self) -> None:
        self._editing_task_id = None
        self._editing_new_task = False
        self.task_editor_title_label.setText("Edit task")
        self.task_editor_panel.hide()
        self.task_editor_status_label.clear()

    def edit_task(
        self,
        task_id: str,
        updates: Mapping[str, object] | None = None,
        **changes: object,
    ) -> TaskView | None:
        """Update one task view locally, or open its editor when no fields are given."""

        normalized_id = _task_identifier(task_id)
        current = self._tasks.get(normalized_id)
        if current is None:
            return None
        if updates:
            changes = {**dict(updates), **changes}
        if not changes:
            return self.open_task_editor(normalized_id)
        data: dict[str, object] = {
            "task_id": current.task_id,
            "title": current.title,
            "notes": current.notes,
            "state": current.state,
            "priority": current.priority,
            "due_at": current.due_at,
            "due_is_all_day": current.due_is_all_day,
            "timezone": current.timezone,
            "list_id": current.list_id,
            "tags": current.tags,
            "recurrence": current.recurrence,
            "reminder_offsets_minutes": current.reminder_offsets_minutes,
            "created_at": current.created_at,
            "completed_at": current.completed_at,
        }
        data.update(changes)
        data["updated_at"] = datetime.now().astimezone()
        updated = _coerce_task(data, 0)
        self._tasks[normalized_id] = updated
        self._render_task_list()
        self.task_updated.emit(updated)
        return updated

    def complete_task(self, task_id: str) -> TaskView | None:
        normalized_id = _task_identifier(task_id)
        current = self._tasks.get(normalized_id)
        if current is None or current.state != "open":
            return None
        self._task_undo_stack.append((normalized_id, current))
        completed = replace(
            current,
            state="completed",
            completed_at=datetime.now().astimezone(),
            updated_at=datetime.now().astimezone(),
        )
        self._tasks[normalized_id] = completed
        self._render_task_list()
        self.task_completed.emit(completed)
        return completed

    def archive_task(self, task_id: str) -> TaskView | None:
        normalized_id = _task_identifier(task_id)
        current = self._tasks.get(normalized_id)
        if current is None or current.state == "archived":
            return None
        self._task_undo_stack.append((normalized_id, current))
        archived = replace(
            current,
            state="archived",
            updated_at=datetime.now().astimezone(),
        )
        self._tasks[normalized_id] = archived
        self._render_task_list()
        self.task_archived.emit(archived)
        return archived

    def selected_task(self) -> TaskView | None:
        item = self.tasks_list.currentItem() if hasattr(self, "tasks_list") else None
        if item is None:
            return None
        return self._tasks.get(_task_identifier(item.data(Qt.ItemDataRole.UserRole)))

    def complete_selected_task(self) -> TaskView | None:
        task = self.selected_task()
        return self.complete_task(task.task_id) if task is not None else None

    def archive_selected_task(self) -> TaskView | None:
        task = self.selected_task()
        return self.archive_task(task.task_id) if task is not None else None

    def edit_selected_task(self) -> TaskView | None:
        task = self.selected_task()
        return self.open_task_editor(task.task_id) if task is not None else None

    def undo_task(self, task_id: str | None = None) -> TaskView | None:
        """Restore the most recent completion/archive action."""

        if not self._task_undo_stack:
            return None
        entry_index = len(self._task_undo_stack) - 1
        if task_id is not None:
            normalized_id = _task_identifier(task_id)
            for index in range(len(self._task_undo_stack) - 1, -1, -1):
                if self._task_undo_stack[index][0] == normalized_id:
                    entry_index = index
                    break
            else:
                return None
        normalized_id, previous = self._task_undo_stack.pop(entry_index)
        self._tasks[normalized_id] = previous
        self._render_task_list()
        self.task_undo_requested.emit(previous)
        return previous

    undo_last_task_action = undo_task
    undo_completion = undo_task
    undo_archive = undo_task

    def _render_tasks_from_state(self) -> None:
        if hasattr(self, "tasks_list"):
            self._render_task_list()

    def _build_watchlists_page(self) -> QWidget:
        """Build the native News/Watchlists presentation surface.

        Feed collection is intentionally absent here.  This page only renders
        bounded plain-text views and emits typed local UI requests for the
        composition root to handle.
        """

        page, layout = self._new_page(
            "watchlistsPage",
            "Watchlists and news page",
            scrollable=True,
        )
        self.watchlists_page = page
        self.news_page = page
        self._watchlists_layout = layout
        self._add_page_header(
            layout,
            "Watchlists",
            "Manage webpage watches and RSS or Atom feeds.",
        )
        self._build_web_watch_panel(layout)

        toolbar = QFrame()
        toolbar.setObjectName("newsToolbar")
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(0, 0, 0, 0)
        toolbar_layout.setSpacing(8)
        self.news_freshness_label = QLabel()
        self.news_freshness_label.setObjectName("newsFreshnessLabel")
        self.news_freshness_label.setAccessibleName("News freshness")
        self.news_freshness_label.setTextFormat(Qt.TextFormat.PlainText)
        toolbar_layout.addWidget(self.news_freshness_label)
        self.news_status_label = QLabel()
        self.news_status_label.setObjectName("newsStatusLabel")
        self.news_status_label.setAccessibleName("News status details")
        self.news_status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.news_status_label.setWordWrap(True)
        toolbar_layout.addWidget(self.news_status_label, 1)
        self.news_refresh_button = QPushButton("Refresh")
        self.news_refresh_button.setObjectName("newsRefreshButton")
        self.news_refresh_button.setAccessibleName("Refresh news feeds")
        self.news_refresh_button.setAccessibleDescription(
            "Request a bounded refresh of enabled feeds."
        )
        self.news_refresh_button.setAutoDefault(False)
        self.news_refresh_button.clicked.connect(self._request_refresh)
        toolbar_layout.addWidget(self.news_refresh_button)
        layout.addWidget(toolbar)

        filters = QFrame()
        filters.setObjectName("newsFiltersPanel")
        filters.setAccessibleName("News filters")
        filters_layout = QGridLayout(filters)
        filters_layout.setContentsMargins(12, 10, 12, 10)
        filters_layout.setHorizontalSpacing(8)
        filters_layout.setVerticalSpacing(6)
        include_label = QLabel("Include keywords")
        include_label.setObjectName("newsFilterLabel")
        include_label.setTextFormat(Qt.TextFormat.PlainText)
        include_label.setAccessibleName("Include keyword filter label")
        filters_layout.addWidget(include_label, 0, 0)
        self.news_include_input = QLineEdit()
        self.news_include_input.setObjectName("newsIncludeKeywordsInput")
        self.news_include_input.setAccessibleName("Include news keywords")
        self.news_include_input.setAccessibleDescription(
            "Comma-separated words or phrases. Matching entries remain visible."
        )
        self.news_include_input.setPlaceholderText("e.g. security, local news")
        self.news_include_input.setClearButtonEnabled(True)
        self.news_include_input.textChanged.connect(self._news_include_changed)
        filters_layout.addWidget(self.news_include_input, 0, 1)
        exclude_label = QLabel("Exclude keywords")
        exclude_label.setObjectName("newsFilterLabel")
        exclude_label.setTextFormat(Qt.TextFormat.PlainText)
        exclude_label.setAccessibleName("Exclude keyword filter label")
        filters_layout.addWidget(exclude_label, 1, 0)
        self.news_exclude_input = QLineEdit()
        self.news_exclude_input.setObjectName("newsExcludeKeywordsInput")
        self.news_exclude_input.setAccessibleName("Exclude news keywords")
        self.news_exclude_input.setAccessibleDescription(
            "Comma-separated words or phrases. Matching entries are hidden."
        )
        self.news_exclude_input.setPlaceholderText("e.g. sponsored, opinion")
        self.news_exclude_input.setClearButtonEnabled(True)
        self.news_exclude_input.textChanged.connect(self._news_exclude_changed)
        filters_layout.addWidget(self.news_exclude_input, 1, 1)
        self.news_unread_checkbox = QCheckBox("Unread only")
        self.news_unread_checkbox.setObjectName("newsUnreadOnlyCheckBox")
        self.news_unread_checkbox.setAccessibleName("Show unread news only")
        self.news_unread_checkbox.setAccessibleDescription(
            "Hide entries already marked read on this device."
        )
        self.news_unread_checkbox.toggled.connect(self._news_unread_filter_toggled)
        filters_layout.addWidget(self.news_unread_checkbox, 0, 2, 2, 1)
        self.news_filter_status_label = QLabel()
        self.news_filter_status_label.setObjectName("newsFilterStatusLabel")
        self.news_filter_status_label.setAccessibleName("News filter status")
        self.news_filter_status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.news_filter_status_label.setWordWrap(True)
        filters_layout.addWidget(self.news_filter_status_label, 2, 0, 1, 3)
        layout.addWidget(filters)
        self.news_filters = filters
        self.include_keywords_input = self.news_include_input
        self.exclude_keywords_input = self.news_exclude_input

        editor = QFrame()
        editor.setObjectName("newsSubscriptionEditor")
        editor.setAccessibleName("Feed subscription editor")
        editor_layout = QGridLayout(editor)
        editor_layout.setContentsMargins(12, 10, 12, 10)
        editor_layout.setHorizontalSpacing(8)
        editor_layout.setVerticalSpacing(6)
        editor_title = QLabel("Add a feed")
        editor_title.setObjectName("newsEditorTitle")
        editor_title.setAccessibleName("Feed editor heading")
        editor_title.setTextFormat(Qt.TextFormat.PlainText)
        editor_layout.addWidget(editor_title, 0, 0, 1, 4)
        self.news_subscription_editor_title = editor_title
        title_label = QLabel("Title")
        title_label.setObjectName("newsEditorLabel")
        title_label.setTextFormat(Qt.TextFormat.PlainText)
        editor_layout.addWidget(title_label, 1, 0)
        self.news_subscription_title_input = QLineEdit()
        self.news_subscription_title_input.setObjectName("newsSubscriptionTitleInput")
        self.news_subscription_title_input.setAccessibleName("Feed title")
        self.news_subscription_title_input.setPlaceholderText("A short name for this feed")
        self.news_subscription_title_input.setMaxLength(180)
        editor_layout.addWidget(self.news_subscription_title_input, 1, 1, 1, 3)
        url_label = QLabel("Feed URL")
        url_label.setObjectName("newsEditorLabel")
        url_label.setTextFormat(Qt.TextFormat.PlainText)
        editor_layout.addWidget(url_label, 2, 0)
        self.news_subscription_url_input = QLineEdit()
        self.news_subscription_url_input.setObjectName("newsSubscriptionUrlInput")
        self.news_subscription_url_input.setAccessibleName("RSS or Atom feed URL")
        self.news_subscription_url_input.setAccessibleDescription(
            "Use one exact HTTP(S) feed URL. HTTPS is preferred; HTTP requires opt-in."
        )
        self.news_subscription_url_input.setPlaceholderText("https://example.com/feed.xml")
        self.news_subscription_url_input.setMaxLength(512)
        editor_layout.addWidget(self.news_subscription_url_input, 2, 1, 1, 3)
        interval_label = QLabel("Refresh minutes")
        interval_label.setObjectName("newsEditorLabel")
        interval_label.setTextFormat(Qt.TextFormat.PlainText)
        editor_layout.addWidget(interval_label, 3, 0)
        self.news_subscription_interval_input = QLineEdit("30")
        self.news_subscription_interval_input.setObjectName("newsSubscriptionIntervalInput")
        self.news_subscription_interval_input.setAccessibleName("Feed refresh interval in minutes")
        self.news_subscription_interval_input.setAccessibleDescription(
            "The minimum allowed RSS/Atom refresh interval is 15 minutes."
        )
        self.news_subscription_interval_input.setMaxLength(4)
        editor_layout.addWidget(self.news_subscription_interval_input, 3, 1)
        self.news_subscription_http_checkbox = QCheckBox("Allow HTTP")
        self.news_subscription_http_checkbox.setObjectName("newsSubscriptionHttpCheckBox")
        self.news_subscription_http_checkbox.setAccessibleName("Allow insecure HTTP feed URL")
        self.news_subscription_http_checkbox.setAccessibleDescription(
            "Explicitly opt in when the exact feed URL uses HTTP instead of HTTPS."
        )
        editor_layout.addWidget(self.news_subscription_http_checkbox, 3, 2)
        self.news_subscription_enabled_checkbox = QCheckBox("Enabled")
        self.news_subscription_enabled_checkbox.setObjectName("newsSubscriptionEnabledCheckBox")
        self.news_subscription_enabled_checkbox.setAccessibleName("Enable this feed subscription")
        self.news_subscription_enabled_checkbox.setChecked(True)
        editor_layout.addWidget(self.news_subscription_enabled_checkbox, 3, 3)
        include_label = QLabel("Feed include")
        include_label.setObjectName("newsEditorLabel")
        include_label.setTextFormat(Qt.TextFormat.PlainText)
        editor_layout.addWidget(include_label, 4, 0)
        self.news_subscription_include_input = QLineEdit()
        self.news_subscription_include_input.setObjectName("newsSubscriptionIncludeInput")
        self.news_subscription_include_input.setAccessibleName("Feed include keywords")
        self.news_subscription_include_input.setPlaceholderText("optional, comma-separated")
        self.news_subscription_include_input.setMaxLength(512)
        editor_layout.addWidget(self.news_subscription_include_input, 4, 1)
        exclude_label = QLabel("Feed exclude")
        exclude_label.setObjectName("newsEditorLabel")
        exclude_label.setTextFormat(Qt.TextFormat.PlainText)
        editor_layout.addWidget(exclude_label, 4, 2)
        self.news_subscription_exclude_input = QLineEdit()
        self.news_subscription_exclude_input.setObjectName("newsSubscriptionExcludeInput")
        self.news_subscription_exclude_input.setAccessibleName("Feed exclude keywords")
        self.news_subscription_exclude_input.setPlaceholderText("optional, comma-separated")
        self.news_subscription_exclude_input.setMaxLength(512)
        editor_layout.addWidget(self.news_subscription_exclude_input, 4, 3)
        self.news_subscription_editor_status = QLabel()
        self.news_subscription_editor_status.setObjectName("newsSubscriptionEditorStatus")
        self.news_subscription_editor_status.setAccessibleName("Feed editor validation")
        self.news_subscription_editor_status.setTextFormat(Qt.TextFormat.PlainText)
        self.news_subscription_editor_status.setWordWrap(True)
        editor_layout.addWidget(self.news_subscription_editor_status, 5, 0, 1, 4)
        digest_checkbox = QCheckBox("Digest only")
        digest_checkbox.setObjectName("newsSubscriptionDigestOnlyCheckBox")
        digest_checkbox.setAccessibleName("Include feed in digest only")
        digest_checkbox.setAccessibleDescription(
            "Keep entries in the local feed view without immediate notifications."
        )
        self.news_subscription_digest_checkbox = digest_checkbox
        editor_layout.addWidget(digest_checkbox, 6, 0)
        editor_buttons = QHBoxLayout()
        editor_buttons.addStretch(1)
        self.news_subscription_cancel_button = QPushButton("Cancel")
        self.news_subscription_cancel_button.setObjectName("newsSubscriptionCancelButton")
        self.news_subscription_cancel_button.setAccessibleName("Cancel feed editing")
        self.news_subscription_cancel_button.setAutoDefault(False)
        self.news_subscription_cancel_button.clicked.connect(self.cancel_feed_subscription_edit)
        editor_buttons.addWidget(self.news_subscription_cancel_button)
        self.news_subscription_remove_button = QPushButton("Remove")
        self.news_subscription_remove_button.setObjectName("newsSubscriptionRemoveButton")
        self.news_subscription_remove_button.setAccessibleName("Remove feed subscription")
        self.news_subscription_remove_button.setAccessibleDescription(
            "Request removal of the selected local subscription; no provider data is changed."
        )
        self.news_subscription_remove_button.setAutoDefault(False)
        self.news_subscription_remove_button.setEnabled(False)
        self.news_subscription_remove_button.clicked.connect(self.request_delete_feed_subscription)
        editor_buttons.addWidget(self.news_subscription_remove_button)
        self.news_subscription_save_button = QPushButton("Add feed")
        self.news_subscription_save_button.setObjectName("newsSubscriptionSaveButton")
        self.news_subscription_save_button.setAccessibleName("Add or save feed subscription")
        self.news_subscription_save_button.setAutoDefault(False)
        self.news_subscription_save_button.clicked.connect(self._request_save_feed_subscription)
        editor_buttons.addWidget(self.news_subscription_save_button)
        editor_layout.addLayout(editor_buttons, 6, 1, 1, 3)
        self.news_subscription_editor = editor
        layout.addWidget(editor)

        subscriptions_panel = QFrame()
        subscriptions_panel.setObjectName("newsSubscriptionsPanel")
        subscriptions_panel.setAccessibleName("Feed subscriptions")
        subscriptions_layout = QHBoxLayout(subscriptions_panel)
        subscriptions_layout.setContentsMargins(12, 8, 12, 8)
        subscriptions_layout.setSpacing(8)
        self.news_subscriptions_title = QLabel("Feeds")
        self.news_subscriptions_title.setObjectName("newsSubscriptionsTitle")
        self.news_subscriptions_title.setAccessibleName("Feed subscriptions heading")
        self.news_subscriptions_title.setTextFormat(Qt.TextFormat.PlainText)
        subscriptions_layout.addWidget(self.news_subscriptions_title)
        subscriptions_layout.addStretch(1)
        self.news_subscriptions_layout = subscriptions_layout
        self.news_subscription_checkboxes: dict[str, QCheckBox] = {}
        self.news_subscription_widgets = self.news_subscription_checkboxes
        self.news_subscription_edit_buttons: dict[str, QPushButton] = {}
        self.news_subscription_remove_buttons: dict[str, QPushButton] = {}
        self.news_subscription_enable_checkboxes: dict[str, QCheckBox] = {}
        layout.addWidget(subscriptions_panel)

        self.news_entries_status_label = QLabel("No feed entries yet.")
        self.news_entries_status_label.setObjectName("newsEntriesStatusLabel")
        self.news_entries_status_label.setAccessibleName("News entries status")
        self.news_entries_status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.news_entries_status_label.setWordWrap(True)
        layout.addWidget(self.news_entries_status_label)
        self.news_entries_list = QListWidget()
        self.news_entries_list.setObjectName("newsEntriesList")
        self.news_entries_list.setAccessibleName("News feed entries")
        self.news_entries_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.news_entries_list.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.news_entries_list.setMinimumHeight(180)
        self.news_entries_list.itemDoubleClicked.connect(self._open_news_list_item)
        self.news_entries_list.itemActivated.connect(self._open_news_list_item)
        layout.addWidget(self.news_entries_list, 1)
        self.news_list = self.news_entries_list
        self.feed_entries_list = self.news_entries_list
        self.news_entry_rows: dict[str, QFrame] = {}
        self.news_entry_read_buttons: dict[str, QPushButton] = {}
        self.news_entry_open_buttons: dict[str, QPushButton] = {}

        self._render_news_sources()
        self._render_news_entries()
        self.news_freshness_label.setText(_NEWS_FRESHNESS_MESSAGES["ready"])
        self.news_status_label.clear()
        return page

    def _build_web_watch_panel(self, layout: QVBoxLayout) -> None:
        panel = QFrame()
        panel.setObjectName("webWatchPanel")
        panel.setAccessibleName("Deal, restock, and webpage watches")
        grid = QGridLayout(panel)
        grid.setContentsMargins(12, 10, 12, 10)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        title = QLabel("Web watches")
        title.setObjectName("webWatchTitle")
        title.setTextFormat(Qt.TextFormat.PlainText)
        grid.addWidget(title, 0, 0, 1, 6)

        self.watch_name_input = QLineEdit()
        self.watch_name_input.setObjectName("watchNameInput")
        self.watch_name_input.setAccessibleName("Watch name")
        self.watch_name_input.setPlaceholderText("Watch name")
        self.watch_name_input.setMaxLength(240)
        grid.addWidget(self.watch_name_input, 1, 0, 1, 3)
        self.watch_url_input = QLineEdit()
        self.watch_url_input.setObjectName("watchUrlInput")
        self.watch_url_input.setAccessibleName("Exact watched webpage URL")
        self.watch_url_input.setPlaceholderText("https://shop.example/product")
        self.watch_url_input.setMaxLength(2_048)
        grid.addWidget(self.watch_url_input, 1, 3, 1, 3)

        self.watch_kind_combo = QComboBox()
        self.watch_kind_combo.setObjectName("watchKindCombo")
        self.watch_kind_combo.setAccessibleName("Watch type")
        for label, value in (
            ("Page text", "page_text"),
            ("Page selector", "page_selector"),
            ("Product price", "product_price"),
            ("Product restock", "product_restock"),
            ("changedetection.io", "changedetection"),
        ):
            self.watch_kind_combo.addItem(label, value)
        grid.addWidget(self.watch_kind_combo, 2, 0, 1, 3)
        self.watch_condition_combo = QComboBox()
        self.watch_condition_combo.setObjectName("watchConditionCombo")
        self.watch_condition_combo.setAccessibleName("Typed watch condition")
        for label, value in (
            ("Changed", "changed"),
            ("Contains term", "contains"),
            ("Price below", "price_below"),
            ("Percentage decrease", "percentage_decrease"),
            ("Restocked", "restocked"),
        ):
            self.watch_condition_combo.addItem(label, value)
        grid.addWidget(self.watch_condition_combo, 2, 3, 1, 3)
        self.watch_threshold_input = QLineEdit()
        self.watch_threshold_input.setObjectName("watchThresholdInput")
        self.watch_threshold_input.setAccessibleName("Price threshold or percentage")
        self.watch_threshold_input.setPlaceholderText("threshold or %")
        self.watch_threshold_input.setMaxLength(16)
        grid.addWidget(self.watch_threshold_input, 3, 0, 1, 2)
        self.watch_term_input = QLineEdit()
        self.watch_term_input.setObjectName("watchTermInput")
        self.watch_term_input.setAccessibleName("Watch match term")
        self.watch_term_input.setPlaceholderText("term")
        self.watch_term_input.setMaxLength(240)
        grid.addWidget(self.watch_term_input, 3, 2, 1, 2)
        self.watch_selector_input = QLineEdit()
        self.watch_selector_input.setObjectName("watchSelectorInput")
        self.watch_selector_input.setAccessibleName("Inert webpage selector")
        self.watch_selector_input.setPlaceholderText("#stock or [itemprop=price]")
        self.watch_selector_input.setMaxLength(240)
        grid.addWidget(self.watch_selector_input, 3, 4, 1, 2)

        self.watch_interval_input = QLineEdit("60")
        self.watch_interval_input.setObjectName("watchIntervalInput")
        self.watch_interval_input.setAccessibleName("Watch refresh interval in minutes")
        self.watch_interval_input.setMaxLength(4)
        grid.addWidget(self.watch_interval_input, 4, 0)
        self.watch_cooldown_input = QLineEdit("60")
        self.watch_cooldown_input.setObjectName("watchCooldownInput")
        self.watch_cooldown_input.setAccessibleName("Watch alert cooldown in minutes")
        self.watch_cooldown_input.setMaxLength(5)
        grid.addWidget(self.watch_cooldown_input, 4, 1)
        self.watch_notification_combo = QComboBox()
        self.watch_notification_combo.setObjectName("watchNotificationCombo")
        self.watch_notification_combo.setAccessibleName("Watch notification policy")
        for label, value in (
            ("Immediate", "immediate"),
            ("Digest", "digest"),
            ("Inbox only", "inbox_only"),
        ):
            self.watch_notification_combo.addItem(label, value)
        grid.addWidget(self.watch_notification_combo, 4, 2, 1, 2)
        self.watch_enabled_checkbox = QCheckBox("Enabled")
        self.watch_enabled_checkbox.setObjectName("watchEnabledCheckBox")
        self.watch_enabled_checkbox.setChecked(True)
        grid.addWidget(self.watch_enabled_checkbox, 4, 4)
        self.watch_http_checkbox = QCheckBox("Approve HTTP")
        self.watch_http_checkbox.setObjectName("watchHttpCheckBox")
        self.watch_http_checkbox.setAccessibleDescription(
            "Allow the exact HTTP origin only when the global Hub policy also permits it."
        )
        grid.addWidget(self.watch_http_checkbox, 4, 5)

        buttons = QHBoxLayout()
        self.watch_test_button = QPushButton("Test now")
        self.watch_test_button.setObjectName("watchTestButton")
        self.watch_test_button.clicked.connect(self.request_test_watch)
        buttons.addWidget(self.watch_test_button)
        self.watch_cancel_button = QPushButton("Cancel")
        self.watch_cancel_button.setObjectName("watchCancelButton")
        self.watch_cancel_button.clicked.connect(self.cancel_watch_edit)
        buttons.addWidget(self.watch_cancel_button)
        self.watch_remove_button = QPushButton("Remove")
        self.watch_remove_button.setObjectName("watchRemoveButton")
        self.watch_remove_button.setEnabled(False)
        self.watch_remove_button.clicked.connect(self.request_delete_watch)
        buttons.addWidget(self.watch_remove_button)
        self.watch_save_button = QPushButton("Add watch")
        self.watch_save_button.setObjectName("watchSaveButton")
        self.watch_save_button.clicked.connect(self._request_save_watch)
        buttons.addWidget(self.watch_save_button)
        buttons.addStretch(1)
        grid.addLayout(buttons, 5, 0, 1, 6)

        self.watch_editor_status = QLabel()
        self.watch_editor_status.setObjectName("watchEditorStatus")
        self.watch_editor_status.setAccessibleName("Watch validation or test result")
        self.watch_editor_status.setTextFormat(Qt.TextFormat.PlainText)
        self.watch_editor_status.setWordWrap(True)
        grid.addWidget(self.watch_editor_status, 6, 0, 1, 6)
        self.watch_list = QListWidget()
        self.watch_list.setObjectName("watchList")
        self.watch_list.setAccessibleName("Configured webpage watches")
        self.watch_list.setMinimumHeight(120)
        self.watch_list.itemDoubleClicked.connect(
            lambda item: self.edit_watch(item.data(Qt.ItemDataRole.UserRole))
        )
        grid.addWidget(self.watch_list, 7, 0, 1, 6)
        self.watch_alerts_label = QLabel("No watch alerts.")
        self.watch_alerts_label.setObjectName("watchAlertsLabel")
        self.watch_alerts_label.setTextFormat(Qt.TextFormat.PlainText)
        self.watch_alerts_label.setWordWrap(True)
        grid.addWidget(self.watch_alerts_label, 8, 0, 1, 6)
        layout.addWidget(panel)
        self.web_watch_panel = panel

    def _validated_watch_payload(
        self,
        values: Mapping[str, object] | None = None,
    ) -> dict[str, object] | None:
        data = dict(values or {})
        name = _safe_text(data.get("name", self.watch_name_input.text()), limit=240)
        source_url = _safe_http_target(data.get("source_url", self.watch_url_input.text()))
        if not name:
            self.watch_editor_status.setText("A watch name is required.")
            return None
        if not source_url:
            self.watch_editor_status.setText("Enter one valid HTTP(S) webpage URL.")
            return None
        kind = _safe_text(
            data.get("kind", self.watch_kind_combo.currentData()), limit=32
        ).casefold()
        condition_kind = _safe_text(
            data.get("condition_kind", self.watch_condition_combo.currentData()), limit=32
        ).casefold()
        if kind not in {
            "page_text",
            "page_selector",
            "product_price",
            "product_restock",
            "changedetection",
        }:
            self.watch_editor_status.setText("Select a supported watch type.")
            return None
        if condition_kind not in {
            "changed",
            "contains",
            "price_below",
            "percentage_decrease",
            "restocked",
        }:
            self.watch_editor_status.setText("Select a typed watch condition.")
            return None
        try:
            refresh_minutes = int(data.get("refresh_minutes", self.watch_interval_input.text()))
            cooldown_minutes = int(data.get("cooldown_minutes", self.watch_cooldown_input.text()))
        except (TypeError, ValueError):
            self.watch_editor_status.setText("Refresh and cooldown must be whole minutes.")
            return None
        minimum = 15 if kind in {"product_price", "product_restock"} else 30
        if not minimum <= refresh_minutes <= 1_440:
            self.watch_editor_status.setText(
                f"This watch type requires {minimum}–1440 refresh minutes."
            )
            return None
        if not 0 <= cooldown_minutes <= 43_200:
            self.watch_editor_status.setText("Cooldown must be between 0 and 43200 minutes.")
            return None
        selector = _safe_text(data.get("selector", self.watch_selector_input.text()), limit=240)
        term = _safe_text(data.get("term", self.watch_term_input.text()), limit=240)
        if kind == "page_selector" and not selector:
            self.watch_editor_status.setText("A page selector watch requires an inert selector.")
            return None
        if selector and not re.fullmatch(
            r"(?:[a-zA-Z][a-zA-Z0-9_-]*)?(?:#[a-zA-Z][a-zA-Z0-9_-]*|\.[a-zA-Z][a-zA-Z0-9_-]*|\[(?:itemprop|property|name)=(?:[a-zA-Z0-9:_-]+)\])?",
            selector,
        ):
            self.watch_editor_status.setText(
                "Selectors are limited to one tag, id, class, or metadata attribute."
            )
            return None
        if condition_kind == "contains" and not term:
            self.watch_editor_status.setText("A contains condition requires a term.")
            return None
        threshold: float | None = None
        percentage: float | None = None
        if condition_kind in {"price_below", "percentage_decrease"}:
            try:
                number = float(data.get("threshold", self.watch_threshold_input.text()))
            except (TypeError, ValueError):
                self.watch_editor_status.setText("Enter a numeric threshold or percentage.")
                return None
            if number < 0 or (condition_kind == "percentage_decrease" and not 0 < number <= 100):
                self.watch_editor_status.setText(
                    "The configured numeric condition is out of range."
                )
                return None
            if condition_kind == "price_below":
                threshold = number
            else:
                percentage = number
        if kind == "product_restock" and condition_kind != "restocked":
            self.watch_editor_status.setText("A product restock watch requires Restocked.")
            return None
        if kind == "product_price" and condition_kind not in {
            "changed",
            "price_below",
            "percentage_decrease",
        }:
            self.watch_editor_status.setText("Select a price-compatible condition.")
            return None
        allow_http = bool(data.get("allow_http", self.watch_http_checkbox.isChecked()))
        if urlsplit(source_url).scheme.casefold() == "http" and not allow_http:
            self.watch_editor_status.setText(
                "HTTP requires explicit approval for this exact watch."
            )
            return None
        notification_policy = _safe_text(
            data.get("notification_policy", self.watch_notification_combo.currentData()),
            limit=16,
        )
        if notification_policy not in {"immediate", "digest", "inbox_only"}:
            self.watch_editor_status.setText("Select a notification policy.")
            return None
        return {
            "name": name,
            "kind": kind,
            "source_url": source_url,
            "condition": {
                "kind": condition_kind,
                "threshold": threshold,
                "percentage": percentage,
                "term": term or None,
                "selector": selector or None,
                "alert_on_current_match": bool(data.get("alert_on_current_match", False)),
            },
            "refresh_minutes": refresh_minutes,
            "cooldown_minutes": cooldown_minutes,
            "notification_policy": notification_policy,
            "enabled": bool(data.get("enabled", self.watch_enabled_checkbox.isChecked())),
            "allow_http": allow_http,
            "approved_redirect_origins": tuple(
                _coerce_text_tuple(data.get("approved_redirect_origins", ()), limit=512)
            ),
        }

    def _request_save_watch(self) -> None:
        payload = self._validated_watch_payload()
        if payload is None:
            return
        if self._watch_editor_id is None:
            self.watch_create_requested.emit(payload)
        else:
            payload["watch_id"] = self._watch_editor_id
            self.watch_update_requested.emit(payload)
        self.watch_editor_status.setText("Watch request sent for durable validation.")

    def request_create_watch(
        self, values: Mapping[str, object] | None = None
    ) -> dict[str, object] | None:
        payload = self._validated_watch_payload(values)
        if payload is not None:
            self.watch_create_requested.emit(payload)
        return payload

    def request_test_watch(self, values: Mapping[str, object] | None = None) -> object | None:
        if self._watch_editor_id is not None and values is None:
            payload: object = {"watch_id": self._watch_editor_id}
        else:
            payload = self._validated_watch_payload(values)
            if payload is None:
                return None
        self.watch_editor_status.setText("Testing extraction without saving a baseline…")
        self.watch_test_requested.emit(payload)
        return payload

    def request_delete_watch(self, watch_id: object | None = None) -> str | None:
        identifier = _safe_text(watch_id or self._watch_editor_id, limit=_MAX_ID_LENGTH)
        if not identifier:
            self.watch_editor_status.setText("Select a watch before removing it.")
            return None
        self.watch_delete_requested.emit({"watch_id": identifier})
        self.cancel_watch_edit()
        return identifier

    def edit_watch(self, watch_id: object) -> WebWatchView | None:
        identifier = _safe_text(watch_id, limit=_MAX_ID_LENGTH)
        watch = next((item for item in self._web_watches if item.watch_id == identifier), None)
        if watch is None:
            return None
        self._watch_editor_id = watch.watch_id
        self.watch_name_input.setText(watch.name)
        self.watch_url_input.setText(watch.source_url)
        self._set_combo_data(self.watch_kind_combo, watch.kind, fallback="page_text")
        self._set_combo_data(self.watch_condition_combo, watch.condition_kind, fallback="changed")
        numeric = watch.threshold if watch.threshold is not None else watch.percentage
        self.watch_threshold_input.setText("" if numeric is None else str(numeric))
        self.watch_term_input.setText(watch.term)
        self.watch_selector_input.setText(watch.selector)
        self.watch_interval_input.setText(str(watch.refresh_minutes))
        self.watch_cooldown_input.setText(str(watch.cooldown_minutes))
        self._set_combo_data(
            self.watch_notification_combo,
            watch.notification_policy,
            fallback="immediate",
        )
        self.watch_enabled_checkbox.setChecked(watch.enabled)
        self.watch_http_checkbox.setChecked(watch.allow_http)
        self.watch_remove_button.setEnabled(True)
        self.watch_save_button.setText("Save watch")
        return watch

    def cancel_watch_edit(self) -> None:
        self._watch_editor_id = None
        self.watch_name_input.clear()
        self.watch_url_input.clear()
        self.watch_threshold_input.clear()
        self.watch_term_input.clear()
        self.watch_selector_input.clear()
        self.watch_interval_input.setText("60")
        self.watch_cooldown_input.setText("60")
        self.watch_enabled_checkbox.setChecked(True)
        self.watch_http_checkbox.setChecked(False)
        self.watch_remove_button.setEnabled(False)
        self.watch_save_button.setText("Add watch")
        self.watch_editor_status.clear()

    def render_watch_test_result(self, result: object) -> None:
        if isinstance(result, Mapping):
            success = bool(result.get("success", False))
            message = _safe_text(result.get("message"), limit=500)
            value = _safe_text(result.get("value"), limit=160)
        else:
            success, message, value = False, "Watch test failed safely.", ""
        if success:
            self.watch_editor_status.setText(
                "Extraction succeeded." + (f" Value: {value}" if value else "")
            )
        else:
            self.watch_editor_status.setText(message or "Watch test failed safely.")

    def render_web_watches(self, view: WebWatchesView) -> None:
        if not isinstance(view, WebWatchesView):
            raise TypeError("web watch rendering expects WebWatchesView")
        self._web_watches_view = view
        self._web_watches = view.watches[:1_000]
        self._watch_alerts = view.alerts[:1_000]
        self.watch_list.clear()
        for watch in self._web_watches:
            name = self._display_text(watch.name, sensitive=watch.sensitive, fallback="Watch")
            value = self._display_text(watch.last_value, sensitive=watch.sensitive)
            rendered = f"{name} · {watch.kind.replace('_', ' ')} · {watch.status}"
            if value:
                rendered += f" — {value}"
            item = QListWidgetItem(rendered)
            item.setData(Qt.ItemDataRole.UserRole, watch.watch_id)
            item.setToolTip(rendered)
            self.watch_list.addItem(item)
        if self._watch_alerts:
            latest = self._watch_alerts[0]
            self.watch_alerts_label.setText(
                self._display_text(
                    f"{len(self._watch_alerts)} alert(s) · {latest.title}: {latest.summary}",
                    sensitive=latest.sensitive,
                )
            )
        else:
            self.watch_alerts_label.setText(view.detail or "No watch alerts.")

    render_watches = render_web_watches

    @property
    def web_watches(self) -> tuple[WebWatchView, ...]:
        return self._web_watches

    @property
    def watch_alerts(self) -> tuple[WatchAlertView, ...]:
        return self._watch_alerts

    @staticmethod
    def _news_keyword_values(value: object) -> tuple[str, ...]:
        if isinstance(value, str):
            value = value.replace(";", ",")
        return _coerce_text_tuple(value, limit=80, max_items=24)

    def _visible_news_entries(self) -> tuple[FeedEntryView, ...]:
        subscription_by_id = {
            subscription.subscription_id: subscription for subscription in self._news_subscriptions
        }
        if self._news_selection_explicit:
            selected = self._news_selected_ids
        else:
            selected = {
                subscription.subscription_id
                for subscription in self._news_subscriptions
                if subscription.enabled
            }
        include_terms = tuple(term.casefold() for term in self._news_include_terms if term)
        exclude_terms = tuple(term.casefold() for term in self._news_exclude_terms if term)
        result: list[FeedEntryView] = []
        for entry in self._news_entries:
            if selected and entry.subscription_id and entry.subscription_id not in selected:
                continue
            if self._news_selection_explicit and not selected and self._news_subscriptions:
                continue
            if self._news_unread_only and entry.read:
                continue
            subscription = subscription_by_id.get(entry.subscription_id)
            searchable = " ".join(
                (entry.title, entry.summary_text, entry.author, entry.source_name)
            ).casefold()
            if include_terms and not any(term in searchable for term in include_terms):
                continue
            if exclude_terms and any(term in searchable for term in exclude_terms):
                continue
            if subscription is not None:
                sub_include = tuple(term.casefold() for term in subscription.include_terms)
                sub_exclude = tuple(term.casefold() for term in subscription.exclude_terms)
                if sub_include and not any(term in searchable for term in sub_include):
                    continue
                if sub_exclude and any(term in searchable for term in sub_exclude):
                    continue
            result.append(entry)
            if len(result) >= self._news_view.max_entries:
                break
        return tuple(result)

    def _news_subscription_toggled(self, subscription_id: str, selected: bool) -> None:
        if selected:
            self._news_selected_ids.add(subscription_id)
        else:
            self._news_selected_ids.discard(subscription_id)
        self._news_selection_explicit = True
        self._sync_news_view()
        self._render_news_entries()
        self._render_news_today_section()
        selected_ids = self.selected_subscription_ids
        self.news_subscription_selection_changed.emit(selected_ids)
        self.news_changed.emit(self.news_view)

    def _validated_feed_subscription_payload(
        self,
        *,
        subscription_id: str | None = None,
        values: Mapping[str, object] | None = None,
    ) -> dict[str, object] | None:
        """Validate an editor draft before emitting a local request signal."""

        data = values or {
            "title": self.news_subscription_title_input.text(),
            "feed_url": self.news_subscription_url_input.text(),
            "refresh_minutes": self.news_subscription_interval_input.text(),
            "include_terms": self.news_subscription_include_input.text(),
            "exclude_terms": self.news_subscription_exclude_input.text(),
            "digest_only": self.news_subscription_digest_checkbox.isChecked(),
            "enabled": self.news_subscription_enabled_checkbox.isChecked(),
            "allow_http": self.news_subscription_http_checkbox.isChecked(),
        }
        title = _safe_feed_content(data.get("title", ""), limit=180)
        if not title:
            self.news_subscription_editor_status.setText("A feed title is required.")
            return None
        raw_url = _safe_text(data.get("feed_url", data.get("url", "")), limit=512)
        feed_url = _safe_http_target(raw_url)
        if not feed_url:
            self.news_subscription_editor_status.setText(
                "Use one exact HTTP(S) feed URL without embedded credentials."
            )
            return None
        try:
            parsed = urlsplit(feed_url)
        except ValueError:
            self.news_subscription_editor_status.setText("The feed URL is not valid.")
            return None
        allow_http = _bool_value(data.get("allow_http", data.get("http_opt_in", False)))
        if parsed.scheme.casefold() == "http" and not allow_http:
            self.news_subscription_editor_status.setText(
                "HTTP feeds require the explicit Allow HTTP opt-in."
            )
            return None
        try:
            interval = int(str(data.get("refresh_minutes", data.get("refresh", 30))).strip())
        except (TypeError, ValueError):
            interval = 0
        if interval < 15:
            self.news_subscription_editor_status.setText(
                "Feed refresh interval must be at least 15 minutes."
            )
            return None
        if interval > 1440:
            self.news_subscription_editor_status.setText(
                "Feed refresh interval cannot exceed 1440 minutes."
            )
            return None
        payload: dict[str, object] = {
            "title": title,
            "feed_url": feed_url,
            "refresh_minutes": interval,
            "include_terms": self._news_keyword_values(
                data.get("include_terms", data.get("include_keywords", ()))
            ),
            "exclude_terms": self._news_keyword_values(
                data.get("exclude_terms", data.get("exclude_keywords", ()))
            ),
            "digest_only": _bool_value(data.get("digest_only", False)),
            "enabled": _bool_value(data.get("enabled", True), default=True),
            "allow_http": allow_http,
        }
        if subscription_id:
            payload["subscription_id"] = _safe_text(subscription_id, limit=_MAX_ID_LENGTH)
        return payload

    def _request_save_feed_subscription(self) -> None:
        if self._news_subscription_editor_id:
            self.request_update_feed_subscription(self._news_subscription_editor_id)
        else:
            self.request_create_feed_subscription()

    def request_create_feed_subscription(
        self, values: Mapping[str, object] | None = None
    ) -> dict[str, object] | None:
        payload = self._validated_feed_subscription_payload(values=values)
        if payload is None:
            return None
        self.feed_subscription_create_requested.emit(payload)
        self.news_subscription_editor_status.setText(
            "Add request sent to the Hub controller; the feed will appear after persistence."
        )
        return payload

    create_feed_subscription = request_create_feed_subscription
    request_add_feed_subscription = request_create_feed_subscription

    def request_update_feed_subscription(
        self,
        subscription_id: object | None = None,
        values: Mapping[str, object] | None = None,
    ) -> dict[str, object] | None:
        identifier = _safe_text(
            subscription_id if subscription_id is not None else self._news_subscription_editor_id,
            limit=_MAX_ID_LENGTH,
        )
        if not identifier:
            self.news_subscription_editor_status.setText("Select a feed before saving changes.")
            return None
        payload = self._validated_feed_subscription_payload(
            subscription_id=identifier, values=values
        )
        if payload is None:
            return None
        self.feed_subscription_update_requested.emit(payload)
        self.news_subscription_editor_status.setText(
            "Update request sent to the Hub controller; changes will appear after persistence."
        )
        return payload

    update_feed_subscription = request_update_feed_subscription
    request_edit_feed_subscription = request_update_feed_subscription

    def request_delete_feed_subscription(
        self, subscription_id: object | None = None
    ) -> dict[str, str] | None:
        identifier = _safe_text(
            subscription_id if subscription_id is not None else self._news_subscription_editor_id,
            limit=_MAX_ID_LENGTH,
        )
        if not identifier:
            self.news_subscription_editor_status.setText("Select a feed before removing it.")
            return None
        payload = {"subscription_id": identifier}
        self.feed_subscription_delete_requested.emit(payload)
        self.cancel_feed_subscription_edit()
        return payload

    delete_feed_subscription = request_delete_feed_subscription
    request_remove_feed_subscription = request_delete_feed_subscription

    def request_feed_subscription_enabled(
        self, subscription_id: object, enabled: bool
    ) -> dict[str, object] | None:
        identifier = _safe_text(subscription_id, limit=_MAX_ID_LENGTH)
        if not identifier:
            return None
        payload = {"subscription_id": identifier, "enabled": bool(enabled)}
        self.feed_subscription_enabled_changed.emit(payload)
        return payload

    set_feed_subscription_enabled = request_feed_subscription_enabled
    request_subscription_enabled = request_feed_subscription_enabled

    def edit_feed_subscription(self, subscription_id: object) -> FeedSubscriptionView | None:
        identifier = _safe_text(subscription_id, limit=_MAX_ID_LENGTH)
        subscription = next(
            (item for item in self._news_subscriptions if item.subscription_id == identifier), None
        )
        if subscription is None:
            return None
        self._news_subscription_editor_id = subscription.subscription_id
        self.news_subscription_editor_title.setText("Edit feed")
        self.news_subscription_title_input.setText(subscription.title)
        self.news_subscription_url_input.setText(subscription.feed_url)
        self.news_subscription_interval_input.setText(str(subscription.refresh_minutes))
        self.news_subscription_include_input.setText(", ".join(subscription.include_terms))
        self.news_subscription_exclude_input.setText(", ".join(subscription.exclude_terms))
        self.news_subscription_digest_checkbox.setChecked(subscription.digest_only)
        self.news_subscription_enabled_checkbox.setChecked(subscription.enabled)
        self.news_subscription_http_checkbox.setChecked(
            urlsplit(subscription.feed_url).scheme.casefold() == "http"
        )
        self.news_subscription_remove_button.setEnabled(True)
        self.news_subscription_save_button.setText("Save changes")
        self.news_subscription_editor_status.setText(
            "Editing this local subscription. Saving emits a request; no provider is changed here."
        )
        return subscription

    edit_subscription = edit_feed_subscription

    def cancel_feed_subscription_edit(self) -> None:
        self._news_subscription_editor_id = None
        self.news_subscription_editor_title.setText("Add a feed")
        self.news_subscription_title_input.clear()
        self.news_subscription_url_input.clear()
        self.news_subscription_interval_input.setText("30")
        self.news_subscription_include_input.clear()
        self.news_subscription_exclude_input.clear()
        self.news_subscription_digest_checkbox.setChecked(False)
        self.news_subscription_enabled_checkbox.setChecked(True)
        self.news_subscription_http_checkbox.setChecked(False)
        self.news_subscription_remove_button.setEnabled(False)
        self.news_subscription_save_button.setText("Add feed")
        self.news_subscription_editor_status.clear()

    cancel_subscription_edit = cancel_feed_subscription_edit

    def _render_news_sources(self) -> None:
        while self.news_subscriptions_layout.count() > 1:
            item = self.news_subscriptions_layout.takeAt(1)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.news_subscription_checkboxes.clear()
        self.news_subscription_edit_buttons.clear()
        self.news_subscription_remove_buttons.clear()
        self.news_subscription_enable_checkboxes.clear()
        if not self._news_subscriptions:
            empty = QLabel("No feeds subscribed.")
            empty.setObjectName("newsSubscriptionsEmpty")
            empty.setAccessibleName("No feed subscriptions")
            empty.setTextFormat(Qt.TextFormat.PlainText)
            self.news_subscriptions_layout.insertWidget(1, empty)
            self.news_subscriptions_layout.addStretch(1)
            return
        for subscription in self._news_subscriptions:
            name = self._display_text(
                subscription.title, sensitive=subscription.sensitive, fallback="Feed"
            )
            parts = [name, "RSS/Atom"]
            origin = _feed_origin_label(subscription.feed_url)
            if origin:
                parts.append(self._display_text(origin, sensitive=subscription.sensitive))
            if subscription.source_name:
                parts.append(
                    self._display_text(subscription.source_name, sensitive=subscription.sensitive)
                )
            if subscription.account:
                parts.append(
                    self._display_text(subscription.account, sensitive=subscription.sensitive)
                )
            if subscription.status != "ready":
                parts.append(subscription.status)
            subscription_row = QFrame()
            subscription_row.setObjectName("newsSubscriptionRow")
            subscription_row.setAccessibleName(f"Feed subscription {name}")
            subscription_row_layout = QHBoxLayout(subscription_row)
            subscription_row_layout.setContentsMargins(2, 0, 2, 0)
            subscription_row_layout.setSpacing(4)
            checkbox = QCheckBox(" · ".join(parts))
            checkbox.setObjectName("newsSubscriptionCheckBox")
            checkbox.setAccessibleName(f"Select feed {name}")
            checkbox.setAccessibleDescription(
                "Include this feed in News, Watchlists, and the Today news digest."
            )
            checkbox.setEnabled(subscription.enabled)
            checkbox.setChecked(subscription.subscription_id in self._news_selected_ids)
            checkbox.toggled.connect(
                lambda enabled, subscription_id=subscription.subscription_id: (
                    self._news_subscription_toggled(subscription_id, enabled)
                )
            )
            subscription_row_layout.addWidget(checkbox)
            self.news_subscription_checkboxes[subscription.subscription_id] = checkbox
            enabled_checkbox = QCheckBox("Enabled")
            enabled_checkbox.setObjectName("newsSubscriptionEnabledCheckBox")
            enabled_checkbox.setAccessibleName(f"Enable feed {name}")
            enabled_checkbox.setChecked(subscription.enabled)
            enabled_checkbox.toggled.connect(
                lambda enabled, subscription_id=subscription.subscription_id: (
                    self.request_feed_subscription_enabled(subscription_id, enabled)
                )
            )
            subscription_row_layout.addWidget(enabled_checkbox)
            self.news_subscription_enable_checkboxes[subscription.subscription_id] = (
                enabled_checkbox
            )
            edit_button = QPushButton("Edit")
            edit_button.setObjectName("newsSubscriptionEditButton")
            edit_button.setAccessibleName(f"Edit feed {name}")
            edit_button.setAutoDefault(False)
            edit_button.clicked.connect(
                lambda _checked=False, subscription_id=subscription.subscription_id: (
                    self.edit_feed_subscription(subscription_id)
                )
            )
            subscription_row_layout.addWidget(edit_button)
            self.news_subscription_edit_buttons[subscription.subscription_id] = edit_button
            remove_button = QPushButton("Remove")
            remove_button.setObjectName("newsSubscriptionRemoveRowButton")
            remove_button.setAccessibleName(f"Remove feed {name}")
            remove_button.setAutoDefault(False)
            remove_button.clicked.connect(
                lambda _checked=False, subscription_id=subscription.subscription_id: (
                    self.request_delete_feed_subscription(subscription_id)
                )
            )
            subscription_row_layout.addWidget(remove_button)
            self.news_subscription_remove_buttons[subscription.subscription_id] = remove_button
            self.news_subscriptions_layout.addWidget(subscription_row)
        self.news_subscriptions_layout.addStretch(1)

    def _build_news_entry_row(self, entry: FeedEntryView) -> QFrame:
        row = QFrame()
        row.setObjectName("newsEntryRow")
        row.setAccessibleName(
            f"News entry: {self._display_text(entry.title, sensitive=entry.sensitive)}"
        )
        row.setFrameShape(QFrame.Shape.StyledPanel)
        row_layout = QVBoxLayout(row)
        row_layout.setContentsMargins(12, 9, 12, 9)
        row_layout.setSpacing(4)
        title = QLabel(
            self._display_text(entry.title, sensitive=entry.sensitive, fallback="Feed entry")
        )
        title.setObjectName("newsEntryTitle")
        title.setAccessibleName("News entry title")
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setWordWrap(True)
        row_layout.addWidget(title)
        metadata: list[str] = []
        if entry.source_name:
            metadata.append(
                self._display_text(entry.source_name, sensitive=entry.sensitive, fallback="Feed")
            )
        if entry.author:
            metadata.append(self._display_text(entry.author, sensitive=entry.sensitive))
        published = _coerce_datetime(entry.published_at)
        if published is not None:
            metadata.append(published.astimezone().strftime("%Y-%m-%d %H:%M"))
        metadata.append("Read" if entry.read else "Unread")
        metadata_label = QLabel(" · ".join(part for part in metadata if part))
        metadata_label.setObjectName("newsEntryMetadata")
        metadata_label.setAccessibleName("News entry metadata")
        metadata_label.setTextFormat(Qt.TextFormat.PlainText)
        metadata_label.setWordWrap(True)
        row_layout.addWidget(metadata_label)
        if entry.summary_text:
            summary = QLabel(
                self._display_text(entry.summary_text, sensitive=entry.sensitive, fallback="")
            )
            summary.setObjectName("newsEntrySummary")
            summary.setAccessibleName("News entry summary")
            summary.setTextFormat(Qt.TextFormat.PlainText)
            summary.setWordWrap(True)
            row_layout.addWidget(summary)
        actions = QHBoxLayout()
        actions.setContentsMargins(0, 2, 0, 0)
        actions.addStretch(1)
        read_button = QPushButton("Mark unread" if entry.read else "Mark read")
        read_button.setObjectName("newsEntryReadButton")
        read_button.setAccessibleName(f"Mark news entry {'unread' if entry.read else 'read'}")
        read_button.setAutoDefault(False)
        read_button.clicked.connect(
            lambda _checked=False, entry_id=entry.entry_id: self.set_feed_entry_read(
                entry_id, not entry.read
            )
        )
        actions.addWidget(read_button)
        self.news_entry_read_buttons[entry.entry_id] = read_button
        if entry.navigation_target:
            open_button = QPushButton("Open source")
            open_button.setObjectName("newsEntryOpenButton")
            open_button.setAccessibleName("Open news source")
            open_button.setAccessibleDescription(
                "Request ordinary browser navigation to the original HTTP(S) source."
            )
            open_button.setAutoDefault(False)
            open_button.clicked.connect(
                lambda _checked=False, entry_id=entry.entry_id: self.open_news_entry(entry_id)
            )
            actions.addWidget(open_button)
            self.news_entry_open_buttons[entry.entry_id] = open_button
        row_layout.addLayout(actions)
        return row

    def _render_news_entries(self) -> None:
        self.news_entries_list.clear()
        self.news_entry_rows.clear()
        self.news_entry_read_buttons.clear()
        self.news_entry_open_buttons.clear()
        visible = self._visible_news_entries()
        if not visible:
            if self._news_entries:
                message = "No entries match the current feed or keyword filters."
            else:
                message = _NEWS_FRESHNESS_MESSAGES[self._news_view.freshness]
            self.news_entries_status_label.setText(message)
            self.news_filter_status_label.setText(
                "Filters apply locally to the bounded entries supplied by the feed service."
            )
            return
        unread = sum(not entry.read for entry in visible)
        self.news_entries_status_label.setText(
            f"{len(visible)} entr{'y' if len(visible) == 1 else 'ies'} · {unread} unread"
        )
        self.news_filter_status_label.setText(
            "Showing selected feeds"
            + (" · unread only" if self._news_unread_only else "")
            + (
                " · include/exclude filters active"
                if self._news_include_terms or self._news_exclude_terms
                else ""
            )
        )
        for entry in visible:
            item = QListWidgetItem()
            item.setText(
                self._display_text(entry.title, sensitive=entry.sensitive, fallback="Feed entry")
            )
            item.setData(Qt.ItemDataRole.UserRole, entry.entry_id)
            row = self._build_news_entry_row(entry)
            item.setSizeHint(row.sizeHint())
            self.news_entries_list.addItem(item)
            self.news_entries_list.setItemWidget(item, row)
            self.news_entry_rows[entry.entry_id] = row

    def _news_filter_changed(self, *, include: bool, value: str) -> None:
        terms = self._news_keyword_values(value)
        if include:
            self._news_include_terms = terms
        else:
            self._news_exclude_terms = terms
        self._sync_news_view()
        self._render_news_entries()
        self._render_news_today_section()
        self.news_filter_changed.emit(
            {
                "include_terms": self._news_include_terms,
                "exclude_terms": self._news_exclude_terms,
                "unread_only": self._news_unread_only,
            }
        )
        self.news_changed.emit(self.news_view)

    def _news_include_changed(self, value: str) -> None:
        self._news_filter_changed(include=True, value=value)

    def _news_exclude_changed(self, value: str) -> None:
        self._news_filter_changed(include=False, value=value)

    def _news_unread_filter_toggled(self, enabled: bool) -> None:
        self._news_unread_only = bool(enabled)
        self._sync_news_view()
        self._render_news_entries()
        self._render_news_today_section()
        self.news_filter_changed.emit(
            {
                "include_terms": self._news_include_terms,
                "exclude_terms": self._news_exclude_terms,
                "unread_only": self._news_unread_only,
            }
        )
        self.news_changed.emit(self.news_view)

    def _open_news_list_item(self, item: QListWidgetItem) -> None:
        self.open_news_entry(item.data(Qt.ItemDataRole.UserRole))

    def _render_news_today_section(self) -> None:
        section = self._today_sections.get("news")
        if section is None:
            return
        visible = self._visible_news_entries()[:8]
        items = tuple(
            TodayItemView(
                item_id=f"news:{entry.entry_id}",
                kind="news",
                title=entry.title,
                summary=entry.summary_text,
                starts_at=entry.published_at,
                severity="info",
                source=entry.source_name or "News feed",
                navigation_target=entry.navigation_target,
                sensitive=entry.sensitive,
            )
            for entry in visible
        )
        if visible:
            body = f"{len(visible)} feed entr{'y' if len(visible) == 1 else 'ies'} available."
        else:
            body = _NEWS_FRESHNESS_MESSAGES[self._news_view.freshness]
        updated = replace(section, body=body, items=items)
        if updated == section:
            return
        self._today_sections["news"] = updated
        self._sync_today_view()
        if hasattr(self, "today_sections_layout"):
            self._render_today_sections()
        self.today_changed.emit(self._today_view)

    def _sync_news_view(self) -> None:
        selected = tuple(
            subscription.subscription_id
            for subscription in self._news_subscriptions
            if subscription.subscription_id in self._news_selected_ids
        )
        self._news_view = replace(
            self._news_view,
            selected_subscription_ids=selected,
            selection_explicit=self._news_selection_explicit,
            include_terms=self._news_include_terms,
            exclude_terms=self._news_exclude_terms,
            unread_only=self._news_unread_only,
        )

    @property
    def news_view(self) -> NewsView:
        self._sync_news_view()
        return self._news_view

    @property
    def watchlist_view(self) -> NewsView:
        return self.news_view

    @property
    def news_subscriptions(self) -> tuple[FeedSubscriptionView, ...]:
        return self._news_subscriptions

    @property
    def feed_subscriptions(self) -> tuple[FeedSubscriptionView, ...]:
        return self._news_subscriptions

    @property
    def news_entries(self) -> tuple[FeedEntryView, ...]:
        return self._news_entries

    @property
    def feed_entries(self) -> tuple[FeedEntryView, ...]:
        return self._news_entries

    @property
    def selected_subscription_ids(self) -> tuple[str, ...]:
        return tuple(
            subscription.subscription_id
            for subscription in self._news_subscriptions
            if subscription.subscription_id in self._news_selected_ids
        )

    @property
    def news_freshness(self) -> str:
        return self._news_view.freshness

    @property
    def feed_freshness(self) -> str:
        return self.news_freshness

    def set_news_subscription_selection(self, selected: Iterable[str]) -> tuple[str, ...]:
        requested = set(_coerce_text_tuple(selected, limit=_MAX_ID_LENGTH))
        known = {subscription.subscription_id for subscription in self._news_subscriptions}
        self._news_selected_ids = requested & known
        self._news_selection_explicit = True
        for subscription_id, checkbox in self.news_subscription_checkboxes.items():
            checkbox.blockSignals(True)
            checkbox.setChecked(subscription_id in self._news_selected_ids)
            checkbox.blockSignals(False)
        self._sync_news_view()
        self._render_news_entries()
        self._render_news_today_section()
        selected_ids = self.selected_subscription_ids
        self.news_subscription_selection_changed.emit(selected_ids)
        self.news_changed.emit(self.news_view)
        return selected_ids

    select_feed_subscriptions = set_news_subscription_selection
    set_feed_selection = set_news_subscription_selection

    def set_news_keyword_filters(
        self,
        include_terms: Iterable[str] | str = (),
        exclude_terms: Iterable[str] | str = (),
    ) -> dict[str, tuple[str, ...]]:
        self._news_include_terms = self._news_keyword_values(include_terms)
        self._news_exclude_terms = self._news_keyword_values(exclude_terms)
        if hasattr(self, "news_include_input"):
            self.news_include_input.blockSignals(True)
            self.news_include_input.setText(", ".join(self._news_include_terms))
            self.news_include_input.blockSignals(False)
            self.news_exclude_input.blockSignals(True)
            self.news_exclude_input.setText(", ".join(self._news_exclude_terms))
            self.news_exclude_input.blockSignals(False)
        self._sync_news_view()
        self._render_news_entries()
        self._render_news_today_section()
        payload = {
            "include_terms": self._news_include_terms,
            "exclude_terms": self._news_exclude_terms,
            "unread_only": self._news_unread_only,
        }
        self.news_filter_changed.emit(payload)
        self.news_changed.emit(self.news_view)
        return payload

    set_feed_keyword_filters = set_news_keyword_filters

    def set_news_unread_only(self, enabled: bool) -> bool:
        self._news_unread_only = bool(enabled)
        if hasattr(self, "news_unread_checkbox"):
            self.news_unread_checkbox.blockSignals(True)
            self.news_unread_checkbox.setChecked(self._news_unread_only)
            self.news_unread_checkbox.blockSignals(False)
        self._sync_news_view()
        self._render_news_entries()
        self._render_news_today_section()
        self.news_filter_changed.emit(
            {
                "include_terms": self._news_include_terms,
                "exclude_terms": self._news_exclude_terms,
                "unread_only": self._news_unread_only,
            }
        )
        self.news_changed.emit(self.news_view)
        return self._news_unread_only

    def set_feed_entry_read(self, entry_id: object, read: bool = True) -> FeedEntryView | None:
        identifier = _safe_text(
            _field(entry_id, "entry_id", _field(entry_id, "id", entry_id)),
            limit=_MAX_ID_LENGTH,
        )
        for index, entry in enumerate(self._news_entries):
            if entry.entry_id != identifier:
                continue
            updated = replace(entry, read=bool(read))
            values = list(self._news_entries)
            values[index] = updated
            self._news_entries = tuple(values)
            self._news_view = replace(self._news_view, entries=self._news_entries)
            self._render_news_entries()
            self._render_news_today_section()
            payload = {"entry_id": identifier, "read": bool(read)}
            self.news_entry_read_changed.emit(payload)
            self.news_changed.emit(self.news_view)
            return updated
        return None

    mark_news_entry_read = set_feed_entry_read
    mark_entry_read = set_feed_entry_read

    def open_news_entry(self, entry_id: object) -> FeedEntryView | None:
        if isinstance(entry_id, FeedEntryView):
            identifier = entry_id.entry_id
        else:
            identifier = _safe_text(
                _field(entry_id, "entry_id", _field(entry_id, "id", entry_id)),
                limit=_MAX_ID_LENGTH,
            )
        if not identifier:
            return None
        entry = next((item for item in self._news_entries if item.entry_id == identifier), None)
        if entry is None or not entry.navigation_target:
            return None
        self.news_entry_open_requested.emit(entry)
        return entry

    open_feed_entry = open_news_entry
    open_news_deep_link = open_news_entry

    def render_news(
        self,
        view: NewsView | Mapping[str, object] | Iterable[object] | None,
    ) -> None:
        """Render bounded feed data without provider, browser, or network work."""

        if view is None:
            news = NewsView()
        elif isinstance(view, (NewsView, Mapping)):
            news = _coerce_news_view(view)
        else:
            news = _coerce_news_view({"entries": view})
        self._news_view = news
        self._news_subscriptions = news.subscriptions
        self._news_entries = news.entries
        if news.selection_explicit or news.selected_subscription_ids:
            self._news_selected_ids = {
                subscription_id
                for subscription_id in news.selected_subscription_ids
                if subscription_id in {item.subscription_id for item in news.subscriptions}
            }
            self._news_selection_explicit = True
        elif news.subscriptions:
            self._news_selected_ids = {
                subscription.subscription_id
                for subscription in news.subscriptions
                if subscription.enabled
            }
            self._news_selection_explicit = False
        else:
            self._news_selected_ids = set()
            self._news_selection_explicit = False
        self._news_include_terms = news.include_terms
        self._news_exclude_terms = news.exclude_terms
        self._news_unread_only = news.unread_only
        self.news_include_input.blockSignals(True)
        self.news_include_input.setText(", ".join(self._news_include_terms))
        self.news_include_input.blockSignals(False)
        self.news_exclude_input.blockSignals(True)
        self.news_exclude_input.setText(", ".join(self._news_exclude_terms))
        self.news_exclude_input.blockSignals(False)
        self.news_unread_checkbox.blockSignals(True)
        self.news_unread_checkbox.setChecked(self._news_unread_only)
        self.news_unread_checkbox.blockSignals(False)
        self._render_news_sources()
        self._render_news_entries()
        self._render_news_today_section()
        self.news_freshness_label.setText(_NEWS_FRESHNESS_MESSAGES[news.freshness])
        detail = news.detail
        generated = _coerce_datetime(news.generated_at)
        if generated is not None:
            suffix = f"Updated {generated.isoformat(timespec='minutes')}"
            detail = f"{detail} · {suffix}" if detail else suffix
        self.news_status_label.setText(detail)
        self.news_changed.emit(self.news_view)

    update_news = render_news
    set_news_view = render_news
    render_feeds = render_news
    render_watchlists = render_news

    def render_today_news(self, entries: Iterable[object] | None) -> None:
        """Update only the Today News section from a bounded entry sequence."""

        self.render_news({"entries": tuple(_iter_values(entries))})

    set_today_news = render_today_news

    def _build_attention_notification_panel(
        self,
    ) -> tuple[QFrame, QLabel, QLabel, QPushButton, QPushButton]:
        panel = QFrame()
        panel.setObjectName("attentionNotificationPanel")
        panel.setAccessibleName("Attention message notification")
        panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        panel_layout = QHBoxLayout(panel)
        panel_layout.setContentsMargins(18, 12, 18, 12)
        panel_layout.setSpacing(14)

        body = QWidget(panel)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(3)
        title = QLabel("Attention messages", body)
        title.setObjectName("attentionNotificationTitle")
        title.setAccessibleName("Attention message notification heading")
        title.setTextFormat(Qt.TextFormat.PlainText)
        body_layout.addWidget(title)
        summary = QLabel(body)
        summary.setObjectName("attentionNotificationBody")
        summary.setAccessibleName("Attention message notification details")
        summary.setTextFormat(Qt.TextFormat.PlainText)
        summary.setWordWrap(True)
        body_layout.addWidget(summary)
        panel_layout.addWidget(body, 1)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(8)
        open_button = QPushButton("Review messages", panel)
        open_button.setObjectName("attentionNotificationOpenButton")
        open_button.setAccessibleName("Review attention messages")
        open_button.setAutoDefault(False)
        open_button.clicked.connect(self._open_attention_notification)
        actions.addWidget(open_button)
        dismiss_button = QPushButton("Dismiss", panel)
        dismiss_button.setObjectName("attentionNotificationDismissButton")
        dismiss_button.setAccessibleName("Dismiss attention message notification")
        dismiss_button.setAutoDefault(False)
        dismiss_button.clicked.connect(self._dismiss_attention_notification)
        actions.addWidget(dismiss_button)
        panel_layout.addLayout(actions)
        panel.hide()
        return panel, title, summary, open_button, dismiss_button

    @staticmethod
    def _build_source_failure_panel() -> tuple[QFrame, QLabel, QLabel]:
        panel = QFrame()
        panel.setObjectName("sourceFailurePanel")
        panel.setAccessibleName("Source failures")
        failure_layout = QVBoxLayout(panel)
        failure_layout.setContentsMargins(18, 16, 18, 16)
        failure_layout.setSpacing(6)
        title = QLabel("Source attention")
        title.setObjectName("sourceFailureTitle")
        title.setAccessibleName("Source failure heading")
        title.setTextFormat(Qt.TextFormat.PlainText)
        failure_layout.addWidget(title)
        body = QLabel()
        body.setObjectName("sourceFailureBody")
        body.setAccessibleName("Source failure details")
        body.setTextFormat(Qt.TextFormat.PlainText)
        body.setWordWrap(True)
        failure_layout.addWidget(body)
        panel.hide()
        return panel, title, body

    def _build_inbox_page(self) -> QWidget:
        page, layout = self._new_page("inboxPage", "Inbox page")
        self._add_page_header(
            layout,
            "Inbox",
            "Review alerts, reminders, and selected message summaries.",
        )

        self.inbox_mode_tabs = QTabWidget()
        self.inbox_mode_tabs.setObjectName("inboxModeTabs")
        self.inbox_mode_tabs.setAccessibleName("Inbox content")
        self.inbox_mode_tabs.currentChanged.connect(self._inbox_mode_tab_changed)
        self.inbox_tabs = self.inbox_mode_tabs

        alerts_page = QWidget()
        alerts_page.setObjectName("inboxAlertsPage")
        alerts_page.setAccessibleName("Inbox alerts")
        alerts_layout = QVBoxLayout(alerts_page)
        alerts_layout.setContentsMargins(4, 8, 4, 4)
        alerts_layout.setSpacing(8)

        self.inbox_status_label = QLabel("No attention items yet.")
        self.inbox_status_label.setObjectName("pageSubtitle")
        self.inbox_status_label.setAccessibleName("Inbox status")
        self.inbox_status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.inbox_status_label.setWordWrap(True)
        alerts_layout.addWidget(self.inbox_status_label)
        self.inbox_list = QListWidget()
        self.inbox_list.setObjectName("inboxList")
        self.inbox_list.setAccessibleName("Inbox items")
        self.inbox_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.inbox_list.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.inbox_list.currentItemChanged.connect(self._inbox_selection_changed)
        alerts_layout.addWidget(self.inbox_list, 1)
        inbox_actions = QHBoxLayout()
        self.inbox_read_button = QPushButton("Mark read")
        self.inbox_read_button.setAccessibleName("Mark selected Inbox item read or unread")
        self.inbox_read_button.setEnabled(False)
        self.inbox_read_button.clicked.connect(self._toggle_selected_inbox_read)
        inbox_actions.addWidget(self.inbox_read_button)
        self.inbox_dismiss_button = QPushButton("Dismiss")
        self.inbox_dismiss_button.setAccessibleName("Dismiss selected Inbox item")
        self.inbox_dismiss_button.setEnabled(False)
        self.inbox_dismiss_button.clicked.connect(
            lambda: self._request_selected_inbox_state("dismissed")
        )
        inbox_actions.addWidget(self.inbox_dismiss_button)
        self.inbox_archive_button = QPushButton("Archive")
        self.inbox_archive_button.setAccessibleName("Archive selected Inbox item")
        self.inbox_archive_button.setEnabled(False)
        self.inbox_archive_button.clicked.connect(
            lambda: self._request_selected_inbox_state("archived")
        )
        inbox_actions.addWidget(self.inbox_archive_button)
        inbox_actions.addStretch(1)
        alerts_layout.addLayout(inbox_actions)

        messages_page = QWidget()
        messages_page.setObjectName("inboxMessagesPage")
        messages_page.setAccessibleName("Inbox attention messages")
        messages_layout = QVBoxLayout(messages_page)
        messages_layout.setContentsMargins(4, 8, 4, 4)
        messages_layout.setSpacing(8)

        self.message_status_label = QLabel("No message sources configured.")
        self.message_status_label.setObjectName("pageSubtitle")
        self.message_status_label.setAccessibleName("Attention message status")
        self.message_status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.message_status_label.setWordWrap(True)
        messages_layout.addWidget(self.message_status_label)

        category_bar = QFrame()
        category_bar.setObjectName("messageCategoryBar")
        category_bar_layout = QHBoxLayout(category_bar)
        category_bar_layout.setContentsMargins(0, 0, 0, 0)
        category_bar_layout.setSpacing(5)
        self.message_category_group = QButtonGroup(self)
        self.message_category_group.setExclusive(True)
        self.message_category_selector = QToolButton(category_bar)
        self.message_category_selector.setObjectName("messageCategorySelector")
        self.message_category_selector.setText("Category: Primary")
        self.message_category_selector.setAccessibleName("Message category")
        self.message_category_selector.setToolTip("Choose a local message category")
        self.message_category_selector.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.message_category_menu = QMenu(self.message_category_selector)
        self.message_category_actions: dict[str, QAction] = {}
        category_bar_layout.addWidget(self.message_category_selector)
        for category in MESSAGE_CATEGORY_VALUES:
            # Keep the button-level contract for callers and tests while the
            # normal UI uses one compact category menu at narrow widths.
            button = QPushButton(MESSAGE_CATEGORY_LABELS[category], category_bar)
            button.setObjectName(f"messageCategory{category.title()}Button")
            button.setAccessibleName(f"Show {MESSAGE_CATEGORY_LABELS[category]} messages")
            button.setCheckable(True)
            button.setToolTip(f"Show {MESSAGE_CATEGORY_LABELS[category]} messages")
            button.clicked.connect(
                lambda _checked=False, selected=category: self.set_message_category(selected)
            )
            self.message_category_group.addButton(button)
            self.message_category_buttons[category] = button
            button.hide()
            action = self.message_category_menu.addAction(MESSAGE_CATEGORY_LABELS[category])
            action.setCheckable(True)
            action.setData(category)
            action.triggered.connect(
                lambda _checked=False, selected=category: self.set_message_category(selected)
            )
            self.message_category_actions[category] = action
        category_bar_layout.addStretch(1)
        self.message_category_buttons["primary"].setChecked(True)
        self.message_category_actions["primary"].setChecked(True)
        self.message_category_selector.setMenu(self.message_category_menu)
        self.message_category_status_label = QLabel("Primary: 0 messages")
        self.message_category_status_label.setObjectName("messageCategoryStatus")
        self.message_category_status_label.setAccessibleName("Selected message category status")
        self.message_category_status_label.setTextFormat(Qt.TextFormat.PlainText)
        category_bar_layout.addWidget(self.message_category_status_label)
        messages_layout.addWidget(category_bar)

        self.message_list = QListWidget()
        self.message_list.setObjectName("messageSummaryList")
        self.message_list.setAccessibleName("Selected message summaries")
        self.message_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.message_list.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.message_list.currentItemChanged.connect(self._message_selection_changed)
        self.message_list.itemDoubleClicked.connect(
            lambda _item: self._request_selected_message_body()
        )

        message_actions = QHBoxLayout()
        self.message_open_button = QPushButton("View body")
        self.message_open_button.setObjectName("messageOpenButton")
        self.message_open_button.setAccessibleName("View selected message body")
        self.message_open_button.setEnabled(False)
        self.message_open_button.clicked.connect(self._request_selected_message_body)
        message_actions.addWidget(self.message_open_button)
        self.message_provider_button = QPushButton("Open provider")
        self.message_provider_button.setObjectName("messageProviderButton")
        self.message_provider_button.setAccessibleName("Open selected message in provider")
        self.message_provider_button.setEnabled(False)
        self.message_provider_button.clicked.connect(self._request_selected_message_link)
        message_actions.addWidget(self.message_provider_button)
        self.message_clear_button = QPushButton("Clear summaries")
        self.message_clear_button.setObjectName("messageClearButton")
        self.message_clear_button.setAccessibleName("Clear retained message summaries")
        self.message_clear_button.clicked.connect(self.message_clear_requested.emit)
        message_actions.addWidget(self.message_clear_button)
        message_actions.addStretch(1)
        left_messages = QWidget()
        left_messages.setObjectName("messageListPane")
        left_messages.setAccessibleName("Message summary list and actions")
        left_layout = QVBoxLayout(left_messages)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)
        left_layout.addWidget(self.message_list, 1)
        left_layout.addLayout(message_actions)

        body_heading = QLabel("Message body (memory only)")
        body_heading.setObjectName("sectionTitle")
        body_heading.setAccessibleName("Memory-only message body heading")
        body_heading.setTextFormat(Qt.TextFormat.PlainText)
        self.message_body_view = QPlainTextEdit()
        self.message_body_view.setObjectName("messageBodyView")
        self.message_body_view.setAccessibleName("Sanitized memory-only message body")
        self.message_body_view.setReadOnly(True)
        self.message_body_view.setPlaceholderText(
            "Select a summary and choose View body. Remote content is never loaded."
        )
        self.message_body_view.setMaximumBlockCount(4_000)
        self.message_link_list = QListWidget()
        self.message_link_list.setObjectName("messageBodyLinks")
        self.message_link_list.setAccessibleName("Safe links in the selected message")
        self.message_link_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.message_link_list.itemDoubleClicked.connect(
            lambda item: self.message_deep_link_requested.emit(item.text())
        )
        right_messages = QWidget()
        right_messages.setObjectName("messageDetailPane")
        right_messages.setAccessibleName("Selected message details")
        right_layout = QVBoxLayout(right_messages)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)
        right_layout.addWidget(body_heading)
        right_layout.addWidget(self.message_body_view, 1)
        right_layout.addWidget(self.message_link_list)

        self.message_content_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.message_content_splitter.setObjectName("messageContentSplitter")
        self.message_content_splitter.setAccessibleName("Message summaries and selected body")
        self.message_content_splitter.setChildrenCollapsible(False)
        self.message_content_splitter.addWidget(left_messages)
        self.message_content_splitter.addWidget(right_messages)
        self.message_content_splitter.setStretchFactor(0, 1)
        self.message_content_splitter.setStretchFactor(1, 1)
        messages_layout.addWidget(self.message_content_splitter, 1)

        self.inbox_mode_tabs.addTab(alerts_page, "Alerts")
        self.inbox_mode_tabs.addTab(messages_page, "Messages")
        layout.addWidget(self.inbox_mode_tabs, 1)
        self._update_inbox_tab_labels()
        return page

    def _build_sources_page(self) -> QWidget:
        page, layout = self._new_page("sourcesPage", "Sources page")
        self._add_page_header(
            layout,
            "Sources",
            "Manage connected inputs and view their health.",
        )
        self.sources_status_label = QLabel("No sources connected.")
        self.sources_status_label.setObjectName("pageSubtitle")
        self.sources_status_label.setAccessibleName("Sources status")
        self.sources_status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.sources_status_label.setWordWrap(True)
        layout.addWidget(self.sources_status_label)
        self.sources_list = QListWidget()
        self.sources_list.setObjectName("sourcesList")
        self.sources_list.setAccessibleName("Configured sources")
        self.sources_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.sources_list.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.sources_list.currentItemChanged.connect(self._source_selection_changed)
        layout.addWidget(self.sources_list, 1)
        actions = QHBoxLayout()
        self.source_configure_button = QPushButton("Connect / Configure")
        self.source_configure_button.setObjectName("sourceConfigureButton")
        self.source_configure_button.setAccessibleName("Connect or configure selected platform")
        self.source_configure_button.clicked.connect(
            lambda: self._emit_selected_source(self.platform_configure_requested)
        )
        actions.addWidget(self.source_configure_button)
        self.source_refresh_button = QPushButton("Refresh")
        self.source_refresh_button.setObjectName("sourceRefreshButton")
        self.source_refresh_button.setAccessibleName("Refresh selected source")
        self.source_refresh_button.clicked.connect(
            lambda: self._emit_selected_source(self.source_refresh_requested)
        )
        actions.addWidget(self.source_refresh_button)
        self.source_reconnect_button = QPushButton("Reconnect")
        self.source_reconnect_button.setObjectName("sourceReconnectButton")
        self.source_reconnect_button.setAccessibleName("Reconnect selected source")
        self.source_reconnect_button.clicked.connect(
            lambda: self._emit_selected_source(self.source_reconnect_requested)
        )
        actions.addWidget(self.source_reconnect_button)
        self.source_enable_button = QPushButton("Disable")
        self.source_enable_button.setObjectName("sourceEnableButton")
        self.source_enable_button.setAccessibleName("Disable selected source")
        self.source_enable_button.clicked.connect(self._toggle_selected_source_enabled)
        actions.addWidget(self.source_enable_button)
        self.source_disconnect_button = QPushButton("Disconnect")
        self.source_disconnect_button.setObjectName("sourceDisconnectButton")
        self.source_disconnect_button.setAccessibleName("Disconnect selected source")
        self.source_disconnect_button.clicked.connect(
            lambda: self._emit_selected_source(self.source_disconnect_requested)
        )
        actions.addWidget(self.source_disconnect_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        self._source_selection_changed()
        return page

    def _build_settings_page(self) -> QWidget:
        page, layout = self._new_page(
            "settingsPage",
            "Settings page",
            scrollable=True,
        )
        self.settings_page = page
        self._add_page_header(
            layout,
            "Settings",
            "Manage display, privacy, and lifecycle preferences.",
        )
        self.settings_status_label = QLabel("Settings are ready for Hub-owned preferences.")
        self.settings_status_label.setObjectName("pageSubtitle")
        self.settings_status_label.setAccessibleName("Settings status")
        self.settings_status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.settings_status_label.setWordWrap(True)
        layout.addWidget(self.settings_status_label)
        self.appearance_panel = AppearancePanel()
        self.appearance_panel.changed.connect(self._request_appearance_save)
        layout.addWidget(self.appearance_panel)

        self.settings_values: dict[str, str] = {}
        self.settings_value_labels: dict[str, QLabel] = {}
        settings_card = QFrame()
        settings_card.setObjectName("todayCard")
        settings_card.setAccessibleName("Personal Hub settings summary")
        settings_layout = QVBoxLayout(settings_card)
        settings_layout.setContentsMargins(20, 16, 20, 16)
        settings_layout.setSpacing(0)
        for key, label in (
            ("offline_mode", "Offline mode"),
            ("privacy_masking", "Privacy masking"),
            ("close_to_tray", "Close to tray"),
            ("text_scale", "Text scale"),
            ("high_contrast", "High contrast"),
            ("reduced_motion", "Reduced motion"),
            ("canvas_remove_completed_assignments", "Completed Canvas assignments"),
            ("network_refresh_paused", "Network refresh"),
        ):
            row = QWidget()
            row.setObjectName("settingsRow")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 7, 0, 7)
            name_label = QLabel(label)
            name_label.setAccessibleName(f"{label} setting")
            name_label.setTextFormat(Qt.TextFormat.PlainText)
            value_label = QLabel()
            value_label.setObjectName("settingsValue")
            value_label.setAccessibleName(f"{label} value")
            value_label.setTextFormat(Qt.TextFormat.PlainText)
            value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            row_layout.addWidget(name_label)
            row_layout.addStretch(1)
            row_layout.addWidget(value_label)
            settings_layout.addWidget(row)
            self.settings_value_labels[key] = value_label
        layout.addWidget(settings_card)

        editor = QFrame()
        editor.setObjectName("todayCard")
        editor.setAccessibleName("Editable Personal Hub settings")
        editor_layout = QFormLayout(editor)
        editor_layout.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        editor_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        editor_layout.setContentsMargins(20, 16, 20, 16)
        editor_layout.setHorizontalSpacing(14)
        editor_layout.setVerticalSpacing(8)
        self.settings_checkboxes: dict[str, QCheckBox] = {}
        checkbox_fields = (
            ("offline_mode", "Offline mode"),
            ("privacy_masking", "Privacy masking"),
            ("close_to_tray", "Close to tray"),
            ("start_at_login", "Start at sign-in"),
            ("notifications_enabled", "Windows notifications"),
            ("quiet_hours_enabled", "Quiet hours"),
            ("digest_enabled", "Daily digest"),
            ("high_contrast", "High contrast"),
            ("reduced_motion", "Reduced motion"),
            (
                "canvas_remove_completed_assignments",
                "Remove completed Canvas assignments after due date",
            ),
        )
        for key, label in checkbox_fields:
            checkbox = QCheckBox(label)
            checkbox.setAccessibleName(f"{label} setting")
            editor_layout.addRow(checkbox)
            self.settings_checkboxes[key] = checkbox

        self.settings_inputs: dict[str, QLineEdit | TimezoneComboBox] = {}
        input_fields = (
            ("default_timezone", "Default timezone", "UTC"),
            ("quiet_hours_start", "Quiet hours start", "22:00"),
            ("quiet_hours_end", "Quiet hours end", "07:00"),
            ("digest_time", "Daily digest time", "08:00"),
            ("calendar_refresh_minutes", "Calendar refresh minutes", "15"),
            ("inbox_retention_days", "Inbox retention days", "90"),
            ("audit_retention_days", "Audit retention days", "30"),
            ("feed_retention_days", "Feed retention days", "30"),
            ("feed_refresh_minutes", "Feed refresh minutes", "30"),
            ("watch_retention_days", "Watch retention days", "90"),
            ("watch_refresh_minutes", "Watch refresh minutes", "60"),
            ("message_retention_days", "Message retention days (0 = session)", "7"),
            ("message_refresh_minutes", "Message refresh minutes", "10"),
        )
        for key, label, placeholder in input_fields:
            label_widget = QLabel(label)
            label_widget.setTextFormat(Qt.TextFormat.PlainText)
            label_widget.setAccessibleName(f"{label} label")
            label_widget.setWordWrap(True)
            if key == "default_timezone":
                input_widget = TimezoneComboBox()
                input_widget.setObjectName("defaultTimezoneInput")
            else:
                input_widget = QLineEdit()
                input_widget.setPlaceholderText(placeholder)
            input_widget.setAccessibleName(label)
            editor_layout.addRow(label_widget, input_widget)
            self.settings_inputs[key] = input_widget

        scale_label = QLabel("Text scale")
        scale_label.setTextFormat(Qt.TextFormat.PlainText)
        self.settings_text_scale_combo = QComboBox()
        self.settings_text_scale_combo.setAccessibleName("Text scale setting")
        for scale in (1.0, 1.25, 1.5, 1.75):
            self.settings_text_scale_combo.addItem(f"{scale:g}×", scale)
        editor_layout.addRow(scale_label, self.settings_text_scale_combo)

        settings_actions = QVBoxLayout()
        self.settings_save_button = QPushButton("Save settings")
        self.settings_save_button.setAccessibleName("Save Personal Hub settings")
        self.settings_save_button.clicked.connect(self._request_settings_save)
        settings_actions.addWidget(self.settings_save_button)
        self.settings_backup_button = QPushButton("Back up database")
        self.settings_backup_button.setAccessibleName("Back up Personal Hub database")
        self.settings_backup_button.clicked.connect(self.backup_requested.emit)
        settings_actions.addWidget(self.settings_backup_button)
        self.settings_restore_button = QPushButton("Restore database")
        self.settings_restore_button.setAccessibleName("Restore Personal Hub database")
        self.settings_restore_button.clicked.connect(self.restore_requested.emit)
        settings_actions.addWidget(self.settings_restore_button)
        self.settings_export_button = QPushButton("Export local data")
        self.settings_export_button.setAccessibleName(
            "Export Personal Hub local data without credentials"
        )
        self.settings_export_button.clicked.connect(self.export_requested.emit)
        settings_actions.addWidget(self.settings_export_button)
        editor_layout.addRow(settings_actions)
        layout.addWidget(editor)

        sync_card = QFrame()
        sync_card.setObjectName("todayCard")
        sync_card.setAccessibleName("Encrypted cross-device sync")
        sync_layout = QFormLayout(sync_card)
        sync_layout.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        sync_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        sync_layout.setContentsMargins(20, 16, 20, 16)
        sync_layout.setHorizontalSpacing(14)
        sync_layout.setVerticalSpacing(8)
        sync_title = QLabel("Sync across devices")
        sync_title.setObjectName("diagnosticsTitle")
        sync_title.setTextFormat(Qt.TextFormat.PlainText)
        sync_title.setAccessibleName("Encrypted sync heading")
        sync_layout.addRow(sync_title)
        sync_description = QLabel(
            "Use a shared folder from OneDrive, Dropbox, a network drive, or a USB device. "
            "Only encrypted Hub-owned state is published; credentials stay on each device."
        )
        sync_description.setObjectName("pageSubtitle")
        sync_description.setTextFormat(Qt.TextFormat.PlainText)
        sync_description.setWordWrap(True)
        sync_layout.addRow(sync_description)
        self.sync_status_label = QLabel("Not configured")
        self.sync_status_label.setObjectName("diagnosticsBody")
        self.sync_status_label.setAccessibleName("Encrypted sync status")
        self.sync_status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.sync_status_label.setWordWrap(True)
        sync_layout.addRow(QLabel("Status"), self.sync_status_label)
        self.sync_endpoint_input = QLineEdit()
        self.sync_endpoint_input.setPlaceholderText("Path to a shared sync folder")
        self.sync_endpoint_input.setAccessibleName("Shared sync folder path")
        sync_layout.addRow(QLabel("Shared folder"), self.sync_endpoint_input)
        self.sync_passphrase_input = QLineEdit()
        self.sync_passphrase_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.sync_passphrase_input.setPlaceholderText("Passphrase, never stored by Personal Hub")
        self.sync_passphrase_input.setAccessibleName("Personal Hub sync passphrase")
        sync_layout.addRow(QLabel("Passphrase"), self.sync_passphrase_input)
        sync_actions = QHBoxLayout()
        self.sync_configure_button = QPushButton("Save folder")
        self.sync_configure_button.setAccessibleName("Save encrypted sync folder")
        self.sync_configure_button.clicked.connect(self._request_sync_configure)
        sync_actions.addWidget(self.sync_configure_button)
        self.sync_button = QPushButton("Sync now")
        self.sync_button.setAccessibleName("Sync Personal Hub across devices")
        self.sync_button.clicked.connect(self._request_sync)
        sync_actions.addWidget(self.sync_button)
        self.sync_disable_button = QPushButton("Disable")
        self.sync_disable_button.setAccessibleName("Disable Personal Hub sync")
        self.sync_disable_button.clicked.connect(self.sync_disable_requested.emit)
        sync_actions.addWidget(self.sync_disable_button)
        sync_actions.addStretch(1)
        sync_layout.addRow(sync_actions)
        layout.addWidget(sync_card)

        self.diagnostics_view = QFrame()
        self.diagnostics_view.setObjectName("diagnosticsView")
        self.diagnostics_view.setAccessibleName("Diagnostics skeleton")
        self.diagnostics_panel = self.diagnostics_view
        diagnostics_layout = QVBoxLayout(self.diagnostics_view)
        diagnostics_layout.setContentsMargins(18, 16, 18, 16)
        diagnostics_layout.setSpacing(6)
        diagnostics_title = QLabel("Diagnostics")
        diagnostics_title.setObjectName("diagnosticsTitle")
        diagnostics_title.setAccessibleName("Diagnostics heading")
        diagnostics_title.setTextFormat(Qt.TextFormat.PlainText)
        diagnostics_layout.addWidget(diagnostics_title)
        self.diagnostics_status_label = QLabel("Ready")
        self.diagnostics_status_label.setObjectName("diagnosticsBody")
        self.diagnostics_status_label.setAccessibleName("Diagnostics status")
        self.diagnostics_status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.diagnostics_status_label.setWordWrap(True)
        diagnostics_layout.addWidget(self.diagnostics_status_label)
        self.diagnostics_list = QListWidget()
        self.diagnostics_list.setObjectName("diagnosticsList")
        self.diagnostics_list.setAccessibleName("Diagnostics entries")
        self.diagnostics_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.diagnostics_list.setMinimumHeight(140)
        diagnostics_layout.addWidget(self.diagnostics_list)
        self.diagnostics_page = self.diagnostics_view
        layout.addWidget(self.diagnostics_view, 1)
        layout.addStretch(1)
        self._render_settings_view(self._settings_view, apply_preferences=False)
        self._render_diagnostics_view(self._diagnostics_view)
        return page

    def _build_privacy_overlay(self, parent: QWidget) -> QFrame:
        overlay = QFrame(parent)
        overlay.setObjectName("privacyOverlay")
        overlay.setAccessibleName("Personal Hub privacy lock")
        overlay_layout = QVBoxLayout(overlay)
        overlay_layout.setContentsMargins(26, 26, 26, 26)
        overlay_layout.setSpacing(8)
        overlay_layout.addStretch(1)
        title = QLabel("Personal Hub is locked")
        title.setObjectName("privacyOverlayTitle")
        title.setAccessibleName("Privacy lock title")
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        overlay_layout.addWidget(title)
        body = QLabel("Sensitive previews are hidden until the Hub is unlocked.")
        body.setObjectName("privacyOverlayBody")
        body.setAccessibleName("Privacy lock details")
        body.setTextFormat(Qt.TextFormat.PlainText)
        body.setWordWrap(True)
        body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        overlay_layout.addWidget(body)
        overlay_layout.addStretch(1)
        overlay.hide()
        return overlay

    def _focus_first_available_control(self) -> None:
        self.today_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def _set_responsive_mode(self, compact: bool) -> None:
        compact = bool(compact)
        self._compact_navigation = compact
        self.brand_title.setText("Hub" if compact else APPLICATION_NAME)
        self.navigation_caption.setVisible(not compact)
        if hasattr(self, "today_controls_layout"):
            controls = (
                self.today_date_label,
                self.today_timezone_label,
                self.today_last_refresh_label,
                self.today_refresh_button,
                self.today_appearance_button,
            )
            for widget in controls:
                self.today_controls_layout.removeWidget(widget)
            positions = (
                ((0, 0), (1, 0), (2, 0), (0, 1), (1, 1))
                if compact
                else tuple((0, index) for index in range(len(controls)))
            )
            for widget, (row, column) in zip(controls, positions, strict=True):
                self.today_controls_layout.addWidget(widget, row, column)
            for column in range(5):
                self.today_controls_layout.setColumnStretch(column, 0)
            self.today_controls_layout.setColumnStretch(0 if compact else 2, 1)
        if compact:
            self.navigation.setMinimumWidth(164)
            self.navigation.setMaximumWidth(210)
            self.navigation_layout.setContentsMargins(12, 16, 12, 14)
            self.navigation_layout.setSpacing(3)
        else:
            self.navigation.setMinimumWidth(190)
            self.navigation.setMaximumWidth(260)
            self.navigation_layout.setContentsMargins(18, 22, 18, 18)
            self.navigation_layout.setSpacing(6)

    @property
    def compact_navigation(self) -> bool:
        return self._compact_navigation

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._set_responsive_mode(self.width() < self.RESPONSIVE_BREAKPOINT)
        if hasattr(self, "privacy_overlay"):
            self.privacy_overlay.setGeometry(self.pages.rect())

    def _set_active_destination(self, destination: str, *, emit: bool = True) -> bool:
        if destination == "Today":
            destination = HOME_DESTINATION
        if destination not in self.page_widgets:
            return False
        changed = destination != self._active_destination
        self._active_destination = destination
        self.pages.setCurrentWidget(self.page_widgets[destination])
        if changed and hasattr(self, "page_transition"):
            self.page_transition.start()
        self.navigation_buttons[destination].setChecked(True)
        self._update_status()
        if emit:
            self.destination_changed.emit(destination)
        return True

    def show_destination(self, destination: str) -> bool:
        """Select a visible primary destination without doing I/O."""

        return self._set_active_destination(_safe_text(destination, limit=64))

    set_active_destination = show_destination

    def _show_today(self) -> None:
        self._set_active_destination(TODAY_DESTINATION)

    @property
    def active_destination(self) -> str:
        return self._active_destination

    @property
    def current_destination(self) -> str:
        return self._active_destination

    def _update_status(self) -> None:
        modes: list[str] = [self._active_destination]
        if self._offline_mode:
            modes.append("offline")
        elif self._network_refresh_paused:
            modes.append("refresh paused")
        if self._locked:
            modes.append("locked")
        self.status_label.setText(" · ".join(modes))

    def show_from_tray(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def open_today_from_tray(self) -> None:
        self._set_active_destination(TODAY_DESTINATION)
        self.show_from_tray()

    def open_inbox_from_tray(self) -> None:
        self._set_active_destination("Inbox")
        self.show_from_tray()

    def _request_refresh(self) -> None:
        self.refresh_requested.emit()
        self.status_label.setText("Refresh requested · source work remains outside the UI thread")

    def _handle_tray_pause(self, paused: bool) -> None:
        self._set_network_refresh_paused(paused, sync_tray=False)

    def _handle_tray_offline(self, enabled: bool) -> None:
        self._set_offline_mode(enabled, sync_tray=False)

    def _set_network_refresh_paused(self, paused: bool, *, sync_tray: bool = True) -> None:
        paused = bool(paused)
        changed = paused != self._network_refresh_paused
        self._network_refresh_paused = paused
        if hasattr(self, "settings_values"):
            self.settings_values["network_refresh_paused"] = "Paused" if paused else "Active"
            if "network_refresh_paused" in self.settings_value_labels:
                self.settings_value_labels["network_refresh_paused"].setText(
                    self.settings_values["network_refresh_paused"]
                )
        if sync_tray:
            self.tray_controller.set_network_refresh_paused(paused, emit=False)
        self._update_status()
        if changed:
            self.network_pause_changed.emit(paused)

    def set_network_refresh_paused(self, paused: bool) -> None:
        self._set_network_refresh_paused(paused)

    set_network_paused = set_network_refresh_paused

    @property
    def network_refresh_paused(self) -> bool:
        return self._network_refresh_paused

    def _set_offline_mode(self, enabled: bool, *, sync_tray: bool = True) -> None:
        enabled = bool(enabled)
        changed = enabled != self._offline_mode
        self._offline_mode = enabled
        if hasattr(self, "settings_values"):
            self.settings_values["offline_mode"] = "On" if enabled else "Off"
            if "offline_mode" in self.settings_value_labels:
                self.settings_value_labels["offline_mode"].setText(
                    self.settings_values["offline_mode"]
                )
        if sync_tray:
            self.tray_controller.set_offline_mode(enabled, emit=False)
        self._render_mini_calendar()
        self._update_status()
        if changed:
            self.offline_mode_changed.emit(enabled)

    def set_offline_mode(self, enabled: bool) -> None:
        self._set_offline_mode(enabled)

    @property
    def offline_mode(self) -> bool:
        return self._offline_mode

    def request_quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        self.tray_controller.hide()
        self.quit_requested.emit()
        self.close()

    def set_close_to_tray(self, enabled: bool) -> None:
        self._close_to_tray = bool(enabled)
        if hasattr(self, "settings_values"):
            self.settings_values["close_to_tray"] = "On" if self._close_to_tray else "Off"
            if "close_to_tray" in self.settings_value_labels:
                self.settings_value_labels["close_to_tray"].setText(
                    self.settings_values["close_to_tray"]
                )

    @property
    def close_to_tray(self) -> bool:
        return self._close_to_tray

    def closeEvent(self, event: Any) -> None:
        if self._close_to_tray and not self._quitting:
            event.ignore()
            self.hide()
            self.closed_to_tray.emit()
            return
        self.tray_controller.hide()
        event.accept()

    def _display_text(self, value: str, *, sensitive: bool = False, fallback: str = "") -> str:
        if sensitive and (self._privacy_masked or self._locked):
            return "Hidden by privacy settings"
        return _safe_text(value, fallback=fallback)

    @property
    def inbox_mode(self) -> str:
        return self._inbox_mode_value

    def _inbox_mode_tab_changed(self, index: int) -> None:
        value = "messages" if index == 1 else "alerts"
        changed = value != self._inbox_mode_value
        self._inbox_mode_value = value
        if changed:
            self.inbox_mode_changed.emit(value)

    def show_inbox_mode(self, mode: str) -> bool:
        """Select the alert or message workflow without performing I/O."""

        normalized = _safe_text(mode, fallback="alerts", limit=24).casefold()
        index = {"alerts": 0, "messages": 1}.get(normalized)
        if index is None:
            return False
        self.inbox_mode_tabs.setCurrentIndex(index)
        return True

    def _update_inbox_tab_labels(self) -> None:
        if not hasattr(self, "inbox_mode_tabs"):
            return
        alert_count = len(self._inbox_items)
        alert_unread = sum(item.unread for item in self._inbox_items)
        message_count = len(self._message_all_summaries)
        message_unread = sum(summary.unread for summary in self._message_all_summaries)
        self.inbox_mode_tabs.setTabText(
            0,
            f"Alerts · {alert_unread} unread" if alert_count else "Alerts · 0",
        )
        self.inbox_mode_tabs.setTabToolTip(
            0,
            f"{alert_count} alert{'s' if alert_count != 1 else ''}, {alert_unread} unread",
        )
        self.inbox_mode_tabs.setTabText(
            1,
            f"Messages · {message_unread} unread" if message_count else "Messages · 0",
        )
        self.inbox_mode_tabs.setTabToolTip(
            1,
            f"{message_count} message{'s' if message_count != 1 else ''}, {message_unread} unread",
        )

    @property
    def attention_notification(self) -> AttentionNotificationView | None:
        return self._attention_notification

    def render_attention_notification(self, value: object | None) -> None:
        """Render one ephemeral, bounded notice above the page content."""

        if value is None:
            self.clear_attention_notification()
            return
        notice = _coerce_attention_notification(value)
        self._attention_notification = notice
        self.attention_notification_title.setText(
            self._display_text(notice.title, fallback="Attention messages")
        )
        self.attention_notification_body.setText(
            self._display_text(
                notice.summary,
                sensitive=notice.sensitive,
                fallback="Unread attention messages are ready to review.",
            )
        )
        self.attention_notification_open_button.setText(
            _safe_text(notice.action_label, fallback="Review messages", limit=64)
        )
        enabled = not self._locked
        self.attention_notification_open_button.setEnabled(enabled)
        self.attention_notification_dismiss_button.setEnabled(enabled)
        self.attention_notification_panel.show()

    show_attention_notification = render_attention_notification
    render_attention_notice = render_attention_notification
    show_attention_notice = render_attention_notification

    def clear_attention_notification(self) -> None:
        self._attention_notification = None
        self.attention_notification_panel.hide()
        self.attention_notification_body.clear()
        self.attention_notification_open_button.setEnabled(False)
        self.attention_notification_dismiss_button.setEnabled(False)

    clear_attention_notice = clear_attention_notification

    def _open_attention_notification(self) -> None:
        notice = self._attention_notification
        if notice is None or self._locked:
            return
        self.attention_notification_open_requested.emit(notice)
        self.clear_attention_notification()
        self.show_destination("Inbox")
        self.show_inbox_mode("messages")

    def _dismiss_attention_notification(self) -> None:
        notice = self._attention_notification
        if notice is None or self._locked:
            return
        self.attention_notification_dismissed.emit(notice)
        self.clear_attention_notification()

    @property
    def inbox_items(self) -> tuple[InboxItemView, ...]:
        return self._inbox_items

    def render_inbox(self, items: Iterable[InboxItemView | Mapping[str, object]] | None) -> None:
        """Render bounded Inbox view data without fetching or mutating anything."""

        if isinstance(items, Mapping) and "items" in items:
            items = items["items"]  # type: ignore[assignment]
        selected = self._selected_inbox_item()
        selected_id = None if selected is None else selected.item_id
        self._inbox_items = tuple(
            _coerce_inbox_item(value, index) for index, value in enumerate(_iter_values(items))
        )
        self.inbox_list.clear()
        if not self._inbox_items:
            self.inbox_status_label.setText("No attention items yet.")
            self._inbox_selection_changed(None, None)
            self._update_inbox_tab_labels()
            return
        unread_count = sum(item.unread for item in self._inbox_items)
        self.inbox_status_label.setText(
            f"{len(self._inbox_items)} item{'s' if len(self._inbox_items) != 1 else ''} · "
            f"{unread_count} unread"
        )
        selected_row = -1
        for index, item in enumerate(self._inbox_items):
            title = self._display_text(
                item.title,
                sensitive=item.sensitive,
                fallback="Attention item",
            )
            details: list[str] = []
            summary = self._display_text(item.summary, sensitive=item.sensitive)
            if summary:
                details.append(summary)
            if item.source and not (item.sensitive and (self._privacy_masked or self._locked)):
                details.append(item.source)
            if item.timestamp:
                details.append(item.timestamp)
            prefix = "● " if item.unread else "○ "
            rendered = prefix + title
            if details:
                rendered += " — " + " · ".join(details)
            list_item = QListWidgetItem(rendered)
            list_item.setData(Qt.ItemDataRole.UserRole, item.item_id)
            list_item.setToolTip(rendered)
            self.inbox_list.addItem(list_item)
            row = _InboxRowWidget(item, title, " · ".join(details))
            self.inbox_list.setItemWidget(list_item, row)
            list_item.setSizeHint(row.sizeHint())
            if item.item_id == selected_id:
                selected_row = index
        if selected_row >= 0:
            self.inbox_list.setCurrentRow(selected_row)
        self._inbox_selection_changed(None, None)
        self._update_inbox_tab_labels()

    update_inbox = render_inbox
    set_inbox_items = render_inbox

    def _selected_inbox_item(self) -> InboxItemView | None:
        row = self.inbox_list.currentRow() if hasattr(self, "inbox_list") else -1
        if 0 <= row < len(self._inbox_items):
            return self._inbox_items[row]
        return None

    def _inbox_selection_changed(self, _current: object, _previous: object) -> None:
        selected = self._selected_inbox_item()
        enabled = selected is not None and not self._locked
        self.inbox_read_button.setEnabled(enabled)
        self.inbox_dismiss_button.setEnabled(enabled and selected.state != "dismissed")
        self.inbox_archive_button.setEnabled(enabled and selected.state != "archived")
        self.inbox_read_button.setText(
            "Mark unread" if selected is not None and selected.state == "read" else "Mark read"
        )

    def _request_selected_inbox_state(self, state: str) -> None:
        selected = self._selected_inbox_item()
        if selected is not None and not self._locked:
            self.inbox_state_changed.emit((selected.item_id, state))

    def _toggle_selected_inbox_read(self) -> None:
        selected = self._selected_inbox_item()
        if selected is not None:
            self._request_selected_inbox_state("unread" if selected.state == "read" else "read")

    def _selected_message(self) -> MessageSummaryView | None:
        row = self.message_list.currentRow()
        if 0 <= row < len(self._message_summaries):
            return self._message_summaries[row]
        return None

    def _message_selection_changed(self, _current: object, _previous: object) -> None:
        selected = self._selected_message()
        self.message_open_button.setEnabled(selected is not None and not self._locked)
        self.message_provider_button.setEnabled(
            selected is not None and bool(selected.deep_link) and not self._locked
        )

    def _request_selected_message_body(self) -> None:
        selected = self._selected_message()
        if selected is not None and not self._locked:
            self.message_open_requested.emit(selected)

    def _request_selected_message_link(self) -> None:
        selected = self._selected_message()
        if selected is not None and selected.deep_link and not self._locked:
            self.message_deep_link_requested.emit(selected.deep_link)

    @property
    def messages_view(self) -> MessagesView:
        return self._messages_view

    @property
    def message_body(self) -> MessageBodyView | None:
        return self._message_body

    @property
    def message_category(self) -> str:
        return self._message_category_value

    def set_message_category(self, category: str) -> bool:
        """Switch the local message view without refreshing or mutating a provider."""

        normalized = _safe_text(category, limit=32).casefold()
        if not is_message_category(normalized):
            return False
        changed = normalized != self._message_category_value
        self._message_category_value = normalized
        self._messages_view = replace(self._messages_view, selected_category=normalized)
        self._render_message_rows()
        if changed:
            self.message_category_changed.emit(normalized)
        return True

    set_message_filter = set_message_category

    def _message_category_counts(self) -> tuple[tuple[str, int], ...]:
        counts = {category: 0 for category in MESSAGE_CATEGORY_VALUES}
        for summary in self._message_all_summaries:
            category = summary.category if is_message_category(summary.category) else "primary"
            counts[category] += 1
        return tuple((category, counts[category]) for category in MESSAGE_CATEGORY_VALUES)

    def _render_message_rows(self) -> None:
        selected = self._selected_message()
        selected_key = None if selected is None else (selected.source_id, selected.message_id)
        self._message_summaries = tuple(
            summary
            for summary in self._message_all_summaries
            if (summary.category if is_message_category(summary.category) else "primary")
            == self._message_category_value
        )
        self.message_list.clear()
        selected_row = -1
        for index, summary in enumerate(self._message_summaries):
            title = self._display_text(
                summary.title,
                sensitive=summary.sensitive,
                fallback="Message",
            )
            sender = self._display_text(summary.sender, sensitive=summary.sensitive)
            preview = self._display_text(summary.preview, sensitive=summary.sensitive)
            details = [value for value in (sender, preview) if value]
            if summary.received_at:
                details.append(_safe_text(str(summary.received_at), limit=64))
            prefix = "● " if summary.unread else "○ "
            if summary.importance == "important":
                prefix = "! " + prefix
            rendered = prefix + title
            if details:
                rendered += " — " + " · ".join(details)
            item = QListWidgetItem(rendered)
            item.setData(
                Qt.ItemDataRole.UserRole,
                (summary.source_id, summary.message_id),
            )
            item.setToolTip(rendered)
            self.message_list.addItem(item)
            row = _MessageSummaryRowWidget(summary, title, " · ".join(details))
            self.message_list.setItemWidget(item, row)
            item.setSizeHint(row.sizeHint())
            if selected_key == (summary.source_id, summary.message_id):
                selected_row = index
        if selected_row >= 0:
            self.message_list.setCurrentRow(selected_row)
        self._message_selection_changed(None, None)

        counts = dict(self._message_category_counts())
        selected_count = counts[self._message_category_value]
        unread_count = sum(
            summary.unread
            for summary in self._message_all_summaries
            if (summary.category if is_message_category(summary.category) else "primary")
            == self._message_category_value
        )
        label = message_category_label(self._message_category_value)
        selected_label = label
        self.message_category_status_label.setText(
            f"{label}: {selected_count} message{'s' if selected_count != 1 else ''} · "
            f"{unread_count} unread"
        )
        for category, button in self.message_category_buttons.items():
            total = counts[category]
            label = MESSAGE_CATEGORY_LABELS[category]
            button.setText(label)
            button.setChecked(category == self._message_category_value)
            button.setToolTip(f"Show {MESSAGE_CATEGORY_LABELS[category]} messages ({total})")
            action = self.message_category_actions.get(category)
            if action is not None:
                action.setText(f"{label} · {total}")
                action.setChecked(category == self._message_category_value)
        selected_total = counts[self._message_category_value]
        self.message_category_selector.setText(f"Category: {selected_label} · {selected_total}")
        self.message_category_selector.setAccessibleName(f"Message category: {selected_label}")
        self._update_inbox_tab_labels()

    def render_messages(self, view: MessagesView) -> None:
        """Render retained summaries without triggering provider or body access."""

        if not isinstance(view, MessagesView):
            raise TypeError("message rendering expects MessagesView")
        category = (
            view.selected_category if is_message_category(view.selected_category) else "primary"
        )
        self._messages_view = replace(view, selected_category=category)
        self._message_sources = tuple(view.sources[:100])
        self._message_all_summaries = tuple(view.summaries[:200])
        self._message_category_value = category
        counts = self._message_category_counts()
        self._messages_view = replace(self._messages_view, category_counts=counts)
        self._render_message_rows()
        count = len(self._message_summaries)
        source_count = len(self._message_sources)
        status = _safe_text(view.freshness, fallback="ready", limit=48)
        detail = _safe_text(view.detail)
        summary_label = "summary" if count == 1 else "summaries"
        self.message_status_label.setText(
            f"{count} {summary_label} in {message_category_label(category)} from "
            f"{source_count} configured source{'s' if source_count != 1 else ''} · {status}"
            + (f" — {detail}" if detail else "")
        )
        self.message_clear_button.setEnabled(bool(self._message_all_summaries) and not self._locked)

    def render_message_body(self, body: MessageBodyView | None) -> None:
        """Display only sanitized plain text and inert HTTP(S) link labels."""

        if body is not None and not isinstance(body, MessageBodyView):
            raise TypeError("message body rendering expects MessageBodyView")
        self._message_body = body
        self.message_body_view.clear()
        self.message_link_list.clear()
        if body is None:
            return
        hidden = body.sensitive and (self._privacy_masked or self._locked)
        self.message_body_view.setPlainText(
            "Hidden by privacy settings" if hidden else _safe_text(body.plain_text, limit=100_000)
        )
        if hidden:
            return
        for link in body.links[:50]:
            parts = urlsplit(link)
            if parts.scheme.casefold() in {"http", "https"} and parts.hostname:
                self.message_link_list.addItem(link)

    update_messages = render_messages
    set_messages = render_messages

    def clear_message_body(self) -> None:
        self.render_message_body(None)

    @property
    def sources(self) -> tuple[SourceView, ...]:
        return self._sources

    def render_sources(self, sources: Iterable[SourceView | Mapping[str, object]] | None) -> None:
        """Render safe source health summaries."""

        if isinstance(sources, Mapping) and "sources" in sources:
            sources = sources["sources"]  # type: ignore[assignment]
        selected_id = self._selected_source_id()
        self._sources = tuple(
            _coerce_source(value, index) for index, value in enumerate(_iter_values(sources))
        )
        self.sources_list.clear()
        if not self._sources:
            self.sources_status_label.setText("No sources connected.")
            self._source_selection_changed()
            return
        self.sources_status_label.setText(
            f"{len(self._sources)} source{'s' if len(self._sources) != 1 else ''} configured."
        )
        for source in self._sources:
            name = self._display_text(
                source.display_name,
                sensitive=source.sensitive,
                fallback="Source",
            )
            status = _safe_text(source.status, fallback="unknown", limit=48)
            detail = self._display_text(source.detail, sensitive=source.sensitive)
            rendered = f"{name} · {status}"
            if detail:
                rendered += f" — {detail}"
            if source.last_success_at:
                rendered += f" · Last success: {source.last_success_at}"
            item = QListWidgetItem(rendered)
            item.setData(Qt.ItemDataRole.UserRole, source.source_id)
            item.setToolTip(rendered)
            self.sources_list.addItem(item)
            if source.source_id == selected_id:
                self.sources_list.setCurrentItem(item)
        if self.sources_list.currentRow() < 0:
            self.sources_list.setCurrentRow(0)
        self._source_selection_changed()

    def _selected_source_id(self) -> str | None:
        item = self.sources_list.currentItem() if hasattr(self, "sources_list") else None
        value = None if item is None else item.data(Qt.ItemDataRole.UserRole)
        return value if isinstance(value, str) else None

    def _selected_source(self) -> SourceView | None:
        source_id = self._selected_source_id()
        return next((source for source in self._sources if source.source_id == source_id), None)

    def _source_selection_changed(self, *_args: object) -> None:
        source = self._selected_source()
        enabled = source is not None and not self._locked
        for button in (
            self.source_configure_button,
            self.source_refresh_button,
            self.source_reconnect_button,
            self.source_enable_button,
            self.source_disconnect_button,
        ):
            button.setEnabled(enabled)
        if source is not None:
            is_platform = source.source_id.startswith("platform-")
            self.source_configure_button.setEnabled(enabled and is_platform)
            self.source_enable_button.setText("Disable" if source.enabled else "Enable")
            self.source_enable_button.setAccessibleName(
                f"{'Disable' if source.enabled else 'Enable'} selected source"
            )
            self.source_refresh_button.setEnabled(enabled and source.enabled)

    def set_sources_status(self, message: str) -> None:
        self.sources_status_label.setText(_safe_text(message, fallback="Sources are ready."))

    def set_refresh_busy(self, busy: bool) -> None:
        """Show one stable refresh state without opening transient windows."""

        enabled = not busy and not self._locked
        self.today_refresh_button.setEnabled(enabled)
        self.news_refresh_button.setEnabled(enabled)
        label = "Refreshing…" if busy else "Refresh"
        self.today_refresh_button.setText(label)
        self.news_refresh_button.setText(label)

    def _emit_selected_source(self, signal: object) -> None:
        source_id = self._selected_source_id()
        emitter = getattr(signal, "emit", None)
        if source_id is not None and callable(emitter):
            emitter(source_id)

    def _toggle_selected_source_enabled(self) -> None:
        source = self._selected_source()
        if source is not None:
            self.source_enabled_changed.emit((source.source_id, not source.enabled))

    update_sources = render_sources
    set_sources = render_sources

    def _request_sync_configure(self) -> None:
        endpoint = self.sync_endpoint_input.text().strip()
        if endpoint:
            self.sync_configure_requested.emit(endpoint)

    def _request_sync(self) -> None:
        passphrase = self.sync_passphrase_input.text()
        if passphrase:
            self.sync_requested.emit(passphrase)

    def clear_sync_passphrase(self) -> None:
        if hasattr(self, "sync_passphrase_input"):
            self.sync_passphrase_input.clear()

    def render_sync_status(self, status: SyncView | Mapping[str, object] | None) -> None:
        view = _coerce_sync(status)
        self._sync_view = view
        state_labels = {
            "not_configured": "Not configured",
            "disabled": "Disabled",
            "ready": "Ready to sync",
            "error": "Needs attention",
        }
        state = state_labels.get(view.state, "Unavailable")
        detail = view.last_error or (
            f"{view.record_count} record(s) · {view.remote_device_count} other device(s)"
            if view.last_sync_at
            else "Choose a shared folder and sync passphrase."
        )
        if view.last_sync_at:
            detail = f"{detail} · Last sync {view.last_sync_at}"
        self.sync_status_label.setText(f"{state} — {detail}")
        if view.endpoint and not self.sync_endpoint_input.hasFocus():
            self.sync_endpoint_input.setText(view.endpoint)
        configured = bool(view.endpoint)
        self.sync_button.setEnabled(configured and view.enabled)
        self.sync_disable_button.setEnabled(configured and view.enabled)

    def sync_view(self) -> SyncView:
        return self._sync_view

    def _request_settings_save(self) -> None:
        values: dict[str, object] = {
            key: checkbox.isChecked() for key, checkbox in self.settings_checkboxes.items()
        }
        values.update(
            {
                "default_timezone": self.settings_inputs["default_timezone"].timezone(),
                "notification_quiet_hours_start": self.settings_inputs["quiet_hours_start"].text(),
                "notification_quiet_hours_end": self.settings_inputs["quiet_hours_end"].text(),
                "digest_time": self.settings_inputs["digest_time"].text(),
                "calendar_refresh_minutes": self.settings_inputs["calendar_refresh_minutes"].text(),
                "inbox_retention_days": self.settings_inputs["inbox_retention_days"].text(),
                "audit_retention_days": self.settings_inputs["audit_retention_days"].text(),
                "feed_retention_days": self.settings_inputs["feed_retention_days"].text(),
                "feed_refresh_minutes": self.settings_inputs["feed_refresh_minutes"].text(),
                "watch_observation_retention_days": self.settings_inputs[
                    "watch_retention_days"
                ].text(),
                "watch_refresh_minutes": self.settings_inputs["watch_refresh_minutes"].text(),
                "message_summary_retention_days": self.settings_inputs[
                    "message_retention_days"
                ].text(),
                "message_refresh_minutes": self.settings_inputs["message_refresh_minutes"].text(),
                "ui_text_scale": self.settings_text_scale_combo.currentData(),
                "ui_high_contrast": values.pop("high_contrast"),
                "ui_reduced_motion": values.pop("reduced_motion"),
                "notification_quiet_hours_enabled": values.pop("quiet_hours_enabled"),
            }
        )
        self.settings_save_requested.emit(values)

    def set_settings_status(self, message: str) -> None:
        self.settings_status_label.setText(_safe_text(message, fallback="Settings unchanged."))

    def _request_appearance_save(self, values: object) -> None:
        if isinstance(values, Mapping):
            self.settings_save_requested.emit(dict(values))

    def _render_settings_view(self, view: SettingsView, *, apply_preferences: bool) -> None:
        self._settings_view = view
        self._appearance = {
            "ui_theme": view.ui_theme,
            "ui_accent_color": view.ui_accent_color,
            "ui_background_color": view.ui_background_color,
            "ui_surface_color": view.ui_surface_color,
        }
        self.appearance_panel.set_preferences(
            {**self._appearance, "ui_reduced_motion": view.reduced_motion}
        )
        values = {
            "offline_mode": "On" if view.offline_mode else "Off",
            "privacy_masking": "On" if view.privacy_masking else "Off",
            "close_to_tray": "On" if view.close_to_tray else "Off",
            "text_scale": f"{max(1.0, min(view.text_scale, 1.75)):.2g}×",
            "high_contrast": "On" if view.high_contrast else "Off",
            "reduced_motion": "On" if view.reduced_motion else "Off",
            "canvas_remove_completed_assignments": (
                "On" if view.canvas_remove_completed_assignments else "Off"
            ),
            "network_refresh_paused": "Paused" if view.network_refresh_paused else "Active",
        }
        self.settings_values = values
        for key, label in self.settings_value_labels.items():
            label.setText(values.get(key, "Not set"))
        self.settings_status_label.setText(
            "Hub-owned settings are ready. Changes apply only after Save settings."
        )
        if hasattr(self, "settings_checkboxes"):
            checkbox_values = {
                "offline_mode": view.offline_mode,
                "privacy_masking": view.privacy_masking,
                "close_to_tray": view.close_to_tray,
                "start_at_login": view.start_at_login,
                "notifications_enabled": view.notifications_enabled,
                "quiet_hours_enabled": view.quiet_hours_enabled,
                "digest_enabled": view.digest_enabled,
                "high_contrast": view.high_contrast,
                "reduced_motion": view.reduced_motion,
                "canvas_remove_completed_assignments": view.canvas_remove_completed_assignments,
            }
            for key, checked in checkbox_values.items():
                self.settings_checkboxes[key].setChecked(checked)
            input_values = {
                "default_timezone": view.default_timezone,
                "quiet_hours_start": view.quiet_hours_start,
                "quiet_hours_end": view.quiet_hours_end,
                "digest_time": view.digest_time,
                "calendar_refresh_minutes": view.calendar_refresh_minutes,
                "inbox_retention_days": view.inbox_retention_days,
                "audit_retention_days": view.audit_retention_days,
                "feed_retention_days": view.feed_retention_days,
                "feed_refresh_minutes": view.feed_refresh_minutes,
                "watch_retention_days": view.watch_retention_days,
                "watch_refresh_minutes": view.watch_refresh_minutes,
                "message_retention_days": view.message_retention_days,
                "message_refresh_minutes": view.message_refresh_minutes,
            }
            for key, value in input_values.items():
                input_widget = self.settings_inputs[key]
                if isinstance(input_widget, TimezoneComboBox):
                    input_widget.set_timezone(value)
                else:
                    input_widget.setText(str(value))
            scale_index = self.settings_text_scale_combo.findData(
                max(1.0, min(view.text_scale, 1.75))
            )
            if scale_index >= 0:
                self.settings_text_scale_combo.setCurrentIndex(scale_index)
        if apply_preferences:
            self.set_close_to_tray(view.close_to_tray)
            self._set_network_refresh_paused(view.network_refresh_paused)
            self._set_offline_mode(view.offline_mode)
            self.set_privacy_masked(view.privacy_masking)
            self.apply_accessibility_preferences(
                text_scale=view.text_scale,
                high_contrast=view.high_contrast,
                reduced_motion=view.reduced_motion,
            )

    def render_settings(self, settings: SettingsView | Mapping[str, object] | None) -> None:
        """Render settings view data and apply display/lifecycle hooks."""

        self._render_settings_view(_coerce_settings(settings), apply_preferences=True)

    update_settings = render_settings

    @property
    def settings_view(self) -> SettingsView:
        return self._settings_view

    def _render_diagnostics_view(self, view: DiagnosticsView) -> None:
        self._diagnostics_view = view
        status = _safe_text(view.status, fallback="ready", limit=48)
        detail = _safe_text(view.detail)
        generated = f" · {view.generated_at}" if view.generated_at else ""
        self.diagnostics_status_label.setText(
            f"{status}{generated}" + (f" — {detail}" if detail else "")
        )
        self.diagnostics_list.clear()
        for entry in view.entries:
            self.diagnostics_list.addItem(QListWidgetItem(_safe_text(entry, limit=360)))
        for failure in view.source_failures:
            name = failure.display_name or failure.source_id
            text = f"{_safe_text(name, limit=180)}: {_safe_text(failure.message)}"
            self.diagnostics_list.addItem(QListWidgetItem(text))
        if self.diagnostics_list.count() == 0:
            self.diagnostics_list.addItem(QListWidgetItem("No diagnostic events recorded."))

    def render_diagnostics(
        self, diagnostics: DiagnosticsView | Mapping[str, object] | None
    ) -> None:
        """Render safe diagnostics data in the Settings skeleton."""

        self._render_diagnostics_view(_coerce_diagnostics(diagnostics))

    update_diagnostics = render_diagnostics

    @property
    def diagnostics_data(self) -> DiagnosticsView:
        return self._diagnostics_view

    def _render_source_failures(self) -> None:
        if not self._source_failures:
            self.source_failure_panel.hide()
            self.source_failure_body.clear()
            return
        lines: list[str] = []
        for failure in self._source_failures.values():
            name = failure.display_name or failure.source_id
            line = f"{_safe_text(name, limit=180)}: {_safe_text(failure.message)}"
            if failure.last_success_at:
                line += f" (last success: {_safe_text(failure.last_success_at, limit=120)})"
            if failure.retryable:
                line += " · retry available"
            lines.append(line)
        self.source_failure_body.setText("\n".join(lines))
        self.source_failure_panel.show()

    def _receive_source_failure(self, failure: SourceFailureView) -> None:
        self._source_failures[failure.source_id] = failure
        self.source_failure_received.emit(failure)
        self._render_source_failures()

    def render_source_failure(
        self,
        source: SourceFailureView | Mapping[str, object] | str,
        message: str | None = None,
        *,
        retryable: bool = True,
        last_success_at: str | None = None,
    ) -> None:
        """Display a bounded failure immediately without blocking the UI."""

        self._receive_source_failure(
            _coerce_failure(
                source,
                message,
                retryable=retryable,
                last_success_at=last_success_at,
            )
        )

    show_source_failure = render_source_failure
    display_source_failure = render_source_failure

    def post_source_failure(
        self,
        source: SourceFailureView | Mapping[str, object] | str,
        message: str | None = None,
        *,
        retryable: bool = True,
        last_success_at: str | None = None,
    ) -> None:
        """Queue a source failure for the GUI thread and return immediately."""

        failure = _coerce_failure(
            source,
            message,
            retryable=retryable,
            last_success_at=last_success_at,
        )
        self._ui_bridge.source_failure.emit(failure)

    report_source_failure = post_source_failure

    def clear_source_failures(self, source_id: str | None = None) -> None:
        if source_id is None:
            self._source_failures.clear()
        else:
            self._source_failures.pop(_safe_text(source_id, limit=_MAX_ID_LENGTH), None)
        self._render_source_failures()

    @property
    def source_failures(self) -> tuple[SourceFailureView, ...]:
        return tuple(self._source_failures.values())

    def set_privacy_masked(self, masked: bool) -> None:
        self._privacy_masked = bool(masked)
        if hasattr(self, "settings_values"):
            self.settings_values["privacy_masking"] = "On" if self._privacy_masked else "Off"
            if "privacy_masking" in self.settings_value_labels:
                self.settings_value_labels["privacy_masking"].setText(
                    self.settings_values["privacy_masking"]
                )
        self._render_inbox_from_state()
        self._render_sources_from_state()
        if hasattr(self, "today_sections_layout"):
            self._render_today_sections()
        self._render_tasks_from_state()
        self._render_calendar_from_state()
        self._render_news_from_state()
        self._render_web_watches_from_state()
        self._render_messages_from_state()
        self._render_attention_notification_from_state()
        self._update_status()

    set_privacy_masking = set_privacy_masked

    @property
    def privacy_masked(self) -> bool:
        return self._privacy_masked

    def set_locked(self, locked: bool) -> None:
        self._locked = bool(locked)
        self.pages.setEnabled(not self._locked)
        if self._locked:
            self.privacy_overlay.setGeometry(self.pages.rect())
            self.privacy_overlay.show()
            self.privacy_overlay.raise_()
        else:
            self.privacy_overlay.hide()
        self._render_inbox_from_state()
        self._render_sources_from_state()
        if hasattr(self, "today_sections_layout"):
            self._render_today_sections()
        self._render_tasks_from_state()
        self._render_calendar_from_state()
        self._render_news_from_state()
        self._render_web_watches_from_state()
        if self._locked:
            self.clear_message_body()
        self._render_messages_from_state()
        self._render_attention_notification_from_state()
        self._update_status()

    set_lock_state = set_locked
    set_locked_state = set_locked

    @property
    def locked(self) -> bool:
        return self._locked

    def set_privacy_state(self, *, masked: bool | None = None, locked: bool | None = None) -> None:
        if masked is not None:
            self.set_privacy_masked(masked)
        if locked is not None:
            self.set_locked(locked)

    def _render_inbox_from_state(self) -> None:
        if hasattr(self, "inbox_list"):
            self.render_inbox(self._inbox_items)

    def _render_sources_from_state(self) -> None:
        if hasattr(self, "sources_list"):
            self.render_sources(self._sources)

    def _render_news_from_state(self) -> None:
        if hasattr(self, "news_entries_list"):
            self._render_news_sources()
            self._render_news_entries()
            self._render_news_today_section()

    def _render_web_watches_from_state(self) -> None:
        if hasattr(self, "watch_list"):
            self.render_web_watches(self._web_watches_view)

    def _render_messages_from_state(self) -> None:
        if hasattr(self, "message_list"):
            self.render_messages(self._messages_view)
            if self._message_body is not None:
                self.render_message_body(self._message_body)

    def _render_attention_notification_from_state(self) -> None:
        if hasattr(self, "attention_notification_panel"):
            self.render_attention_notification(self._attention_notification)

    def apply_accessibility_preferences(
        self,
        *,
        text_scale: float | None = None,
        high_contrast: bool | None = None,
        reduced_motion: bool | None = None,
    ) -> None:
        """Apply Hub display preferences and immediately honor reduced motion."""

        if text_scale is not None:
            try:
                self._text_scale = max(1.0, min(float(text_scale), 1.75))
            except (TypeError, ValueError):
                self._text_scale = 1.0
        if high_contrast is not None:
            self._high_contrast = bool(high_contrast)
        if reduced_motion is not None:
            self._reduced_motion = bool(reduced_motion)
        if hasattr(self, "settings_values"):
            self.settings_values.update(
                {
                    "text_scale": f"{self._text_scale:.2g}×",
                    "high_contrast": "On" if self._high_contrast else "Off",
                    "reduced_motion": "On" if self._reduced_motion else "Off",
                }
            )
            for key in ("text_scale", "high_contrast", "reduced_motion"):
                if key in self.settings_value_labels:
                    self.settings_value_labels[key].setText(self.settings_values[key])
        rendered = _accessible_stylesheet(
            theme_stylesheet(
                _BASE_STYLESHEET,
                theme=self._appearance["ui_theme"],
                accent_color=self._appearance["ui_accent_color"],
                background_color=self._appearance["ui_background_color"],
                surface_color=self._appearance["ui_surface_color"],
                high_contrast=self._high_contrast,
            ),
            text_scale=self._text_scale,
            high_contrast=self._high_contrast,
        )
        application = QApplication.instance()
        if application is not None and application.styleSheet() != rendered:
            application.setStyleSheet(rendered)
        if self.styleSheet() != rendered:
            self.setStyleSheet(rendered)
        self._refresh_mini_calendar_geometry()
        # Qt polishes the native calendar's internal table on the next event turn.
        # Measure again after its font inherits a new text scale.
        QTimer.singleShot(0, self._refresh_mini_calendar_geometry)
        self.dashboard.set_reduced_motion(self._reduced_motion)
        self.dashboard.refresh_geometry()
        self.appearance_panel.set_high_contrast(self._high_contrast)
        if hasattr(self, "page_transition"):
            self.page_transition.set_reduced_motion(self._reduced_motion)

    set_accessibility_preferences = apply_accessibility_preferences

    def _refresh_mini_calendar_geometry(self) -> None:
        self.mini_calendar.refresh_style()
        self.today_section_widgets["mini_calendar"].layout().invalidate()
        self.dashboard.refresh_geometry()

    def set_text_scale(self, scale: float) -> None:
        self.apply_accessibility_preferences(text_scale=scale)

    @property
    def text_scale(self) -> float:
        return self._text_scale

    def set_high_contrast(self, enabled: bool) -> None:
        self.apply_accessibility_preferences(high_contrast=enabled)

    @property
    def high_contrast(self) -> bool:
        return self._high_contrast

    def set_reduced_motion(self, enabled: bool) -> None:
        self.apply_accessibility_preferences(reduced_motion=enabled)

    @property
    def reduced_motion(self) -> bool:
        return self._reduced_motion


PersonalHubWindow = PersonalHubMainWindow
MainWindow = PersonalHubMainWindow


def create_main_window(
    parent: QWidget | None = None,
    *,
    close_to_tray: bool = True,
    show_tray: bool = False,
    settings: SettingsView | Mapping[str, object] | None = None,
) -> PersonalHubMainWindow:
    """Create a Hub window, initializing Qt when needed.

    The optional settings mapping is view data only; persistence and lifecycle
    remain with the Hub composition root.
    """

    create_application()
    window = PersonalHubMainWindow(
        parent,
        close_to_tray=close_to_tray,
        show_tray=show_tray,
    )
    if settings is not None:
        window.render_settings(settings)
    return window


def run_desktop(
    argv: Sequence[str] | None = None,
    *,
    app: QApplication | None = None,
    window: PersonalHubMainWindow | None = None,
    start_in_tray: bool = False,
) -> int:
    """Show the independent Hub shell and run Qt's event loop."""

    application = _configure_application(app) if app is not None else create_application(argv)
    hub_window = window or PersonalHubMainWindow()
    hub_window.tray_controller.show()
    if start_in_tray:
        hub_window.hide()
    else:
        hub_window.show()
    return application.exec()


__all__ = [
    "APPLICATION_NAME",
    "CalendarEventView",
    "CalendarEventButton",
    "CalendarImportView",
    "CalendarSourceView",
    "CalendarView",
    "AttentionNotificationView",
    "IcalFeedView",
    "FeedEntryView",
    "FeedSubscriptionView",
    "FeedView",
    "DiagnosticsView",
    "HubCalendarEventView",
    "HubCalendarImportView",
    "HubCalendarSourceView",
    "HubCalendarView",
    "HubFeedEntryView",
    "HubFeedSubscriptionView",
    "HubFeedView",
    "HubNewsView",
    "HubTaskView",
    "HubTodayItemView",
    "HubTodaySectionView",
    "HubTodayView",
    "HOME_DESTINATION",
    "HourlyCalendarGrid",
    "InboxItemView",
    "MainWindow",
    "MessageBodyView",
    "MessageSourceView",
    "MessageSummaryView",
    "MessagesView",
    "NAVIGATION_DESTINATIONS",
    "NAVIGATION_LABELS",
    "NewsFeedView",
    "NewsView",
    "PLANNED_NAVIGATION_DESTINATIONS",
    "PersonalHubMainWindow",
    "PersonalHubTrayController",
    "PersonalHubWindow",
    "SettingsView",
    "SyncView",
    "SourceFailureView",
    "SourceView",
    "TaskItemView",
    "TaskListWidget",
    "TaskRowWidget",
    "TaskView",
    "TimezoneComboBox",
    "TODAY_DESTINATION",
    "TodayItemView",
    "TodaySectionView",
    "TodayView",
    "WatchlistView",
    "WatchAlertView",
    "WebWatchView",
    "WebWatchesView",
    "_accessible_stylesheet",
    "create_application",
    "create_main_window",
    "run_desktop",
]
