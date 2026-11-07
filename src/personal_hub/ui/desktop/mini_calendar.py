"""Compact, keyboard-accessible month navigator for the Home dashboard."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from PySide6.QtCore import QDate, QRect, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPalette, QTextCharFormat
from PySide6.QtWidgets import (
    QCalendarWidget,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from personal_hub.ui.desktop.readable import ReadableList, present_item


@dataclass(frozen=True)
class MiniCalendarItem:
    title: str
    metadata: str


class _MonthGrid(QCalendarWidget):
    """Paint activity dots, a filled today highlight, and a selected-date outline."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.today = QDate.currentDate()
        self.activity: set[QDate] = set()
        self.activity_counts: dict[QDate, tuple[int, int]] = {}
        self.setObjectName("miniCalendarGrid")
        self.setAccessibleName("Mini calendar month grid")
        self.setAccessibleDescription(
            "Click or use arrow keys to select a date. Double-click or press Enter to open Calendar."
        )
        self.setNavigationBarVisible(False)
        self.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        self.setHorizontalHeaderFormat(QCalendarWidget.HorizontalHeaderFormat.ShortDayNames)
        self.setFirstDayOfWeek(Qt.DayOfWeek.Sunday)

    def paintCell(self, painter: QPainter, rect: QRect, value: QDate) -> None:  # noqa: N802
        super().paintCell(painter, rect, value)
        painter.save()
        selected = value == self.selectedDate()
        today = value == self.today
        palette = self.palette()
        color = palette.color(
            QPalette.ColorRole.HighlightedText if today else QPalette.ColorRole.Text
        )
        if today:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(palette.color(QPalette.ColorRole.Highlight))
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 4, 4)
            painter.setPen(color)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, str(value.day()))
        elif selected:
            painter.fillRect(rect, palette.color(QPalette.ColorRole.Base))
            painter.setPen(color)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, str(value.day()))
        if selected:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(palette.color(QPalette.ColorRole.Highlight))
            painter.drawRect(rect.adjusted(2, 2, -3, -3))
        painter.setPen(color)
        event_count, task_count = self.activity_counts.get(value, (0, 0))
        if event_count or task_count:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            bar_width = min(22, max(12, rect.width() - 10))
            bar_height = 4
            bar_x = rect.center().x() - bar_width // 2
            bar_y = rect.bottom() - 6
            dark_surface = palette.color(QPalette.ColorRole.Base).lightness() < 128

            def marker_color(role: QPalette.ColorRole) -> QColor:
                color = palette.color(role)
                return color.lighter(130) if dark_surface else color

            if event_count and task_count:
                segment_width = (bar_width - 2) // 2
                painter.setBrush(marker_color(QPalette.ColorRole.Link))
                painter.drawRoundedRect(bar_x, bar_y, segment_width, bar_height, 2, 2)
                task_color_role = (
                    QPalette.ColorRole.HighlightedText if today else QPalette.ColorRole.Highlight
                )
                painter.setBrush(marker_color(task_color_role))
                painter.drawRoundedRect(
                    bar_x + segment_width + 2,
                    bar_y,
                    bar_width - segment_width - 2,
                    bar_height,
                    2,
                    2,
                )
            else:
                role = QPalette.ColorRole.Link if event_count else QPalette.ColorRole.Highlight
                if today and task_count:
                    role = QPalette.ColorRole.HighlightedText
                painter.setBrush(marker_color(role))
                painter.drawRoundedRect(bar_x, bar_y, bar_width, bar_height, 2, 2)
        painter.restore()


class MiniCalendar(QWidget):
    """A month grid that emits dates; its caller owns data and navigation."""

    date_requested = Signal(object)
    month_changed = Signal(int, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("miniCalendar")
        self.setAccessibleName("Mini calendar")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        toolbar = QHBoxLayout()
        self.previous_button = QPushButton("‹", self)
        self.previous_button.setAccessibleName("Mini calendar previous month")
        self.next_button = QPushButton("›", self)
        self.next_button.setAccessibleName("Mini calendar next month")
        self.today_button = QPushButton("Today", self)
        self.today_button.setAccessibleName("Mini calendar return to today")
        self.month_label = QLabel(self)
        self.month_label.setTextFormat(Qt.TextFormat.PlainText)
        self.month_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.month_label.setAccessibleName("Mini calendar displayed month")
        toolbar.addWidget(self.previous_button)
        toolbar.addWidget(self.month_label, 1)
        toolbar.addWidget(self.next_button)
        toolbar.addWidget(self.today_button)
        layout.addLayout(toolbar)
        self.grid = _MonthGrid(self)
        layout.addWidget(self.grid)
        self.activity_label = QLabel(self)
        self.activity_label.setObjectName("miniCalendarActivity")
        self.activity_label.setTextFormat(Qt.TextFormat.PlainText)
        self.activity_label.setWordWrap(True)
        self.activity_label.setToolTip(
            "Blue bars mark calendar events; accent bars mark due tasks. "
            "Double-click a date to open Calendar."
        )
        self.activity_label.setAccessibleDescription(self.activity_label.toolTip())
        layout.addWidget(self.activity_label)
        self.items_list = ReadableList(self)
        self.items_list.setObjectName("miniCalendarItems")
        self.items_list.setAccessibleName("Events and tasks on selected calendar day")
        self.items_list.setAccessibleDescription(
            "Titles are followed by time and item type. Activate an entry to open this day in Calendar."
        )
        self.items_list.itemActivated.connect(
            lambda _item: self._request_date(self.grid.selectedDate())
        )
        layout.addWidget(self.items_list)
        self.empty_label = QLabel("No items on this day", self)
        self.empty_label.setTextFormat(Qt.TextFormat.PlainText)
        self.empty_label.setWordWrap(True)
        layout.addWidget(self.empty_label)
        self.more_button = QPushButton(self)
        self.more_button.clicked.connect(lambda: self._request_date(self.grid.selectedDate()))
        self.more_button.hide()
        layout.addWidget(self.more_button)
        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(7)
        self.status_dot = QFrame(self)
        self.status_dot.setObjectName("miniCalendarStatusDot")
        self.status_dot.setFixedSize(8, 8)
        self.status_dot.setAccessibleName("Calendar readiness indicator")
        self.status_dot.setAccessibleDescription("Green means ready; red means not ready.")
        status_row.addWidget(self.status_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        self.status_label = QLabel(self)
        self.status_label.setObjectName("miniCalendarStatus")
        self.status_label.setAccessibleName("Calendar status")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        status_row.addWidget(self.status_label, 1)
        layout.addLayout(status_row)
        self._counts: dict[date, tuple[int, int]] = {}
        self._items: dict[date, tuple[MiniCalendarItem, ...]] = {}
        self._shown_items: tuple[MiniCalendarItem, ...] | None = None
        self.previous_button.clicked.connect(self.grid.showPreviousMonth)
        self.next_button.clicked.connect(self.grid.showNextMonth)
        self.today_button.clicked.connect(self.go_to_today)
        self.grid.currentPageChanged.connect(self._page_changed)
        self.grid.selectionChanged.connect(self._update_activity_label)
        self.grid.activated.connect(self._request_date)
        self._update_month_label()

    def _update_month_label(self) -> None:
        self.month_label.setText(
            QDate(self.grid.yearShown(), self.grid.monthShown(), 1).toString("MMMM yyyy")
        )

    def _page_changed(self, year: int, month: int) -> None:
        self._update_month_label()
        self.month_changed.emit(year, month)

    def _request_date(self, value: QDate) -> None:
        self.date_requested.emit(value.toPython())

    def go_to_today(self) -> None:
        self.grid.setSelectedDate(self.grid.today)
        self.grid.setCurrentPage(self.grid.today.year(), self.grid.today.month())

    def set_today(self, value: date) -> None:
        previous = self.grid.today
        current = QDate(value.year, value.month, value.day)
        if current == previous:
            return
        following_today = (
            self.grid.yearShown() == previous.year() and self.grid.monthShown() == previous.month()
        )
        self.grid.today = current
        if following_today:
            self.go_to_today()
        self.grid.updateCells()

    def set_activity(
        self,
        counts: Mapping[date, int | tuple[int, int]],
        *,
        status: str,
        ready: bool,
        items: Mapping[date, Sequence[MiniCalendarItem]] | None = None,
    ) -> None:
        normalized: dict[date, tuple[int, int]] = {}
        for day, value in counts.items():
            normalized[day] = (value, 0) if isinstance(value, int) else value
        self._counts = normalized
        self._items = {day: tuple(lines) for day, lines in (items or {}).items()}
        self.grid.activity_counts = {
            QDate(day.year, day.month, day.day): value for day, value in normalized.items()
        }
        self.grid.activity = set(self.grid.activity_counts)
        self.status_dot.setProperty("ready", bool(ready))
        self.status_dot.style().unpolish(self.status_dot)
        self.status_dot.style().polish(self.status_dot)
        self.status_dot.update()
        self.status_label.setText(status)
        self._update_activity_label()
        self._refresh_minimum_height()
        self.grid.updateCells()

    def refresh_style(self) -> None:
        """Keep native calendar headers and minimum cell sizes consistent with Hub themes."""
        header = QTextCharFormat()
        header.setForeground(self.grid.palette().color(QPalette.ColorRole.Text))
        header.setBackground(self.grid.palette().color(QPalette.ColorRole.Base))
        self.grid.setHeaderTextFormat(header)
        weekday = QTextCharFormat()
        weekday.setForeground(self.grid.palette().color(QPalette.ColorRole.Text))
        for day in (Qt.DayOfWeek.Saturday, Qt.DayOfWeek.Sunday):
            self.grid.setWeekdayTextFormat(day, weekday)
        self.grid.setMinimumSize(self.grid.minimumSizeHint())
        self._refresh_minimum_height()
        self.grid.updateCells()

    def _refresh_minimum_height(self) -> None:
        width = self.grid.minimumSizeHint().width()
        for label in (self.activity_label, self.empty_label, self.status_label):
            label.setMinimumHeight(max(label.sizeHint().height(), label.heightForWidth(width)))
        line_height = self.items_list.fontMetrics().lineSpacing()
        rows_height = sum(
            self.items_list.sizeHintForRow(row) for row in range(self.items_list.count())
        )
        self.items_list.setFixedHeight(min(rows_height + 4, line_height * 10 + 28))
        self.layout().invalidate()
        self.setMinimumHeight(self.layout().minimumSize().height())
        self.updateGeometry()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "_items"):
            self._refresh_minimum_height()

    def _update_activity_label(self) -> None:
        selected = self.grid.selectedDate().toPython()
        event_count, task_count = self._counts.get(selected, (0, 0))
        self.activity_label.setText(
            f"{selected.strftime('%b')} {selected.day}: "
            f"{event_count} event{'s' if event_count != 1 else ''} · "
            f"{task_count} task{'s' if task_count != 1 else ''}"
        )
        entries = self._items.get(selected, ())
        if entries != self._shown_items:
            self.items_list.clear()
            for entry in entries:
                item = QListWidgetItem(f"{entry.title}\n{entry.metadata}")
                present_item(item, entry.title, entry.metadata)
                self.items_list.addItem(item)
            self._shown_items = entries
        self.items_list.setVisible(bool(entries))
        self.empty_label.setVisible(not entries)
        remaining = max(0, event_count + task_count - len(entries))
        self.more_button.setText(f"+{remaining} more · Open day in Calendar")
        self.more_button.setVisible(remaining > 0)
        self._refresh_minimum_height()
