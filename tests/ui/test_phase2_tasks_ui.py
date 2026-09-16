from __future__ import annotations

from datetime import datetime, timedelta

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from PySide6.QtCore import Qt

from personal_hub.ui.desktop.main_window import (
    TaskView,
    TodaySectionView,
    TodayView,
    create_main_window,
)


def _window(qtbot):
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(window.isVisible, timeout=2_000)
    return window


@pytest.mark.ui
def test_task_view_contract_quick_add_editor_and_fields(qtbot):
    window = _window(qtbot)
    window.show_destination("Tasks")

    assert isinstance(TaskView("task-1", "Write notes"), TaskView)
    for name in (
        "task_title_input",
        "task_notes_input",
        "task_due_input",
        "task_all_day_input",
        "task_timezone_input",
        "task_priority_input",
        "task_list_input",
        "task_tags_input",
        "task_recurrence_input",
        "task_reminders_input",
    ):
        assert getattr(window, name).accessibleName()

    created: list[TaskView] = []
    window.task_created.connect(created.append)
    window.task_quick_add_input.setFocus()
    qtbot.keyClicks(window.task_quick_add_input, "Capture an idea")
    qtbot.keyClick(window.task_quick_add_input, Qt.Key.Key_Return)
    assert len(created) == 1
    task = created[0]
    assert task.title == "Capture an idea"
    assert window.tasks_list.count() == 1

    window.open_task_editor(task.task_id)
    assert window.task_editor_panel.isVisible()
    window.task_title_input.setText("Capture an edited idea")
    window.task_notes_input.setPlainText("Keep the details local.")
    window.task_has_due_input.setChecked(True)
    window.task_all_day_input.setChecked(True)
    window.task_timezone_input.setText("America/Chicago")
    window.task_priority_input.setCurrentText("High")
    window.task_list_input.setText("Personal")
    window.task_tags_input.setText("writing, ideas")
    window.task_recurrence_input.setCurrentText("Weekly")
    window.task_reminders_input.setText("15, 60")
    window.task_save_button.click()

    edited = window.tasks[0]
    assert edited.title == "Capture an edited idea"
    assert edited.notes == "Keep the details local."
    assert edited.due_is_all_day is True
    assert edited.timezone == "America/Chicago"
    assert edited.priority == "high"
    assert edited.list_id == "Personal"
    assert edited.tags == ("writing", "ideas")
    assert edited.recurrence == "Weekly"
    assert edited.reminder_offsets_minutes == (15, 60)


@pytest.mark.ui
def test_task_filters_sort_keyboard_completion_archive_and_undo(qtbot):
    window = _window(qtbot)
    window.show_destination("Tasks")
    now = datetime.now().astimezone()
    window.render_tasks(
        [
            {"task_id": "overdue", "title": "Pay bill", "due_at": now - timedelta(days=1)},
            {"task_id": "today", "title": "Write plan", "due_at": now},
            {"task_id": "upcoming", "title": "Book trip", "due_at": now + timedelta(days=2)},
            {"task_id": "unscheduled", "title": "Someday idea"},
            {"task_id": "done", "title": "Already done", "state": "completed"},
        ]
    )
    assert window.tasks_list.count() == 5

    window.set_task_due_filter("overdue")
    assert window.tasks_list.count() == 1
    assert window.tasks_list.itemWidget(window.tasks_list.item(0)).task.task_id == "overdue"
    window.set_task_due_filter("today")
    assert window.tasks_list.count() == 1
    window.set_task_due_filter("upcoming")
    assert window.tasks_list.count() == 1
    window.set_task_due_filter("unscheduled")
    assert window.tasks_list.count() == 2
    window.set_task_due_filter("all")

    window.set_task_state_filter("completed")
    assert window.tasks_list.count() == 1
    window.set_task_state_filter("all")
    window.set_task_sort("priority")
    window.edit_task("upcoming", priority="high")
    assert window.tasks_list.itemWidget(window.tasks_list.item(0)).task.task_id == "upcoming"

    completed: list[TaskView] = []
    archived: list[TaskView] = []
    undone: list[TaskView] = []
    window.task_completed.connect(completed.append)
    window.task_archived.connect(archived.append)
    window.task_undo_requested.connect(undone.append)

    window.set_task_sort("title")
    window.set_task_filters(search="Write plan")
    row = window.tasks_list.itemWidget(window.tasks_list.item(0))
    row.setFocus()
    qtbot.keyClick(row, Qt.Key.Key_Space)
    assert completed[-1].task_id == "today"
    assert window.tasks[1].state == "completed"
    window.undo_task("today")
    assert undone[-1].task_id == "today"
    assert next(task for task in window.tasks if task.task_id == "today").state == "open"

    window.set_task_filters(search="Pay bill")
    row = window.tasks_list.itemWidget(window.tasks_list.item(0))
    row.archive_button.click()
    assert archived[-1].task_id == "overdue"
    window.undo_task("overdue")
    assert next(task for task in window.tasks if task.task_id == "overdue").state == "open"


@pytest.mark.ui
def test_canvas_task_shows_native_open_action_only_with_safe_link(qtbot):
    window = _window(qtbot)
    window.show_destination("Tasks")
    requests: list[TaskView] = []
    window.task_native_open_requested.connect(requests.append)

    window.render_tasks(
        [
            {
                "task_id": "canvas-1",
                "title": "Submit assignment",
                "tags": ("canvas",),
                "external_url": "https://canvas.example.edu/courses/10/assignments/101",
            }
        ]
    )
    row = window.tasks_list.itemWidget(window.tasks_list.item(0))
    assert row is not None
    assert row.native_open_button is not None
    assert row.native_open_button.text() == "Open in Canvas"
    row.native_open_button.click()
    assert [task.task_id for task in requests] == ["canvas-1"]

    window.render_tasks(
        [
            {
                "task_id": "canvas-2",
                "title": "Course reading",
                "tags": ("canvas",),
            }
        ]
    )
    row_without_link = window.tasks_list.itemWidget(window.tasks_list.item(0))
    assert row_without_link is not None
    assert row_without_link.native_open_button is None


@pytest.mark.ui
def test_today_sections_render_in_order_and_support_visibility_hooks(qtbot):
    window = _window(qtbot)
    sections = (
        TodaySectionView("schedule", "Current and next", items=("Stand-up",)),
        TodaySectionView("today", "Due today", items=("Write plan",)),
        TodaySectionView("source_problems", "Source problems", body="Calendar is offline."),
    )
    rendered: list[TodayView] = []
    order_changes: list[tuple[str, ...]] = []
    visibility_changes: list[dict[str, bool]] = []
    window.today_changed.connect(rendered.append)
    window.today_section_order_changed.connect(order_changes.append)
    window.today_section_visibility_changed.connect(visibility_changes.append)

    window.render_today(
        TodayView(sections=sections, local_date="2026-08-31", timezone="America/Chicago")
    )
    assert window.today_date_label.text() == "2026-08-31"
    assert window.today_section_order == ("schedule", "today", "source_problems")
    assert list(window.today_section_widgets) == ["schedule", "today", "source_problems"]
    assert window.today_section_item_lists["schedule"].count() == 1

    window.set_today_section_order(("source_problems", "schedule"))
    assert window.today_section_order == ("source_problems", "schedule", "today")
    assert order_changes[-1] == window.today_section_order
    window.set_today_section_visibility("schedule", False)
    assert window.today_section_visibility["schedule"] is False
    assert not window.today_section_widgets["schedule"].isVisible()
    assert visibility_changes[-1] == {"schedule": False}
    assert rendered


@pytest.mark.ui
def test_task_views_disclose_advanced_filters_and_use_an_editor_inspector(qtbot):
    window = _window(qtbot)
    window.show_destination("Tasks")
    assert not window.task_filters_panel.isVisible()
    assert window.task_content_splitter.widget(0) is window.tasks_list
    assert window.task_content_splitter.widget(1) is window.task_editor_panel

    window.set_task_quick_view("today")
    assert window.task_filters["due"] == "today"
    assert window.task_quick_view_button.text() == "View: Due today"

    window.task_filters_button.click()
    assert window.task_filters_panel.isVisible()
    assert window.task_filters_button.text() == "Hide filters"

    window.set_task_quick_view("all")
    task = window.add_task("Review the new task surface")
    window.open_task_editor(task.task_id)
    assert window.task_editor_panel.isVisible()

    window._cancel_task_edit()
    row = window.tasks_list.itemWidget(window.tasks_list.item(0))
    assert row is not None
    row.action_menu.actions()[0].trigger()
    assert window.task_editor_panel.isVisible()
