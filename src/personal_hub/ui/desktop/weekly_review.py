"""Accessible weekly review over existing local records and reflection drafts."""

from datetime import datetime
from zoneinfo import ZoneInfo

from PySide6.QtCore import QDate, QSignalBlocker, Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QGroupBox,
    QLabel,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from personal_hub.logging.contracts import LogFilter, ReflectionEntry
from personal_hub.logging.weekly_review import WeeklyReview, week_bounds
from personal_hub.ui.desktop.controls import WheelSafeComboBox
from personal_hub.ui.desktop.daily_overview import DailyOverview
from personal_hub.ui.desktop.readable import ReadableList, SummaryCards, present_item


def seconds_text(seconds):
    if seconds is None:
        return "No recorded usage"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m {seconds % 60}s"


def minutes_text(minutes):
    return f"{minutes} minutes" if minutes is not None else "Not tracked"


class WeeklyReviewPage(DailyOverview):
    route = "Weekly review"
    save_action = "save_weekly_reflection"
    step_days = 7

    def __init__(self, parent=None):
        super().__init__(parent)
        self.day = week_bounds(self.day)[0]
        with QSignalBlocker(self.date):
            self.date.setDate(QDate(self.day))
            self.date.setMinimumDate(QDate(1752, 9, 18))
            self.date.setMaximumDate(QDate(9999, 12, 26))
        self.date.setAccessibleName("Choose a date in the review week")
        self.previous.setText("Previous week")
        self.next.setText("Next week")
        self.today.setText("This week")
        self.refresh_button = QPushButton("Refresh review")
        self.refresh_button.setToolTip("Update this review from local records")
        self.refresh_button.clicked.connect(
            lambda: self.requested.emit("refresh_weekly_review", None)
        )
        self.date_actions.addWidget(self.refresh_button, 0, 4)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(60_000)
        self.refresh_timer.timeout.connect(self.refresh_if_visible)
        self.refresh_timer.start()
        self.facts.setObjectName("weeklyReviewFacts")
        self.facts.setAccessibleName("Weekly accomplishments")
        self.completed.setAccessibleName("Weekly review · Tasks completed")
        self.progress.setAccessibleName("Weekly review · Logged progress")
        self.caption.setAccessibleName("Selected review week and timezone")
        layout = self.layout()
        self.time_facts = SummaryCards()
        self.time_facts.setAccessibleName("Separate weekly time totals")
        layout.insertWidget(3, self.time_facts)
        self.time_help = self.label(
            "Logged work, app time and website time can overlap and must not be added together. "
            "App and site usage is from this PC; gaps and disabled recording are unrecorded time."
        )
        layout.insertWidget(4, self.time_help)
        self.day_list = ReadableList()
        self.day_list.setAccessibleName("Seven review days; activate a day to open Daily overview")
        self.day_list.setMinimumHeight(230)
        self.day_list.setMaximumHeight(380)
        self.day_list.itemActivated.connect(lambda _: self.open_day())
        layout.insertWidget(5, self.day_list)
        self.open_day_button = QPushButton("Open selected day's overview")
        self.open_day_button.clicked.connect(self.open_day)
        layout.insertWidget(6, self.open_day_button)
        self.breakdown = ReadableList()
        self.breakdown.setAccessibleName(
            "Weekly activity by task list and top app and website usage"
        )
        self.breakdown.setMinimumHeight(150)
        self.breakdown.setMaximumHeight(260)
        layout.insertWidget(7, self.breakdown)
        layout.insertWidget(
            7, self.label("Activity by list and top usage · Up to ten entries per group")
        )
        metrics_group = QGroupBox("Selected metrics")
        metrics_layout = QVBoxLayout(metrics_group)
        self.metric_choice = WheelSafeComboBox()
        self.metric_choice.setAccessibleName("Metric to include in Weekly review")
        self.metric_enabled = QCheckBox("Include this metric in Weekly review")
        self.metric_enabled.setAccessibleName("Include selected metric in Weekly review")
        self.metric_choice.currentIndexChanged.connect(self.metric_changed)
        self.metric_enabled.toggled.connect(self.toggle_metric)
        self.selected_metrics = ()
        self.metric_values = ReadableList()
        self.metric_values.setAccessibleName("Weekly primary-source metric values and aggregation")
        self.metric_values.setMinimumHeight(100)
        self.metric_values.setMaximumHeight(260)
        metrics_layout.addWidget(self.metric_choice)
        metrics_layout.addWidget(self.metric_enabled)
        metrics_layout.addWidget(self.metric_values)
        metrics_layout.addWidget(
            self.label(
                "Values use each metric's primary source, aggregation and recorded reporting dates. "
                "Missing records are unavailable, not zero; available records may cover only part of a week."
            )
        )
        metric_settings = QPushButton("Manage metrics and primary sources")
        metric_settings.clicked.connect(lambda: self.requested.emit("navigate", "Metrics"))
        metrics_layout.addWidget(metric_settings)
        layout.insertWidget(9, metrics_group)
        self.chooser.setAccessibleName(
            "Saved reflection from the selected week, or a new reflection"
        )
        self.title.setAccessibleName("Weekly reflection title (optional)")
        self.body.setAccessibleName("Weekly reflection writing")
        self.body.setPlaceholderText("Write about your week…")
        self.saved.setAccessibleName("Weekly reflection saved state")
        reflection_group = self.body.parentWidget()
        reflection_group.layout().insertWidget(
            0,
            self.label(
                "Optional · New reflections are dated Monday and also appear in Reflection and Daily overview. "
                "Choose an existing reflection below to edit it. New reflection starts a separate entry."
            ),
        )

    @staticmethod
    def label(text):
        label = QLabel(text)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        return label

    def select_day(self, selected, *, follow_today=False):
        if self.masked or not selected.isValid():
            return
        start, _ = week_bounds(selected.toPython())
        if start < self.date.minimumDate().toPython() or start > self.date.maximumDate().toPython():
            with QSignalBlocker(self.date):
                self.date.setDate(QDate(self.day))
            return
        super().select_day(QDate(start), follow_today=follow_today)

    def new_reflection(self):
        return ReflectionEntry(display_date=self.day, title=f"Week of {self.day:%B %d, %Y}")

    def changed(self):
        if not self.masked and self.reflection is None and not self.title.text():
            with QSignalBlocker(self.title):
                self.title.setText(f"Week of {self.day:%B %d, %Y}")
        super().changed()

    def options(self):
        start, end = week_bounds(self.day)
        return LogFilter(start=start, end=end, timezone=self.timezone, limit=self.limit)

    def render_review(self, review: WeeklyReview):
        if self.masked:
            return
        # Existing daily reflections are available by choice, never automatically edited.
        self.loaded_day = self.day
        super().render(review.facts, review.entries, review.reflections)
        for widget, count, empty in (
            (self.completed, review.facts["task_completion"], "No tasks completed in this week."),
            (self.progress, review.facts["manual"], "No progress logged in this week."),
        ):
            if not count:
                widget.item(0).setText(empty)
        self.caption.setText(
            f"{review.start:%b %d, %Y} – {review.end:%b %d, %Y} · Monday–Sunday · {review.timezone}"
        )
        today = datetime.now(ZoneInfo(self.timezone)).date()
        if review.start <= today <= review.end:
            self.caption.setText(self.caption.text() + " · Current week is incomplete")
        self.previous.setEnabled(self.day > self.date.minimumDate().toPython())
        self.next.setEnabled(self.day < week_bounds(self.date.maximumDate().toPython())[0])
        durations = review.facts["durations"]
        self.time_facts.set_cards(
            (
                ("Entered work duration", minutes_text(durations.get("entered"))),
                ("Timed work saved to Activity", minutes_text(durations.get("timer"))),
                (
                    "App foreground time",
                    seconds_text(sum(review.apps.values()) if review.apps else None),
                ),
                (
                    "Website foreground time",
                    seconds_text(sum(review.sites.values()) if review.sites else None),
                ),
            )
        )
        selected_day = self.day_list.currentItem()
        selected_day = selected_day.data(Qt.ItemDataRole.UserRole) if selected_day else None
        self.day_list.clear()
        for day in review.days:
            detail = (
                f"{day.completed} tasks completed · {day.manual} progress entries\n"
                f"Entered: {minutes_text(day.entered_minutes)} · Timed: {minutes_text(day.timed_minutes)}\n"
                f"Apps: {seconds_text(day.app_seconds)} · Sites: {seconds_text(day.site_seconds)}"
            )
            title = f"{day.day:%A, %b %d}" + (" · Future date" if day.day > today else "")
            item = QListWidgetItem(f"{title}\n{detail}")
            present_item(item, title, detail)
            item.setData(Qt.ItemDataRole.UserRole, day.day)
            self.day_list.addItem(item)
            if day.day == selected_day:
                self.day_list.setCurrentItem(item)
        if self.day_list.currentRow() < 0:
            self.day_list.setCurrentRow(0)
        self.breakdown.clear()
        groups = (
            ("Activity by list", review.facts["lists"], lambda value: f"{value} entries"),
            ("App usage", review.apps, seconds_text),
            ("Website usage", review.sites, seconds_text),
        )
        for label, values, formatter in groups:
            for name, value in sorted(values.items(), key=lambda item: (-item[1], item[0]))[:10]:
                context = f"{label} · {formatter(value)}"
                item = QListWidgetItem(f"{name}\n{context}")
                present_item(item, name, context)
                self.breakdown.addItem(item)
        if not self.breakdown.count():
            self.breakdown.addItem("No activity or usage recorded in this week.")
        self.breakdown.setAccessibleDescription(
            "Up to ten lists, apps and websites per group. Totals cover all matching history."
        )
        self.selected_metrics = review.selected_metrics
        selected_metric = self.metric_choice.currentData()
        with QSignalBlocker(self.metric_choice):
            self.metric_choice.clear()
            for definition in review.definitions:
                self.metric_choice.addItem(definition.name, definition.metric_id)
            self.metric_choice.setCurrentIndex(max(0, self.metric_choice.findData(selected_metric)))
        self.metric_changed()
        self.metric_values.clear()
        for metric in review.metrics:
            summary = metric.summary
            value = (
                f"{summary.value:g} {summary.unit}" if summary.value is not None else "Unavailable"
            )
            detail = (
                f"{value} · {metric.definition.aggregation} · {summary.count} records\n"
                f"Primary source: {metric.source_name or 'Not selected'}"
            )
            if metric.reporting_timezone:
                detail += f" · Reporting timezone: {metric.reporting_timezone}"
            item = QListWidgetItem(f"{metric.definition.name}\n{detail}")
            present_item(item, metric.definition.name, detail)
            self.metric_values.addItem(item)
        if not self.metric_values.count():
            self.metric_values.addItem("Choose metrics above, or add a metric in Metrics.")
        # Include the date on accomplishment rows and reflection choices.
        for widget, kind in ((self.completed, "task_completion"), (self.progress, "manual")):
            for index, entry in enumerate(e for e in review.entries if e.kind == kind):
                local = entry.occurred_at.astimezone(ZoneInfo(self.timezone))
                context = f"{local:%a, %b %d · %H:%M} · {entry.list_name or 'No list'}"
                if entry.tags:
                    context += " · " + " ".join(f"#{tag}" for tag in entry.tags)
                if entry.duration_minutes is not None:
                    context += f" · {entry.duration_minutes} minutes ({entry.duration_source})"
                widget.item(index).setText(f"{entry.description}\n{context}")
                present_item(widget.item(index), entry.description, context)
        with QSignalBlocker(self.chooser):
            for index in range(1, self.chooser.count()):
                entry = self.reflections[self.chooser.itemData(index)]
                self.chooser.setItemText(
                    index, f"{entry.display_date:%b %d} · {entry.title or 'Untitled reflection'}"
                )

    def metric_changed(self, *_):
        identifier = self.metric_choice.currentData()
        with QSignalBlocker(self.metric_enabled):
            self.metric_enabled.setEnabled(identifier is not None)
            self.metric_enabled.setChecked(identifier in self.selected_metrics)

    def refresh_if_visible(self):
        if self.isVisible() and not self.masked:
            self.requested.emit("refresh_weekly_review", None)

    def toggle_metric(self, enabled):
        identifier = self.metric_choice.currentData()
        if not self.masked and identifier:
            self.requested.emit("weekly_metric", (identifier, enabled))

    def open_day(self):
        item = self.day_list.currentItem()
        if not self.masked and item:
            self.requested.emit("open_review_day", item.data(Qt.ItemDataRole.UserRole))

    def set_compact(self, compact):
        super().set_compact(compact)
        if hasattr(self, "time_facts"):
            self.time_facts.set_compact(compact)
        if hasattr(self, "refresh_button"):
            self.date_actions.removeWidget(self.refresh_button)
            self.date_actions.addWidget(
                self.refresh_button, 4 if compact else 0, 0 if compact else 4
            )

    def set_private(self, private):
        super().set_private(private)
        if private:
            self.day_list.clear()
            self.breakdown.clear()
            self.metric_values.clear()
            self.time_facts.clear()
            with QSignalBlocker(self.metric_choice):
                self.metric_choice.clear()
            with QSignalBlocker(self.metric_enabled):
                self.metric_enabled.setChecked(False)
