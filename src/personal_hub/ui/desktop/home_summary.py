"""Content-free Home counts, supplied by the desktop composition root."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QGridLayout, QLabel, QVBoxLayout, QWidget


class HomeSummary(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("homeSummary")
        self.setAccessibleName("Home totals")
        self._counts: tuple[int | None, ...] = (None, None, None)
        self._masked = False
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(12)
        self.cards: list[QFrame] = []
        self.values: list[QLabel] = []
        for title in ("Open tasks", "Unread alerts", "Logged today"):
            card = QFrame(self)
            card.setObjectName("homeSummaryCard")
            card.setAccessibleName(title)
            layout = QVBoxLayout(card)
            layout.setContentsMargins(16, 12, 16, 12)
            layout.setSpacing(3)
            label = QLabel(title)
            label.setObjectName("homeSummaryLabel")
            label.setWordWrap(True)
            value = QLabel("—")
            value.setObjectName("homeSummaryValue")
            value.setTextFormat(Qt.TextFormat.PlainText)
            value.setAccessibleName(title)
            layout.addWidget(label)
            layout.addWidget(value)
            self.cards.append(card)
            self.values.append(value)
        self.set_compact(False)

    def set_counts(self, *, open_tasks: int, unread_alerts: int, activity_entries: int) -> None:
        self._counts = (open_tasks, unread_alerts, activity_entries)
        self._render()

    def set_masked(self, masked: bool) -> None:
        self._masked = bool(masked)
        self._render()

    def _render(self) -> None:
        for label, value in zip(self.values, self._counts, strict=True):
            label.setText("—" if self._masked or value is None else str(max(0, value)))

    def set_compact(self, compact: bool) -> None:
        for index, card in enumerate(self.cards):
            self._grid.removeWidget(card)
            self._grid.addWidget(card, index if compact else 0, 0 if compact else index)
