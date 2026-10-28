"""Connect private Logging presentation to the local controller, never providers."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from PySide6.QtCore import QDate, QSignalBlocker
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.exc import SQLAlchemyError


class LoggingCoordinator:
    def __init__(self, controller, window, *, refresh_home=None):
        self.controller = controller
        self.window = window
        self.view = window.logging_workspace
        self.refresh_home = refresh_home
        self._reflection_day_initialized = False
        self.view.requested.connect(self.handle)
        self.view.privacy_changed.connect(lambda private: self.refresh() if not private else None)
        window.destination_changed.connect(self.destination_changed)
        self._build_settings()

    def _build_settings(self):
        # Settings is already a scrolling page; keep history controls together.
        settings_page = self.window.page_widgets["Settings"]
        panel = QWidget()
        panel.setAccessibleName("Logging privacy and history")
        layout = QVBoxLayout(panel)
        label = QLabel(
            "Logging history is kept until you delete it. Completed-task cleanup preserves Activity and reflections."
        )
        label.setWordWrap(True)
        layout.addWidget(label)
        self.reflection_sync = QCheckBox(
            "Include private reflections in encrypted shared-folder sync"
        )
        self.reflection_sync.setAccessibleName(
            "Explicitly include private reflections in encrypted sync"
        )
        self.reflection_sync.setChecked(
            self.controller.settings_repository.get("logging.sync_reflections", False) is True
        )
        self.reflection_sync.toggled.connect(self.controller.set_reflection_sync)
        layout.addWidget(self.reflection_sync)
        help_label = QLabel(
            "Off by default on each device. Activity follows enabled shared-folder sync. Turning this off stops receiving and publishing reflections here; copies already shared remain on other devices and in their encrypted shards."
        )
        help_label.setWordWrap(True)
        layout.addWidget(help_label)
        line = QHBoxLayout()
        self.days = QSpinBox()
        self.days.setRange(0, 36500)
        self.days.setSpecialValueText("Keep until deleted")
        self.days.setSuffix(" days")
        self.days.setAccessibleName("Logging history age for explicit deletion")
        self.days.valueChanged.connect(self._invalidate_preview)
        line.addWidget(self.days)
        preview = QPushButton("Preview Logging deletion")
        preview.setAccessibleName("Preview Logging history deletion")
        preview.clicked.connect(self.preview)
        line.addWidget(preview)
        self.apply_button = QPushButton("Delete previewed history")
        self.apply_button.setAccessibleName("Delete previewed Activity and reflections")
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self.apply_retention)
        line.addWidget(self.apply_button)
        layout.addLayout(line)
        self.preview_label = QLabel(
            "No Logging retention policy is enabled. Preview before deleting history."
        )
        self.preview_label.setWordWrap(True)
        layout.addWidget(self.preview_label)
        self.previewed = None
        self.preview_time = None
        # _build_page provides a scroll area with the actual vertical layout.
        target = settings_page.widget() if hasattr(settings_page, "widget") else settings_page
        target.layout().addWidget(panel)

    def _invalidate_preview(self, *_):
        self.previewed = None
        self.apply_button.setEnabled(False)

    def preview(self):
        from datetime import UTC, datetime

        if self.view.masked or not self.view.flush():
            return
        self.preview_time = datetime.now(UTC)
        self.previewed = self.controller.logging_retention(
            days=self.days.value() or None, now=self.preview_time
        )
        self.preview_label.setText(
            f"Will remove {self.previewed['activity']} Activity entries and {self.previewed['reflections']} reflections. Tasks will remain."
        )
        self.apply_button.setEnabled(self.days.value() > 0 and any(self.previewed.values()))

    def apply_retention(self):
        if self.view.masked or self.previewed is None or not self.view.flush():
            return
        current = self.controller.logging_retention(days=self.days.value(), now=self.preview_time)
        if current != self.previewed:
            self.preview()
            self.preview_label.setText(
                "History changed. Review the refreshed preview before deleting."
            )
            return
        self.controller.logging_retention(days=self.days.value(), now=self.preview_time, apply=True)
        if self.view.reflection and not self.controller.reflection_repository.get(
            self.view.reflection.reflection_id
        ):
            self.view.reflection = None
            self.view.dirty = False
            self.view._load_editor()
        self._invalidate_preview()
        self.preview_label.setText("Previewed Logging history deleted. Task items were preserved.")
        self.refresh_home() if self.refresh_home else self.refresh()

    def destination_changed(self, destination):
        self.controller.settings_repository.set(
            "workspace_modes", self.window.workspace_mode_preferences()
        )
        if destination in self.view.pages:
            self.refresh()

    def refresh(self):
        view = self.view
        if self.controller.is_closed:
            return
        view.timezone = self.controller.settings.default_timezone
        if not self._reflection_day_initialized:
            with QSignalBlocker(view.reflection_date):
                view.reflection_date.setDate(QDate(datetime.now(ZoneInfo(view.timezone)).date()))
            self._reflection_day_initialized = True
        if view.masked:
            return
        if view.reflection and not view.dirty:
            current = self.controller.reflection_repository.get(view.reflection.reflection_id)
            if current != view.reflection:
                view.reflection = current
                view._load_editor()
        view.tasks = tuple(
            (str(task.task_id), task.title) for task in self.controller.list_tasks(limit=10_000)
        )
        lists = {str(item.list_id): item.name for item in self.controller.list_task_lists()}
        for identifier, label in self.controller.activity_repository.list_context():
            lists.setdefault(identifier, label)
        view.lists = tuple(lists.items())
        for filters in view.filters.values():
            selected = filters.task_list.currentData()
            with QSignalBlocker(filters.task_list):
                filters.task_list.clear()
                filters.task_list.addItem("All lists", None)
                for identifier, name in view.lists:
                    filters.task_list.addItem(name, identifier)
                filters.task_list.setCurrentIndex(max(0, filters.task_list.findData(selected)))
        selected_task = view.reflection_task.currentData()
        with QSignalBlocker(view.reflection_task):
            view.reflection_task.clear()
            view.reflection_task.addItem("No linked task", None)
            for identifier, name in view.tasks:
                view.reflection_task.addItem(name, identifier)
            if selected_task and view.reflection_task.findData(selected_task) < 0:
                view.reflection_task.addItem("Original task unavailable", selected_task)
            view.reflection_task.setCurrentIndex(
                max(0, view.reflection_task.findData(selected_task))
            )
        with QSignalBlocker(self.reflection_sync):
            self.reflection_sync.setChecked(
                self.controller.settings_repository.get("logging.sync_reflections", False) is True
            )
        for name in view.pages:
            self.query(name)

    def query(self, name):
        if self.view.masked:
            return
        try:
            options = self.view.filters[name].options(
                self.view.timezone, limit=self.view.limits[name]
            )
            if name == "Activity":
                self.view.render_activity(self.controller.list_activity(options))
            elif name == "Reflection":
                self.view.render_reflections(self.controller.list_reflections(options))
            else:
                self.view.render_summary(self.controller.activity_summary(options))
        except (ValueError, OverflowError):
            self.view.statuses[name].setText("Choose a valid date range and filters.")

    def handle(self, action, payload):
        if self.view.masked:
            return
        try:
            if action == "query":
                self.query(payload)
            elif action == "save_activity":
                entry, values = payload["entry"], payload["values"]
                if entry:
                    self.controller.update_activity(str(entry.entry_id), **values)
                else:
                    # Missing optional context can inherit the linked task's context.
                    values = {
                        key: value for key, value in values.items() if value not in (None, ())
                    }
                    self.controller.log_progress(**values)
                payload["dialog"].accept()
                self.refresh_home() if self.refresh_home else self.refresh()
            elif action == "delete_activity":
                self.controller.delete_activity(payload)
                self.refresh_home() if self.refresh_home else self.refresh()
            elif action == "save_reflection":
                self.view.reflection_saved(self.controller.save_reflection(payload))
                self.query("Reflection")
            elif action == "delete_reflection":
                self.controller.delete_reflection(payload)
                self.view.reflection = None
                self.view.dirty = False
                self.view._load_editor()
                self.query("Reflection")
            elif action == "open_task":
                if self.controller.get_task(payload):
                    self.window.show_destination("Tasks")
                    self.window.open_task_editor(payload)
                else:
                    self.view.statuses["Activity"].setText(
                        "Original task is no longer available. History is preserved."
                    )
            elif action == "navigate":
                self.window.show_destination(payload)
        except (ValueError, KeyError, TypeError, SQLAlchemyError, OSError, RuntimeError):
            if action == "save_activity":
                payload["dialog"].status.setText(
                    "Could not save. Enter a description, valid simple tags, and notes of at most 20,000 characters."
                )
            elif action == "save_reflection":
                self.view.saved.setText("Not saved. Your writing remains in the editor; try again.")
            else:
                self.view.statuses["Activity"].setText("Logging could not be updated. Try again.")
