"""Responsive Home widget board with direct placement and free resizing."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Any, Final, TypedDict

from PySide6.QtCore import QEasingCurve, QPoint, QPointF, QPropertyAnimation, QRect, Qt, Signal
from PySide6.QtGui import QCursor, QKeyEvent, QMouseEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

LAYOUT_VERSION: Final[int] = 3
GRID_COLUMNS: Final[int] = 48
GRID_BREAKPOINT: Final[int] = 760
GRID_GAP: Final[int] = 12
GRID_SNAP: Final[int] = 6
MIN_CARD_WIDTH: Final[int] = 240
MAX_CARD_HEIGHT: Final[int] = 1800
MAX_CARD_TOP: Final[int] = 24000
_LEGACY_LAYOUT_VERSION: Final[int] = 1
_PREVIOUS_LAYOUT_VERSION: Final[int] = 2
_PREVIOUS_GRID_COLUMNS: Final[int] = 12
_PREVIOUS_GRID_SNAP: Final[int] = 24
_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_MAX_TITLE_LENGTH: Final[int] = 180

SECTION_DESTINATIONS: Final[dict[str, str]] = {
    "mini_calendar": "Calendar",
    "schedule": "Calendar",
    "calendar": "Calendar",
    "overdue": "Tasks",
    "today": "Tasks",
    "upcoming": "Tasks",
    "unscheduled": "Tasks",
    "tasks": "Tasks",
    "inbox": "Inbox",
    "attention": "Inbox",
    "watch_alerts": "Watchlists",
    "watchlist": "Watchlists",
    "watchlists": "Watchlists",
    "news": "Watchlists",
    "source_problems": "Sources",
    "sources": "Sources",
}


class DashboardRectangle(TypedDict):
    """Logical grid rectangle stored for one widget."""

    x: int
    y: int
    w: int
    h: int


class DashboardLayout(TypedDict):
    """JSON-safe saved dashboard layout."""

    version: int
    order: list[str]
    hidden: list[str]
    geometry: dict[str, DashboardRectangle]


def _section_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 128 or _ID_PATTERN.fullmatch(value) is None:
        return None
    return value


def _title(value: object) -> str:
    if not isinstance(value, str):
        return "Widget"
    value = " ".join(value.split())
    return value[:_MAX_TITLE_LENGTH] or "Widget"


def _global_point(event: QMouseEvent | QKeyEvent) -> QPoint:
    """Return the Qt 6 pointer position with compatibility for test doubles."""

    position = getattr(event, "globalPosition", None)
    if callable(position):
        value = position()
        if isinstance(value, QPointF):
            return value.toPoint()
    legacy = getattr(event, "globalPos", None)
    if callable(legacy):
        value = legacy()
        if isinstance(value, QPoint):
            return value
    return QCursor.pos()


def _key_direction(key: int) -> tuple[int, int] | None:
    return {
        Qt.Key.Key_Left: (-1, 0),
        Qt.Key.Key_Right: (1, 0),
        Qt.Key.Key_Up: (0, -1),
        Qt.Key.Key_Down: (0, 1),
    }.get(key)


def _snapped(value: int, step: int) -> int:
    step = max(1, step)
    return max(0, round(value / step) * step)


class _CardHeader(QWidget):
    """Keyboard and pointer handle for moving one dashboard widget."""

    drag_started = Signal(object)
    drag_moved = Signal(object)
    drag_finished = Signal(object)
    keyboard_move_requested = Signal(int)

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("dashboardCardHeader")
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleDescription(
            "Drag this heading to move the widget. When focused, use the arrow keys to move it."
        )
        self._drag_origin: QPoint | None = None
        self._dragging = False
        self._drag_enabled = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.drag_indicator = QLabel("⠿", self)
        self.drag_indicator.setToolTip("Drag to move this widget")
        self.drag_indicator.setAccessibleName("Move widget")
        self.drag_indicator.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.drag_indicator.hide()
        layout.addWidget(self.drag_indicator)

        self.title_label = QLabel(title, self)
        self.title_label.setObjectName("dashboardCardTitle")
        self.title_label.setTextFormat(Qt.TextFormat.PlainText)
        self.title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.title_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.title_label, 1)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API
        if event.button() == Qt.MouseButton.LeftButton and self._drag_enabled:
            self._drag_origin = _global_point(event)
            self._dragging = False
            self.drag_started.emit(self._drag_origin)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API
        if self._drag_origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            point = _global_point(event)
            if not self._dragging and (point - self._drag_origin).manhattanLength() >= 4:
                self._dragging = True
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
            if self._dragging:
                self.drag_moved.emit(point)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API
        if event.button() == Qt.MouseButton.LeftButton and self._drag_origin is not None:
            point = _global_point(event) if self._dragging else None
            self._drag_origin = None
            self._dragging = False
            self.setCursor(
                Qt.CursorShape.OpenHandCursor if self._drag_enabled else Qt.CursorShape.ArrowCursor
            )
            self.drag_finished.emit(point)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt API
        if (
            self._drag_enabled
            and _key_direction(event.key()) is not None
        ):
            self.keyboard_move_requested.emit(event.key())
            event.accept()
            return
        super().keyPressEvent(event)

    def set_drag_enabled(self, enabled: bool) -> None:
        self._drag_enabled = bool(enabled)
        self.drag_indicator.setVisible(self._drag_enabled)
        self.setCursor(
            Qt.CursorShape.OpenHandCursor if self._drag_enabled else Qt.CursorShape.ArrowCursor
        )


class _ResizeGrip(QFrame):
    """Focusable pointer and keyboard resize handle for one edge or corner."""

    drag_started = Signal(str, object)
    drag_moved = Signal(str, object)
    drag_finished = Signal(str, object)
    keyboard_resize_requested = Signal(str, int)

    _CURSORS: Final[dict[str, Qt.CursorShape]] = {
        "n": Qt.CursorShape.SizeVerCursor,
        "s": Qt.CursorShape.SizeVerCursor,
        "e": Qt.CursorShape.SizeHorCursor,
        "w": Qt.CursorShape.SizeHorCursor,
        "ne": Qt.CursorShape.SizeBDiagCursor,
        "sw": Qt.CursorShape.SizeBDiagCursor,
        "nw": Qt.CursorShape.SizeFDiagCursor,
        "se": Qt.CursorShape.SizeFDiagCursor,
    }
    _DESCRIPTIONS: Final[dict[str, str]] = {
        "n": "top edge",
        "s": "bottom edge",
        "e": "right edge",
        "w": "left edge",
        "ne": "top right corner",
        "nw": "top left corner",
        "se": "bottom right corner",
        "sw": "bottom left corner",
    }

    def __init__(self, mode: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.mode = mode
        object_name = (
            "dashboardResizeGrip" if mode == "se" else f"dashboardResizeGrip{mode.upper()}"
        )
        self.setObjectName(object_name)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setCursor(self._CURSORS[mode])
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setToolTip(f"Resize from the {self._DESCRIPTIONS[mode]}")
        self.setAccessibleName(f"Resize widget from {self._DESCRIPTIONS[mode]}")
        self.setAccessibleDescription(
            "Drag this handle or use the arrow keys to resize the widget."
        )
        if mode in {"n", "s"}:
            self.setFixedSize(28, 7)
        elif mode in {"e", "w"}:
            self.setFixedSize(7, 28)
        else:
            self.setFixedSize(14, 14)
        self._dragging = False

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self.drag_started.emit(self.mode, _global_point(event))
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API
        if self._dragging:
            self.drag_moved.emit(self.mode, _global_point(event))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            self.drag_finished.emit(self.mode, _global_point(event))
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt API
        if _key_direction(event.key()) is not None:
            self.keyboard_resize_requested.emit(self.mode, event.key())
            event.accept()
            return
        super().keyPressEvent(event)


class _DashboardCard(QFrame):
    """Presentation shell around one caller-provided content widget."""

    remove_requested = Signal(str)
    drag_started = Signal(str, object)
    drag_moved = Signal(str, object)
    drag_finished = Signal(str, object)
    keyboard_move_requested = Signal(str, int)
    resize_started = Signal(str, str, object)
    resize_moved = Signal(str, str, object)
    resize_finished = Signal(str, str, object)
    keyboard_resize_requested = Signal(str, str, int)
    open_requested = Signal(str)

    _GRIP_MODES: Final[tuple[str, ...]] = ("n", "s", "e", "w", "ne", "nw", "se", "sw")

    def __init__(
        self,
        section_id: str,
        title: str,
        content: QWidget,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.section_id = section_id
        self.title = title
        self.content = content
        self.setObjectName("dashboardWidgetCard")
        self.setProperty("dashboardWidgetId", section_id)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName(f"{title} widget")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)

        self.header = _CardHeader(title, self)
        self.header.setAccessibleName(f"Move {title} widget")
        self.header.drag_started.connect(
            lambda point: self.drag_started.emit(self.section_id, point)
        )
        self.header.drag_moved.connect(lambda point: self.drag_moved.emit(self.section_id, point))
        self.header.drag_finished.connect(
            lambda point: self.drag_finished.emit(self.section_id, point)
        )
        self.header.keyboard_move_requested.connect(
            lambda key: self.keyboard_move_requested.emit(self.section_id, key)
        )
        outer.addWidget(self.header)

        content.setParent(self)
        content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        outer.addWidget(content, 1)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(4)
        self.open_button = QPushButton("Open", self)
        self.open_button.setObjectName("dashboardOpenButton")
        self.open_button.setAutoDefault(False)
        self.open_button.setAccessibleName(f"Open {title}")
        self.open_button.clicked.connect(lambda: self.open_requested.emit(self.section_id))
        footer.addWidget(self.open_button)

        footer.addStretch(1)
        self.edit_controls = QWidget(self)
        self.edit_controls.setObjectName("dashboardEditControls")
        edit_layout = QHBoxLayout(self.edit_controls)
        edit_layout.setContentsMargins(0, 0, 0, 0)
        edit_layout.setSpacing(3)
        self.remove_button = QPushButton("Remove", self.edit_controls)
        self.remove_button.setObjectName("dashboardRemoveButton")
        self.remove_button.setAutoDefault(False)
        self.remove_button.setAccessibleName(f"Remove {title} widget")
        self.remove_button.clicked.connect(lambda: self.remove_requested.emit(self.section_id))
        edit_layout.addWidget(self.remove_button)
        footer.addWidget(self.edit_controls)

        outer.addLayout(footer)
        self._outer_layout = outer

        self.resize_grips: dict[str, _ResizeGrip] = {}
        for mode in self._GRIP_MODES:
            grip = _ResizeGrip(mode, self)
            grip.setVisible(False)
            grip.drag_started.connect(
                lambda grip_mode, point: self.resize_started.emit(self.section_id, grip_mode, point)
            )
            grip.drag_moved.connect(
                lambda grip_mode, point: self.resize_moved.emit(self.section_id, grip_mode, point)
            )
            grip.drag_finished.connect(
                lambda grip_mode, point: self.resize_finished.emit(
                    self.section_id, grip_mode, point
                )
            )
            grip.keyboard_resize_requested.connect(
                lambda grip_mode, key: self.keyboard_resize_requested.emit(
                    self.section_id, grip_mode, key
                )
            )
            self.resize_grips[mode] = grip
        self.resize_grip = self.resize_grips["se"]

        self._geometry_animation = QPropertyAnimation(self, b"geometry", self)
        self._geometry_animation.setDuration(160)
        self._geometry_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.set_editing(False)

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        if not hasattr(self, "resize_grips"):
            return
        w, h = self.width(), self.height()
        positions = {
            "n": ((w - 28) // 2, 0),
            "s": ((w - 28) // 2, max(0, h - 7)),
            "e": (max(0, w - 7), (h - 28) // 2),
            "w": (0, (h - 28) // 2),
            "ne": (max(0, w - 14), 0),
            "nw": (0, 0),
            "se": (max(0, w - 14), max(0, h - 14)),
            "sw": (0, max(0, h - 14)),
        }
        for mode, grip in self.resize_grips.items():
            grip.move(*positions[mode])
            grip.raise_()

    def set_title(self, title: str) -> None:
        self.title = title
        self.header.title_label.setText(title)
        self.header.setAccessibleName(f"Move {title} widget")
        self.setAccessibleName(f"{title} widget")
        self.open_button.setAccessibleName(f"Open {title}")
        self.remove_button.setAccessibleName(f"Remove {title} widget")

    def replace_content(
        self,
        content: QWidget,
        *,
        preserve_old_ids: set[int] | None = None,
    ) -> None:
        """Swap the caller-owned body while keeping the card shell and controls."""

        if content is self.content:
            return
        old_content = self.content
        index = self._outer_layout.indexOf(old_content)
        if index >= 0:
            self._outer_layout.removeWidget(old_content)
        old_content.setParent(None)
        if preserve_old_ids is None or id(old_content) not in preserve_old_ids:
            old_content.deleteLater()
        self.content = content
        content.setParent(self)
        content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._outer_layout.insertWidget(max(0, index), content, 1)

    def detach_content_for_reuse(self) -> None:
        """Detach the body without scheduling deletion during a content swap."""

        old_content = self.content
        index = self._outer_layout.indexOf(old_content)
        if index >= 0:
            self._outer_layout.removeWidget(old_content)
        old_content.setParent(None)

    def set_editing(self, enabled: bool) -> None:
        enabled = bool(enabled)
        self.edit_controls.setVisible(enabled)
        for grip in self.resize_grips.values():
            grip.setVisible(enabled)
        self.header.set_drag_enabled(enabled)
        self.setProperty("dashboardEditing", enabled)

    def stop_geometry_animation(self) -> None:
        self._geometry_animation.stop()

    def animate_geometry(self, target: QRect, *, enabled: bool) -> None:
        if not enabled:
            self._geometry_animation.stop()
            self.setGeometry(target)
            return
        current = self.geometry()
        if current == target:
            return
        self._geometry_animation.stop()
        self._geometry_animation.setStartValue(current)
        self._geometry_animation.setEndValue(target)
        self._geometry_animation.start()


class _GridHost(QWidget):
    """Child surface that tells the board when the responsive width changes."""

    resized = Signal()

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self.resized.emit()


class DashboardBoard(QWidget):
    """Responsive Home widget board with direct placement and free resizing."""

    layout_changed = Signal(object)
    open_requested = Signal(str)
    editing_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("dashboardBoard")
        self.setAccessibleName("Personal Hub dashboard widgets")
        self.setMinimumWidth(280)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        self._cards: dict[str, _DashboardCard] = {}
        self._order: list[str] = []
        self._hidden: set[str] = set()
        self._default_visibility: dict[str, bool] = {}
        self._geometry: dict[str, DashboardRectangle] = {}
        self._editing = False
        self._reduced_motion = False
        self._has_laid_out = False
        self._layouting = False
        self._target_geometries: dict[str, QRect] = {}
        self._drag_state: dict[str, Any] | None = None
        self._resize_state: dict[str, Any] | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)

        toolbar = QFrame(self)
        toolbar.setObjectName("dashboardToolbar")
        toolbar.setAccessibleName("Dashboard customization toolbar")
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(0, 0, 0, 0)
        toolbar_layout.setSpacing(8)

        self.add_button = QToolButton(toolbar)
        self.add_button.setObjectName("dashboardAddButton")
        self.add_button.setText("Add widget")
        self.add_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.add_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.add_button.setAccessibleName("Add dashboard widget")
        self.add_menu = QMenu(self.add_button)
        self.add_menu.setObjectName("dashboardAddMenu")
        self.add_menu.aboutToShow.connect(self._refresh_add_menu)
        self.add_button.setMenu(self.add_menu)
        toolbar_layout.addWidget(self.add_button)

        self.edit_button = QPushButton("Edit layout", toolbar)
        self.edit_button.setObjectName("dashboardEditToggle")
        self.edit_button.setCheckable(True)
        self.edit_button.setAutoDefault(False)
        self.edit_button.setAccessibleName("Edit dashboard layout")
        self.edit_button.setAccessibleDescription(
            "Drag widget headings to move them and drag any edge or corner to resize. "
            "Focused headings and resize handles also work with the arrow keys."
        )
        self.edit_button.toggled.connect(self.set_editing)
        toolbar_layout.addWidget(self.edit_button)

        self.reset_button = QPushButton("Reset layout", toolbar)
        self.reset_button.setObjectName("dashboardResetButton")
        self.reset_button.setAutoDefault(False)
        self.reset_button.setAccessibleName("Reset dashboard layout")
        self.reset_button.clicked.connect(self.reset_layout)
        toolbar_layout.addWidget(self.reset_button)
        toolbar_layout.addStretch(1)
        outer.addWidget(toolbar)

        self._grid_panel = QFrame(self)
        self._grid_panel.setObjectName("dashboardGridPanel")
        self._grid_panel.setAccessibleName("Dashboard widget panel")
        self._grid_panel.setAccessibleDescription(
            "The outlined panel contains the available dashboard widget space."
        )
        self._grid_panel.setFrameShape(QFrame.Shape.NoFrame)
        self._grid_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        panel_layout = QVBoxLayout(self._grid_panel)
        panel_layout.setContentsMargins(8, 8, 8, 8)
        panel_layout.setSpacing(0)

        self._grid_host = _GridHost(self._grid_panel)
        self._grid_host.setObjectName("dashboardGrid")
        self._grid_host.setAccessibleName("Dashboard widget grid")
        self._grid_host.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        panel_layout.addWidget(self._grid_host, 1)
        outer.addWidget(self._grid_panel, 1)
        self._grid_host.resized.connect(self._grid_resized)

        self._gesture_preview = QFrame(self._grid_host)
        self._gesture_preview.setObjectName("dashboardDropPreview")
        self._gesture_preview.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._gesture_preview.hide()
        self._drag_indicator = self._gesture_preview

        self.toolbar = toolbar
        self.grid = self._grid_host
        self.widgets = self._cards
        self.customize_button = self.edit_button
        self.edit_toggle = self.edit_button
        self.add_widget_button = self.add_button
        self.widget_catalog = self.add_menu
        self.reset_layout_button = self.reset_button

    @property
    def editing(self) -> bool:
        return self._editing

    @property
    def reduced_motion(self) -> bool:
        return self._reduced_motion

    @property
    def column_count(self) -> int:
        return self._column_count()

    @property
    def widget_ids(self) -> tuple[str, ...]:
        return tuple(self._order)

    @property
    def visible_widget_ids(self) -> tuple[str, ...]:
        return tuple(section_id for section_id in self._order if section_id not in self._hidden)

    def widget(self, section_id: str) -> QWidget | None:
        """Return a card shell by ID for focus and integration tests."""

        return self._cards.get(section_id)

    def destination_for(self, section_id: str) -> str | None:
        """Return the safe navigation destination for a section, if known."""

        if not isinstance(section_id, str):
            return None
        return SECTION_DESTINATIONS.get(section_id.casefold())

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        self._layout_cards(animate=False)

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self._layout_cards(animate=not self._reduced_motion)

    def set_sections(self, sections: Sequence[tuple[str, str, QWidget, bool]]) -> None:
        """Replace content while retaining geometry, order, and hidden state."""

        normalized: list[tuple[str, str, QWidget, bool]] = []
        seen: set[str] = set()
        seen_content_ids: set[int] = set()
        for item in sections:
            if not isinstance(item, (tuple, list)) or len(item) != 4:
                continue
            section_id = _section_id(item[0])
            content = item[2]
            if (
                section_id is None
                or section_id in seen
                or not isinstance(content, QWidget)
                or id(content) in seen_content_ids
            ):
                continue
            seen.add(section_id)
            seen_content_ids.add(id(content))
            normalized.append((section_id, _title(item[1]), content, bool(item[3])))

        old_order = list(self._order)
        old_hidden = set(self._hidden)
        old_geometry = {key: dict(value) for key, value in self._geometry.items()}
        self._default_visibility = {
            section_id: self._default_visibility.get(section_id, visible)
            for section_id, _, _, visible in normalized
        }
        new_content_ids = {id(content) for _, _, content, _ in normalized}
        desired_content = {section_id: content for section_id, _, content, _ in normalized}

        for section_id, card in tuple(self._cards.items()):
            if (
                id(card.content) in new_content_ids
                and desired_content.get(section_id) is not card.content
            ):
                card.detach_content_for_reuse()

        for section_id, card in tuple(self._cards.items()):
            card.stop_geometry_animation()
            if section_id not in seen:
                card.setParent(None)
                card.deleteLater()
                self._cards.pop(section_id, None)

        self._order = [section_id for section_id in old_order if section_id in seen]
        self._order.extend(
            section_id for section_id, *_ in normalized if section_id not in self._order
        )
        self._hidden = {section_id for section_id in old_hidden if section_id in seen}
        self._geometry = {
            section_id: self._normalized_rectangle(old_geometry[section_id])
            for section_id in seen
            if section_id in old_geometry
        }

        for section_id, title, content, visible in normalized:
            if section_id not in old_order and not visible:
                self._hidden.add(section_id)
            card = self._cards.get(section_id)
            if card is None:
                card = _DashboardCard(section_id, title, content, self._grid_host)
                card.remove_requested.connect(self.remove_widget)
                card.open_requested.connect(self._open_card)
                card.drag_started.connect(self._begin_drag)
                card.drag_moved.connect(self._move_drag)
                card.drag_finished.connect(self._finish_drag)
                card.keyboard_move_requested.connect(self._keyboard_move)
                card.resize_started.connect(self._begin_resize)
                card.resize_moved.connect(self._preview_resize)
                card.resize_finished.connect(self._finish_resize)
                card.keyboard_resize_requested.connect(self._keyboard_resize)
                self._cards[section_id] = card
            else:
                card.set_title(title)
                card.replace_content(content, preserve_old_ids=new_content_ids)
            card.set_editing(self._editing)

        self._seed_missing_geometry()
        self._refresh_add_menu()
        self._layout_cards(animate=False)

    def export_layout(self) -> DashboardLayout:
        """Return a JSON-safe, allowlisted copy of the current layout."""

        order = [section_id for section_id in self._order if section_id in self._cards]
        return {
            "version": LAYOUT_VERSION,
            "order": order,
            "hidden": [section_id for section_id in order if section_id in self._hidden],
            "geometry": {
                section_id: dict(self._geometry[section_id])
                for section_id in order
                if section_id in self._geometry
            },
        }

    def validate_layout(self, payload: object) -> DashboardLayout | None:
        """Validate and normalize a saved layout against the current ID allowlist."""

        return self._normalized_layout(payload)

    def restore_layout(self, payload: Mapping[str, object]) -> bool:
        """Restore a v2 layout or migrate a previously saved v1 preset layout."""

        normalized = self._normalized_layout(payload)
        if normalized is None:
            return False
        changed = normalized != self.export_layout()
        self._order = list(normalized["order"])
        self._hidden = set(normalized["hidden"])
        self._geometry = {key: dict(value) for key, value in normalized["geometry"].items()}
        self._refresh_add_menu()
        self._layout_cards(animate=changed)
        if changed:
            self._emit_layout_changed()
        return True

    def set_editing(self, enabled: bool) -> None:
        """Toggle direct-manipulation controls without changing card data."""

        enabled = bool(enabled)
        if self._editing == enabled:
            self.edit_button.blockSignals(True)
            self.edit_button.setChecked(enabled)
            self.edit_button.blockSignals(False)
            return
        self._editing = enabled
        self.edit_button.blockSignals(True)
        self.edit_button.setChecked(enabled)
        self.edit_button.blockSignals(False)
        self.edit_button.setText("Done editing" if enabled else "Edit layout")
        self._grid_panel.setProperty("dashboardEditing", enabled)
        self._grid_panel.style().unpolish(self._grid_panel)
        self._grid_panel.style().polish(self._grid_panel)
        self._grid_panel.update()
        for card in self._cards.values():
            card.set_editing(enabled)
        self.refresh_geometry()
        self.editing_changed.emit(enabled)

    def set_reduced_motion(self, enabled: bool) -> None:
        """Stop active animations and settle immediately when motion is reduced."""

        self._reduced_motion = bool(enabled)
        if self._reduced_motion:
            for card in self._cards.values():
                card.stop_geometry_animation()
            self._layout_cards(animate=False)

    def refresh_geometry(self) -> None:
        """Recompute card bounds after a font, theme, or stylesheet change."""

        self._layout_cards(animate=False)

    def reset_layout(self) -> None:
        """Restore the current section catalog to a predictable default grid."""

        if not self._cards:
            return
        self._order = list(self._cards)
        self._hidden = {
            section_id
            for section_id in self._order
            if not self._default_visibility.get(section_id, True)
        }
        self._geometry = self._default_geometry_for_order(self._order)
        self._refresh_add_menu()
        self._layout_cards(animate=not self._reduced_motion)
        self._emit_layout_changed()

    def remove_widget(self, section_id: str) -> bool:
        """Hide a widget while retaining it in the Add widget catalog."""

        if section_id not in self._cards or section_id in self._hidden:
            return False
        self._hidden.add(section_id)
        self._layout_cards(animate=not self._reduced_motion)
        self._refresh_add_menu()
        self._emit_layout_changed()
        return True

    def add_widget(self, section_id: str) -> bool:
        """Make one hidden widget visible again."""

        if section_id not in self._cards or section_id not in self._hidden:
            return False
        self._hidden.discard(section_id)
        self._layout_cards(animate=not self._reduced_motion)
        self._refresh_add_menu()
        self._emit_layout_changed()
        return True

    show_widget = add_widget

    def set_widget_visible(self, section_id: str, visible: bool) -> bool:
        return self.add_widget(section_id) if visible else self.remove_widget(section_id)

    def set_widget_order(self, order: Sequence[str]) -> bool:
        """Apply an explicit order while preserving each widget's saved size."""

        requested: list[str] = []
        for value in order:
            section_id = _section_id(value)
            if section_id in self._cards and section_id not in requested:
                requested.append(section_id)
        requested.extend(section_id for section_id in self._order if section_id not in requested)
        if requested == self._order:
            return False

        old_geometry = self._geometry
        reordered: dict[str, DashboardRectangle] = {}
        y = 0
        x = 0
        row_height = 0
        for section_id in requested:
            original = old_geometry.get(
                section_id,
                {"x": 0, "y": 0, "w": GRID_COLUMNS // 2, "h": 0},
            )
            span = max(1, min(GRID_COLUMNS, original["w"]))
            height = max(self._natural_height(self._cards[section_id]), original["h"])
            if x + span > GRID_COLUMNS:
                y += row_height + GRID_GAP
                x = 0
                row_height = 0
            reordered[section_id] = {"x": x, "y": y, "w": span, "h": original["h"]}
            x += span
            row_height = max(row_height, height)
            if x >= GRID_COLUMNS:
                y += row_height + GRID_GAP
                x = 0
                row_height = 0

        self._order = requested
        self._geometry = reordered
        self._layout_cards(animate=not self._reduced_motion)
        self._emit_layout_changed()
        return True

    def move_widget_earlier(self, section_id: str) -> bool:
        """Compatibility operation for callers with a legacy order-only control."""

        return self._move_widget_by_order(section_id, -1)

    def move_widget_later(self, section_id: str) -> bool:
        """Compatibility operation for callers with a legacy order-only control."""

        return self._move_widget_by_order(section_id, 1)

    move_earlier = move_widget_earlier
    move_later = move_widget_later

    def set_widget_size(self, section_id: str, preset: str) -> bool:
        """Compatibility adapter that converts a legacy preset into grid geometry."""

        if section_id not in self._cards:
            return False
        presets = {
            "compact": {"x": 0, "y": 0, "w": GRID_COLUMNS // 2, "h": 240},
            "comfortable": {"x": 0, "y": 0, "w": GRID_COLUMNS // 2, "h": 0},
            "wide": {"x": 0, "y": 0, "w": GRID_COLUMNS, "h": 0},
            "tall": {"x": 0, "y": 0, "w": GRID_COLUMNS // 2, "h": 360},
        }
        rectangle = presets.get(str(preset).casefold())
        if rectangle is None or self._geometry.get(section_id) == rectangle:
            return False
        self._geometry[section_id] = self._normalized_rectangle(rectangle)
        self._layout_cards(animate=not self._reduced_motion)
        self._emit_layout_changed()
        return True

    resize_widget = set_widget_size

    def _move_widget_by_order(self, section_id: str, direction: int) -> bool:
        visible = [value for value in self._order if value not in self._hidden]
        if section_id not in self._cards or section_id not in visible:
            return False
        current_index = visible.index(section_id)
        target_index = current_index + direction
        if target_index < 0 or target_index >= len(visible):
            return False
        visible[current_index], visible[target_index] = (
            visible[target_index],
            visible[current_index],
        )
        hidden = [value for value in self._order if value in self._hidden]
        self._order = visible + hidden
        self._geometry = self._default_geometry_for_order(self._order)
        self._layout_cards(animate=not self._reduced_motion)
        self._emit_layout_changed()
        return True

    def _normalized_layout(self, payload: object) -> DashboardLayout | None:
        if not isinstance(payload, Mapping):
            return None
        version = payload.get("version", payload.get("layout_version", _LEGACY_LAYOUT_VERSION))
        if isinstance(version, bool) or not isinstance(version, int):
            return None
        if version not in {
            _LEGACY_LAYOUT_VERSION,
            _PREVIOUS_LAYOUT_VERSION,
            LAYOUT_VERSION,
        }:
            return None

        raw_order = payload.get("order", list(self._order))
        raw_hidden = payload.get("hidden", [])
        if not isinstance(raw_order, (list, tuple)):
            return None
        if not isinstance(raw_hidden, (list, tuple, set, frozenset)):
            return None

        known = set(self._cards)
        order: list[str] = []
        for value in raw_order:
            section_id = _section_id(value)
            if section_id in known and section_id not in order:
                order.append(section_id)
        order.extend(section_id for section_id in self._order if section_id not in order)
        order.extend(section_id for section_id in self._cards if section_id not in order)

        hidden: list[str] = []
        for value in raw_hidden:
            section_id = _section_id(value)
            if section_id in known and section_id not in hidden:
                hidden.append(section_id)
        hidden = [section_id for section_id in order if section_id in hidden]

        if version == _LEGACY_LAYOUT_VERSION:
            raw_sizes = payload.get("sizes", {})
            if not isinstance(raw_sizes, Mapping):
                return None
            previous_geometry = self._legacy_geometry(order, raw_sizes)
            geometry = {
                section_id: self._migrate_previous_rectangle(previous_geometry[section_id])
                for section_id in order
            }
        elif version == _PREVIOUS_LAYOUT_VERSION:
            raw_geometry = payload.get("geometry", {})
            if not isinstance(raw_geometry, Mapping):
                return None
            defaults = self._default_geometry_for_order(order)
            geometry = {
                section_id: (
                    self._migrate_previous_rectangle(raw_geometry[section_id])
                    if section_id in raw_geometry
                    else defaults[section_id]
                )
                for section_id in order
            }
        else:
            raw_geometry = payload.get("geometry", {})
            if not isinstance(raw_geometry, Mapping):
                return None
            defaults = self._default_geometry_for_order(order)
            geometry = {
                section_id: self._normalized_rectangle(
                    raw_geometry.get(section_id, defaults[section_id])
                )
                for section_id in order
            }

        return {
            "version": LAYOUT_VERSION,
            "order": order,
            "hidden": hidden,
            "geometry": geometry,
        }

    def _legacy_geometry(
        self,
        order: list[str],
        sizes: Mapping[str, object],
    ) -> dict[str, DashboardRectangle]:
        geometry: dict[str, DashboardRectangle] = {}
        y = 0
        row_height = 0
        column = 0
        for section_id in order:
            preset = str(sizes.get(section_id, "comfortable")).casefold()
            width = _PREVIOUS_GRID_COLUMNS if preset == "wide" else 6
            height = {"compact": 240, "tall": 360}.get(preset, 0)
            estimated_height = height or self._natural_height(self._cards[section_id])
            if width == _PREVIOUS_GRID_COLUMNS and column:
                y += row_height + GRID_GAP
                row_height = 0
                column = 0
            if width == _PREVIOUS_GRID_COLUMNS:
                geometry[section_id] = {"x": 0, "y": y, "w": width, "h": height}
                y += estimated_height + GRID_GAP
                continue
            x = 0 if column == 0 else 6
            geometry[section_id] = {"x": x, "y": y, "w": width, "h": height}
            row_height = max(row_height, estimated_height)
            if column == 0:
                column = 1
            else:
                y += row_height + GRID_GAP
                row_height = 0
                column = 0
        return geometry

    def _migrate_previous_rectangle(self, value: object) -> DashboardRectangle:
        """Expand one v1/v2 rectangle into the finer v3 grid."""

        if not isinstance(value, Mapping):
            return {"x": 0, "y": 0, "w": GRID_COLUMNS // 2, "h": 0}
        x, y, width, height = (
            value.get(key, default)
            for key, default in (
                ("x", 0),
                ("y", 0),
                ("w", _PREVIOUS_GRID_COLUMNS // 2),
                ("h", 0),
            )
        )
        numbers = (x, y, width, height)
        if any(isinstance(number, bool) or not isinstance(number, int) for number in numbers):
            return {"x": 0, "y": 0, "w": GRID_COLUMNS // 2, "h": 0}
        width = max(1, min(_PREVIOUS_GRID_COLUMNS, width))
        x = max(0, min(_PREVIOUS_GRID_COLUMNS - width, x))
        y = _snapped(max(0, min(MAX_CARD_TOP, y)), _PREVIOUS_GRID_SNAP)
        height = (
            0
            if height <= 0
            else _snapped(
                max(_PREVIOUS_GRID_SNAP, min(MAX_CARD_HEIGHT, height)),
                _PREVIOUS_GRID_SNAP,
            )
        )
        return self._normalized_rectangle({"x": x * 4, "y": y, "w": width * 4, "h": height})

    def _normalized_rectangle(self, value: object) -> DashboardRectangle:
        if not isinstance(value, Mapping):
            return {"x": 0, "y": 0, "w": GRID_COLUMNS // 2, "h": 0}
        x, y, width, height = (
            value.get(key, default)
            for key, default in (
                ("x", 0),
                ("y", 0),
                ("w", GRID_COLUMNS // 2),
                ("h", 0),
            )
        )
        numbers = (x, y, width, height)
        if any(isinstance(number, bool) or not isinstance(number, int) for number in numbers):
            return {"x": 0, "y": 0, "w": GRID_COLUMNS // 2, "h": 0}
        width = max(1, min(GRID_COLUMNS, width))
        x = max(0, min(GRID_COLUMNS - width, x))
        y = _snapped(max(0, min(MAX_CARD_TOP, y)), GRID_SNAP)
        height = (
            0 if height <= 0 else _snapped(max(GRID_SNAP, min(MAX_CARD_HEIGHT, height)), GRID_SNAP)
        )
        return {"x": x, "y": y, "w": width, "h": height}

    def _seed_missing_geometry(self) -> None:
        missing = [section_id for section_id in self._order if section_id not in self._geometry]
        if not missing:
            return
        if not self._geometry:
            self._geometry.update(self._default_geometry_for_order(self._order))
            return
        bottom = max(
            (
                self._geometry[section_id]["y"]
                + max(
                    self._natural_height(self._cards[section_id]),
                    self._geometry[section_id]["h"],
                )
                for section_id in self._geometry
                if section_id in self._cards
            ),
            default=0,
        )
        y = _snapped(bottom + GRID_GAP, GRID_SNAP)
        row_height = 0
        column = 0
        half_columns = GRID_COLUMNS // 2
        for section_id in missing:
            height = self._natural_height(self._cards[section_id])
            self._geometry[section_id] = {
                "x": half_columns if column else 0,
                "y": y,
                "w": half_columns,
                "h": 0,
            }
            row_height = max(row_height, height)
            if column:
                y += row_height + GRID_GAP
                row_height = 0
                column = 0
            else:
                column = 1

    def _default_geometry_for_order(self, order: Sequence[str]) -> dict[str, DashboardRectangle]:
        geometry: dict[str, DashboardRectangle] = {}
        y = 0
        row_height = 0
        column = 0
        half_columns = GRID_COLUMNS // 2
        for section_id in order:
            card = self._cards.get(section_id)
            height = self._natural_height(card) if card is not None else 240
            geometry[section_id] = {
                "x": 0 if column == 0 else half_columns,
                "y": y,
                "w": half_columns,
                "h": 0,
            }
            row_height = max(row_height, height)
            if column == 0:
                column = 1
            else:
                y += row_height + GRID_GAP
                row_height = 0
                column = 0
        return geometry

    def _natural_height(self, card: _DashboardCard) -> int:
        content_height = max(
            0,
            card.content.minimumSizeHint().height(),
            card.content.minimumHeight(),
        )
        chrome_height = max(
            96,
            card.header.sizeHint().height()
            + max(card.open_button.sizeHint().height(), card.remove_button.sizeHint().height())
            + 38,
        )
        return min(MAX_CARD_HEIGHT, max(210, content_height + chrome_height))

    def _height_for(self, section_id: str) -> int:
        card = self._cards[section_id]
        configured = self._geometry[section_id]["h"]
        return min(MAX_CARD_HEIGHT, max(self._natural_height(card), configured or 0))

    def _column_count(self) -> int:
        minimum_card_width = max(
            (card.minimumSizeHint().width() for card in self._cards.values()),
            default=MIN_CARD_WIDTH,
        )
        threshold = max(GRID_BREAKPOINT, 2 * max(MIN_CARD_WIDTH, minimum_card_width) + GRID_GAP)
        return 2 if self._grid_host.width() >= threshold else 1

    def _grid_metrics(self) -> tuple[float, float]:
        width = max(1, self._grid_host.width())
        unit_width = max(1.0, (width - GRID_GAP * (GRID_COLUMNS - 1)) / GRID_COLUMNS)
        return unit_width, unit_width + GRID_GAP

    def _minimum_width_units(self, card: _DashboardCard) -> int:
        _unit_width, step = self._grid_metrics()
        minimum = max(MIN_CARD_WIDTH, card.minimumSizeHint().width())
        return max(1, min(GRID_COLUMNS, math.ceil((minimum + GRID_GAP) / step)))

    def _display_rectangle(self, section_id: str, rectangle: DashboardRectangle) -> QRect:
        card = self._cards[section_id]
        width = max(1, self._grid_host.width())
        height = max(self._natural_height(card), rectangle["h"] or 0)
        if self._column_count() == 1:
            return QRect(0, rectangle["y"], width, height)
        unit_width, step = self._grid_metrics()
        span = min(GRID_COLUMNS, max(rectangle["w"], self._minimum_width_units(card)))
        x_cell = min(rectangle["x"], GRID_COLUMNS - span)
        x = round(x_cell * step)
        card_width = (
            width if span == GRID_COLUMNS else round(span * unit_width + (span - 1) * GRID_GAP)
        )
        return QRect(x, rectangle["y"], max(1, card_width), height)

    def _layout_cards(self, *, animate: bool) -> None:
        if self._layouting:
            return
        self._layouting = True
        try:
            if self._column_count() == 1:
                self._order.sort(
                    key=lambda section_id: (
                        self._geometry[section_id]["y"],
                        self._geometry[section_id]["x"],
                    )
                )
            visible = [
                section_id
                for section_id in self._order
                if section_id in self._cards and section_id not in self._hidden
            ]
            targets: dict[str, QRect] = {}
            width = max(1, self._grid_host.width())
            if self._column_count() == 1:
                y = 0
                for section_id in visible:
                    height = self._height_for(section_id)
                    targets[section_id] = QRect(0, y, width, height)
                    y += height + GRID_GAP
            else:
                preferred = sorted(
                    visible,
                    key=lambda section_id: (
                        self._geometry[section_id]["y"],
                        self._geometry[section_id]["x"],
                        self._order.index(section_id),
                    ),
                )
                for section_id in preferred:
                    rectangle = self._geometry[section_id]
                    card = self._cards[section_id]
                    minimum_units = self._minimum_width_units(card)
                    span = min(GRID_COLUMNS, max(minimum_units, rectangle["w"]))
                    x_cell = min(rectangle["x"], GRID_COLUMNS - span)
                    if x_cell != rectangle["x"] or span != rectangle["w"]:
                        rectangle["x"] = x_cell
                        rectangle["w"] = span
                    target = self._display_rectangle(section_id, rectangle)
                    while any(target.intersects(placed) for placed in targets.values()):
                        target.translate(0, GRID_SNAP)
                    rectangle["y"] = target.top()
                    targets[section_id] = target

            self._target_geometries = targets
            total_height = max((target.bottom() + 1 for target in targets.values()), default=1)
            self._grid_host.setMinimumHeight(total_height)
            self._grid_host.setMaximumHeight(16777215)
            animation_enabled = bool(animate and self._has_laid_out and not self._reduced_motion)

            for section_id, card in self._cards.items():
                target = targets.get(section_id)
                if target is None:
                    card.stop_geometry_animation()
                    card.hide()
                    continue
                card.setMinimumWidth(min(target.width(), self._minimum_card_width(section_id)))
                card.setMaximumWidth(16777215)
                card.setMinimumHeight(self._natural_height(card))
                card.setMaximumHeight(MAX_CARD_HEIGHT)
                for mode, grip in card.resize_grips.items():
                    horizontal_only = mode in {"e", "w"}
                    grip.setVisible(
                        self._editing and not (self._column_count() == 1 and horizontal_only)
                    )
                card.show()
                card.animate_geometry(target, enabled=animation_enabled)

            self._order.sort(
                key=lambda section_id: (
                    self._target_geometries.get(
                        section_id,
                        QRect(0, self._geometry[section_id]["y"], 0, 0),
                    ).top(),
                    self._target_geometries.get(
                        section_id,
                        QRect(0, self._geometry[section_id]["y"], 0, 0),
                    ).left(),
                )
            )
            self._has_laid_out = True
        finally:
            self._layouting = False

    def _minimum_card_width(self, section_id: str) -> int:
        return max(MIN_CARD_WIDTH, self._cards[section_id].minimumSizeHint().width())

    def _grid_resized(self) -> None:
        if not self._layouting:
            self._layout_cards(animate=not self._reduced_motion)

    def _emit_layout_changed(self) -> None:
        self.layout_changed.emit(dict(self.export_layout()))

    def _open_card(self, section_id: str) -> None:
        destination = self.destination_for(section_id)
        if destination is not None:
            self.open_requested.emit(destination)

    def _refresh_add_menu(self) -> None:
        self.add_menu.clear()
        hidden = [section_id for section_id in self._order if section_id in self._hidden]
        for section_id in hidden:
            card = self._cards.get(section_id)
            if card is None:
                continue
            action = self.add_menu.addAction(f"Show {card.title}")
            action.setData(section_id)
            action.setToolTip(f"Add the {card.title} widget to the dashboard.")
            action.triggered.connect(
                lambda _checked=False, value=section_id: self.add_widget(value)
            )
        if not hidden:
            action = self.add_menu.addAction("All widgets are visible")
            action.setEnabled(False)

    def _begin_drag(self, section_id: str, point: object) -> None:
        if not self._editing or not isinstance(point, QPoint):
            return
        card = self._cards.get(section_id)
        if card is None:
            return
        top_left = card.mapToGlobal(QPoint(0, 0))
        self._drag_state = {
            "section_id": section_id,
            "offset": point - top_left,
            "rectangle": dict(self._geometry[section_id]),
            "candidate": dict(self._geometry[section_id]),
        }
        card.raise_()

    def _move_drag(self, section_id: str, point: object) -> None:
        state = self._drag_state
        if state is None or state["section_id"] != section_id or not isinstance(point, QPoint):
            return
        local_top_left = self._grid_host.mapFromGlobal(point - state["offset"])
        _unit_width, step = self._grid_metrics()
        rectangle = dict(state["rectangle"])
        if self._column_count() > 1:
            rectangle["w"] = min(
                GRID_COLUMNS,
                max(rectangle["w"], self._minimum_width_units(self._cards[section_id])),
            )
            rectangle["x"] = max(
                0,
                min(GRID_COLUMNS - rectangle["w"], round(local_top_left.x() / step)),
            )
        rectangle["y"] = _snapped(local_top_left.y(), GRID_SNAP)
        state["candidate"] = rectangle
        self._gesture_preview.setGeometry(self._display_rectangle(section_id, rectangle))
        self._gesture_preview.raise_()
        self._gesture_preview.show()

    def _finish_drag(self, section_id: str, point: object) -> None:
        state = self._drag_state
        if state is None or state["section_id"] != section_id:
            return
        self._drag_state = None
        self._gesture_preview.hide()
        if not isinstance(point, QPoint):
            return
        local_top_left = self._grid_host.mapFromGlobal(point - state["offset"])
        _unit_width, step = self._grid_metrics()
        rectangle = dict(state["candidate"])
        if self._column_count() > 1:
            rectangle["x"] = max(
                0,
                min(GRID_COLUMNS - rectangle["w"], round(local_top_left.x() / step)),
            )
        rectangle["y"] = _snapped(local_top_left.y(), GRID_SNAP)
        self._geometry[section_id] = self._normalized_rectangle(rectangle)
        self._layout_cards(animate=not self._reduced_motion)
        self._emit_layout_changed()

    def _begin_resize(self, section_id: str, mode: str, point: object) -> None:
        if not self._editing or not isinstance(point, QPoint):
            return
        card = self._cards.get(section_id)
        if card is None:
            return
        self._resize_state = {
            "section_id": section_id,
            "mode": mode,
            "origin": point,
            "screen": QRect(card.geometry()),
            "candidate": QRect(card.geometry()),
            "rectangle": dict(self._geometry[section_id]),
        }

    def _preview_resize(self, section_id: str, mode: str, point: object) -> None:
        state = self._resize_state
        if (
            state is None
            or state["section_id"] != section_id
            or state["mode"] != mode
            or not isinstance(point, QPoint)
        ):
            return
        origin = state["origin"]
        candidate = self._resized_screen_rectangle(
            section_id,
            mode,
            state["screen"],
            point.x() - origin.x(),
            point.y() - origin.y(),
        )
        state["candidate"] = candidate
        self._gesture_preview.setGeometry(candidate)
        self._gesture_preview.raise_()
        self._gesture_preview.show()

    def _finish_resize(self, section_id: str, mode: str, point: object) -> None:
        state = self._resize_state
        if state is None or state["section_id"] != section_id or state["mode"] != mode:
            return
        if isinstance(point, QPoint):
            origin = state["origin"]
            state["candidate"] = self._resized_screen_rectangle(
                section_id,
                mode,
                state["screen"],
                point.x() - origin.x(),
                point.y() - origin.y(),
            )
        self._resize_state = None
        self._gesture_preview.hide()
        rectangle = self._logical_rectangle(state["candidate"])
        if self._column_count() == 1:
            rectangle["x"] = state["rectangle"]["x"]
            rectangle["w"] = state["rectangle"]["w"]
        self._geometry[section_id] = self._normalized_rectangle(rectangle)
        self._layout_cards(animate=not self._reduced_motion)
        self._emit_layout_changed()

    def _resized_screen_rectangle(
        self,
        section_id: str,
        mode: str,
        start: QRect,
        dx: int,
        dy: int,
    ) -> QRect:
        _unit_width, step = self._grid_metrics()
        left, right = start.left(), start.right() + 1
        top, bottom = start.top(), start.bottom() + 1
        if self._column_count() == 1:
            dx = 0
        minimum_width = self._minimum_card_width(section_id)
        minimum_height = self._natural_height(self._cards[section_id])
        if "w" in mode:
            left = min(_snapped(max(0, left + dx), round(step)), right - minimum_width)
        elif "e" in mode:
            desired = _snapped(max(0, right + dx), round(step))
            right = max(left + minimum_width, min(self._grid_host.width(), desired))
        if "n" in mode:
            top = min(_snapped(max(0, top + dy), GRID_SNAP), bottom - minimum_height)
        elif "s" in mode:
            desired = _snapped(max(0, bottom + dy), GRID_SNAP)
            bottom = max(top + minimum_height, min(MAX_CARD_HEIGHT + top, desired))
        right = max(left + minimum_width, right)
        bottom = max(top + minimum_height, bottom)
        return QRect(left, top, right - left, bottom - top)

    def _logical_rectangle(self, screen: QRect) -> DashboardRectangle:
        _unit_width, step = self._grid_metrics()
        span = max(1, min(GRID_COLUMNS, round((screen.width() + GRID_GAP) / step)))
        x = max(0, min(GRID_COLUMNS - span, round(screen.left() / step)))
        return {
            "x": x,
            "y": _snapped(screen.top(), GRID_SNAP),
            "w": span,
            "h": _snapped(screen.height(), GRID_SNAP),
        }

    def _keyboard_move(self, section_id: str, key: int) -> None:
        if not self._editing or section_id not in self._geometry:
            return
        direction = _key_direction(key)
        if direction is None:
            return
        dx, dy = direction
        if self._column_count() == 1:
            dx = 0
        rectangle = dict(self._geometry[section_id])
        rectangle["x"] = max(0, min(GRID_COLUMNS - rectangle["w"], rectangle["x"] + dx))
        rectangle["y"] = max(0, min(MAX_CARD_TOP, rectangle["y"] + dy * GRID_SNAP))
        self._geometry[section_id] = self._normalized_rectangle(rectangle)
        self._layout_cards(animate=not self._reduced_motion)
        self._emit_layout_changed()

    def _keyboard_resize(self, section_id: str, mode: str, key: int) -> None:
        if not self._editing or section_id not in self._cards:
            return
        direction = _key_direction(key)
        if direction is None:
            return
        dx, dy = direction
        if not any(edge in mode for edge in ("e", "w")):
            dx = 0
        if not any(edge in mode for edge in ("n", "s")):
            dy = 0
        if not dx and not dy:
            return
        start = QRect(self._cards[section_id].geometry())
        _unit_width, step = self._grid_metrics()
        candidate = self._resized_screen_rectangle(
            section_id,
            mode,
            start,
            int(round(step)) * dx,
            GRID_SNAP * dy,
        )
        rectangle = self._logical_rectangle(candidate)
        if self._column_count() == 1:
            rectangle["x"] = self._geometry[section_id]["x"]
            rectangle["w"] = self._geometry[section_id]["w"]
        self._geometry[section_id] = self._normalized_rectangle(rectangle)
        self._layout_cards(animate=not self._reduced_motion)
        self._emit_layout_changed()


__all__ = [
    "DashboardBoard",
    "DashboardLayout",
    "DashboardRectangle",
    "GRID_BREAKPOINT",
    "GRID_COLUMNS",
    "GRID_GAP",
    "GRID_SNAP",
    "LAYOUT_VERSION",
    "SECTION_DESTINATIONS",
]
