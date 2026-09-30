"""Dashboard preferences survive refresh/reopen and preserve privacy controls."""

from datetime import UTC, datetime

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from PySide6.QtCore import QAbstractAnimation
from PySide6.QtWidgets import QApplication

from personal_hub.bootstrap import build_hub_container
from personal_hub.config import HubSettings
from personal_hub.ui.desktop.application import HubDesktopCoordinator
from personal_hub.ui.desktop.main_window import (
    TodayItemView,
    TodaySectionView,
    TodayView,
    create_main_window,
)


def test_dashboard_layout_and_appearance_survive_refresh_and_restart(qtbot, tmp_path):
    settings = HubSettings(data_dir=tmp_path / "hub", offline_mode=True)
    container = build_hub_container(settings)
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    coordinator = HubDesktopCoordinator(container, window, QApplication.instance())
    window.dashboard.set_widget_size("schedule", "wide")
    window.dashboard.set_widget_size("mini_calendar", "compact")
    layout = window.dashboard.export_layout()
    layout["order"] = list(reversed(layout["order"]))
    layout["hidden"] = ["news", "upcoming", "mini_calendar"]
    window.restore_dashboard_layout(layout)
    window.dashboard.layout_changed.emit(window.dashboard.export_layout())
    window.appearance_panel.changed.emit(
        {
            "ui_theme": "light",
            "ui_accent_color": "#765432",
            "ui_reduced_motion": True,
            "ui_background_color": "#f3f5f7",
            "ui_surface_color": "#ffffff",
        }
    )
    expected = window.dashboard.export_layout()
    assert expected["sizes"]["schedule"] == "wide"
    assert expected["order"] == layout["order"]
    assert set(expected["hidden"]) == {"news", "upcoming", "mini_calendar"}
    assert expected["sizes"]["mini_calendar"] == "compact"
    assert window.reduced_motion
    coordinator.render_task_surfaces()
    assert window.dashboard.export_layout() == expected
    coordinator.shutdown()
    container.close()
    reopened = build_hub_container(settings)
    second = create_main_window(close_to_tray=False)
    qtbot.addWidget(second)
    second_coordinator = HubDesktopCoordinator(reopened, second, QApplication.instance())
    try:
        assert second.dashboard.export_layout() == expected
        assert second.settings_view.ui_theme == "light"
        assert second.settings_view.ui_accent_color == "#765432"
        assert second.reduced_motion
        assert reopened.controller.settings.offline_mode
    finally:
        second_coordinator.shutdown()
        reopened.close()


@pytest.mark.hub_ui
def test_refresh_updates_today_content_without_replacing_visible_widgets(qtbot, tmp_path):
    """A refresh must not reparent visible Today content through a blank state."""

    container = build_hub_container(HubSettings(data_dir=tmp_path / "hub", offline_mode=True))
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    window.show()
    coordinator = HubDesktopCoordinator(container, window, QApplication.instance())
    try:
        card_ids = {
            section_id: id(window.dashboard.widget(section_id))
            for section_id in window.dashboard.widget_ids
        }
        content_ids = {
            section_id: id(window.dashboard.widget(section_id).content)
            for section_id in window.dashboard.widget_ids
        }
        item_list_ids = {
            section_id: id(window.today_section_item_lists[section_id])
            for section_id in window.dashboard.widget_ids
        }

        coordinator.render_task_surfaces()
        coordinator.render_task_surfaces()
        qtbot.wait(0)

        assert window.isVisible()
        assert {
            section_id: id(window.dashboard.widget(section_id))
            for section_id in window.dashboard.widget_ids
        } == card_ids
        assert {
            section_id: id(window.dashboard.widget(section_id).content)
            for section_id in window.dashboard.widget_ids
        } == content_ids
        assert {
            section_id: id(window.today_section_item_lists[section_id])
            for section_id in window.dashboard.widget_ids
        } == item_list_ids
        assert all(
            window.dashboard.widget(section_id).isVisible()
            for section_id in window.dashboard.visible_widget_ids
        )
    finally:
        coordinator.shutdown()
        container.close()


def test_disabling_motion_stops_active_transition_and_future_navigation(qtbot):
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    window.show()
    window.show_destination("Tasks")
    assert window.page_transition.animation.state() == QAbstractAnimation.State.Running
    window.set_reduced_motion(True)
    assert window.page_transition.animation.state() == QAbstractAnimation.State.Stopped
    assert window.page_transition.effect.opacity() == 1.0
    window.show_destination("Calendar")
    assert window.page_transition.animation.state() == QAbstractAnimation.State.Stopped


def test_dashboard_content_masks_after_layout_changes_and_displays_event_time(qtbot):
    window = create_main_window(close_to_tray=False)
    window.set_reduced_motion(True)
    qtbot.addWidget(window)
    window.render_today(
        TodayView(
            timezone="America/Chicago",
            sections=(
                TodaySectionView(
                    "schedule",
                    "Calendar",
                    items=(
                        TodayItemView(
                            item_id="event",
                            title="Private appointment",
                            sensitive=True,
                            starts_at=datetime(2026, 9, 7, 17, 0, tzinfo=UTC),
                        ),
                    ),
                ),
            ),
        )
    )
    assert "12:00" in window.today_section_item_lists["schedule"].item(0).text()
    window.set_privacy_masked(True)
    layout = window.dashboard.export_layout()
    window.restore_dashboard_layout(layout)
    assert "Private appointment" not in window.today_section_item_lists["schedule"].item(0).text()
