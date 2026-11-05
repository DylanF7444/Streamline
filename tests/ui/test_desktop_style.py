"""Home totals and customization remain functional through the desktop restyle."""

from __future__ import annotations

import pytest
from PySide6.QtGui import QPalette

from personal_hub.bootstrap import build_hub_container
from personal_hub.config import HubSettings
from personal_hub.contracts import InboxItem
from personal_hub.ui.desktop.appearance import _relative_luminance
from personal_hub.ui.desktop.application import HubDesktopCoordinator
from personal_hub.ui.desktop.main_window import WebWatchesView, WebWatchView, create_main_window

pytestmark = pytest.mark.ui


def test_home_counts_follow_local_mutations_and_are_cleared_for_privacy(qtbot, qapp, tmp_path):
    container = build_hub_container(HubSettings(data_dir=tmp_path))
    hub = container.controller
    task = hub.create_task(title="Local task")
    hub.log_progress(description="Outlined the work", duration_minutes=10)
    alert = container.inbox_repository.add(
        InboxItem(
            item_id="style-alert",
            kind="system",
            title="Attention",
            safe_summary="Summary",
            reason="Local test",
            navigation_target="hub://inbox/style-alert",
        )
    )
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    coordinator = HubDesktopCoordinator(container, window, qapp)
    try:
        assert [value.text() for value in window.home_summary.values] == ["1", "1", "1"]
        hub.complete_task(str(task.task_id))
        coordinator.render_task_surfaces()
        assert [value.text() for value in window.home_summary.values] == ["0", "1", "2"]
        coordinator._inbox_state_changed((str(alert.item_id), "read"))
        assert [value.text() for value in window.home_summary.values] == ["0", "0", "2"]
        window.set_privacy_masked(True)
        assert all(value.text() == "—" for value in window.home_summary.values)
        coordinator.render_task_surfaces()
        assert all(value.text() == "—" for value in window.home_summary.values)
        window.set_privacy_masked(False)
        assert [value.text() for value in window.home_summary.values] == ["0", "0", "2"]
        window.set_locked(True)
        assert all(value.text() == "—" for value in window.home_summary.values)
        window.set_locked(False)
        hub.undo_task(str(task.task_id))
        coordinator.render_task_surfaces()
        assert [value.text() for value in window.home_summary.values] == ["1", "0", "1"]
    finally:
        coordinator.shutdown()
        container.close()


def test_dashboard_menu_retains_editing_appearance_and_saved_geometry(qtbot):
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    window.set_reduced_motion(True)
    board = window.dashboard
    board.set_widget_size("schedule", "wide")
    saved = board.export_layout()
    board.edit_action.trigger()
    assert board.editing and not board.edit_button.isHidden()
    board.edit_button.click()
    assert not board.editing and board.edit_button.isHidden()
    window.set_high_contrast(True)
    assert board.export_layout() == saved
    appearance = next(
        action for action in board.customize_menu.actions() if action.text() == "Appearance"
    )
    appearance.trigger()
    assert window.active_destination == "Settings"
    assert board.export_layout() == saved
    assert all(not button.icon().isNull() for button in window.navigation_buttons.values())


def test_high_contrast_selected_navigation_is_readable_and_home_reflows(qtbot):
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    window.resize(720, 460)
    window.apply_accessibility_preferences(text_scale=1.75, high_contrast=True, reduced_motion=True)
    window.show()
    qtbot.wait(25)
    page = window.today_page
    assert page.horizontalScrollBar().maximum() == 0
    assert window.today_subtitle.isHidden()
    assert (
        window.home_add_task_button.width() >= window.home_add_task_button.minimumSizeHint().width()
    )
    assert (
        window.dashboard.customize_button.width()
        >= window.dashboard.customize_button.minimumSizeHint().width()
    )
    palette = window.today_button.palette()
    colors = sorted(
        _relative_luminance(palette.color(role).name())
        for role in (QPalette.ColorRole.ButtonText, QPalette.ColorRole.Button)
    )
    assert (colors[1] + 0.05) / (colors[0] + 0.05) >= 4.5


def test_watch_editor_is_explicit_keyboard_accessible_and_hidden_when_masked(qtbot):
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    window.set_reduced_motion(True)
    window.show_destination("Watchlists")
    window.show()
    assert window.watch_editor_panel.isHidden()
    window.watch_add_button.click()
    assert not window.watch_editor_panel.isHidden()
    qtbot.waitUntil(window.watch_name_input.hasFocus, timeout=2000)
    window.watch_name_input.setText("Draft")
    window.watch_cancel_button.click()
    assert window.watch_editor_panel.isHidden()
    watch = WebWatchView(
        watch_id="style-watch",
        name="Private watch",
        kind="page_text",
        source_url="https://example.test/item",
    )
    window.render_web_watches(WebWatchesView(watches=(watch,)))
    window.edit_watch(watch.watch_id)
    assert not window.watch_editor_panel.isHidden()
    assert window.watch_name_input.text() == "Private watch"
    window.set_privacy_masked(True)
    assert window.watch_editor_panel.isHidden()
    assert not window.watch_add_button.isEnabled()
