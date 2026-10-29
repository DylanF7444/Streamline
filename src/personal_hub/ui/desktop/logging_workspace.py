"""Quiet, keyboard-accessible presentation for Activity, Reflection and Summary."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from PySide6.QtCore import QDate, QDateTime, QObject, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from personal_hub.logging.contracts import ActivityEntry, LogFilter, ReflectionEntry
from personal_hub.ui.desktop.status_label import StatusLabel


def _label(text: str) -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


def _button(text: str, action, layout) -> QPushButton:
    button = QPushButton(text)
    button.setAccessibleName(text)
    button.clicked.connect(action)
    layout.addWidget(button, 0, layout.count())
    return button


class LogFilters(QWidget):
    changed = Signal()

    def __init__(self, *, activity: bool, parent=None):
        super().__init__(parent)
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        self.period = QComboBox()
        self.period.addItems(["Today", "Last 7 days", "Custom range", "All dates"])
        self.period.setCurrentIndex(1)
        self.period.setAccessibleName("Logging date range")
        self.start = QDateEdit(QDate.currentDate().addDays(-6))
        self.end = QDateEdit(QDate.currentDate())
        for control, name in ((self.start, "From date"), (self.end, "Through date")):
            control.setCalendarPopup(True)
            control.setAccessibleName(name)
        self.search = QLineEdit()
        self.search.setMaxLength(240)
        self.search.setPlaceholderText(
            "Search descriptions and notes" if activity else "Search titles and writing"
        )
        self.search.setAccessibleName("Search Logging")
        grid.addWidget(self.period, 0, 0)
        self.from_label = _label("From")
        self.from_label.setBuddy(self.start)
        self.through_label = _label("Through")
        self.through_label.setBuddy(self.end)
        grid.addWidget(self.from_label, 1, 0)
        grid.addWidget(self.start, 1, 1, 1, 2)
        grid.addWidget(self.through_label, 2, 0)
        grid.addWidget(self.end, 2, 1, 1, 2)
        grid.addWidget(self.search, 3, 0, 1, 3)
        self.kind = QComboBox()
        self.kind.addItem("All activity", None)
        self.kind.addItem("Completed tasks", "task_completion")
        self.kind.addItem("Manual progress", "manual")
        self.kind.setAccessibleName("Activity type")
        self.task_list = QComboBox()
        self.task_list.addItem("All lists", None)
        self.task_list.setAccessibleName("Activity task list")
        self.tag = QLineEdit()
        self.tag.setMaxLength(32)
        self.tag.setPlaceholderText("Filter by tag")
        self.tag.setAccessibleName("Activity tag")
        self.undone = QCheckBox("Include corrected history (Undone)")
        self.undone.setAccessibleName("Include undone task completions")
        if activity:
            grid.addWidget(self.kind, 4, 0)
            grid.addWidget(self.task_list, 4, 1)
            grid.addWidget(self.tag, 4, 2)
            grid.addWidget(self.undone, 5, 0, 1, 3)
        self.period.currentIndexChanged.connect(self._period_changed)
        self.start.dateChanged.connect(self.changed)
        self.end.dateChanged.connect(self.changed)
        self.search.textChanged.connect(self.changed)
        self.kind.currentIndexChanged.connect(self.changed)
        self.task_list.currentIndexChanged.connect(self.changed)
        self.tag.textChanged.connect(self.changed)
        self.undone.toggled.connect(self.changed)
        self._period_changed()

    def _period_changed(self, *_):
        custom = self.period.currentIndex() == 2
        self.start.setEnabled(custom)
        self.end.setEnabled(custom)
        for control in (self.start, self.end, self.from_label, self.through_label):
            control.setVisible(custom)
        self.changed.emit()

    def set_compact(self, compact: bool) -> None:
        if self.kind.parent() is None:
            return
        grid = self.layout()
        for control in (self.kind, self.task_list, self.tag, self.undone):
            grid.removeWidget(control)
        for index, control in enumerate((self.kind, self.task_list, self.tag)):
            grid.addWidget(
                control,
                4 + index if compact else 4,
                0 if compact else index,
                1,
                3 if compact else 1,
            )
        grid.addWidget(self.undone, 7 if compact else 5, 0, 1, 3)

    def options(self, timezone: str, *, limit: int = 200) -> LogFilter:
        today = datetime.now(ZoneInfo(timezone)).date()
        period = self.period.currentIndex()
        start, end = None, None
        if period == 0:
            start = end = today
        elif period == 1:
            start, end = today - timedelta(days=6), today
        elif period == 2:
            start, end = self.start.date().toPython(), self.end.date().toPython()
        return LogFilter(
            start=start,
            end=end,
            timezone=timezone,
            search=self.search.text(),
            kind=self.kind.currentData(),
            list_id=self.task_list.currentData(),
            tag=self.tag.text().strip() or None,
            include_undone=self.undone.isChecked(),
            limit=limit,
        )


class ProgressDialog(QDialog):
    def __init__(
        self,
        workspace: LoggingWorkspace,
        entry: ActivityEntry | None = None,
        task_id: str | None = None,
    ):
        super().__init__(workspace.pages["Activity"])
        self.workspace = workspace
        self.entry = entry
        self.setWindowTitle(
            "Completion note" if entry and entry.kind == "task_completion" else "Log progress"
        )
        self.setMinimumWidth(400)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.description = QLineEdit(entry.description if entry else "")
        self.description.setMaxLength(240)
        self.when = QDateTimeEdit()
        self.when.setCalendarPopup(True)
        self.when.setDisplayFormat("yyyy-MM-dd HH:mm")
        stamp = (entry.occurred_at if entry else datetime.now(UTC)).astimezone(
            ZoneInfo(workspace.timezone)
        )
        self.when.setDateTime(QDateTime(stamp.replace(tzinfo=None)))
        self.task = QComboBox()
        self.task.addItem("No linked task", None)
        for identifier, label in workspace.tasks:
            self.task.addItem(label, identifier)
        linked = str(entry.task_id) if entry and entry.task_id else task_id
        if linked:
            index = self.task.findData(linked)
            if index < 0:
                self.task.addItem("Original task unavailable", linked)
                index = self.task.count() - 1
            self.task.setCurrentIndex(index)
        self.task_list = QComboBox()
        self.task_list.addItem("No list", None)
        for identifier, label in workspace.lists:
            self.task_list.addItem(label, identifier)
        if entry and entry.list_id:
            index = self.task_list.findData(str(entry.list_id))
            if index < 0:
                self.task_list.addItem(
                    entry.list_name or "Original list unavailable", str(entry.list_id)
                )
                index = self.task_list.count() - 1
            self.task_list.setCurrentIndex(index)
        self.tags = QLineEdit(", ".join(entry.tags) if entry else "")
        self.notes = QPlainTextEdit(entry.notes if entry else "")
        self.notes.setTabChangesFocus(True)
        self.notes.setMaximumHeight(180)
        self.duration = QSpinBox()
        self.duration.setRange(0, 525_600)
        self.duration.setSpecialValueText("Not tracked")
        self.duration.setSuffix(" minutes")
        self.duration.setValue(entry.duration_minutes or 0 if entry else 0)
        for label, control in (
            ("Work or result (required)", self.description),
            (f"When ({workspace.timezone})", self.when),
            ("Task (optional)", self.task),
            ("List (optional)", self.task_list),
            ("Tags (optional, comma separated)", self.tags),
            ("Notes (optional)", self.notes),
            ("Duration (optional, minutes)", self.duration),
        ):
            control.setAccessibleName(label)
            form.addRow(label, control)
        layout.addLayout(form)
        layout.addWidget(_label("Linking progress never completes or changes the task."))
        self.status = StatusLabel("")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setAccessibleName("Progress save or validation status")
        layout.addWidget(self.status)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self._save)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        if entry and entry.kind == "task_completion":
            for control in (
                self.description,
                self.when,
                self.task,
                self.task_list,
                self.tags,
                self.duration,
            ):
                control.setEnabled(False)

    def _save(self):
        if self.workspace.masked:
            return
        values = {"notes": self.notes.toPlainText()}
        if not self.entry or self.entry.kind == "manual":
            if not self.description.text().strip():
                self.status.setText(
                    "Enter a short description of the work or result before saving."
                )
                self.description.setFocus()
                return
            wall = self.when.dateTime().toPython().replace(tzinfo=None)
            zone = ZoneInfo(self.workspace.timezone)
            aware = wall.replace(tzinfo=zone)
            if (
                aware.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != wall
                or aware.utcoffset() != wall.replace(tzinfo=zone, fold=1).utcoffset()
            ):
                self.status.setText(
                    "Choose a time that exists once in this timezone. Use UTC for a clock-change hour."
                )
                return
            values.update(
                description=self.description.text(),
                occurred_at=aware.astimezone(UTC),
                duration_minutes=self.duration.value() or None,
                task_id=UUID(self.task.currentData()) if self.task.currentData() else None,
                list_id=UUID(self.task_list.currentData())
                if self.task_list.currentData()
                else None,
                tags=tuple(tag.strip() for tag in self.tags.text().split(",") if tag.strip()),
            )
        self.workspace.requested.emit(
            "save_activity", {"dialog": self, "entry": self.entry, "values": values}
        )


class LoggingWorkspace(QObject):
    requested = Signal(str, object)
    privacy_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.timezone = "UTC"
        self.masked = False
        self.tasks: tuple[tuple[str, str], ...] = ()
        self.lists: tuple[tuple[str, str], ...] = ()
        self.pages: dict[str, QWidget] = {}
        self.filters: dict[str, LogFilters] = {}
        self.statuses: dict[str, QLabel] = {}
        self.limits = {"Activity": 200, "Reflection": 200, "Summary": 200}
        self.activity: dict[str, ActivityEntry] = {}
        self.reflections: dict[str, ReflectionEntry] = {}
        self.reflection: ReflectionEntry | None = None
        self.dirty = False
        self.loading = False
        self.dialog: ProgressDialog | None = None
        self.private_draft = None
        self.action_layouts: list[QGridLayout] = []
        for name in ("Activity", "Reflection", "Summary"):
            page = QWidget()
            page.setObjectName(f"logging{name}Page")
            page.setAccessibleName(f"Logging · {name}")
            layout = QVBoxLayout(page)
            layout.setContentsMargins(24, 20, 24, 20)
            layout.setSpacing(12)
            heading = _label(f"Logging · {name}")
            heading.setObjectName("pageTitle")
            layout.addWidget(heading)
            filters = LogFilters(activity=name != "Reflection")
            if name == "Summary":
                filters.undone.hide()
            filters.changed.connect(lambda n=name: self.reload(n))
            self.filters[name] = filters
            layout.addWidget(filters)
            status = _label("")
            status.setAccessibleName(f"{name} status")
            self.statuses[name] = status
            layout.addWidget(status)
            self.pages[name] = page
        self._build_activity()
        self._build_reflection()
        self._build_summary()
        for name, content in tuple(self.pages.items()):
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setAccessibleName(f"Logging {name} page")
            scroll.setWidget(content)
            self.pages[name] = scroll
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(400)
        self.timer.timeout.connect(self.flush)

    def _build_activity(self):
        layout = self.pages["Activity"].layout()
        layout.addWidget(
            _label(
                "Completed tasks appear automatically. Log unplanned work or progress on an open task whenever useful."
            )
        )
        actions = QGridLayout()
        self.action_layouts.append(actions)
        self.add_button = _button("Log progress", self.log_progress, actions)
        self.edit_button = _button("Edit entry / note", self.edit_activity, actions)
        self.delete_button = _button("Delete entry", self.delete_activity, actions)
        layout.addLayout(actions)
        self.activity_list = QListWidget()
        self.activity_list.setAccessibleName("Activity grouped by local date, newest first")
        self.activity_list.currentItemChanged.connect(self._activity_selected)
        self.activity_list.itemActivated.connect(lambda _: self.edit_activity())
        layout.addWidget(self.activity_list, 1)
        self.activity_detail = QPlainTextEdit()
        self.activity_detail.setReadOnly(True)
        self.activity_detail.setMaximumHeight(150)
        self.activity_detail.setAccessibleName("Activity details and notes")
        layout.addWidget(self.activity_detail)
        actions = QGridLayout()
        self.action_layouts.append(actions)
        self.open_task_button = _button("Open linked task", self.open_task, actions)
        self.reflect_button = _button("Reflect on this day", self.reflect_on_day, actions)
        self.more_activity = _button("Load more activity", lambda: self.more("Activity"), actions)
        layout.addLayout(actions)
        self._activity_selected()

    def _build_reflection(self):
        layout = self.pages["Reflection"].layout()
        layout.addWidget(
            _label(
                "Private writing, saved on this device. Prompts are optional; write as much or as little as you like."
            )
        )
        actions = QGridLayout()
        self.action_layouts.append(actions)
        self.new_reflection_button = _button("Write a reflection", self.new_reflection, actions)
        self.delete_reflection_button = _button(
            "Delete reflection", self.delete_reflection, actions
        )
        self.more_reflection = _button(
            "Load more reflections", lambda: self.more("Reflection"), actions
        )
        layout.addLayout(actions)
        split = QSplitter(Qt.Orientation.Vertical)
        self.reflection_list = QListWidget()
        self.reflection_list.setAccessibleName("Saved reflections by date")
        self.reflection_list.currentItemChanged.connect(self._reflection_selected)
        split.addWidget(self.reflection_list)
        self.editor = QWidget()
        edit_layout = QVBoxLayout(self.editor)
        self.reflection_date = QDateEdit(QDate.currentDate())
        self.reflection_date.setCalendarPopup(True)
        self.reflection_date.setAccessibleName("Date this reflection is about")
        self.reflection_title = QLineEdit()
        self.reflection_title.setMaxLength(240)
        self.reflection_title.setPlaceholderText("Title (optional)")
        self.reflection_title.setAccessibleName("Reflection title (optional)")
        line = QHBoxLayout()
        line.addWidget(self.reflection_date)
        line.addWidget(self.reflection_title, 1)
        edit_layout.addLayout(line)
        self.prompt = QComboBox()
        self.prompt.addItems(
            ["Optional prompt · skip", "What moved forward?", "What should I carry into tomorrow?"]
        )
        self.prompt.setAccessibleName("Optional reflection prompt")
        self.prompt.currentTextChanged.connect(
            lambda text: self.body.setPlaceholderText(
                text if self.prompt.currentIndex() else "Write freely…"
            )
        )
        edit_layout.addWidget(self.prompt)
        self.body = QPlainTextEdit()
        self.body.setPlaceholderText("Write freely…")
        self.body.setAccessibleName("Reflection writing")
        self.body.setStyleSheet("QPlainTextEdit { padding: 16px; }")
        edit_layout.addWidget(self.body, 1)
        self.reflection_task = QComboBox()
        self.reflection_task.addItem("No linked task", None)
        self.reflection_task.setAccessibleName("Optional reflection task link")
        edit_layout.addWidget(self.reflection_task)
        self.saved = StatusLabel("Choose Write a reflection to begin.")
        self.saved.setTextFormat(Qt.TextFormat.PlainText)
        self.saved.setWordWrap(True)
        self.saved.setAccessibleName("Reflection saved state")
        edit_layout.addWidget(self.saved)
        line = QGridLayout()
        self.action_layouts.append(line)
        _button("View this day's Activity", self.view_reflection_activity, line)
        _button("Link selected Activity entry", self.link_activity, line)
        edit_layout.addLayout(line)
        self.links = _label("")
        edit_layout.addWidget(self.links)
        split.addWidget(self.editor)
        split.setStretchFactor(1, 3)
        layout.addWidget(split, 1)
        self.editor.setEnabled(False)
        self.body.textChanged.connect(self._changed)
        self.reflection_title.textChanged.connect(self._changed)
        self.reflection_date.dateChanged.connect(self._changed)
        self.reflection_task.currentIndexChanged.connect(self._changed)

    def _build_summary(self):
        layout = self.pages["Summary"].layout()
        self.summary_text = QPlainTextEdit()
        self.summary_text.setReadOnly(True)
        self.summary_text.setAccessibleName("Factual activity summary")
        layout.addWidget(self.summary_text, 1)

    def set_compact(self, compact: bool) -> None:
        for filters in self.filters.values():
            filters.set_compact(compact)
        for layout in self.action_layouts:
            buttons = [layout.itemAt(index).widget() for index in range(layout.count())]
            for button in buttons:
                layout.removeWidget(button)
            for index, button in enumerate(buttons):
                layout.addWidget(button, index if compact else 0, 0 if compact else index)

    def reload(self, name: str):
        if name == "Reflection" and not self.flush():
            return
        if not self.masked:
            self.requested.emit("query", name)

    def more(self, name: str):
        self.limits[name] = min(10_000, self.limits[name] + 200)
        self.reload(name)

    def log_progress(self, _checked=False, *, task_id: str | None = None):
        if self.masked:
            return
        self.dialog = ProgressDialog(self, task_id=task_id)
        self.dialog.open()

    def selected_activity(self) -> ActivityEntry | None:
        item = self.activity_list.currentItem()
        return self.activity.get(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def edit_activity(self):
        if self.masked or (entry := self.selected_activity()) is None:
            return
        self.dialog = ProgressDialog(self, entry)
        self.dialog.open()

    def delete_activity(self):
        if not self.masked and (entry := self.selected_activity()):
            self.requested.emit("delete_activity", str(entry.entry_id))

    def _activity_selected(self, *_):
        entry = self.selected_activity()
        self.edit_button.setEnabled(bool(entry) and not self.masked)
        self.delete_button.setEnabled(bool(entry) and not self.masked)
        self.open_task_button.setEnabled(
            bool(entry and entry.task_id and str(entry.task_id) in dict(self.tasks))
            and not self.masked
        )
        self.reflect_button.setEnabled(bool(entry) and not self.masked)
        if entry and not self.masked:
            parts = [
                entry.description,
                entry.notes,
                f"List: {entry.list_name or 'No list'}",
                f"Tags: {', '.join(entry.tags) or 'None'}",
            ]
            if entry.task_id and str(entry.task_id) not in dict(self.tasks):
                parts.append("Original task is no longer available. History is preserved.")
            if entry.duration_minutes is not None:
                parts.append(f"Duration you entered: {entry.duration_minutes} minutes")
            if entry.undone_at:
                parts.append("Undone · excluded from completed counts")
            self.activity_detail.setPlainText("\n\n".join(part for part in parts if part))
        else:
            self.activity_detail.clear()

    def render_activity(self, entries: list[ActivityEntry]):
        selected = self.selected_activity()
        self.activity = {str(entry.entry_id): entry for entry in entries}
        blocker = QSignalBlocker(self.activity_list)
        self.activity_list.clear()
        previous_date = None
        for entry in entries:
            local = entry.occurred_at.astimezone(ZoneInfo(self.timezone))
            if local.date() != previous_date:
                item = QListWidgetItem(local.date().isoformat())
                item.setFlags(Qt.ItemFlag.NoItemFlags)
                self.activity_list.addItem(item)
                previous_date = local.date()
            kind = "Completed task" if entry.kind == "task_completion" else "Manual progress"
            correction = " · Undone" if entry.undone_at else ""
            item = QListWidgetItem(f"{local:%H:%M} · {kind}{correction} · {entry.description}")
            item.setData(Qt.ItemDataRole.UserRole, str(entry.entry_id))
            self.activity_list.addItem(item)
            if selected and selected.entry_id == entry.entry_id:
                self.activity_list.setCurrentItem(item)
        del blocker
        self.more_activity.setEnabled(
            len(entries) >= self.limits["Activity"] and self.limits["Activity"] < 10_000
        )
        self.statuses["Activity"].setText(
            f"{len(entries)} entries shown."
            if entries
            else "No activity in this view. Complete a task or choose Log progress."
        )
        self._activity_selected()

    def open_task(self):
        if not self.masked and (entry := self.selected_activity()) and entry.task_id:
            self.requested.emit("open_task", str(entry.task_id))

    def reflect_on_day(self):
        if (entry := self.selected_activity()) and not self.masked:
            self.new_reflection(day=entry.occurred_at.astimezone(ZoneInfo(self.timezone)).date())
            self.requested.emit("navigate", "Reflection")

    def new_reflection(self, _checked=False, *, day: date | None = None):
        if self.masked or not self.flush():
            return
        self.reflection = ReflectionEntry(
            display_date=day or self.reflection_date.date().toPython()
        )
        self._load_editor()
        self.dirty = True
        self.flush()
        self.body.setFocus()

    def _reflection_selected(self, current, _previous):
        if self.masked or not current:
            return
        # Saving refreshes the history list and destroys its old Qt items.
        # Capture identity before flushing a pending draft.
        identifier = current.data(Qt.ItemDataRole.UserRole)
        if not self.flush():
            self.render_reflections(list(self.reflections.values()))
            return
        self.reflection = self.reflections.get(identifier)
        self._load_editor()
        self.requested.emit("query", "Reflection")

    def _load_editor(self):
        self.loading = True
        entry = self.reflection
        self.editor.setEnabled(entry is not None and not self.masked)
        self.reflection_title.setText(entry.title if entry and not self.masked else "")
        self.body.setPlainText(entry.body if entry and not self.masked else "")
        if entry:
            self.reflection_date.setDate(QDate(entry.display_date))
            linked = str(entry.task_id) if entry.task_id else None
            index = self.reflection_task.findData(linked)
            if linked and index < 0:
                self.reflection_task.addItem("Original task unavailable", linked)
                index = self.reflection_task.count() - 1
            self.reflection_task.setCurrentIndex(max(0, index))
        self.links.setText(f"Linked Activity entries: {len(entry.activity_ids)}" if entry else "")
        self.saved.setText(
            "Saved on this device"
            if entry and not self.dirty
            else "Choose Write a reflection to begin."
        )
        self.loading = False

    def _changed(self, *_):
        if self.loading or self.masked or self.reflection is None:
            return
        self.dirty = True
        self.saved.setText("Saving on this device…")
        self.timer.start()

    def flush(self) -> bool:
        if self.masked:
            return True
        if not self.dirty or self.loading or self.masked or self.reflection is None:
            return not self.dirty
        self.timer.stop()
        try:
            entry = ReflectionEntry.model_validate(
                {
                    **self.reflection.model_dump(),
                    "title": self.reflection_title.text(),
                    "body": self.body.toPlainText(),
                    "display_date": self.reflection_date.date().toPython(),
                    "task_id": UUID(self.reflection_task.currentData())
                    if self.reflection_task.currentData()
                    else None,
                }
            )
        except ValueError:
            self.saved.setText(
                "Not saved: writing must be at most 20,000 characters. Shorten it and try again."
            )
            return False
        self.requested.emit("save_reflection", entry)
        return not self.dirty

    def reflection_saved(self, entry: ReflectionEntry):
        self.reflection = entry
        self.dirty = False
        self.saved.setText("Saved on this device")

    def render_reflections(self, entries: list[ReflectionEntry]):
        self.reflections = {str(entry.reflection_id): entry for entry in entries}
        blocker = QSignalBlocker(self.reflection_list)
        self.reflection_list.clear()
        for entry in entries:
            item = QListWidgetItem(f"{entry.display_date} · {entry.title or 'Untitled reflection'}")
            item.setData(Qt.ItemDataRole.UserRole, str(entry.reflection_id))
            self.reflection_list.addItem(item)
            if self.reflection and self.reflection.reflection_id == entry.reflection_id:
                self.reflection_list.setCurrentItem(item)
        del blocker
        self.more_reflection.setEnabled(
            len(entries) >= self.limits["Reflection"] and self.limits["Reflection"] < 10_000
        )
        self.statuses["Reflection"].setText(
            f"{len(entries)} reflections shown. Multiple entries per date are welcome."
            if entries
            else "No reflections in this view. Writing is optional."
        )

    def delete_reflection(self):
        if not self.masked and self.reflection:
            self.timer.stop()
            self.requested.emit("delete_reflection", str(self.reflection.reflection_id))

    def view_reflection_activity(self):
        day = self.reflection_date.date()
        filters = self.filters["Activity"]
        filters.period.setCurrentIndex(2)
        filters.start.setDate(day)
        filters.end.setDate(day)
        self.requested.emit("navigate", "Activity")

    def link_activity(self):
        if self.reflection and (entry := self.selected_activity()) and not self.masked:
            if (
                len(self.reflection.activity_ids) >= 100
                and entry.entry_id not in self.reflection.activity_ids
            ):
                self.saved.setText("This reflection already has the maximum of 100 Activity links.")
                return
            self.reflection = self.reflection.model_copy(
                update={
                    "activity_ids": tuple(
                        dict.fromkeys((*self.reflection.activity_ids, entry.entry_id))
                    )
                }
            )
            self._changed()
            self.flush()
            self.links.setText(f"Linked Activity entries: {len(self.reflection.activity_ids)}")

    def render_summary(self, facts: dict[str, object]):
        duration = facts["duration_minutes"]
        text = [
            f"Tasks completed: {facts['task_completion']}",
            f"Manual progress entries: {facts['manual']}",
            f"Sum of durations you entered: {duration} minutes"
            if duration is not None
            else "Duration was not tracked.",
            "Undone completions are excluded.",
        ]
        for heading, key in (("By day", "days"), ("By task list", "lists"), ("By tag", "tags")):
            text.extend(["", heading])
            text.extend(f"{label}: {count}" for label, count in sorted(facts[key].items()))
        self.summary_text.setPlainText("\n".join(text))

    def set_private(self, private: bool):
        if private == self.masked:
            return
        if private and not self.masked:
            self.flush()
            if self.dirty:
                self.private_draft = (
                    self.reflection_title.text(),
                    self.body.toPlainText(),
                    self.reflection_date.date(),
                    self.reflection_task.currentData(),
                )
        self.masked = private
        if private:
            if self.dialog:
                self.dialog.reject()
                self.dialog.deleteLater()
                self.dialog = None
            self.activity_list.clear()
            self.reflection_list.clear()
            self.activity_detail.clear()
            self.summary_text.setPlainText("Logging is hidden for privacy.")
            self._load_editor()
            for status in self.statuses.values():
                status.setText("Logging is hidden for privacy.")
        else:
            self._load_editor()
            if self.private_draft:
                title, body, day, task = self.private_draft
                self.loading = True
                self.reflection_title.setText(title)
                self.body.setPlainText(body)
                self.reflection_date.setDate(day)
                self.reflection_task.setCurrentIndex(max(0, self.reflection_task.findData(task)))
                self.loading = False
                self.private_draft = None
                self.saved.setText(
                    "Not saved. Your writing remains here; saving will retry when you edit or navigate."
                )
        for page in self.pages.values():
            page.setEnabled(not private)
            page.widget().setVisible(not private)
        self.privacy_changed.emit(private)
