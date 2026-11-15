"""Shared desktop controls with interaction safeguards."""

from __future__ import annotations

from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDateTimeEdit,
    QDoubleSpinBox,
    QSpinBox,
    QTimeEdit,
)


class WheelSafeComboBox(QComboBox):
    """Keep wheel scrolling from changing a combo box's selected value."""

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 - Qt API name
        """Pass wheel input to the surrounding scroll area."""

        event.ignore()


class WheelSafeDateEdit(QDateEdit):
    """Keep wheel scrolling from changing a selected date."""

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 - Qt API name
        """Pass wheel input to the surrounding scroll area."""

        event.ignore()


class WheelSafeDateTimeEdit(QDateTimeEdit):
    """Keep wheel scrolling from changing a selected date and time."""

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 - Qt API name
        """Pass wheel input to the surrounding scroll area."""

        event.ignore()


class WheelSafeTimeEdit(QTimeEdit):
    """Keep wheel scrolling from changing a selected time."""

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 - Qt API name
        """Pass wheel input to the surrounding scroll area."""

        event.ignore()


class WheelSafeSpinBox(QSpinBox):
    """Keep wheel scrolling from changing a numeric value."""

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 - Qt API name
        """Pass wheel input to the surrounding scroll area."""

        event.ignore()


class WheelSafeDoubleSpinBox(QDoubleSpinBox):
    """Keep wheel scrolling from changing a decimal value."""

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 - Qt API name
        """Pass wheel input to the surrounding scroll area."""

        event.ignore()
