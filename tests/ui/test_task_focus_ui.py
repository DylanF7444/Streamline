"""Real desktop controls and timer completion, without waiting for wall time."""

import pytest

from personal_hub.bootstrap import build_hub_container
from personal_hub.config import HubSettings
from personal_hub.ui.desktop.application import HubDesktopCoordinator
from personal_hub.ui.desktop.main_window import create_main_window

pytestmark = pytest.mark.ui


@pytest.fixture
def ui(qtbot, qapp, tmp_path):
    container = build_hub_container(HubSettings(data_dir=tmp_path / "hub"))
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    coordinator = HubDesktopCoordinator(container, window, qapp)
    coordinator.focus_coordinator.timer.stop()
    yield container.controller, window, coordinator
    coordinator.shutdown()
    container.close()


def tick(coordinator, seconds=1):
    focus = coordinator.focus_coordinator
    focus.last_tick -= seconds
    focus.tick()


def test_editor_starts_pauses_completes_and_undoes_goal(ui):
    hub, window, coordinator = ui
    task = hub.create_task(title="Study math")
    coordinator.render_task_surfaces()
    window.show_destination("Tasks")
    window.open_task_editor(str(task.task_id))
    window.task_time_mode_input.setCurrentIndex(1)
    window.task_target_minutes_input.setValue(1)
    window.task_log_focus_input.setChecked(True)
    window._save_task_edit()
    saved = hub.get_task(str(task.task_id))
    assert saved.time_mode == "goal" and saved.target_minutes == 1
    row = window.tasks_list.itemWidget(window.tasks_list.item(0))
    row.focus_button.click()
    focus = coordinator.focus_coordinator
    focus.timer.stop()
    tick(coordinator, 10)
    assert window.focus_panel.progress.value() > 0
    window.focus_panel.buttons["pause"].click()
    tick(coordinator, 10)
    assert focus.repository.current().elapsed_seconds == 10
    window.focus_panel.buttons["resume"].click()
    focus.timer.stop()
    for _ in range(5):
        tick(coordinator, 10)
    assert hub.get_task(str(task.task_id)).state == "completed"
    assert window.focus_panel.progress.value() == 1000
    assert "completed" in window.focus_panel.status.text()
    window.undo_task(str(task.task_id))
    assert hub.get_task(str(task.task_id)).state == "open"
    assert focus.repository.current() is None


def test_estimate_confirmation_keep_working_and_stop(ui):
    hub, window, coordinator = ui
    task = hub.create_task(title="Report", time_mode="estimate", target_minutes=1)
    coordinator.render_task_surfaces()
    focus = coordinator.focus_coordinator
    focus.start(str(task.task_id))
    focus.timer.stop()
    for _ in range(6):
        tick(coordinator, 10)
    assert hub.get_task(str(task.task_id)).state == "open"
    assert window.focus_panel.buttons["resume"].text() == "Keep working"
    window.focus_panel.buttons["resume"].click()
    focus.timer.stop()
    tick(coordinator, 10)
    assert focus.repository.current().elapsed_seconds == 70
    window.focus_panel.buttons["complete"].click()
    assert hub.get_task(str(task.task_id)).state == "completed"


def test_privacy_lock_sleep_and_restart(ui):
    hub, window, coordinator = ui
    task = hub.create_task(title="Private study", time_mode="goal", target_minutes=60)
    coordinator.render_task_surfaces()
    focus = coordinator.focus_coordinator
    focus.start(str(task.task_id))
    focus.timer.stop()
    tick(coordinator, 5)
    window.set_privacy_masked(True)
    assert window.focus_panel.title.text() == ""
    assert window.focus_panel.isHidden()
    window.set_privacy_masked(False)
    window.set_locked(True)
    assert not focus.repository.current().running
    window.set_locked(False)
    focus.start(str(task.task_id))
    focus.timer.stop()
    tick(coordinator, 30)
    assert not focus.repository.current().running
    assert focus.repository.current().elapsed_seconds == 5
    focus.start(str(task.task_id))
    focus.timer.stop()
    focus.close()
    assert not focus.repository.current().running
