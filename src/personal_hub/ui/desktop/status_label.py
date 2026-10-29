"""Plain-text outcomes that also announce visible changes to assistive technology."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QAccessible, QAccessibleAnnouncementEvent
from PySide6.QtWidgets import QLabel


class StatusLabel(QLabel):
    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(text, parent)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(True)

    def setText(self, text: str) -> None:
        changed = text != self.text()
        super().setText(text)
        if changed and text and self.isVisible():
            QAccessible.updateAccessibility(QAccessibleAnnouncementEvent(self, text))
