"""A small, reusable, directly-manipulable Personal Hub dashboard board.

The board deliberately owns presentation state only.  Callers provide the
already-rendered content for each section and receive a versioned layout
mapping when the user changes visibility, order, or size.  The content widgets
are never inspected or mutated beyond being reparented into their card shell.

The implementation uses a hand-laid-out grid instead of a ``QGridLayout`` so
that the same card wrappers can support responsive columns, animated geometry,
and a resize grip without creating a second source of truth for card bounds.
The parent page remains responsible for scrolling the board.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final, TypedDict

from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRect,
    Qt,
    Signal,
)
from PySide6.QtGui import QAction, QCursor, QMouseEvent
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

LAYOUT_VERSION: Final[int] = 1
"""Current persisted dashboard layout version."""

GRID_BREAKPOINT: Final[int] = 760
"""Board width at which the second card column becomes available."""

GRID_GAP: Final[int] = 12
"""Gap between cards in pixels."""

GRID_SNAP: Final[int] = 24
"""Resize-grip snap increment in pixels."""

SIZE_PRESETS: Final[tuple[str, ...]] = (
    "compact",
    "comfortable",
    "wide",
    "tall",
)

SIZE_LABELS: Final[dict[str, str]] = {
    "compact": "Compact",
    "comfortable": "Comfortable",
    "wide": "Wide",
    "tall": "Tall",
}

SECTION_DESTINATIONS: Final[dict[str, str]] = {
    "schedule": "Calendar",
    "calendar": "Calendar",
    "overdue": "Tasks",
    "today": "Tasks",
    "upcoming": "Tasks",
    "unscheduled": "Tasks",
    "tasks": "Tasks",
    # ``inbox`` is retained for the existing Attention messages section.
    "inbox": "Inbox",
    "attention": "Inbox",
    "watch_alerts": "Watchlists",
    "watchlist": "Watchlists",
    "watchlists": "Watchlists",
    "news": "Watchlists",
    "source_problems": "Sources",
    "sources": "Sources",
}
"""Stable Today section IDs and their detail destinations."""

_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_MAX_TITLE_LENGTH: Final[int] = 180


class DashboardLayout(TypedDict):
    """The JSON-safe persisted shape emitted by :meth:`DashboardBoard.export_layout`."""

    version: int
    order: list[str]
    hidden: list[str]
    sizes: dict[str, str]


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


def _size_preset(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().casefold()
    return value if value in SIZE_PRESETS else None


def _global_point(event: QMouseEvent) -> QPoint:
    """Return a Qt 6 mouse position while retaining compatibility with test doubles."""

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


class _CardHeader(QWidget):
    """Header that emits drag gestures while leaving its buttons clickable."""

    drag_started = Signal(object)
    drag_moved = Signal(object)
    drag_finished = Signal(object)

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("dashboardCardHeader")
        self.setMouseTracking(True)
        self._drag_origin: QPoint | None = None
        self._dragging = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.title_label = QLabel(title, self)
        self.title_label.setObjectName("dashboardCardTitle")
        self.title_label.setTextFormat(Qt.TextFormat.PlainText)
        self.title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.title_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.title_label, 1)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self.cursor().shape() == Qt.CursorShape.OpenHandCursor
        ):
            self._drag_origin = _global_point(event)
            self._dragging = False
            self.grabMouse()
            self.drag_started.emit(self._drag_origin)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API
        if self._drag_origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            point = _global_point(event)
            if not self._dragging and (point - self._drag_origin).manhattanLength() >= 4:
                self._dragging = True
            if self._dragging:
                self.drag_moved.emit(point)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API
        if event.button() == Qt.MouseButton.LeftButton and self._drag_origin is not None:
            self.releaseMouse()
            self.drag_finished.emit(_global_point(event) if self._dragging else None)
            self._drag_origin = None
            self._dragging = False
        super().mouseReleaseEvent(event)


class _ResizeGrip(QFrame):
    """Bottom-right grip with a short, bounded drag gesture."""

    drag_started = Signal(object)
    drag_moved = Signal(object)
    drag_finished = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("dashboardResizeGrip")
        self.setFixedSize(16, 16)
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self.setToolTip("Resize widget")
        self.setAccessibleName("Resize widget")
        self._dragging = False

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            point = _global_point(event)
            self.drag_started.emit(point)
            self.grabMouse()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API
        if self._dragging:
            self.drag_moved.emit(_global_point(event))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            self.releaseMouse()
            self.drag_finished.emit(_global_point(event))
            event.accept()
            return
        super().mouseReleaseEvent(event)


class _DashboardCard(QFrame):
    """Presentation shell around one caller-provided content widget."""

    remove_requested = Signal(str)
    move_earlier_requested = Signal(str)
    move_later_requested = Signal(str)
    size_requested = Signal(str, str)
    drag_started = Signal(str, object)
    drag_moved = Signal(str, object)
    drag_finished = Signal(str, object)
    resize_started = Signal(str, object)
    resize_moved = Signal(str, object)
    resize_finished = Signal(str, object)
    open_requested = Signal(str)

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
        self._size_preset = "comfortable"
        self.setObjectName("dashboardWidgetCard")
        self.setProperty("dashboardWidgetId", section_id)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName(f"{title} widget")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)

        self.header = _CardHeader(title, self)
        self.header.setAccessibleName(f"{title} widget header")
        self.header.drag_started.connect(
            lambda point: self.drag_started.emit(self.section_id, point)
        )
        self.header.drag_moved.connect(lambda point: self.drag_moved.emit(self.section_id, point))
        self.header.drag_finished.connect(
            lambda point: self.drag_finished.emit(self.section_id, point)
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

        self.move_earlier_button = QToolButton(self.edit_controls)
        self.move_earlier_button.setObjectName("dashboardMoveEarlierButton")
        self.move_earlier_button.setText("↑")
        self.move_earlier_button.setToolTip("Move earlier")
        self.move_earlier_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.move_earlier_button.setAccessibleName(f"Move {title} earlier")
        self.move_earlier_button.clicked.connect(
            lambda: self.move_earlier_requested.emit(self.section_id)
        )
        edit_layout.addWidget(self.move_earlier_button)

        self.move_later_button = QToolButton(self.edit_controls)
        self.move_later_button.setObjectName("dashboardMoveLaterButton")
        self.move_later_button.setText("↓")
        self.move_later_button.setToolTip("Move later")
        self.move_later_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.move_later_button.setAccessibleName(f"Move {title} later")
        self.move_later_button.clicked.connect(
            lambda: self.move_later_requested.emit(self.section_id)
        )
        edit_layout.addWidget(self.move_later_button)

        self.size_button = QToolButton(self.edit_controls)
        self.size_button.setObjectName("dashboardSizeButton")
        self.size_button.setText("Size")
        self.size_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.size_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.size_button.setAccessibleName(f"Resize {title}")
        self.size_menu = QMenu(self.size_button)
        self.size_menu.setObjectName("dashboardSizeMenu")
        self.size_actions: dict[str, QAction] = {}
        for preset in SIZE_PRESETS:
            action = self.size_menu.addAction(SIZE_LABELS[preset])
            action.setCheckable(True)
            action.setData(preset)
            action.setToolTip(f"Use the {SIZE_LABELS[preset].lower()} size.")
            action.triggered.connect(
                lambda _checked=False, value=preset: self.size_requested.emit(
                    self.section_id, value
                )
            )
            self.size_actions[preset] = action
        self.size_button.setMenu(self.size_menu)
        edit_layout.addWidget(self.size_button)

        self.remove_button = QPushButton("Remove", self.edit_controls)
        self.remove_button.setObjectName("dashboardRemoveButton")
        self.remove_button.setAutoDefault(False)
        self.remove_button.setAccessibleName(f"Remove {title} widget")
        self.remove_button.clicked.connect(lambda: self.remove_requested.emit(self.section_id))
        edit_layout.addWidget(self.remove_button)
        footer.addWidget(self.edit_controls)

        outer.addLayout(footer)
        self._outer_layout = outer

        self.resize_grip = _ResizeGrip(self)
        self.resize_grip.setVisible(False)
        self.resize_grip.drag_started.connect(
            lambda point: self.resize_started.emit(self.section_id, point)
        )
        self.resize_grip.drag_moved.connect(
            lambda point: self.resize_moved.emit(self.section_id, point)
        )
        self.resize_grip.drag_finished.connect(
            lambda point: self.resize_finished.emit(self.section_id, point)
        )

        self._geometry_animation = QPropertyAnimation(self, b"geometry", self)
        self._geometry_animation.setDuration(160)
        self._geometry_animation.setEasingCurve(QEasingCurve.Type.OutCubic)

        self.set_editing(False)

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        if not hasattr(self, "resize_grip"):
            return
        self.resize_grip.move(
            max(0, self.width() - self.resize_grip.width() - 4),
            max(0, self.height() - self.resize_grip.height() - 4),
        )
        self.resize_grip.raise_()

    @property
    def size_preset(self) -> str:
        return self._size_preset

    def set_title(self, title: str) -> None:
        self.title = title
        self.header.title_label.setText(title)
        self.setAccessibleName(f"{title} widget")
        self.open_button.setAccessibleName(f"Open {title}")
        self.move_earlier_button.setAccessibleName(f"Move {title} earlier")
        self.move_later_button.setAccessibleName(f"Move {title} later")
        self.size_button.setAccessibleName(f"Resize {title}")
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

    def set_size_preset(self, preset: str) -> None:
        preset = _size_preset(preset) or "comfortable"
        self._size_preset = preset
        self.setProperty("dashboardSize", preset)
        for name, action in self.size_actions.items():
            action.setChecked(name == preset)

    def set_editing(self, enabled: bool) -> None:
        enabled = bool(enabled)
        self.edit_controls.setVisible(enabled)
        self.resize_grip.setVisible(enabled)
        self.header.setCursor(
            Qt.CursorShape.OpenHandCursor if enabled else Qt.CursorShape.ArrowCursor
        )
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
    """Responsive, customizable board for caller-owned Hub section widgets.

    ``set_sections`` accepts tuples of ``(section_id, title, content,
    visible)``.  A section ID is the allowlist for its persisted layout entry;
    unknown IDs in a restored payload are ignored.  Existing IDs retain their
    saved order, hidden state, and size when fresh content is supplied.
    """

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
        self._sizes: dict[str, str] = {}
        self._editing = False
        self._reduced_motion = False
        self._has_laid_out = False
        self._layouting = False
        self._target_geometries: dict[str, QRect] = {}
        self._dragging_id: str | None = None
        self._resize_state: dict[str, Any] | None = None
        self._last_resize_size: str | None = None

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
            "Toggle controls for moving, resizing, and removing dashboard widgets."
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

        self._grid_host = _GridHost(self)
        self._grid_host.setObjectName("dashboardGrid")
        self._grid_host.setAccessibleName("Dashboard widget grid")
        self._grid_host.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        outer.addWidget(self._grid_host, 1)
        self._grid_host.resized.connect(self._grid_resized)
        self._drag_indicator = QFrame(self._grid_host)
        self._drag_indicator.setObjectName("dashboardDragIndicator")
        self._drag_indicator.setFixedHeight(4)
        self._drag_indicator.setStyleSheet(
            "background-color: palette(highlight); border-radius: 2px;"
        )
        self._drag_indicator.hide()

        # Public aliases make integration and test discovery straightforward.
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

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        self._layout_cards(animate=False)

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self._layout_cards(animate=not self._reduced_motion)

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

    def set_sections(self, sections: Sequence[tuple[str, str, QWidget, bool]]) -> None:
        """Replace section content while retaining layout state for known IDs.

        Existing IDs retain the board's current order, hidden state, and size.
        New IDs use the supplied ``visible`` flag and append after existing
        IDs.  Removed IDs are dropped from the allowlist and persisted state.
        """

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
        old_sizes = dict(self._sizes)
        self._default_visibility = {
            section_id: self._default_visibility.get(section_id, visible)
            for section_id, _, _, visible in normalized
        }
        new_content_ids = {id(content) for _, _, content, _ in normalized}
        desired_content = {section_id: content for section_id, _, content, _ in normalized}

        # Detach reused content first.  This handles both a same-ID refresh
        # and an intentional swap of two existing body widgets without
        # allowing an old wrapper's deferred deletion to take the new body
        # with it.
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
        self._sizes = {
            section_id: old_sizes.get(section_id, "comfortable")
            for section_id in seen
            if old_sizes.get(section_id, "comfortable") in SIZE_PRESETS
        }

        for section_id, title, content, visible in normalized:
            if section_id not in old_order and not visible:
                self._hidden.add(section_id)
            self._sizes.setdefault(section_id, "comfortable")
            card = self._cards.get(section_id)
            if card is None:
                card = _DashboardCard(section_id, title, content, self._grid_host)
                card.remove_requested.connect(self.remove_widget)
                card.move_earlier_requested.connect(self.move_widget_earlier)
                card.move_later_requested.connect(self.move_widget_later)
                card.size_requested.connect(self.set_widget_size)
                card.open_requested.connect(self._open_card)
                card.drag_started.connect(self._begin_drag)
                card.drag_moved.connect(self._move_drag)
                card.drag_finished.connect(self._finish_drag)
                card.resize_started.connect(self._begin_resize)
                card.resize_moved.connect(self._preview_resize)
                card.resize_finished.connect(self._finish_resize)
                self._cards[section_id] = card
            else:
                card.set_title(title)
                card.replace_content(content, preserve_old_ids=new_content_ids)
            card.set_size_preset(self._sizes[section_id])
            card.set_editing(self._editing)

        self._refresh_add_menu()
        self._layout_cards(animate=False)

    def export_layout(self) -> DashboardLayout:
        """Return a JSON-safe, allowlisted copy of the current layout."""

        order = [section_id for section_id in self._order if section_id in self._cards]
        return {
            "version": LAYOUT_VERSION,
            "order": order,
            "hidden": [section_id for section_id in order if section_id in self._hidden],
            "sizes": {
                section_id: self._sizes.get(section_id, "comfortable") for section_id in order
            },
        }

    def validate_layout(self, payload: object) -> DashboardLayout | None:
        """Validate and normalize a saved payload against the current ID allowlist."""

        normalized = self._normalized_layout(payload)
        return normalized

    def restore_layout(self, payload: Mapping[str, object]) -> bool:
        """Restore a validated layout, ignoring unknown section IDs.

        A malformed top-level payload leaves the current layout untouched and
        returns ``False``.  Unknown IDs are expected when a newer saved layout
        is opened with an older or partially loaded section catalog, so those
        entries are filtered while known entries continue to restore safely.
        """

        normalized = self._normalized_layout(payload)
        if normalized is None:
            return False
        changed = normalized != self.export_layout()
        self._order = list(normalized["order"])
        self._hidden = set(normalized["hidden"])
        self._sizes = dict(normalized["sizes"])
        for section_id, card in self._cards.items():
            card.set_size_preset(self._sizes[section_id])
        self._refresh_add_menu()
        self._layout_cards(animate=changed)
        if changed:
            self._emit_layout_changed()
        return True

    def set_editing(self, enabled: bool) -> None:
        """Toggle direct-manipulation controls without changing card data."""

        enabled = bool(enabled)
        if self._editing == enabled:
            # Keep a programmatically toggled toolbar button in sync.
            self.edit_button.blockSignals(True)
            self.edit_button.setChecked(enabled)
            self.edit_button.blockSignals(False)
            return
        self._editing = enabled
        self.edit_button.blockSignals(True)
        self.edit_button.setChecked(enabled)
        self.edit_button.blockSignals(False)
        self.edit_button.setText("Done editing" if enabled else "Edit layout")
        for card in self._cards.values():
            card.set_editing(enabled)
        self.refresh_geometry()
        self.editing_changed.emit(enabled)

    def set_reduced_motion(self, enabled: bool) -> None:
        """Stop active animations and settle immediately when motion is reduced."""

        enabled = bool(enabled)
        self._reduced_motion = enabled
        if enabled:
            for card in self._cards.values():
                card.stop_geometry_animation()
            self._layout_cards(animate=False)

    def refresh_geometry(self) -> None:
        """Recompute card bounds after a font, theme, or stylesheet change."""

        self._layout_cards(animate=False)

    def reset_layout(self) -> None:
        """Restore the current section catalog to its predictable default."""

        if not self._cards:
            return
        self._order = list(self._cards)
        self._hidden = {
            section_id
            for section_id in self._order
            if not self._default_visibility.get(section_id, True)
        }
        self._sizes = {section_id: "comfortable" for section_id in self._order}
        for card in self._cards.values():
            card.set_size_preset("comfortable")
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

    def move_widget_earlier(self, section_id: str) -> bool:
        return self._move_widget(section_id, -1)

    def move_widget_later(self, section_id: str) -> bool:
        return self._move_widget(section_id, 1)

    move_earlier = move_widget_earlier
    move_later = move_widget_later

    def set_widget_size(self, section_id: str, preset: str) -> bool:
        """Apply one of the four bounded size presets to a known widget."""

        preset = _size_preset(preset)
        if section_id not in self._cards or preset is None:
            return False
        if self._sizes.get(section_id) == preset:
            return False
        self._sizes[section_id] = preset
        self._cards[section_id].set_size_preset(preset)
        self._layout_cards(animate=not self._reduced_motion)
        self._emit_layout_changed()
        return True

    resize_widget = set_widget_size

    def _move_widget(self, section_id: str, direction: int) -> bool:
        visible = [value for value in self._order if value not in self._hidden]
        if section_id not in self._cards or section_id not in visible:
            return False
        visible_index = visible.index(section_id)
        target_visible_index = visible_index + direction
        if target_visible_index < 0 or target_visible_index >= len(visible):
            return False
        index = self._order.index(section_id)
        target = self._order.index(visible[target_visible_index])
        self._order[index], self._order[target] = self._order[target], self._order[index]
        self._layout_cards(animate=not self._reduced_motion)
        self._emit_layout_changed()
        return True

    def _normalized_layout(self, payload: object) -> DashboardLayout | None:
        if not isinstance(payload, Mapping):
            return None
        version = payload.get("version", payload.get("layout_version", LAYOUT_VERSION))
        if isinstance(version, bool) or not isinstance(version, int) or version != LAYOUT_VERSION:
            return None

        raw_order = payload.get("order", list(self._order))
        raw_hidden = payload.get("hidden", [])
        raw_sizes = payload.get("sizes", {})
        if not isinstance(raw_order, (list, tuple)):
            return None
        if not isinstance(raw_hidden, (list, tuple, set, frozenset)):
            return None
        if not isinstance(raw_sizes, Mapping):
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

        sizes: dict[str, str] = {}
        for section_id in order:
            value = raw_sizes.get(section_id, self._sizes.get(section_id, "comfortable"))
            sizes[section_id] = _size_preset(value) or "comfortable"

        return {
            "version": LAYOUT_VERSION,
            "order": order,
            "hidden": hidden,
            "sizes": sizes,
        }

    def _column_count(self) -> int:
        minimum_card_width = max(
            (card.minimumSizeHint().width() for card in self._cards.values()), default=0
        )
        threshold = max(GRID_BREAKPOINT, 2 * minimum_card_width + GRID_GAP)
        return 2 if self._grid_host.width() >= threshold else 1

    def _base_height(self, card: _DashboardCard, width: int) -> int:
        content_hint = max(
            0,
            card.content.minimumSizeHint().height(),
            card.content.minimumHeight(),
        )
        # Content is allowed to request a larger card; presets only add a
        # bounded amount of breathing room around that natural size.
        chrome_hint = max(
            96,
            card.header.sizeHint().height()
            + max(card.open_button.sizeHint().height(), card.edit_controls.sizeHint().height())
            + 38,
        )
        natural = max(200, content_hint + chrome_hint)
        if card.size_preset == "compact":
            if content_hint + chrome_hint <= 220:
                return max(188, min(natural, 220))
            return natural
        if card.size_preset == "tall":
            return max(300, natural + 100)
        # A flexible Qt list or view often reports a generous default
        # ``sizeHint`` even when it has only a couple of rows.  The minimum
        # height above is the useful content bound; cap ordinary cards only
        # while the content itself remains small, and always honor a large
        # explicitly requested minimum.
        return max(220, natural)

    def _layout_cards(self, *, animate: bool) -> None:
        if self._layouting:
            return
        self._layouting = True
        try:
            columns = self._column_count()
            width = max(1, self._grid_host.width())
            cell_width = max(1, (width - GRID_GAP * (columns - 1)) // columns)
            visible = [
                section_id
                for section_id in self._order
                if section_id in self._cards and section_id not in self._hidden
            ]
            targets: dict[str, QRect] = {}
            row_y = 0
            column = 0
            row_height = 0

            for section_id in visible:
                card = self._cards[section_id]
                span = 2 if card.size_preset == "wide" and columns == 2 else 1
                if span == 2 and column != 0:
                    row_y += row_height + GRID_GAP
                    column = 0
                    row_height = 0
                if column >= columns or (span == 2 and column + span > columns):
                    row_y += row_height + GRID_GAP
                    column = 0
                    row_height = 0
                x = column * (cell_width + GRID_GAP)
                card_width = cell_width * span + GRID_GAP * (span - 1)
                height = self._base_height(card, card_width)
                targets[section_id] = QRect(x, row_y, card_width, height)
                row_height = max(row_height, height)
                column += span
                if column >= columns:
                    row_y += row_height + GRID_GAP
                    column = 0
                    row_height = 0

            total_height = max(1, row_y + row_height)
            self._target_geometries = targets
            self._grid_host.setMinimumHeight(total_height)
            self._grid_host.setMaximumHeight(16777215)
            animation_enabled = bool(animate and self._has_laid_out and not self._reduced_motion)

            for section_id, card in self._cards.items():
                target = targets.get(section_id)
                if target is None:
                    card.stop_geometry_animation()
                    card.hide()
                    continue
                card.show()
                card.setMinimumHeight(target.height())
                card.setMaximumHeight(target.height())
                card.animate_geometry(target, enabled=animation_enabled)
            self._has_laid_out = True
        finally:
            self._layouting = False

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
        self._dragging_id = section_id

    def _move_drag(self, section_id: str, point: object) -> None:
        # Layout is committed on release so a drag cannot emit a stream of
        # partially-written payloads while the pointer crosses a card.  The
        # marker gives a clear target while the order is still staged.
        if self._dragging_id != section_id or not isinstance(point, QPoint):
            return
        target_index, target_rect, before = self._drop_details(
            self._grid_host.mapFromGlobal(point), section_id
        )
        _ = target_index
        if target_rect is None:
            self._drag_indicator.hide()
            return
        y = target_rect.top() - 2 if before else target_rect.bottom() - 1
        self._drag_indicator.setGeometry(target_rect.left(), max(0, y), target_rect.width(), 4)
        self._drag_indicator.raise_()
        self._drag_indicator.show()

    def _finish_drag(self, section_id: str, point: object) -> None:
        if self._dragging_id != section_id:
            return
        self._dragging_id = None
        self._drag_indicator.hide()
        if not isinstance(point, QPoint) or section_id not in self._order:
            return
        local = self._grid_host.mapFromGlobal(point)
        target_index = self._drop_index(local, section_id)
        current_index = self._order.index(section_id)
        remaining = [value for value in self._order if value != section_id]
        if target_index == current_index and target_index < len(remaining):
            return
        self._order = remaining
        self._order.insert(max(0, min(target_index, len(self._order))), section_id)
        self._layout_cards(animate=not self._reduced_motion)
        self._emit_layout_changed()

    def _drop_index(self, point: QPoint, dragged_id: str) -> int:
        index, _rect, _before = self._drop_details(point, dragged_id)
        return index

    def _drop_details(self, point: QPoint, dragged_id: str) -> tuple[int, QRect | None, bool]:
        """Return insertion index and marker geometry for a pointer position."""

        remaining = [section_id for section_id in self._order if section_id != dragged_id]
        visible = [
            section_id
            for section_id in remaining
            if section_id in self._cards and section_id not in self._hidden
        ]
        for section_id in visible:
            geometry = self._target_geometries.get(section_id)
            if geometry is None or not geometry.contains(point):
                continue
            # In a two-column row, the horizontal half is the natural choice;
            # on a tall card the vertical half remains easier to predict.
            before = (
                point.x() < geometry.center().x()
                if geometry.width() <= self._grid_host.width() // 2
                else point.y() < geometry.center().y()
            )
            index = remaining.index(section_id)
            return (index if before else index + 1), geometry, before

        ordered_geometry = [
            (section_id, self._target_geometries[section_id])
            for section_id in visible
            if section_id in self._target_geometries
        ]
        ordered_geometry.sort(key=lambda item: (item[1].top(), item[1].left()))
        for section_id, geometry in ordered_geometry:
            if point.y() < geometry.center().y() or (
                point.y() <= geometry.bottom() and point.x() < geometry.left()
            ):
                return remaining.index(section_id), geometry, True
        if ordered_geometry:
            section_id, geometry = ordered_geometry[-1]
            return len(remaining), geometry, False
        return len(remaining), None, True

    def _begin_resize(self, section_id: str, point: object) -> None:
        if not self._editing or not isinstance(point, QPoint):
            return
        card = self._cards.get(section_id)
        if card is None:
            return
        self._resize_state = {
            "section_id": section_id,
            "origin": point,
            "geometry": QRect(card.geometry()),
            "initial_size": self._sizes.get(section_id, "comfortable"),
        }
        self._last_resize_size = self._resize_state["initial_size"]

    def _preview_resize(self, section_id: str, point: object) -> None:
        state = self._resize_state
        if state is None or state.get("section_id") != section_id or not isinstance(point, QPoint):
            return
        origin = state["origin"]
        initial = state["geometry"]
        dx = round((point.x() - origin.x()) / GRID_SNAP) * GRID_SNAP
        dy = round((point.y() - origin.y()) / GRID_SNAP) * GRID_SNAP
        desired_width = max(112, initial.width() + dx)
        desired_height = max(112, initial.height() + dy)
        preset = self._preset_for_drag(section_id, desired_width, desired_height)
        if preset == self._last_resize_size:
            return
        self._last_resize_size = preset
        self._sizes[section_id] = preset
        self._cards[section_id].set_size_preset(preset)
        self._layout_cards(animate=False)

    def _finish_resize(self, section_id: str, point: object) -> None:
        if self._resize_state is None or self._resize_state.get("section_id") != section_id:
            return
        self._resize_state = None
        self._last_resize_size = None
        self._layout_cards(animate=not self._reduced_motion)
        self._emit_layout_changed()

    def _preset_for_drag(self, section_id: str, width: int, height: int) -> str:
        card = self._cards[section_id]
        base = max(
            132,
            card.content.minimumSizeHint().height(),
            card.content.minimumHeight(),
        )
        if self._column_count() == 2 and width >= self._grid_host.width() * 0.70:
            return "wide"
        if height >= max(280, base + 70):
            return "tall"
        if height <= min(156, base + 10):
            return "compact"
        return "comfortable"


__all__ = [
    "DashboardBoard",
    "DashboardLayout",
    "GRID_BREAKPOINT",
    "GRID_GAP",
    "GRID_SNAP",
    "LAYOUT_VERSION",
    "SECTION_DESTINATIONS",
    "SIZE_LABELS",
    "SIZE_PRESETS",
]
