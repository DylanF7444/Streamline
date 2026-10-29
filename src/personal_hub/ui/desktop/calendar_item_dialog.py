"""Accessible editor for local calendar items; saving never performs network work."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from PySide6.QtCore import QDate, QDateTime, Qt, QTime, QTimeZone
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from personal_hub.calendar.local import ImportedCalendarEvent
from personal_hub.calendar.native import LOCAL_CALENDAR_ID, CalendarSyncBinding
from personal_hub.ui.desktop.status_label import StatusLabel


class CalendarItemDialog(QDialog):
    def __init__(
        self,
        *,
        day: date,
        timezone: str,
        current: ImportedCalendarEvent | None = None,
        binding: CalendarSyncBinding | None = None,
        google_calendars: tuple[tuple[str, str], ...] = (),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.current = current
        self.binding = binding
        self._payload: dict[str, object] | None = None
        self.setObjectName("calendarItemDialog")
        self.setWindowTitle("Edit calendar item" if current else "Add calendar item")
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.title_input = QLineEdit(current.title if current else "")
        self.title_input.setMaxLength(240)
        self.title_input.setAccessibleName("Calendar item title")
        form.addRow("Title (required)", self.title_input)
        self.timezone_input = QLineEdit(current.timezone if current else timezone)
        self.timezone_input.setMaxLength(128)
        self.timezone_input.setAccessibleName("Calendar item timezone")
        form.addRow("Timezone", self.timezone_input)
        zone = ZoneInfo(current.timezone if current else timezone)
        start = (
            current.starts_at.astimezone(zone)
            if current
            else datetime.combine(day, time(9), tzinfo=zone)
        )
        end = current.ends_at.astimezone(zone) if current else start + timedelta(hours=1)
        self.start_input = self._time_input(start, "Calendar item start")
        self.end_input = self._time_input(end, "Calendar item end")
        form.addRow("Start", self.start_input)
        form.addRow("End (exclusive)", self.end_input)
        self.all_day_checkbox = QCheckBox("All day")
        self.all_day_checkbox.setChecked(bool(current and current.all_day))
        self.all_day_checkbox.toggled.connect(self._all_day_changed)
        form.addRow("Schedule", self.all_day_checkbox)
        self._all_day_changed(self.all_day_checkbox.isChecked())
        self.location_input = QLineEdit(current.location if current else "")
        self.location_input.setMaxLength(512)
        self.location_input.setAccessibleName("Calendar item location")
        form.addRow("Location (optional)", self.location_input)
        self.description_input = QPlainTextEdit(current.description if current else "")
        self.description_input.setMaximumHeight(120)
        self.description_input.setAccessibleName("Calendar item notes")
        self.description_input.setTabChangesFocus(True)
        form.addRow("Notes (optional)", self.description_input)
        self.sync_combo = QComboBox()
        self.sync_combo.setAccessibleName("Calendar item sync destination")
        self.sync_combo.addItem("Keep on this device", None)
        if binding is not None:
            self.sync_combo.addItem("Google: " + binding.calendar_id, binding.calendar_id)
            self.sync_combo.setCurrentIndex(1)
        else:
            for identifier, label in google_calendars:
                self.sync_combo.addItem("Google: " + label, identifier)
        form.addRow("Optional sync", self.sync_combo)
        if binding is not None:
            states = {
                "pending": "Waiting to sync",
                "synced": "Linked to Google",
                "conflict": "Local and Google changes need review",
                "remote_deleted": "Removed at Google; the local copy is retained",
            }
            sync_status = QLabel(states[binding.state])
            sync_status.setTextFormat(Qt.TextFormat.PlainText)
            sync_status.setWordWrap(True)
            sync_status.setAccessibleName("Calendar item sync status")
            form.addRow("Sync status", sync_status)
        self.conflict_combo = QComboBox()
        self.conflict_combo.addItem("Keep both versions for review", None)
        self.conflict_combo.addItem("Use Google changes", "google")
        self.conflict_combo.addItem("Send these local changes", "local")
        self.conflict_combo.setAccessibleName("Resolve calendar sync conflict")
        if binding is not None and binding.state == "conflict":
            form.addRow("Conflicting changes", self.conflict_combo)
        else:
            self.conflict_combo.hide()
        layout.addLayout(form)
        description = QLabel(
            "Items save locally. Choose Sync Google in Calendar to send selected items and receive changes. Removing a local item keeps its Google copy."
        )
        description.setWordWrap(True)
        description.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(description)
        self.status_label = StatusLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setAccessibleName("Calendar item validation status")
        layout.addWidget(self.status_label)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _time_input(value: datetime, name: str) -> QDateTimeEdit:
        editor = QDateTimeEdit()
        # Keep wall-clock fields independent of the host's daylight-saving rules.
        # Apply the selected IANA zone only when validating the saved item.
        editor.setTimeZone(QTimeZone.utc())
        editor.setDateTime(
            QDateTime(
                QDate(value.year, value.month, value.day),
                QTime(value.hour, value.minute),
                QTimeZone.utc(),
            )
        )
        editor.setCalendarPopup(True)
        editor.setAccessibleName(name)
        return editor

    def _all_day_changed(self, all_day: bool) -> None:
        for editor in (self.start_input, self.end_input):
            editor.setDisplayFormat("yyyy-MM-dd" if all_day else "yyyy-MM-dd HH:mm")
        if all_day and self.end_input.date() <= self.start_input.date():
            self.end_input.setDate(self.start_input.date().addDays(1))

    def accept(self) -> None:
        if not self.title_input.text().strip():
            self.status_label.setText("Enter a title for this calendar item before saving.")
            self.title_input.setFocus()
            return
        try:
            zone = ZoneInfo(self.timezone_input.text().strip())
            values = []
            for editor in (self.start_input, self.end_input):
                value = editor.dateTime().toPython().replace(tzinfo=None)
                if self.all_day_checkbox.isChecked():
                    value = datetime.combine(value.date(), time.min)
                aware = value.replace(tzinfo=zone)
                if aware.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != value:
                    raise ValueError(
                        "This time does not exist because the clocks change. Choose another time."
                    )
                if aware.utcoffset() != value.replace(tzinfo=zone, fold=1).utcoffset():
                    raise ValueError(
                        "This time occurs twice because the clocks change. Enter the time in UTC."
                    )
                values.append(aware)
            fields = {
                "title": self.title_input.text(),
                "starts_at": values[0],
                "ends_at": values[1],
                "all_day": self.all_day_checkbox.isChecked(),
                "timezone": zone.key,
                "location": self.location_input.text(),
                "description": self.description_input.toPlainText(),
            }
            ImportedCalendarEvent(import_id=LOCAL_CALENDAR_ID, stable_key=str(uuid4()), **fields)
        except (TypeError, ValueError, KeyError):
            self.status_label.setText(
                "Enter a title, a valid timezone, and an end after the start. Times must be unambiguous and exist in that timezone."
            )
            return
        self._payload = {
            **fields,
            "google_calendar_id": self.sync_combo.currentData(),
            "conflict_resolution": self.conflict_combo.currentData(),
        }
        super().accept()

    def payload(self) -> dict[str, object]:
        return dict(self._payload or {})
