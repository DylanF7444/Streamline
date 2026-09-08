"""Small, presentation-only editor for Personal Hub iCalendar feeds."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from urllib.parse import urlsplit
from uuid import UUID

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from personal_hub.calendar.ical import IcalCalendarFeed
from personal_hub.feeds.contracts import FeedPolicyError
from personal_hub.feeds.security import origin_for_url


class IcalFeedDialog(QDialog):
    """Collect one direct, read-only iCalendar URL without performing network work."""

    def __init__(
        self,
        current: IcalCalendarFeed | Mapping[str, object] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.current = (
            current
            if isinstance(current, IcalCalendarFeed)
            else self._coerce_current(current)
            if current is not None
            else None
        )
        self._payload: dict[str, object] | None = None
        self.setObjectName("icalFeedDialog")
        self.setWindowTitle("Import iCal feed" if self.current is None else "Edit iCal feed")
        self.setAccessibleName("iCalendar feed setup")
        self.setModal(True)
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        description = QLabel(
            "Import a public iCalendar URL for read-only display in Calendar. "
            "Events are kept in memory and the feed URL is stored locally."
        )
        description.setObjectName("icalFeedDescription")
        description.setTextFormat(Qt.TextFormat.PlainText)
        description.setWordWrap(True)
        layout.addWidget(description)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.title_input = QLineEdit(self.current.title if self.current is not None else "")
        self.title_input.setObjectName("icalFeedTitleInput")
        self.title_input.setAccessibleName("iCalendar feed title")
        self.title_input.setPlaceholderText("A short name for this calendar")
        self.title_input.setMaxLength(240)
        form.addRow("Title", self.title_input)

        self.url_input = QLineEdit(str(self.current.feed_url) if self.current is not None else "")
        self.url_input.setObjectName("icalFeedUrlInput")
        self.url_input.setAccessibleName("iCalendar feed URL")
        self.url_input.setAccessibleDescription(
            "Use an HTTPS URL when possible. HTTP requires explicit opt-in."
        )
        self.url_input.setPlaceholderText("https://calendar.example/events.ics")
        self.url_input.setMaxLength(2_048)
        form.addRow("iCal URL", self.url_input)

        self.interval_input = QLineEdit(
            str(self.current.refresh_minutes) if self.current is not None else "30"
        )
        self.interval_input.setObjectName("icalFeedRefreshInput")
        self.interval_input.setAccessibleName("iCalendar feed refresh interval in minutes")
        self.interval_input.setMaxLength(4)
        form.addRow("Refresh minutes", self.interval_input)

        self.http_checkbox = QCheckBox("Allow HTTP")
        self.http_checkbox.setObjectName("icalFeedHttpCheckBox")
        self.http_checkbox.setAccessibleName("Allow insecure HTTP iCalendar URL")
        self.http_checkbox.setAccessibleDescription(
            "HTTP is disabled by default and requires both this opt-in and the Hub network setting."
        )
        self.http_checkbox.setChecked(
            self.current.allow_http if self.current is not None else False
        )
        form.addRow("Network policy", self.http_checkbox)

        self.enabled_checkbox = QCheckBox("Enabled")
        self.enabled_checkbox.setObjectName("icalFeedEnabledCheckBox")
        self.enabled_checkbox.setAccessibleName("Enable iCalendar feed")
        self.enabled_checkbox.setChecked(self.current.enabled if self.current is not None else True)
        form.addRow("Status", self.enabled_checkbox)
        layout.addLayout(form)

        self.status_label = QLabel()
        self.status_label.setObjectName("icalFeedValidationStatus")
        self.status_label.setAccessibleName("iCalendar feed validation status")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Import" if self.current is None else "Save changes"
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _coerce_current(
        current: Mapping[str, object],
    ) -> IcalCalendarFeed:
        values = dict(current)
        feed_id = values.get("feed_id")
        if feed_id is not None and not isinstance(feed_id, UUID):
            with suppress(AttributeError, TypeError, ValueError):
                values["feed_id"] = UUID(str(feed_id))
        return IcalCalendarFeed.model_validate(values)

    def accept(self) -> None:
        title = " ".join(self.title_input.text().split())
        if not title:
            self.status_label.setText("Enter a title for this iCalendar feed.")
            return
        url = self.url_input.text().strip()
        try:
            parts = urlsplit(url)
        except ValueError:
            parts = None
        if parts is None or parts.scheme.casefold() not in {"http", "https"} or not parts.hostname:
            self.status_label.setText("Enter a valid HTTP or HTTPS iCalendar URL.")
            return
        if parts.username is not None or parts.password is not None:
            self.status_label.setText("iCalendar URLs cannot contain account credentials.")
            return
        if parts.scheme.casefold() == "http" and not self.http_checkbox.isChecked():
            self.status_label.setText("Enable Allow HTTP to import an HTTP iCalendar URL.")
            return
        try:
            refresh_minutes = int(self.interval_input.text().strip())
        except ValueError:
            refresh_minutes = 0
        if not 15 <= refresh_minutes <= 1_440:
            self.status_label.setText("Refresh minutes must be between 15 and 1440.")
            return
        try:
            reviewed_origin = origin_for_url(url) if parts.scheme.casefold() == "http" else None
        except (FeedPolicyError, ValueError):
            self.status_label.setText("That iCalendar feed origin is not allowed.")
            return
        try:
            IcalCalendarFeed(
                title=title,
                feed_url=url,
                enabled=self.enabled_checkbox.isChecked(),
                refresh_minutes=refresh_minutes,
                allow_http=self.http_checkbox.isChecked(),
                reviewed_origin=reviewed_origin,
            )
        except (TypeError, ValueError):
            self.status_label.setText("That iCalendar feed could not be validated safely.")
            return
        self._payload = {
            "title": title,
            "feed_url": url,
            "enabled": self.enabled_checkbox.isChecked(),
            "refresh_minutes": refresh_minutes,
            "allow_http": self.http_checkbox.isChecked(),
        }
        super().accept()

    def feed_payload(self) -> dict[str, object] | None:
        return None if self._payload is None else dict(self._payload)


__all__ = ["IcalFeedDialog"]
