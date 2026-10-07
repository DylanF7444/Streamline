"""Compact, keyboard-accessible month navigator for the Home dashboard."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

from PySide6.QtCore import QDate, QRect, Qt, Signal
from PySide6.QtGui import QPainter, QPalette, QTextCharFormat
from PySide6.QtWidgets import (
    QCalendarWidget,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class _MonthGrid(QCalendarWidget):
    """Paint content-free activity dots and a distinct outline around today."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.today = QDate.currentDate()
        self.activity: set[QDate] = set()
        self.setObjectName("miniCalendarGrid")
        self.setAccessibleName("Mini calendar month grid")
        self.setAccessibleDescription("Use arrow keys to select a date and Enter to open Calendar.")
        self.setNavigationBarVisible(False)
        self.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        self.setHorizontalHeaderFormat(QCalendarWidget.HorizontalHeaderFormat.ShortDayNames)
        self.setFirstDayOfWeek(Qt.DayOfWeek.Sunday)

    def paintCell(self, painter: QPainter, rect: QRect, value: QDate) -> None:  # noqa: N802
        super().paintCell(painter, rect, value)
        painter.save()
        selected = value == self.selectedDate()
        color = self.palette().color(
            QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text
        )
        painter.setPen(color)
        if value == self.today:
            painter.drawRect(rect.adjusted(2, 2, -3, -3))
        if value in self.activity:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setBrush(color)
            painter.drawEllipse(rect.center().x() - 2, rect.bottom() - 5, 4, 4)
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
            "Dots mark dates with events or due tasks in loaded data. Select a date to open Calendar."
        )
        self.activity_label.setAccessibleDescription(self.activity_label.toolTip())
        layout.addWidget(self.activity_label)
        self.status_label = QLabel(self)
        self.status_label.setObjectName("miniCalendarStatus")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self._counts: dict[date, int] = {}
        self.previous_button.clicked.connect(self.grid.showPreviousMonth)
        self.next_button.clicked.connect(self.grid.showNextMonth)
        self.today_button.clicked.connect(self.go_to_today)
        self.grid.currentPageChanged.connect(self._page_changed)
        self.grid.selectionChanged.connect(self._update_activity_label)
        self.grid.clicked.connect(self._request_date)
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

    def set_activity(self, counts: Mapping[date, int], *, status: str) -> None:
        self._counts = dict(counts)
        self.grid.activity = {QDate(day.year, day.month, day.day) for day in counts}
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
        for label in (self.activity_label, self.status_label):
            label.setMinimumHeight(max(label.sizeHint().height(), label.heightForWidth(width)))
        self.layout().invalidate()
        self.setMinimumHeight(self.layout().minimumSize().height())
        self.updateGeometry()

    def _update_activity_label(self) -> None:
        selected = self.grid.selectedDate().toPython()
        count = self._counts.get(selected, 0)
        self.activity_label.setText(
            f"• {selected.strftime('%b')} {selected.day}: {count} event{'s' if count != 1 else ''} "
            "or due tasks"
        )
