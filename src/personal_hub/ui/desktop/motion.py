"""Short native UI transitions, with immediate reduced-motion cancellation."""

from PySide6.QtCore import QAbstractAnimation, QEasingCurve, QObject, QPropertyAnimation
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


class DisclosureTransition(QObject):
    """Animate a vertical disclosure panel while keeping its final layout unconstrained."""

    def __init__(self, widget: QWidget, *, duration_ms: int = 180) -> None:
        super().__init__(widget)
        self.widget = widget
        self.reduced_motion = False
        self._target_visible = not widget.isHidden()
        self._expanded_maximum = widget.maximumHeight()
        self.animation = QPropertyAnimation(widget, b"maximumHeight", self)
        self.animation.setDuration(duration_ms)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.animation.finished.connect(self._settle)

    def _settle(self) -> None:
        self.widget.setMaximumHeight(self._expanded_maximum)
        self.widget.setVisible(self._target_visible)
        self.widget.updateGeometry()

    def set_reduced_motion(self, enabled: bool) -> None:
        self.reduced_motion = bool(enabled)
        if self.reduced_motion:
            self.animation.stop()
            self._settle()

    def set_visible(self, visible: bool) -> None:
        """Show or hide the panel, animating only while its containing page is visible."""

        visible = bool(visible)
        animation_was_running = self.animation.state() == QAbstractAnimation.State.Running
        current_extent = (
            self.widget.maximumHeight() if animation_was_running else self.widget.height()
        )
        self.animation.stop()
        self._target_visible = visible

        if visible and not animation_was_running and not self.widget.isHidden():
            self._settle()
            return

        parent = self.widget.parentWidget()
        if self.reduced_motion or parent is None or not parent.isVisible():
            self._settle()
            return

        if visible:
            if self.widget.isHidden():
                self.widget.setMaximumHeight(0)
                self.widget.show()
                self.widget.ensurePolished()
                parent_layout = parent.layout()
                if parent_layout is not None:
                    parent_layout.activate()
                layout = self.widget.layout()
                if layout is not None:
                    layout.activate()
                start_extent = 0
            else:
                start_extent = min(max(0, current_extent), self._expanded_maximum)
            target_extent = min(
                max(self.widget.sizeHint().height(), self.widget.minimumHeight()),
                self._expanded_maximum,
            )
            if target_extent <= 0 or start_extent == target_extent:
                self._settle()
                return
            self.animation.setStartValue(start_extent)
            self.animation.setEndValue(target_extent)
        else:
            if self.widget.isHidden():
                self._settle()
                return
            start_extent = current_extent if animation_was_running else self.widget.height()
            start_extent = max(0, start_extent)
            if start_extent <= 0:
                self._settle()
                return
            self.widget.setMaximumHeight(start_extent)
            self.animation.setStartValue(start_extent)
            self.animation.setEndValue(0)

        self.animation.start()
