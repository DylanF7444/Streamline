from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QLabel

from personal_hub.bootstrap import build_hub_container
from personal_hub.calendar.contracts import (
    CalendarEvent,
    CalendarQuery,
    CalendarSnapshot,
    CalendarStatus,
)
from personal_hub.config import HubSettings
from personal_hub.contracts import InboxItem, SourceRef
from personal_hub.feeds import FeedResponse
from personal_hub.sources import SourceState, SourceStatus
from personal_hub.ui.desktop.application import HubDesktopCoordinator
from personal_hub.ui.desktop.main_window import create_main_window
from personal_hub.watches import PageResponse


class _FailingSource:
    source_id = "fake.failure"
    provider_id = "fake"
    display_name = "Fake source"

    def __init__(self) -> None:
        self.refresh_calls = 0
        self.connect_calls = 0
        self.disconnect_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1

    async def disconnect(self) -> None:
        self.disconnect_calls += 1

    async def status(self) -> SourceStatus:
        return SourceStatus(
            source_id=self.source_id,
            provider_id=self.provider_id,
            state=SourceState.READY,
        )

    async def refresh(self):
        self.refresh_calls += 1
        await asyncio.sleep(0.05)
        raise RuntimeError("private-provider-payload-must-not-render")


class _CalendarSource:
    source_id = "calendar.account"
    provider_id = "test-calendar"
    display_name = "Work account"

    def __init__(self) -> None:
        self.calls = 0
        self.deleted = False

    async def status(self) -> CalendarStatus:
        return CalendarStatus(
            source_id=self.source_id,
            provider_id=self.provider_id,
            state="ready",
            checked_at=datetime.now(UTC),
        )

    async def list_events(self, query: CalendarQuery) -> CalendarSnapshot:
        self.calls += 1
        starts_at = datetime.combine(datetime.now(UTC).date(), time(hour=15), tzinfo=UTC)
        return CalendarSnapshot(
            source_id=self.source_id,
            provider_id=self.provider_id,
            collected_at=datetime.now(UTC),
            selected_calendar_ids=("work",),
            available_calendar_ids=("home", "work"),
            calendar_display_names={"home": "Home Calendar", "work": "Work Calendar"},
            events=()
            if self.deleted
            else (
                CalendarEvent(
                    event_id="event-1",
                    calendar_id="work",
                    title="Calendar event",
                    starts_at=starts_at,
                    ends_at=starts_at + timedelta(hours=1),
                    timezone="UTC",
                    source=SourceRef(
                        provider_id=self.provider_id,
                        connection_id=self.source_id,
                        resource_id="work",
                        display_name=self.display_name,
                    ),
                    deep_link="https://calendar.example/event-1",
                ),
            ),
        )


class _FeedFetcher:
    def __init__(self) -> None:
        self.calls = 0

    async def fetch(self, subscription, **_kwargs):
        self.calls += 1
        await asyncio.sleep(0.05)
        return FeedResponse(
            status_code=200,
            headers={"ETag": '"phase-4"'},
            url=str(subscription.feed_url),
            body=b"""<rss version="2.0"><channel><title>Hub updates</title>
            <item><guid>desktop-one</guid><title>Desktop feed entry</title>
            <link>https://news.example.test/desktop-one</link>
            <description>Safe summary</description></item></channel></rss>""",
        )


class _WatchFetcher:
    def __init__(self, prices: list[float]) -> None:
        self.prices = list(prices)
        self.calls = 0
        self.offline = False

    def set_offline(self, value: bool) -> None:
        self.offline = value

    async def fetch(self, _watch):
        self.calls += 1
        await asyncio.sleep(0.05)
        price = self.prices.pop(0)
        return PageResponse(
            status_code=200,
            headers={"Content-Type": "text/html"},
            body=f"""<script type="application/ld+json">{{"@type":"Product",
            "offers":{{"@type":"Offer","price":"{price}","priceCurrency":"USD"}}}}</script>""".encode(),
            url="https://shop.example.test/product",
        )


class _StartupRegistration:
    def __init__(self) -> None:
        self.values: list[bool] = []

    def set_enabled(self, enabled: bool) -> None:
        self.values.append(enabled)


@pytest.mark.ui
def test_global_refresh_uses_one_worker_and_coalesces_repeat_clicks(
    qtbot,
    monkeypatch,
    tmp_path: Path,
):
    from personal_hub import lifecycle as lifecycle_module

    class _TestInstanceLock:
        def close(self) -> None:
            return

    monkeypatch.setattr(
        lifecycle_module,
        "acquire_single_instance",
        lambda _settings: _TestInstanceLock(),
    )
    container = build_hub_container(HubSettings(data_dir=tmp_path / "hub"))
    source = _FailingSource()
    container.source_registry.register(source)
    container.lifecycle.start()
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    window.show()
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    coordinator = HubDesktopCoordinator(container, window, application)
    rendered_task_surfaces: list[bool] = []
    original_render_task_surfaces = coordinator.render_task_surfaces

    def count_task_surface_render() -> None:
        rendered_task_surfaces.append(True)
        original_render_task_surfaces()

    monkeypatch.setattr(coordinator, "render_task_surfaces", count_task_surface_render)
    try:
        visible_top_levels = {
            id(widget) for widget in application.topLevelWidgets() if widget.isVisible()
        }
        coordinator.refresh_all()
        worker = coordinator._global_refresh_thread
        assert worker is not None
        assert coordinator._refresh_thread is None
        assert coordinator._calendar_refresh_thread is None
        assert coordinator._feed_refresh_thread is None
        assert coordinator._watch_refresh_thread is None
        assert coordinator._message_refresh_thread is None
        assert window.today_refresh_button.text() == "Refreshing…"

        coordinator.refresh_all()
        assert coordinator._global_refresh_thread is worker
        assert coordinator._global_refresh_pending
        qtbot.waitUntil(
            lambda: (
                coordinator._global_refresh_thread is None
                and not coordinator._global_refresh_pending
            ),
            timeout=5_000,
        )

        assert source.refresh_calls == 2
        assert len(rendered_task_surfaces) == 2
        assert {
            id(widget) for widget in application.topLevelWidgets() if widget.isVisible()
        } == visible_top_levels
        assert window.today_refresh_button.text() == "Refresh"
    finally:
        coordinator.shutdown()
        container.close()


@pytest.mark.ui
def test_coordinator_refresh_is_nonblocking_and_renders_only_safe_failure(
    qtbot,
    tmp_path: Path,
):
    container = build_hub_container(HubSettings(data_dir=tmp_path / "hub"))
    source = _FailingSource()
    container.source_registry.register(source)
    container.lifecycle.start()
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    coordinator = HubDesktopCoordinator(container, window, application)

    event_loop_progress: list[bool] = []
    QTimer.singleShot(0, lambda: event_loop_progress.append(True))
    coordinator.refresh_sources()

    qtbot.waitUntil(lambda: bool(event_loop_progress), timeout=1_000)
    qtbot.waitUntil(lambda: bool(window.source_failures), timeout=3_000)

    assert source.refresh_calls == 1
    assert window.source_failures[0].message == "Source refresh failed safely."
    rendered = " ".join(label.text() for label in window.findChildren(QLabel))
    assert "private-provider-payload-must-not-render" not in rendered
    assert container.controller.list_inbox()[0].kind == "source_failure"

    window.sources_list.setCurrentRow(0)
    window.source_enable_button.click()
    assert not container.controller.list_sources()[0].enabled
    assert not window.source_refresh_button.isEnabled()
    window.source_enable_button.click()
    assert container.controller.list_sources()[0].enabled
    window.source_reconnect_button.click()
    qtbot.waitUntil(lambda: coordinator._refresh_thread is None, timeout=3_000)
    assert source.connect_calls == 1
    window.source_disconnect_button.click()
    qtbot.waitUntil(lambda: coordinator._refresh_thread is None, timeout=3_000)
    assert source.disconnect_calls == 1

    coordinator.shutdown()
    container.close()


@pytest.mark.ui
def test_coordinator_projects_lifecycle_lock_and_durable_offline_setting(qtbot, tmp_path: Path):
    container = build_hub_container(HubSettings(data_dir=tmp_path / "hub"))
    container.lifecycle.start()
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    coordinator = HubDesktopCoordinator(container, window, application)

    container.lifecycle.handle_session_lock()
    assert window.locked
    assert not container.lifecycle.network_refresh_allowed

    container.lifecycle.handle_session_unlock()
    assert not window.locked
    coordinator.set_offline_mode(True)
    assert container.controller.settings.offline_mode

    coordinator.shutdown()
    container.close()

    restarted = build_hub_container(HubSettings(data_dir=tmp_path / "hub"))
    try:
        assert restarted.controller.settings.offline_mode
    finally:
        restarted.close()


@pytest.mark.ui
def test_coordinator_persists_visual_task_workflow_and_today_preferences(qtbot, tmp_path: Path):
    settings = HubSettings(data_dir=tmp_path / "hub")
    container = build_hub_container(settings)
    container.lifecycle.start()
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    coordinator = HubDesktopCoordinator(container, window, application)

    created = window.add_task("Persist from the visual workflow")
    assert container.controller.get_task(created.task_id) is not None
    due_at = datetime.combine(datetime.now(UTC).date(), time(hour=17), tzinfo=UTC)
    window.edit_task(
        created.task_id,
        title="Edited visually",
        priority="high",
        due_at=due_at,
    )
    persisted = container.controller.get_task(created.task_id)
    assert persisted is not None and persisted.title == "Edited visually"
    assert any(item.task_id == created.task_id for item in window.calendar_events)

    window.complete_task(created.task_id)
    assert container.controller.get_task(created.task_id).state == "completed"
    assert next(
        item for item in window.calendar_events if item.task_id == created.task_id
    ).is_completed
    window.undo_task(created.task_id)
    assert container.controller.get_task(created.task_id).state == "open"
    window.archive_task(created.task_id)
    assert container.controller.get_task(created.task_id).state == "archived"
    window.undo_task(created.task_id)
    assert container.controller.get_task(created.task_id).state == "open"

    window.set_today_section_order(("unscheduled", "today"))
    window.set_today_section_visibility("upcoming", False)
    coordinator.shutdown()
    container.close()

    restarted = build_hub_container(settings)
    restarted.lifecycle.start()
    restarted_window = create_main_window(close_to_tray=False)
    qtbot.addWidget(restarted_window)
    restarted_coordinator = HubDesktopCoordinator(restarted, restarted_window, application)
    try:
        assert restarted.controller.list_tasks()[0].title == "Edited visually"
        assert restarted_window.today_section_order[:2] == ("unscheduled", "today")
        assert restarted_window.today_section_visibility["upcoming"] is False
    finally:
        restarted_coordinator.shutdown()
        restarted.close()


@pytest.mark.ui
def test_coordinator_refreshes_calendar_off_thread_and_persists_calendar_preferences(
    qtbot,
    tmp_path: Path,
):
    container = build_hub_container(HubSettings(data_dir=tmp_path / "hub"))
    source = _CalendarSource()
    container.controller.register_calendar_source(source)
    due_at = datetime.combine(datetime.now(UTC).date(), time(hour=16), tzinfo=UTC)
    task = container.controller.create_task(title="Due to-do", due_at=due_at)
    container.lifecycle.start()
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    coordinator = HubDesktopCoordinator(container, window, application)

    event_loop_progress: list[bool] = []
    QTimer.singleShot(0, lambda: event_loop_progress.append(True))
    coordinator.refresh_calendars()
    qtbot.waitUntil(lambda: bool(event_loop_progress), timeout=1_000)
    qtbot.waitUntil(lambda: bool(window.calendar_events), timeout=3_000)
    qtbot.waitUntil(lambda: coordinator._calendar_refresh_thread is None, timeout=3_000)

    assert source.calls == 1
    assert {item.title for item in window.calendar_events} == {"Calendar event", "Due to-do"}
    schedule = next(
        section for section in window.today_sections if section.section_id == "schedule"
    )
    assert [item.title for item in schedule.items] == ["Calendar event"]
    provider_item = next(
        window.calendar_agenda_view.item(index)
        for index in range(window.calendar_agenda_view.count())
        if window.calendar_agenda_view.item(index).data(Qt.ItemDataRole.UserRole)
        == "calendar.account:event-1"
    )
    task_item = next(
        window.calendar_agenda_view.item(index)
        for index in range(window.calendar_agenda_view.count())
        if window.calendar_agenda_view.item(index).data(Qt.ItemDataRole.UserRole)
        == f"task:{task.task_id}"
    )
    assert not provider_item.flags() & Qt.ItemFlag.ItemIsUserCheckable
    assert task_item.flags() & Qt.ItemFlag.ItemIsUserCheckable
    task_item.setCheckState(Qt.CheckState.Checked)
    assert container.controller.get_task(str(task.task_id)).state == "completed"
    assert next(item for item in window.calendar_events if item.task_id).is_completed is True

    window.set_calendar_selection(("calendar.account/work",))
    qtbot.waitUntil(lambda: source.calls == 2, timeout=3_000)
    qtbot.waitUntil(lambda: coordinator._calendar_refresh_thread is None, timeout=3_000)
    assert window.calendar_source_checkboxes["calendar.account/home"].isChecked() is False
    assert window.calendar_source_checkboxes["calendar.account/work"].isChecked() is True
    assert "Home Calendar" in window.calendar_source_checkboxes["calendar.account/home"].text()
    assert "Work Calendar" in window.calendar_source_checkboxes["calendar.account/work"].text()
    window.set_calendar_mode("week")
    qtbot.waitUntil(lambda: source.calls == 3, timeout=3_000)
    qtbot.waitUntil(lambda: coordinator._calendar_refresh_thread is None, timeout=3_000)
    assert container.settings_repository.get("calendar_selected_ids") == ["calendar.account/work"]
    assert container.settings_repository.get("calendar_view_mode") == "week"

    source.deleted = True
    coordinator.refresh_calendars()
    qtbot.waitUntil(lambda: source.calls == 4, timeout=3_000)
    qtbot.waitUntil(lambda: coordinator._calendar_refresh_thread is None, timeout=3_000)
    assert [item.title for item in window.calendar_events] == ["Due to-do"]

    coordinator.set_offline_mode(True)
    coordinator.refresh_calendars()
    qtbot.wait(50)
    assert source.calls == 4
    assert window.calendar_freshness == "offline"

    coordinator.shutdown()
    container.close()


@pytest.mark.ui
def test_coordinator_persists_visual_news_workflow_and_refreshes_off_thread(
    qtbot,
    tmp_path: Path,
):
    settings = HubSettings(data_dir=tmp_path / "hub")
    container = build_hub_container(settings)
    fetcher = _FeedFetcher()
    container.feed_service.fetcher = fetcher
    container.lifecycle.start()
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    coordinator = HubDesktopCoordinator(container, window, application)

    event_loop_progress: list[bool] = []
    QTimer.singleShot(0, lambda: event_loop_progress.append(True))
    payload = window.request_create_feed_subscription(
        {
            "title": "Desktop news",
            "feed_url": "https://news.example.test/feed.xml",
            "refresh_minutes": 30,
            "include_terms": (),
            "exclude_terms": (),
            "digest_only": False,
            "enabled": True,
        }
    )
    assert payload is not None
    qtbot.waitUntil(lambda: bool(event_loop_progress), timeout=1_000)
    qtbot.waitUntil(lambda: bool(window.news_entries), timeout=3_000)
    qtbot.waitUntil(lambda: coordinator._feed_refresh_thread is None, timeout=3_000)

    assert fetcher.calls == 1
    subscription = container.controller.list_feed_subscriptions()[0]
    assert window.news_entries[0].title == "Desktop feed entry"
    news_section = next(
        section for section in window.today_sections if section.section_id == "news"
    )
    assert [item.title for item in news_section.items] == ["Desktop feed entry"]

    window.set_feed_entry_read(window.news_entries[0].entry_id, True)
    assert container.controller.list_feed_entries()[0].state == "read"
    window.set_news_subscription_selection(())
    window.set_news_keyword_filters(include_terms=("desktop",), exclude_terms=("ads",))
    window.set_news_unread_only(True)
    assert container.settings_repository.get("news_selected_subscription_ids") == []
    assert container.settings_repository.get("news_view_filters") == {
        "include_terms": ["desktop"],
        "exclude_terms": ["ads"],
        "unread_only": True,
    }

    coordinator.set_offline_mode(True)
    coordinator.refresh_feeds((subscription.subscription_id,), force=True)
    qtbot.wait(50)
    assert fetcher.calls == 1
    assert window.news_freshness == "offline"

    coordinator.shutdown()
    container.close()

    restarted = build_hub_container(settings)
    restarted.lifecycle.start()
    restarted_window = create_main_window(close_to_tray=False)
    qtbot.addWidget(restarted_window)
    restarted_coordinator = HubDesktopCoordinator(restarted, restarted_window, application)
    try:
        assert restarted_window.selected_subscription_ids == ()
        assert restarted_window.news_view.selection_explicit
        assert restarted_window.news_view.include_terms == ("desktop",)
        assert restarted_window.news_view.exclude_terms == ("ads",)
        assert restarted_window.news_view.unread_only
        assert restarted.controller.list_feed_entries()[0].state == "read"
    finally:
        restarted_coordinator.shutdown()
        restarted.close()


@pytest.mark.ui
def test_coordinator_tests_and_persists_visual_watch_workflow_off_thread(
    qtbot,
    monkeypatch,
    tmp_path: Path,
):
    settings = HubSettings(data_dir=tmp_path / "hub", default_timezone="UTC")
    container = build_hub_container(settings)
    fetcher = _WatchFetcher([100, 100, 80])
    container.watch_service.fetcher = fetcher
    container.lifecycle.start()
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    coordinator = HubDesktopCoordinator(container, window, application)
    shown_notifications: list[tuple[str, str]] = []
    monkeypatch.setattr(
        window.tray_controller,
        "show_notification",
        lambda title, message: shown_notifications.append((title, message)) or True,
    )
    values = {
        "name": "Visual deal",
        "kind": "product_price",
        "source_url": "https://shop.example.test/product",
        "condition_kind": "price_below",
        "threshold": 90,
        "refresh_minutes": 60,
        "cooldown_minutes": 60,
        "notification_policy": "immediate",
    }

    window.request_test_watch(values)
    qtbot.waitUntil(lambda: coordinator._watch_test_thread is None, timeout=3_000)
    assert "Extraction succeeded" in window.watch_editor_status.text()
    assert container.watch_repository.counts() == (0, 0, 0)

    window.request_create_watch(values)
    qtbot.waitUntil(lambda: bool(window.web_watches), timeout=3_000)
    qtbot.waitUntil(lambda: coordinator._watch_refresh_thread is None, timeout=3_000)
    watch = container.controller.list_watches()[0]
    assert container.watch_repository.counts() == (1, 1, 0)
    assert window.web_watches[0].name == "Visual deal"

    event_loop_progress: list[bool] = []
    QTimer.singleShot(0, lambda: event_loop_progress.append(True))
    coordinator.refresh_watches((watch.watch_id,), force=True)
    qtbot.waitUntil(lambda: bool(event_loop_progress), timeout=1_000)
    qtbot.waitUntil(lambda: coordinator._watch_refresh_thread is None, timeout=3_000)
    assert container.watch_repository.counts() == (1, 2, 1)
    assert window.watch_alerts[0].reason_code == "price_below"
    assert any(item.kind == "watch_alert" for item in container.controller.list_inbox())
    qtbot.waitUntil(lambda: len(shown_notifications) == 1, timeout=1_000)
    assert shown_notifications == [
        ("Visual deal", "Price moved from 100 to 80, below 90."),
    ]
    delivery = container.notification_repository.list(limit=1)[0]
    assert delivery.channel == "windows" and delivery.status == "delivered"
    section = next(item for item in window.today_sections if item.section_id == "watch_alerts")
    assert [item.title for item in section.items] == ["Visual deal"]

    coordinator.set_offline_mode(True)
    calls = fetcher.calls
    coordinator.refresh_watches((watch.watch_id,), force=True)
    qtbot.wait(50)
    assert fetcher.calls == calls

    coordinator.shutdown()
    container.close()

    restarted = build_hub_container(settings)
    restarted.lifecycle.start()
    restarted_window = create_main_window(close_to_tray=False)
    qtbot.addWidget(restarted_window)
    restarted_coordinator = HubDesktopCoordinator(restarted, restarted_window, application)
    try:
        assert restarted_window.web_watches[0].name == "Visual deal"
        assert restarted_window.watch_alerts[0].reason_code == "price_below"
        assert restarted.watch_repository.counts() == (1, 2, 1)
    finally:
        restarted_coordinator.shutdown()
        restarted.close()


@pytest.mark.ui
def test_inbox_state_controls_persist_without_mutating_source_data(qtbot, tmp_path: Path):
    container = build_hub_container(HubSettings(data_dir=tmp_path / "hub"))
    item = container.inbox_repository.add(
        InboxItem(
            item_id="inbox-control-test",
            kind="system",
            title="Local attention item",
            safe_summary="Safe summary",
            reason="User interface test",
            navigation_target="hub://inbox/inbox-control-test",
        )
    )
    container.lifecycle.start()
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    coordinator = HubDesktopCoordinator(container, window, application)
    try:
        window.inbox_list.setCurrentRow(0)
        assert window.inbox_read_button.isEnabled()
        window.inbox_read_button.click()
        assert container.controller.list_inbox()[0].state == "read"
        assert item.item_id == window.inbox_items[0].item_id
        window.inbox_archive_button.click()
        assert container.controller.list_inbox()[0].state == "archived"
    finally:
        coordinator.shutdown()
        container.close()


@pytest.mark.ui
def test_settings_editor_persists_validated_values_and_startup_choice(qtbot, tmp_path: Path):
    settings = HubSettings(data_dir=tmp_path / "hub")
    container = build_hub_container(settings)
    container.lifecycle.start()
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    startup = _StartupRegistration()
    coordinator = HubDesktopCoordinator(
        container,
        window,
        application,
        startup_registration=startup,
    )
    try:
        window.settings_checkboxes["start_at_login"].setChecked(True)
        window.settings_checkboxes["privacy_masking"].setChecked(True)
        window.settings_checkboxes["quiet_hours_enabled"].setChecked(True)
        window.settings_inputs["default_timezone"].setText("America/Chicago")
        window.settings_inputs["digest_time"].setText("07:30")
        window.settings_inputs["message_retention_days"].setText("0")
        window.settings_text_scale_combo.setCurrentIndex(
            window.settings_text_scale_combo.findData(1.5)
        )
        window.settings_save_button.click()

        persisted = container.controller.settings
        assert persisted.start_at_login
        assert persisted.privacy_masking
        assert persisted.notification_quiet_hours_enabled
        assert persisted.default_timezone == "America/Chicago"
        assert persisted.digest_time == "07:30"
        assert persisted.message_summary_retention_days == 0
        assert persisted.ui_text_scale == 1.5
        assert startup.values == [True]
        assert window.settings_status_label.text() == "Settings saved."
    finally:
        coordinator.shutdown()
        container.close()

    restarted = build_hub_container(settings)
    try:
        assert restarted.controller.settings.default_timezone == "America/Chicago"
        assert restarted.controller.settings.message_summary_retention_days == 0
    finally:
        restarted.close()


@pytest.mark.ui
def test_settings_backup_restore_and_offline_maintenance_are_functional(
    qtbot,
    monkeypatch,
    tmp_path: Path,
):
    from personal_hub.ui.desktop import application as application_module

    container = build_hub_container(HubSettings(data_dir=tmp_path / "hub", offline_mode=True))
    now = datetime.now(UTC) - timedelta(minutes=1)
    original = container.controller.create_task(
        title="Included in backup",
        due_at=now,
        timezone="UTC",
        reminder_offsets_minutes=(0,),
        created_at=now - timedelta(hours=1),
        updated_at=now - timedelta(hours=1),
    )
    container.lifecycle.start()
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    coordinator = HubDesktopCoordinator(container, window, application)
    reminder_notifications: list[tuple[str, str]] = []
    monkeypatch.setattr(
        window.tray_controller,
        "show_notification",
        lambda title, message: reminder_notifications.append((title, message)) or True,
    )
    backup = tmp_path / "selected" / "hub-backup.db"
    monkeypatch.setattr(
        application_module.QFileDialog,
        "getSaveFileName",
        lambda *_args, **_kwargs: (str(backup), "SQLite database (*.db)"),
    )
    monkeypatch.setattr(
        application_module.QFileDialog,
        "getOpenFileName",
        lambda *_args, **_kwargs: (str(backup), "SQLite database (*.db)"),
    )
    try:
        window.settings_backup_button.click()
        assert backup.is_file()
        extra = container.controller.create_task(title="Removed by restore")
        assert container.controller.get_task(extra.task_id) is not None
        window.settings_restore_button.click()
        assert container.controller.get_task(original.task_id) is not None
        assert container.controller.get_task(extra.task_id) is None

        coordinator.run_maintenance()
        qtbot.waitUntil(lambda: coordinator._maintenance_thread is None, timeout=3_000)
        assert container.settings_repository.get("retention_cleanup_date", "")
        assert any(
            event.event_type == "maintenance.retention"
            for event in container.controller.audit_events()
        )
        reminders = [
            item for item in container.controller.list_inbox() if item.kind == "task_reminder"
        ]
        assert len(reminders) == 1
        qtbot.waitUntil(lambda: bool(reminder_notifications), timeout=1_000)
        assert reminder_notifications
        reminder_delivery = container.notification_repository.get(
            reminders[0].deduplication_key,
            "windows",
        )
        assert reminder_delivery is not None and reminder_delivery.status == "delivered"
        coordinator.run_maintenance()
        qtbot.waitUntil(lambda: coordinator._maintenance_thread is None, timeout=3_000)
        assert (
            len(
                [item for item in container.controller.list_inbox() if item.kind == "task_reminder"]
            )
            == 1
        )
    finally:
        coordinator.shutdown()
        container.close()


@pytest.mark.ui
def test_refresh_notification_burst_uses_one_native_notification(
    qtbot, monkeypatch, tmp_path: Path
):
    container = build_hub_container(HubSettings(data_dir=tmp_path / "hub", offline_mode=True))
    container.lifecycle.start()
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    coordinator = HubDesktopCoordinator(container, window, application)
    shown_notifications: list[tuple[str, str]] = []
    monkeypatch.setattr(
        window.tray_controller,
        "show_notification",
        lambda title, message: shown_notifications.append((title, message)) or True,
    )
    now = datetime.now(UTC)
    reminders = tuple(
        SimpleNamespace(
            kind="task_reminder",
            deduplication_key=f"task-reminder-{index}",
            title=f"Task {index}",
            safe_summary=f"Task {index} is due.",
            created_at=now,
        )
        for index in (1, 2, 3)
    )

    try:
        coordinator._maintenance_completed({"created": reminders})

        assert shown_notifications == []
        qtbot.waitUntil(lambda: len(shown_notifications) == 1, timeout=2_000)
        assert shown_notifications == [
            (
                "Personal Hub attention",
                "3 attention items are ready. Open Personal Hub to review.",
            )
        ]
        for reminder in reminders:
            delivery = container.notification_repository.get(
                reminder.deduplication_key,
                "windows",
            )
            assert delivery is not None and delivery.status == "delivered"

        coordinator._maintenance_completed({"created": reminders})
        qtbot.wait(350)
        assert len(shown_notifications) == 1
    finally:
        coordinator.shutdown()
        container.close()


@pytest.mark.ui
def test_refresh_notification_batch_waits_for_late_stage(qtbot, monkeypatch, tmp_path: Path):
    container = build_hub_container(HubSettings(data_dir=tmp_path / "hub", offline_mode=True))
    container.lifecycle.start()
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    coordinator = HubDesktopCoordinator(container, window, application)
    shown_notifications: list[tuple[str, str]] = []
    monkeypatch.setattr(
        window.tray_controller,
        "show_notification",
        lambda title, message: shown_notifications.append((title, message)) or True,
    )
    now = datetime.now(UTC)
    first = SimpleNamespace(
        kind="task_reminder",
        deduplication_key="task-reminder-early",
        title="Early task",
        safe_summary="Early task is due.",
        created_at=now,
    )
    second = SimpleNamespace(
        kind="task_reminder",
        deduplication_key="task-reminder-late",
        title="Late task",
        safe_summary="Late task is due.",
        created_at=now,
    )

    try:
        coordinator._begin_system_notification_batch()
        coordinator._maintenance_completed({"created": (first,)})
        coordinator._maintenance_thread = SimpleNamespace()
        coordinator._maybe_finish_system_notification_batch()
        qtbot.wait(350)
        assert shown_notifications == []

        coordinator._maintenance_completed({"created": (second,)})
        qtbot.wait(350)
        assert shown_notifications == []

        coordinator._maintenance_thread = None
        coordinator._maybe_finish_system_notification_batch()
        qtbot.waitUntil(lambda: len(shown_notifications) == 1, timeout=1_000)
        assert shown_notifications == [
            (
                "Personal Hub attention",
                "2 attention items are ready. Open Personal Hub to review.",
            )
        ]
    finally:
        coordinator._maintenance_thread = None
        coordinator.shutdown()
        container.close()


@pytest.mark.ui
def test_visible_hub_suppresses_native_refresh_notification(qtbot, monkeypatch, tmp_path: Path):
    container = build_hub_container(HubSettings(data_dir=tmp_path / "hub", offline_mode=True))
    container.lifecycle.start()
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    window.show()
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    coordinator = HubDesktopCoordinator(container, window, application)
    shown_notifications: list[tuple[str, str]] = []
    monkeypatch.setattr(
        window.tray_controller,
        "show_notification",
        lambda title, message: shown_notifications.append((title, message)) or True,
    )
    reminder = SimpleNamespace(
        kind="task_reminder",
        deduplication_key="task-reminder-visible-window",
        title="Visible task",
        safe_summary="Visible task is due.",
        created_at=datetime.now(UTC),
    )

    try:
        coordinator._maintenance_completed({"created": (reminder,)})
        qtbot.waitUntil(
            lambda: (
                (
                    delivery := container.notification_repository.get(
                        reminder.deduplication_key,
                        "windows",
                    )
                )
                is not None
                and delivery.status == "suppressed"
            ),
            timeout=1_000,
        )
        assert shown_notifications == []
    finally:
        coordinator.shutdown()
        container.close()


@pytest.mark.ui
def test_startup_refresh_suppresses_native_notification_when_hub_hidden(
    qtbot, monkeypatch, tmp_path: Path
):
    container = build_hub_container(HubSettings(data_dir=tmp_path / "hub", offline_mode=True))
    now = datetime.now(UTC) - timedelta(minutes=1)
    container.controller.create_task(
        title="Hidden startup task",
        due_at=now,
        timezone="UTC",
        reminder_offsets_minutes=(0,),
        created_at=now - timedelta(hours=1),
        updated_at=now - timedelta(hours=1),
    )
    container.lifecycle.start()
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    coordinator = HubDesktopCoordinator(container, window, application)
    shown_notifications: list[tuple[str, str]] = []
    monkeypatch.setattr(
        window.tray_controller,
        "show_notification",
        lambda title, message: shown_notifications.append((title, message)) or True,
    )
    try:
        coordinator.refresh_startup()
        qtbot.waitUntil(lambda: coordinator._maintenance_thread is None, timeout=3_000)
        qtbot.wait(350)
        assert shown_notifications == []
        reminders = [
            item for item in container.controller.list_inbox() if item.kind == "task_reminder"
        ]
        assert len(reminders) == 1
        reminder = reminders[0]
        qtbot.waitUntil(
            lambda: (
                (
                    delivery := container.notification_repository.get(
                        reminder.deduplication_key,
                        "windows",
                    )
                )
                is not None
                and delivery.status == "suppressed"
            ),
            timeout=1_000,
        )
        assert shown_notifications == []
    finally:
        coordinator._maintenance_thread = None
        coordinator.shutdown()
        container.close()
