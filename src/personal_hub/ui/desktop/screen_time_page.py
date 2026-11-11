"""Accessible screen-time review, explicit recording consent and local controls."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from PySide6.QtCore import QDate, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QPainter
from PySide6.QtWidgets import (
    QCheckBox,
    QDateEdit,
    QFormLayout,
    QGroupBox,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.exc import SQLAlchemyError

from personal_hub.screen_time.collector import Observation, ScreenTimeRecorder, WindowsObserver
from personal_hub.screen_time.contracts import AppRule, ScreenTimeSettings
from personal_hub.screen_time.repository import bounds
from personal_hub.ui.desktop.browser_time_panel import BrowserTimePanel


def duration(seconds):
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m {seconds % 60}s"


class UsageTimeline(QWidget):
    def __init__(self):
        super().__init__()
        self.segments = []
        self.setMinimumHeight(42)
        self.setAccessibleName(
            "Day timeline; gaps are unrecorded. Exact intervals follow in the table."
        )

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#252a33"))
        for start, end in self.segments:
            painter.fillRect(
                int(start * self.width()),
                5,
                max(1, int((end - start) * self.width())),
                self.height() - 10,
                QColor("#53bda4"),
            )


class ScreenTimePage(QScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setAccessibleName("Screen time")
        content = QWidget()
        self.setWidget(content)
        self.layout_box = QVBoxLayout(content)
        self.private = False
        self.controller = None
        self.window = parent
        self.status = QLabel("Paused · Record foreground app usage on this PC")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.layout_box.addWidget(self.status)
        help_text = QLabel(
            "Opt in to record app names and durations on this PC. Windows titles, text, "
            "screenshots and website addresses are excluded from app recording. Collection continues "
            "in the tray while Hub runs. Idle or unavailable periods leave gaps."
        )
        help_text.setWordWrap(True)
        self.layout_box.addWidget(help_text)
        self.toggle = QPushButton("Start recording screen time")
        self.toggle.clicked.connect(self.toggle_recording)
        self.layout_box.addWidget(self.toggle)
        self.settings_button = QPushButton("Recording settings and app rules")
        self.settings_button.setCheckable(True)
        self.layout_box.addWidget(self.settings_button)
        settings_group = QGroupBox("Recording and privacy")
        self.settings_group = settings_group
        settings_group.hide()
        self.settings_button.toggled.connect(settings_group.setVisible)
        form = QFormLayout(settings_group)
        self.startup = QCheckBox("Start recording when Hub opens")
        self.card = QCheckBox("Show screen time in Daily overview")
        self.idle = QSpinBox()
        self.idle.setRange(30, 1800)
        self.idle.setSuffix(" seconds")
        self.exclusions = QLineEdit()
        self.exclusions.setMaxLength(160 * 256)
        self.exclusions.setPlaceholderText("passwordmanager.exe, banking.exe")
        form.addRow(self.startup)
        form.addRow(self.card)
        form.addRow("Pause after no input", self.idle)
        form.addRow("Exclude executable names", self.exclusions)
        self.save = QPushButton("Save recording settings")
        self.save.clicked.connect(self.save_settings)
        form.addRow(self.save)
        self.layout_box.addWidget(settings_group)
        dates = QFormLayout()
        self.start_date = QDateEdit(QDate.currentDate())
        self.end_date = QDateEdit(QDate.currentDate())
        for control in (self.start_date, self.end_date):
            control.setCalendarPopup(True)
            control.setMaximumDate(QDate(9999, 12, 30))
        dates.addRow("From", self.start_date)
        dates.addRow("Through", self.end_date)
        self.filter = QLineEdit()
        self.filter.setMaxLength(160)
        self.filter.setPlaceholderText("Filter app name or category")
        dates.addRow("Filter", self.filter)
        self.layout_box.addLayout(dates)
        self.today = QPushButton("Today")
        self.today.clicked.connect(self.show_today)
        self.layout_box.addWidget(self.today)
        self.total = QLabel()
        self.total.setWordWrap(True)
        self.total.setTextFormat(Qt.TextFormat.PlainText)
        self.layout_box.addWidget(self.total)
        self.timeline = UsageTimeline()
        self.layout_box.addWidget(self.timeline)
        self.apps = self.table(
            "Top apps and categories", ["App", "Category", "Tracked foreground time"]
        )
        self.categories = self.table("Category totals", ["Category", "Tracked foreground time"])
        self.sessions = self.table("Session timeline", ["Started", "Ended", "App", "Duration"])
        self.limit = 200
        self.detail_caption = QLabel()
        self.detail_caption.setWordWrap(True)
        self.layout_box.addWidget(self.detail_caption)
        self.more = QPushButton("Load more intervals")
        self.more.clicked.connect(self.load_more)
        self.layout_box.addWidget(self.more)
        self.week = self.table(
            "Seven-day trend ending on selected date", ["Date", "Tracked foreground time"]
        )
        rules = QGroupBox("App display and category")
        rules.hide()
        self.settings_button.toggled.connect(rules.setVisible)
        rule_form = QFormLayout(rules)
        self.rule_app, self.rule_name, self.rule_category = QLineEdit(), QLineEdit(), QLineEdit()
        for editor in (self.rule_app, self.rule_name):
            editor.setMaxLength(160)
        self.rule_category.setMaxLength(64)
        rule_form.addRow("Executable name", self.rule_app)
        rule_form.addRow("Display name", self.rule_name)
        rule_form.addRow("Category", self.rule_category)
        self.rule_save = QPushButton("Save app rule")
        self.rule_save.clicked.connect(self.save_rule)
        rule_form.addRow(self.rule_save)
        self.layout_box.addWidget(rules)
        self.delete = QPushButton("Preview deletion for selected dates")
        self.delete.clicked.connect(self.delete_history)
        self.layout_box.addWidget(self.delete)
        self.browser = BrowserTimePanel(self)
        self.layout_box.addWidget(self.browser)
        for editor in (self.start_date, self.end_date):
            editor.dateChanged.connect(self.query_changed)
        self.filter.textChanged.connect(self.query_changed)
        self.timer = QTimer(self)
        self.timer.setInterval(5000)
        self.timer.timeout.connect(self.tick)

    def table(self, title, headers):
        self.layout_box.addWidget(QLabel(title))
        table = QTableWidget(0, len(headers))
        table.setAccessibleName(title)
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setMinimumHeight(150)
        self.layout_box.addWidget(table)
        return table

    def bind(self, controller, window):
        self.controller, self.window = controller, window
        try:
            self.settings = ScreenTimeSettings.model_validate(
                controller.settings_repository.get("screen_time.settings", {})
            )
        except ValueError:
            self.settings = ScreenTimeSettings()
        self.startup.setChecked(self.settings.start_on_open)
        self.card.setChecked(self.settings.overview_card)
        self.idle.setValue(self.settings.idle_seconds)
        self.exclusions.setText(", ".join(self.settings.excluded_apps))
        self.observer = WindowsObserver()
        self.recorder = ScreenTimeRecorder(controller.screen_time_repository, self.settings)
        controller.screen_time_runtime = self.recorder
        self.browser.bind(controller)
        self.tray_action = QAction("Start screen-time recording", window)
        self.tray_action.triggered.connect(self.toggle_recording)
        window.tray_controller.menu.insertAction(
            window.tray_controller.quit_action, self.tray_action
        )
        window.destination_changed.connect(
            lambda destination: self.refresh() if destination == "Screen time" else None
        )
        self.daily_label = QLabel()
        self.daily_label.setWordWrap(True)
        self.daily_label.setTextFormat(Qt.TextFormat.PlainText)
        window.logging_workspace.daily.layout().insertWidget(4, self.daily_label)
        window.logging_workspace.daily.date.dateChanged.connect(self.refresh)
        if self.settings.start_on_open and self.observer.supported:
            self.recorder.running = True
        self.timer.start()
        self.tick()

    def toggle_recording(self):
        if not self.controller or self.controller.is_closed or self.window.locked:
            return
        if self.recorder.running:
            self.recorder.pause()
        elif self.observer.supported:
            self.recorder.reset()
            self.recorder.running = True
        else:
            self.status.setText("Automatic screen-time collection requires Windows.")
            return
        self.tick()

    def tick(self):
        if not self.controller or self.controller.is_closed:
            return
        try:
            observation = Observation(None)
            if self.window.locked:
                self.recorder.reset()
            elif self.recorder.running or self.browser.runtime.running:
                observation = self.observer.sample()
                if self.recorder.running:
                    self.recorder.observe(observation)
            self.browser.tick(observation)
            self.refresh()
        except (OSError, ValueError, SQLAlchemyError):
            self.recorder.pause()
            self.browser.runtime.pause()
            self.status.setText(
                "Recording paused because a sample could not be saved. Start to retry."
            )

    def save_settings(self):
        if self.private or not self.controller:
            return
        try:
            updated = ScreenTimeSettings(
                start_on_open=self.startup.isChecked(),
                overview_card=self.card.isChecked(),
                idle_seconds=self.idle.value(),
                rules=self.settings.rules,
                excluded_apps=tuple(
                    x.strip().casefold() for x in self.exclusions.text().split(",") if x.strip()
                ),
            )
            self.controller.settings_repository.set(
                "screen_time.settings", updated.model_dump(mode="json")
            )
            self.settings = self.recorder.settings = updated
            self.recorder.reset()
            self.refresh()
        except (ValueError, SQLAlchemyError):
            self.status.setText("Could not save settings. Use executable names without paths.")

    def save_rule(self):
        if self.private or not self.controller:
            return
        try:
            rules = dict(self.settings.rules)
            rules[self.rule_app.text().strip().casefold()] = AppRule(
                name=self.rule_name.text().strip(),
                category=self.rule_category.text().strip() or "Uncategorized",
            )
            updated = ScreenTimeSettings.model_validate(
                {**self.settings.model_dump(), "rules": rules}
            )
            self.controller.settings_repository.set(
                "screen_time.settings", updated.model_dump(mode="json")
            )
            self.settings = self.recorder.settings = updated
            self.refresh()
        except (ValueError, SQLAlchemyError):
            self.status.setText("Could not save app rule. Check the executable name and category.")

    def query_changed(self):
        self.limit = 200
        self.refresh()

    def show_today(self):
        day = datetime.now(ZoneInfo(self.controller.settings.default_timezone)).date()
        self.start_date.setDate(QDate(day))
        self.end_date.setDate(QDate(day))
        self.refresh()

    def load_more(self):
        self.limit = min(10000, self.limit + 200)
        self.refresh()

    @staticmethod
    def fill(table, rows):
        table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j, value in enumerate(row):
                table.setItem(i, j, QTableWidgetItem(str(value)))

    def refresh(self, *_):
        try:
            self._refresh()
        except (OSError, ValueError, SQLAlchemyError):
            if hasattr(self, "recorder"):
                self.recorder.pause()
            self.browser.runtime.pause()
            self.status.setText(
                "Screen-time history is unavailable. Check local storage and retry."
            )

    def _refresh(self):
        if not self.controller or self.controller.is_closed:
            return
        self.browser.refresh()
        running = self.recorder.running
        self.toggle.setText("Pause recording" if running else "Start recording screen time")
        self.tray_action.setText(
            "Pause screen-time recording" if running else "Start screen-time recording"
        )
        self.status.setText(
            self.recorder.status if not self.private else "Screen-time details hidden"
        )
        if self.private:
            return
        stored = ScreenTimeSettings.model_validate(
            self.controller.settings_repository.get("screen_time.settings", {})
        )
        if stored != self.settings:
            self.settings = self.recorder.settings = stored
            self.startup.setChecked(stored.start_on_open)
            self.card.setChecked(stored.overview_card)
            self.idle.setValue(stored.idle_seconds)
            self.exclusions.setText(", ".join(stored.excluded_apps))
        start, end = self.start_date.date().toPython(), self.end_date.date().toPython()
        if end < start:
            self.total.setText("Choose an end date on or after the start date.")
            for table in (self.apps, self.categories, self.sessions, self.week):
                table.setRowCount(0)
            self.timeline.segments = []
            self.timeline.update()
            self.detail_caption.clear()
            return
        zone_name = self.controller.settings.default_timezone
        zone = ZoneInfo(zone_name)
        repo = self.controller.screen_time_repository
        summary = repo.summary(start, end, zone_name)

        def label(app):
            rule = self.settings.rules.get(app, AppRule())
            return rule.name or app, rule.category

        query = self.filter.text().casefold()

        def matches(app):
            return query in f"{app} {' '.join(label(app))}".casefold()

        summary = {app: seconds for app, seconds in summary.items() if matches(app)}
        self.total.setText(
            f"This PC · {duration(sum(summary.values()))} tracked foreground time · "
            "5-second samples; idle and unrecorded periods leave gaps."
            if summary
            else "This PC · No recorded usage in the selected range."
        )
        self.fill(
            self.apps,
            [
                (*label(app), duration(seconds))
                for app, seconds in sorted(summary.items(), key=lambda item: -item[1])
            ],
        )
        lower, upper = bounds(start, end, zone_name)
        records = repo.records(start, end, zone_name, limit=self.limit)
        selected = [record for record in records if matches(record.app)]
        category_totals = {}
        for app, seconds in summary.items():
            category = label(app)[1]
            category_totals[category] = category_totals.get(category, 0) + seconds
        self.fill(
            self.categories,
            [
                (category, duration(seconds))
                for category, seconds in sorted(category_totals.items(), key=lambda item: -item[1])
            ],
        )
        self.detail_caption.setText(
            f"Showing matching intervals from the first {self.limit} records. "
            "Timeline uses these intervals; totals and trends cover all matching history."
            if len(records) == self.limit
            else "All matching intervals shown. Gaps are unrecorded."
        )
        self.fill(
            self.sessions,
            [
                (
                    max(r.start, lower).astimezone(zone).isoformat(timespec="seconds"),
                    min(r.end, upper).astimezone(zone).isoformat(timespec="seconds"),
                    label(r.app)[0],
                    duration(round((min(r.end, upper) - max(r.start, lower)).total_seconds())),
                )
                for r in selected
            ],
        )
        self.more.setVisible(len(records) == self.limit and self.limit < 10000)
        self.sessions.setAccessibleDescription(
            f"First {self.limit} intervals; totals cover all matching history."
        )
        span = (upper - lower).total_seconds()
        self.timeline.segments = [
            (
                (max(r.start, lower) - lower).total_seconds() / span,
                (min(r.end, upper) - lower).total_seconds() / span,
            )
            for r in selected
        ]
        self.timeline.update()
        days = [
            end - timedelta(days=i)
            for i in reversed(range(min(7, (end - datetime.min.date()).days + 1)))
        ]
        self.fill(
            self.week,
            [
                (
                    day.isoformat(),
                    duration(
                        sum(
                            value
                            for app, value in repo.summary(day, day, zone_name).items()
                            if matches(app)
                        )
                    ),
                )
                for day in days
            ],
        )
        daily = self.window.logging_workspace.daily
        self.daily_label.setVisible(self.settings.overview_card)
        if self.settings.overview_card:
            daily_usage = repo.summary(daily.day, daily.day, zone_name)
            total = sum(daily_usage.values())
            self.daily_label.setText(
                f"Screen time · This PC · {duration(total)} tracked foreground time"
                if daily_usage
                else "Screen time · This PC · No recorded usage"
            )

            site_usage = self.controller.browser_time_repository.summary(
                daily.day, daily.day, zone_name
            )
            self.daily_label.setText(
                self.daily_label.text()
                + f"\nBrowser sites · {duration(sum(site_usage.values()))} (included in browser app time when app recording is enabled)"
            )

    def set_state(self, *, private=False, locked=False):
        self.browser.set_state(private=private, locked=locked)
        changed = private != self.private or locked
        self.private = private
        if changed and hasattr(self, "recorder"):
            self.recorder.reset()
        for table in (self.apps, self.categories, self.sessions, self.week):
            table.setRowCount(0)
        self.total.clear()
        self.detail_caption.clear()
        self.timeline.segments = []
        self.timeline.update()
        self.rule_app.clear()
        self.rule_name.clear()
        self.rule_category.clear()
        self.filter.clear()
        self.exclusions.setText(
            ""
            if private or not hasattr(self, "settings")
            else ", ".join(self.settings.excluded_apps)
        )
        for control in (
            self.startup,
            self.card,
            self.idle,
            self.exclusions,
            self.save,
            self.rule_save,
            self.rule_app,
            self.rule_name,
            self.rule_category,
            self.delete,
            self.filter,
        ):
            control.setEnabled(not private)
        self.toggle.setEnabled(not locked)
        if hasattr(self, "daily_label"):
            self.daily_label.clear()
            self.daily_label.setVisible(False)
        self.refresh()

    def delete_history(self):
        if self.private or not self.controller:
            return
        start, end = self.start_date.date().toPython(), self.end_date.date().toPython()
        try:
            zone = self.controller.settings.default_timezone
            count = self.controller.screen_time_repository.delete_range(
                start, end, zone, preview=True
            )
            answer = QMessageBox.question(
                self,
                "Delete screen-time history",
                f"Delete {count} complete sessions overlapping these dates? Sessions crossing "
                "the range boundary are deleted in full. This affects screen-time history only.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes and not self.private:
                self.recorder.reset()
                self.controller.screen_time_repository.delete_range(start, end, zone)
                self.refresh()
        except (ValueError, SQLAlchemyError):
            self.status.setText("Could not delete history. Check the dates and retry.")

    def close_runtime(self):
        self.timer.stop()
        if hasattr(self.browser, "runtime"):
            self.browser.runtime.pause()
        if hasattr(self, "recorder"):
            self.recorder.pause()
