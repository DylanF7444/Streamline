"""Independent PySide6 desktop surface for Personal Hub."""

from __future__ import annotations

from typing import Any

__all__ = [
    "APPLICATION_NAME",
    "AttentionNotificationView",
    "CalendarEventView",
    "CalendarImportView",
    "CalendarSourceView",
    "CalendarView",
    "IcalFeedView",
    "FeedEntryView",
    "FeedSubscriptionView",
    "FeedView",
    "DiagnosticsView",
    "InboxItemView",
    "HubDesktopCoordinator",
    "HourlyCalendarGrid",
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
    "PersonalHubWindow",
    "PersonalHubMainWindow",
    "PersonalHubTrayController",
    "SettingsView",
    "SourceFailureView",
    "SourceView",
    "TaskItemView",
    "TaskListWidget",
    "TaskRowWidget",
    "TaskView",
    "TODAY_DESTINATION",
    "TodayItemView",
    "TodaySectionView",
    "TodayView",
    "HubTaskView",
    "HubTodayItemView",
    "HubTodaySectionView",
    "HubTodayView",
    "HubCalendarEventView",
    "HubCalendarImportView",
    "HubCalendarSourceView",
    "HubCalendarView",
    "HubFeedEntryView",
    "HubFeedSubscriptionView",
    "HubFeedView",
    "HubNewsView",
    "WatchlistView",
    "WatchAlertView",
    "WebWatchView",
    "WebWatchesView",
    "create_application",
    "create_main_window",
    "run_desktop",
    "run_hub_desktop",
]


def run_desktop(*args: Any, **kwargs: Any) -> int:
    """Create and run the Personal Hub desktop window."""

    from personal_hub.ui.desktop.main_window import run_desktop as _run_desktop

    return _run_desktop(*args, **kwargs)


def __getattr__(name: str) -> Any:
    """Resolve Qt-backed symbols lazily for non-desktop imports."""

    if name in {"HubDesktopCoordinator", "run_hub_desktop"}:
        from personal_hub.ui.desktop import application

        return getattr(application, name)

    if name in {
        "APPLICATION_NAME",
        "AttentionNotificationView",
        "CalendarEventView",
        "CalendarImportView",
        "CalendarSourceView",
        "CalendarView",
        "IcalFeedView",
        "FeedEntryView",
        "FeedSubscriptionView",
        "FeedView",
        "DiagnosticsView",
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
        "PersonalHubWindow",
        "PersonalHubMainWindow",
        "PersonalHubTrayController",
        "SettingsView",
        "SourceFailureView",
        "SourceView",
        "TaskItemView",
        "TaskListWidget",
        "TaskRowWidget",
        "TaskView",
        "TODAY_DESTINATION",
        "TodayItemView",
        "TodaySectionView",
        "TodayView",
        "HubTaskView",
        "HubTodayItemView",
        "HubTodaySectionView",
        "HubTodayView",
        "HubCalendarEventView",
        "HubCalendarImportView",
        "HubCalendarSourceView",
        "HubCalendarView",
        "HourlyCalendarGrid",
        "HubFeedEntryView",
        "HubFeedSubscriptionView",
        "HubFeedView",
        "HubNewsView",
        "WatchlistView",
        "WatchAlertView",
        "WebWatchView",
        "WebWatchesView",
        "create_application",
        "create_main_window",
    }:
        from personal_hub.ui.desktop import main_window

        return getattr(main_window, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
