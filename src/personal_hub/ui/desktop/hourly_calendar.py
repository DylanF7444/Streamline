"""Qt presentation primitive for Personal Hub's hourly calendar views."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QPainter, QPalette
from PySide6.QtWidgets import QFrame, QPushButton, QSizePolicy, QWidget

from personal_hub.calendar.schedule import (
    AllDaySchedulePlacement,
    HourlyScheduleLayout,
    HourlySchedulePlacement,
)


class HourlyCalendarGrid(QWidget):
    """Scrollable, one-hour-row calendar canvas for one or seven days.

    The grid owns only geometry and slot hit targets.  The caller supplies the
    already-sanitized event widgets, keeping provider and task contracts outside
    this reusable Qt primitive.
    """

    slot_requested = Signal(object)

    TIME_COLUMN_WIDTH = 64
    HEADER_HEIGHT = 46
    HOUR_HEIGHT = 72
    DAY_MINIMUM_WIDTH = 118
    ALL_DAY_MINIMUM_HEIGHT = 48
    EVENT_GAP = 3

    def __init__(self, parent: QWidget | None = None, *, column_object_name: str) -> None:
        super().__init__(parent)
        self.setObjectName("calendarHourlyGrid")
        self.setAccessibleName("Hour by hour calendar")
        self.setAccessibleDescription(
            "Select an hour to add a local scheduled task; provider calendar events are read-only."
        )
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._column_object_name = column_object_name
        self._layout: HourlyScheduleLayout | None = None
        self._dynamic_widgets: list[QWidget] = []
        self._widgets_by_key: dict[tuple[str, int], QWidget] = {}
        self._slot_buttons: list[QPushButton] = []
        self._day_columns: dict[date, QFrame] = {}
        self._all_day_height = self.ALL_DAY_MINIMUM_HEIGHT
        self._day_width = self.DAY_MINIMUM_WIDTH
        self._time_top = self.HEADER_HEIGHT + self._all_day_height

    @property
    def day_columns(self) -> dict[date, QFrame]:
        return dict(self._day_columns)

    @property
    def slot_buttons(self) -> tuple[QPushButton, ...]:
        return tuple(self._slot_buttons)

    @property
    def schedule_layout(self) -> HourlyScheduleLayout | None:
        return self._layout

    def render(
        self,
        layout: HourlyScheduleLayout,
        *,
        timed_widgets: Mapping[tuple[str, int], QWidget],
        all_day_widgets: Mapping[tuple[str, int], QWidget],
    ) -> None:
        """Replace the visible schedule and reflow supplied event widgets."""

        if layout.day_count not in {1, 7}:
            raise ValueError("hourly calendar grids support one or seven days")
        self._clear_dynamic_widgets()
        self._layout = layout
        self._all_day_height = self._calculate_all_day_height(layout)
        self._time_top = self.HEADER_HEIGHT + self._all_day_height
        for day_index in range(layout.day_count):
            current = layout.start_date.fromordinal(layout.start_date.toordinal() + day_index)
            column = QFrame(self)
            column.setObjectName(self._column_object_name)
            column.setProperty("calendarDate", current.isoformat())
            column.setAccessibleName(f"Calendar day {current.isoformat()}")
            column.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            column.setStyleSheet("background: transparent; border: 0;")
            self._dynamic_widgets.append(column)
            self._day_columns[current] = column

        self._build_slot_buttons(layout)
        for placement in layout.all_day:
            widget = all_day_widgets.get((placement.event_id, placement.day_index))
            if widget is not None:
                self._attach_widget(widget)
        for placement in layout.timed:
            widget = timed_widgets.get((placement.event_id, placement.day_index))
            if widget is not None:
                self._attach_widget(widget)
        self._resize_canvas()
        self._reflow()
        self.update()

    def resizeEvent(self, event: object) -> None:
        self._resize_canvas()
        self._reflow()
        super().resizeEvent(event)  # type: ignore[arg-type]

    def paintEvent(self, event: object) -> None:
        painter = QPainter(self)
        palette = self.palette()
        base = palette.color(QPalette.ColorRole.Base)
        alternate = palette.color(QPalette.ColorRole.AlternateBase)
        text = palette.color(QPalette.ColorRole.Text)
        grid = palette.color(QPalette.ColorRole.Mid)
        grid.setAlpha(100)
        painter.fillRect(self.rect(), base)
        if self._layout is None:
            painter.end()
            return
        width = self._day_width * self._layout.day_count
        painter.fillRect(QRect(self.TIME_COLUMN_WIDTH, 0, width, self.HEADER_HEIGHT), alternate)
        painter.fillRect(
            QRect(self.TIME_COLUMN_WIDTH, self.HEADER_HEIGHT, width, self._all_day_height),
            alternate,
        )
        painter.setPen(grid)
        painter.drawLine(self.TIME_COLUMN_WIDTH, 0, self.TIME_COLUMN_WIDTH, self.height())
        for index in range(self._layout.day_count + 1):
            x = self.TIME_COLUMN_WIDTH + index * self._day_width
            painter.drawLine(x, 0, x, self.height())
        for hour in range(25):
            y = self._time_top + hour * self.HOUR_HEIGHT
            painter.drawLine(self.TIME_COLUMN_WIDTH, y, self.width(), y)
            if hour < 24:
                painter.setPen(text)
                painter.drawText(
                    QRect(4, y + 4, self.TIME_COLUMN_WIDTH - 10, 20),
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
                    f"{hour:02d}:00",
                )
                painter.setPen(grid)
                half_y = y + self.HOUR_HEIGHT // 2
                painter.drawLine(self.TIME_COLUMN_WIDTH, half_y, self.width(), half_y)
        painter.setPen(text)
        for day_index in range(self._layout.day_count):
            current = self._layout.start_date.fromordinal(
                self._layout.start_date.toordinal() + day_index
            )
            x = self.TIME_COLUMN_WIDTH + day_index * self._day_width
            label = f"{current.strftime('%a')} {current.day}"
            painter.drawText(
                QRect(x + 8, 0, self._day_width - 16, self.HEADER_HEIGHT),
                Qt.AlignmentFlag.AlignCenter,
                label,
            )
        painter.drawText(
            QRect(4, self.HEADER_HEIGHT, self.TIME_COLUMN_WIDTH - 10, self._all_day_height),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            "All-day",
        )
        painter.end()

    def _build_slot_buttons(self, layout: HourlyScheduleLayout) -> None:
        for day_index in range(layout.day_count):
            current = layout.start_date.fromordinal(layout.start_date.toordinal() + day_index)
            for hour in range(24):
                slot = QPushButton(self)
                slot.setObjectName("calendarTimeSlotButton")
                slot.setText("")
                slot.setAutoDefault(False)
                slot.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
                slot.setAccessibleName(f"Add task on {current.isoformat()} at {hour:02d}:00")
                slot.setAccessibleDescription(
                    "Create a local scheduled task in this calendar time slot."
                )
                slot.setToolTip(f"Add a task at {current.isoformat()} {hour:02d}:00")
                slot.clicked.connect(
                    lambda _checked=False, selected_date=current, selected_hour=hour: (
                        self.slot_requested.emit(
                            {
                                "date": selected_date,
                                "minutes": selected_hour * 60,
                            }
                        )
                    )
                )
                self._slot_buttons.append(slot)
                self._dynamic_widgets.append(slot)

    def _attach_widget(self, widget: QWidget) -> None:
        widget.setParent(self)
        widget.show()
        self._dynamic_widgets.append(widget)
        key = widget.property("calendarPlacementKey")
        if (
            isinstance(key, tuple)
            and len(key) == 2
            and isinstance(key[0], str)
            and isinstance(key[1], int)
        ):
            self._widgets_by_key[(key[0], key[1])] = widget

    def _clear_dynamic_widgets(self) -> None:
        for widget in self._dynamic_widgets:
            widget.setParent(None)
            widget.deleteLater()
        self._dynamic_widgets.clear()
        self._widgets_by_key.clear()
        self._slot_buttons.clear()
        self._day_columns.clear()

    def _calculate_all_day_height(self, layout: HourlyScheduleLayout) -> int:
        counts = [0] * layout.day_count
        for placement in layout.all_day:
            counts[placement.day_index] += 1
        return max(self.ALL_DAY_MINIMUM_HEIGHT, min(184, max(counts, default=0) * 28 + 10))

    def _resize_canvas(self) -> None:
        layout = self._layout
        if layout is None:
            return
        self._day_width = max(
            self.DAY_MINIMUM_WIDTH,
            (self.width() - self.TIME_COLUMN_WIDTH) / layout.day_count,
        )
        total_width = int(self.TIME_COLUMN_WIDTH + self._day_width * layout.day_count)
        total_height = self._time_top + self.HOUR_HEIGHT * 24 + 8
        self.setMinimumSize(total_width, total_height)

    def _reflow(self) -> None:
        layout = self._layout
        if layout is None:
            return
        self._resize_canvas()
        for day_index in range(layout.day_count):
            current = layout.start_date.fromordinal(layout.start_date.toordinal() + day_index)
            column = self._day_columns.get(current)
            if column is not None:
                column.setGeometry(
                    int(self.TIME_COLUMN_WIDTH + day_index * self._day_width),
                    0,
                    max(1, int(self._day_width)),
                    self.height(),
                )
        for index, slot in enumerate(self._slot_buttons):
            day_index, hour = divmod(index, 24)
            slot.setGeometry(
                int(self.TIME_COLUMN_WIDTH + day_index * self._day_width + 1),
                self._time_top + hour * self.HOUR_HEIGHT + 1,
                max(1, int(self._day_width) - 2),
                self.HOUR_HEIGHT - 2,
            )
        for placement in layout.all_day:
            widget = self._widget_for(placement)
            if widget is None:
                continue
            x = self.TIME_COLUMN_WIDTH + placement.day_index * self._day_width + self.EVENT_GAP
            row = self._all_day_row(placement, layout)
            widget.setGeometry(
                int(x),
                self.HEADER_HEIGHT + 5 + row * 28,
                max(24, int(self._day_width) - 2 * self.EVENT_GAP),
                25,
            )
        for placement in layout.timed:
            widget = self._widget_for(placement)
            if widget is None:
                continue
            day_x = self.TIME_COLUMN_WIDTH + placement.day_index * self._day_width
            column_width = self._day_width / max(1, placement.column_count)
            x = day_x + placement.column * column_width + self.EVENT_GAP
            y = self._time_top + placement.start_minutes / 60 * self.HOUR_HEIGHT + self.EVENT_GAP
            height = max(
                25, placement.duration_minutes / 60 * self.HOUR_HEIGHT - 2 * self.EVENT_GAP
            )
            width = max(1, column_width - 2 * self.EVENT_GAP)
            widget.setGeometry(int(x), int(y), int(width), int(height))

    def _widget_for(
        self, placement: HourlySchedulePlacement | AllDaySchedulePlacement
    ) -> QWidget | None:
        return self._widgets_by_key.get((placement.event_id, placement.day_index))

    @staticmethod
    def _all_day_row(placement: AllDaySchedulePlacement, layout: HourlyScheduleLayout) -> int:
        return sum(
            1
            for candidate in layout.all_day
            if candidate.day_index == placement.day_index
            and (
                candidate.event_id < placement.event_id
                or (candidate.event_id == placement.event_id and candidate is not placement)
            )
        )


__all__ = ["HourlyCalendarGrid"]
