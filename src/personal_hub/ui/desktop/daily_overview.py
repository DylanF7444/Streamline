"""A local daily review with factual history and an optional reflection draft."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from PySide6.QtCore import QDate, QEvent, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QGridLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from personal_hub.logging.contracts import ActivityEntry, LogFilter, ReflectionEntry
from personal_hub.ui.desktop.status_label import StatusLabel


class DailyOverview(QWidget):
    requested = Signal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.timezone = "UTC"
        self.day = datetime.now(ZoneInfo(self.timezone)).date()
        self.follow_today = True
        self.limit = 200
        self.masked = False
        self.dirty = False
        self.reflection: ReflectionEntry | None = None
        self.reflections: dict[str, ReflectionEntry] = {}
        self.loaded_day: date | None = None
        self.private_draft: tuple[str, str] | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self.date_actions = QGridLayout()
        self.previous = QPushButton("Previous day")
        self.date = QDateEdit(QDate(self.day))
        self.date.setCalendarPopup(True)
        self.date.setDisplayFormat("ddd, MMM d, yyyy")
        self.date.setAccessibleName("Daily overview date")
        # The repository uses an exclusive next-day upper bound.
        self.date.setMaximumDate(QDate(9999, 12, 30))
        self.next = QPushButton("Next day")
        self.today = QPushButton("Today")
        for index, control in enumerate((self.previous, self.date, self.next, self.today)):
            self.date_actions.addWidget(control, 0, index)
        layout.addLayout(self.date_actions)
        self.previous.clicked.connect(lambda: self.select_day(self.date.date().addDays(-1)))
        self.next.clicked.connect(lambda: self.select_day(self.date.date().addDays(1)))
        self.today.clicked.connect(self.show_today)
        self.date.dateChanged.connect(self.select_day)
        self.caption = QLabel()
        self.caption.setWordWrap(True)
        layout.addWidget(self.caption)
        self.facts = QPlainTextEdit()
        self.facts.setReadOnly(True)
        self.facts.setTabChangesFocus(True)
        self.facts.setAccessibleName("Daily overview totals")
        layout.addWidget(self.facts)
        self.completed = self._section("Tasks completed", layout)
        self.progress = self._section("Logged progress", layout)
        self.more = QPushButton("Load more of this day's activity")
        self.more.clicked.connect(self.load_more)
        layout.addWidget(self.more)
        reflection_group = QGroupBox("Reflection")
        reflection_layout = QVBoxLayout(reflection_group)
        help_text = QLabel(
            "Writing is optional and autosaves on this device. "
            "What went well? What would you like to carry into tomorrow?"
        )
        help_text.setWordWrap(True)
        reflection_layout.addWidget(help_text)
        self.chooser = QComboBox()
        self.chooser.setAccessibleName("Reflection for the selected day")
        self.chooser.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.chooser.setMinimumContentsLength(18)
        self.chooser.addItem("New reflection", None)
        self.chooser.currentIndexChanged.connect(self.choose_reflection)
        reflection_layout.addWidget(self.chooser)
        self.title = QLineEdit()
        self.title.setMaxLength(240)
        self.title.setPlaceholderText("Title (optional)")
        self.title.setAccessibleName("Daily reflection title (optional)")
        reflection_layout.addWidget(self.title)
        self.body = QPlainTextEdit()
        self.body.setPlaceholderText("Write about your day…")
        self.body.setAccessibleName("Daily reflection writing")
        self.body.setTabChangesFocus(True)
        self.body.setMinimumHeight(180)
        reflection_layout.addWidget(self.body)
        self.saved = StatusLabel("Write whenever you like. Your reflection will save here.")
        self.saved.setTextFormat(Qt.TextFormat.PlainText)
        self.saved.setWordWrap(True)
        self.saved.setAccessibleName("Daily reflection saved state")
        reflection_layout.addWidget(self.saved)
        layout.addWidget(reflection_group)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(400)
        self.timer.timeout.connect(self.flush)
        self.title.textChanged.connect(self.changed)
        self.body.textChanged.connect(self.changed)
        self._size_sections()

    @staticmethod
    def _section(title, layout):
        group = QGroupBox(title)
        content = QVBoxLayout(group)
        entries = QListWidget()
        entries.setAccessibleName(f"Daily overview · {title}")
        entries.setWordWrap(True)
        content.addWidget(entries)
        layout.addWidget(group)
        return entries

    def _size_sections(self):
        self.facts.setFixedHeight(self.facts.fontMetrics().lineSpacing() * 4 + 20)
        for entries in (self.completed, self.progress):
            rows = max(1, min(entries.count(), 4))
            height = entries.fontMetrics().lineSpacing() * 2 * rows + 20
            entries.setFixedHeight(max(70, min(220, height)))

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange and hasattr(self, "progress"):
            self._size_sections()

    def set_compact(self, compact):
        for index, control in enumerate((self.previous, self.date, self.next, self.today)):
            self.date_actions.removeWidget(control)
            self.date_actions.addWidget(control, index if compact else 0, 0 if compact else index)

    def options(self):
        return LogFilter(start=self.day, end=self.day, timezone=self.timezone, limit=self.limit)

    def show_today(self):
        self.select_day(QDate(datetime.now(ZoneInfo(self.timezone)).date()), follow_today=True)

    def select_day(self, selected: QDate, *, follow_today=False):
        if self.masked:
            return
        if not self.flush():
            with QSignalBlocker(self.date):
                self.date.setDate(QDate(self.day))
            return
        with QSignalBlocker(self.date):
            self.date.setDate(selected)
        self.follow_today = follow_today
        if self.date.date().toPython() != self.day:
            self.day = self.date.date().toPython()
            self.limit = 200
            self.reflection = None
            self.loaded_day = None
            self._load_editor()
        self.requested.emit("query", "Summary")

    def load_more(self):
        if not self.masked:
            self.limit = min(10_000, self.limit + 200)
            self.requested.emit("query", "Summary")

    def render(self, facts, entries: list[ActivityEntry], reflections: list[ReflectionEntry]):
        if self.masked:
            return
        self.caption.setText(
            f"{self.day:%A, %B %d, %Y} · {self.timezone}. "
            "Updates as you log activity. Undone completions are excluded."
        )
        duration = facts["duration_minutes"]
        self.facts.setPlainText(
            f"Tasks completed: {facts['task_completion']}\n"
            f"Manual progress entries: {facts['manual']}\n"
            + (
                f"Sum of durations you entered: {duration} minutes"
                if duration is not None
                else "Duration was not tracked."
            )
        )
        self.completed.clear()
        self.progress.clear()
        for entry in entries:
            local = entry.occurred_at.astimezone(ZoneInfo(self.timezone))
            context = [entry.list_name or "No list", *(f"#{tag}" for tag in entry.tags)]
            if entry.duration_minutes is not None:
                context.append(f"{entry.duration_minutes} minutes entered")
            item = QListWidgetItem(f"{local:%H:%M} · {entry.description}\n" + " · ".join(context))
            target = self.completed if entry.kind == "task_completion" else self.progress
            target.addItem(item)
        total = facts["task_completion"] + facts["manual"]
        for widget, kind, empty in (
            (self.completed, "task_completion", "No tasks completed on this day."),
            (self.progress, "manual", "No progress logged on this day."),
        ):
            if not widget.count():
                item = QListWidgetItem(
                    empty if not facts[kind] else "Load more to see these entries."
                )
                item.setFlags(Qt.ItemFlag.NoItemFlags)
                widget.addItem(item)
        self._size_sections()
        self.more.setVisible(len(entries) < total)
        self.more.setEnabled(self.limit < 10_000)
        self.more.setText(
            f"Showing {len(entries)} of {total} entries · Load more"
            if self.limit < 10_000
            else f"Showing {len(entries)} of {total} entries · Use Activity filters to see more"
        )
        self.reflections = {str(entry.reflection_id): entry for entry in reflections}
        if not self.dirty:
            identifier = str(self.reflection.reflection_id) if self.reflection else None
            self.reflection = self.reflections.get(identifier)
            if self.loaded_day != self.day:
                self.reflection = next(iter(reflections), None)
            self.loaded_day = self.day
            with QSignalBlocker(self.chooser):
                self.chooser.clear()
                self.chooser.addItem("New reflection", None)
                for entry in reflections:
                    self.chooser.addItem(
                        entry.title or "Untitled reflection", str(entry.reflection_id)
                    )
                identifier = str(self.reflection.reflection_id) if self.reflection else None
                self.chooser.setCurrentIndex(max(0, self.chooser.findData(identifier)))
            # Refreshes must preserve cursor, selection, and the user's place in the draft.
            self._load_editor()

    def _load_editor(self):
        entry = self.reflection if not self.masked else None
        for control, text in (
            (self.title, entry.title if entry else ""),
            (self.body, entry.body if entry else ""),
        ):
            current = control.text() if control is self.title else control.toPlainText()
            if current != text:
                with QSignalBlocker(control):
                    control.setText(text) if control is self.title else control.setPlainText(text)
        self.saved.setText(
            "Saved on this device"
            if entry
            else "Write whenever you like. Your reflection will save here."
        )

    def choose_reflection(self, _index):
        if self.masked:
            return
        identifier = self.chooser.currentData()
        if not self.flush():
            with QSignalBlocker(self.chooser):
                current = str(self.reflection.reflection_id) if self.reflection else None
                self.chooser.setCurrentIndex(max(0, self.chooser.findData(current)))
            return
        self.reflection = self.reflections.get(identifier)
        with QSignalBlocker(self.chooser):
            self.chooser.setCurrentIndex(max(0, self.chooser.findData(identifier)))
        self._load_editor()

    def changed(self):
        if self.masked:
            return
        if self.reflection is None:
            self.reflection = ReflectionEntry(display_date=self.day)
        self.dirty = True
        self.saved.setText("Saving on this device…")
        self.timer.start()

    def flush(self):
        if self.masked or not self.dirty:
            return True
        self.timer.stop()
        try:
            entry = ReflectionEntry.model_validate(
                {
                    **self.reflection.model_dump(),
                    "title": self.title.text(),
                    "body": self.body.toPlainText(),
                }
            )
        except ValueError:
            self.saved.setText(
                "Not saved: writing must be at most 20,000 characters. Shorten it and try again."
            )
            return False
        self.requested.emit("save_daily_reflection", entry)
        return not self.dirty

    def reflection_saved(self, entry):
        self.reflection = entry
        self.dirty = False
        self.saved.setText("Saved on this device")

    def set_private(self, private):
        if private == self.masked:
            return
        if private:
            self.flush()
            if self.dirty:
                self.private_draft = (self.title.text(), self.body.toPlainText())
        self.masked = private
        if private:
            self.completed.clear()
            self.progress.clear()
            self.caption.clear()
            self.facts.setPlainText("Logging is hidden for privacy.")
            with QSignalBlocker(self.chooser):
                self.chooser.clear()
        self._load_editor()
        if not private and self.private_draft:
            with QSignalBlocker(self.title), QSignalBlocker(self.body):
                self.title.setText(self.private_draft[0])
                self.body.setPlainText(self.private_draft[1])
            self.private_draft = None
            self.saved.setText(
                "Not saved. Your writing remains here; saving will retry when you edit or navigate."
            )
