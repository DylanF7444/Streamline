"""Short native page transitions, with immediate reduced-motion cancellation."""

from PySide6.QtCore import QEasingCurve, QObject, QPropertyAnimation
from PySide6.QtWidgets import QGraphicsOpacityEffect, QWidget


class PageTransition(QObject):
    def __init__(self, widget: QWidget) -> None:
        super().__init__(widget)
        self.widget = widget
        self.reduced_motion = False
        self.effect = QGraphicsOpacityEffect(widget)
        self.effect.setOpacity(1.0)
        widget.setGraphicsEffect(self.effect)
        self.animation = QPropertyAnimation(self.effect, b"opacity", self)
        self.animation.setDuration(180)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.animation.finished.connect(self._settle)
        self.effect.setEnabled(False)

    def _settle(self) -> None:
        self.effect.setOpacity(1.0)
        self.effect.setEnabled(False)

    def set_reduced_motion(self, enabled: bool) -> None:
        self.reduced_motion = bool(enabled)
        if enabled:
            self.animation.stop()
            self._settle()

    def start(self) -> None:
        self.animation.stop()
        if self.reduced_motion or not self.widget.isVisible():
            self._settle()
            return
        self.effect.setEnabled(True)
        self.animation.setStartValue(0.35)
        self.animation.setEndValue(1.0)
        self.animation.start()
