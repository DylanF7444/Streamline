"""Local workspace navigation entries and their user customization dialog."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import uuid4

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

_ROUTE_ROLE: Final = int(Qt.ItemDataRole.UserRole)
_MAX_NAVIGATION_ITEMS: Final = 512
_MAX_LABEL_LENGTH: Final = 64
_MAX_CALENDAR_VIEWS: Final = 128
_MAX_VIEW_CALENDARS: Final = 512


@dataclass(frozen=True, slots=True)
class WorkspaceNavigationEntry:
    """One visible workspace route, optionally scoped to a local collection."""

    route_id: str
    label: str
    destination: str
    collection_kind: str = ""
    collection_id: str = ""


@dataclass(frozen=True, slots=True)
class WorkspaceCalendarView:
    """A named workspace calendar page backed by a chosen set of calendars."""

    view_id: str
    name: str
    calendar_ids: tuple[str, ...]


class CalendarViewDefinitionDialog(QDialog):
    """Create or update a named view over selected calendars."""

    def __init__(
        self,
        calendars: tuple[tuple[str, str], ...],
        *,
        view: WorkspaceCalendarView | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("calendarViewDefinitionDialog")
        self.setWindowTitle("Calendar view")
        self.setAccessibleName("Calendar view settings")
        self.setMinimumSize(440, 440)
        self._view_id = view.view_id if view is not None else uuid4().hex
        selected_ids = set(view.calendar_ids if view is not None else ())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)
        title = QLabel("Name this calendar view")
        title.setAccessibleName("Calendar view name heading")
        layout.addWidget(title)
        self.name_input = QLineEdit(view.name if view is not None else "")
        self.name_input.setObjectName("calendarViewNameInput")
        self.name_input.setAccessibleName("Calendar view name")
        self.name_input.setMaxLength(_MAX_LABEL_LENGTH)
        self.name_input.setPlaceholderText("For example, Work or Family")
        layout.addWidget(self.name_input)

        instructions = QLabel("Choose the calendars this view should show.")
        instructions.setWordWrap(True)
        instructions.setAccessibleName("Calendar view selection instructions")
        layout.addWidget(instructions)
        self.calendar_items = QListWidget()
        self.calendar_items.setObjectName("calendarViewCalendarItems")
        self.calendar_items.setAccessibleName("Calendars included in this view")
        known_ids: set[str] = set()
        for calendar_id, label in calendars[:_MAX_VIEW_CALENDARS]:
            known_ids.add(calendar_id)
            self._add_calendar_item(calendar_id, label, calendar_id in selected_ids)
        for calendar_id in sorted(selected_ids.difference(known_ids))[:_MAX_VIEW_CALENDARS]:
            self._add_calendar_item(
                calendar_id,
                f"Unavailable calendar · {calendar_id[:24]}",
                True,
            )
        layout.addWidget(self.calendar_items, 1)

        self.status_label = QLabel("")
        self.status_label.setObjectName("calendarViewDefinitionStatus")
        self.status_label.setAccessibleName("Calendar view validation status")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.setObjectName("calendarViewDefinitionButtons")
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setAccessibleName(
            "Save calendar view"
        )
        self.buttons.accepted.connect(self._save)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self._result: WorkspaceCalendarView | None = None

    def _add_calendar_item(self, calendar_id: str, label: str, selected: bool) -> None:
        item = QListWidgetItem(label)
        item.setData(_ROUTE_ROLE, calendar_id)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        item.setCheckState(Qt.CheckState.Checked if selected else Qt.CheckState.Unchecked)
        item.setData(
            Qt.ItemDataRole.AccessibleDescriptionRole,
            "Check to include this calendar in the view.",
        )
        self.calendar_items.addItem(item)

    def _save(self) -> None:
        name = " ".join(self.name_input.text().split())[:_MAX_LABEL_LENGTH]
        if not name:
            self.status_label.setText("Enter a name for this calendar view.")
            self.name_input.setFocus()
            return
        calendar_ids = tuple(
            calendar_id
            for index in range(self.calendar_items.count())
            if (item := self.calendar_items.item(index)).checkState() == Qt.CheckState.Checked
            and isinstance(calendar_id := item.data(_ROUTE_ROLE), str)
        )
        if not calendar_ids:
            self.status_label.setText("Choose at least one calendar for this view.")
            return
        self._result = WorkspaceCalendarView(self._view_id, name, calendar_ids)
        self.accept()

    @property
    def calendar_view(self) -> WorkspaceCalendarView | None:
        return self._result


class CalendarViewGroupsDialog(QDialog):
    """Manage named calendar views shown in workspace navigation."""

    def __init__(
        self,
        views: tuple[WorkspaceCalendarView, ...],
        calendars: tuple[tuple[str, str], ...],
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("calendarViewGroupsDialog")
        self.setWindowTitle("Calendar views")
        self.setAccessibleName("Manage calendar views")
        self.setMinimumSize(600, 500)
        self._views = list(views[:_MAX_CALENDAR_VIEWS])
        self._calendars = calendars[:_MAX_VIEW_CALENDARS]
        self._result: tuple[WorkspaceCalendarView, ...] | None = None
        self.selected_view_id: str | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)
        instructions = QLabel(
            "The main Calendar includes all calendars by default. Add named views to show "
            "specific combinations of calendars."
        )
        instructions.setObjectName("calendarViewGroupsHelp")
        instructions.setAccessibleName("Calendar view instructions")
        instructions.setWordWrap(True)
        layout.addWidget(instructions)

        self.items = QListWidget()
        self.items.setObjectName("calendarViewGroupsItems")
        self.items.setAccessibleName("Saved calendar views")
        self.items.currentRowChanged.connect(self._update_buttons)
        layout.addWidget(self.items, 1)

        actions = QHBoxLayout()
        self.add_button = QPushButton("Add view")
        self.add_button.setObjectName("calendarViewAddButton")
        self.add_button.setAccessibleName("Add a calendar view")
        self.add_button.clicked.connect(self._add_view)
        actions.addWidget(self.add_button)
        self.edit_button = QPushButton("Edit")
        self.edit_button.setObjectName("calendarViewEditButton")
        self.edit_button.setAccessibleName("Edit selected calendar view")
        self.edit_button.clicked.connect(self._edit_view)
        actions.addWidget(self.edit_button)
        self.remove_button = QPushButton("Remove")
        self.remove_button.setObjectName("calendarViewRemoveButton")
        self.remove_button.setAccessibleName("Remove selected calendar view")
        self.remove_button.clicked.connect(self._remove_view)
        actions.addWidget(self.remove_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.status_label = QLabel("")
        self.status_label.setObjectName("calendarViewGroupsStatus")
        self.status_label.setAccessibleName("Calendar views status")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.setObjectName("calendarViewGroupsButtons")
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setAccessibleName(
            "Save calendar views"
        )
        self.buttons.accepted.connect(self._save)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self._refresh_items()

    def _refresh_items(self, selected_id: str | None = None) -> None:
        if selected_id is None and 0 <= self.items.currentRow() < len(self._views):
            selected_id = self._views[self.items.currentRow()].view_id
        self.items.clear()
        for view in self._views:
            item = QListWidgetItem(f"{view.name} · {len(view.calendar_ids)} calendars")
            item.setData(_ROUTE_ROLE, view.view_id)
            item.setData(
                Qt.ItemDataRole.AccessibleDescriptionRole,
                f"Calendar view {view.name}, {len(view.calendar_ids)} calendars.",
            )
            self.items.addItem(item)
        selected_row = next(
            (index for index, view in enumerate(self._views) if view.view_id == selected_id),
            0 if self._views else -1,
        )
        self.items.setCurrentRow(selected_row)
        self._update_buttons()

    def _update_buttons(self, _row: int = -1) -> None:
        has_selection = 0 <= self.items.currentRow() < len(self._views)
        self.edit_button.setEnabled(has_selection)
        self.remove_button.setEnabled(has_selection)
        self.add_button.setEnabled(bool(self._calendars) and len(self._views) < _MAX_CALENDAR_VIEWS)

    def _name_available(self, name: str, *, except_id: str | None = None) -> bool:
        return all(
            view.view_id == except_id or view.name.casefold() != name.casefold()
            for view in self._views
        )

    def _add_view(self) -> None:
        if len(self._views) >= _MAX_CALENDAR_VIEWS:
            self.status_label.setText("The maximum number of calendar views has been reached.")
            return
        editor = CalendarViewDefinitionDialog(self._calendars, parent=self)
        if editor.exec() != QDialog.DialogCode.Accepted or editor.calendar_view is None:
            return
        view = editor.calendar_view
        if not self._name_available(view.name):
            self.status_label.setText("Calendar view names must be unique.")
            return
        self._views.append(view)
        self.status_label.clear()
        self._refresh_items(view.view_id)

    def _edit_view(self) -> None:
        index = self.items.currentRow()
        if not 0 <= index < len(self._views):
            return
        original = self._views[index]
        editor = CalendarViewDefinitionDialog(
            self._calendars,
            view=original,
            parent=self,
        )
        if editor.exec() != QDialog.DialogCode.Accepted or editor.calendar_view is None:
            return
        updated = editor.calendar_view
        if not self._name_available(updated.name, except_id=original.view_id):
            self.status_label.setText("Calendar view names must be unique.")
            return
        self._views[index] = updated
        self.status_label.clear()
        self._refresh_items(updated.view_id)

    def _remove_view(self) -> None:
        index = self.items.currentRow()
        if not 0 <= index < len(self._views):
            return
        self._views.pop(index)
        self.status_label.clear()
        self._refresh_items()

    def _save(self) -> None:
        self._result = tuple(self._views)
        row = self.items.currentRow()
        self.selected_view_id = self._views[row].view_id if 0 <= row < len(self._views) else None
        self.accept()

    @property
    def calendar_views(self) -> tuple[WorkspaceCalendarView, ...] | None:
        return self._result


class WorkspaceNavigationEditor(QDialog):
    """Rename, reorder, and show or hide workspace routes."""

    def __init__(
        self,
        entries: tuple[WorkspaceNavigationEntry, ...],
        *,
        visible_routes: set[str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("workspaceNavigationEditor")
        self.setWindowTitle("Customize workspace")
        self.setAccessibleName("Customize workspace pages")
        self.setMinimumSize(480, 480)
        self._entries = {entry.route_id: entry for entry in entries}
        self._preferences: dict[str, object] | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)

        title = QLabel("Workspace pages")
        title.setObjectName("workspaceNavigationEditorTitle")
        title.setAccessibleName("Workspace pages")
        layout.addWidget(title)

        instructions = QLabel(
            "Drag pages to reorder them. Double-click a name to rename it. "
            "Uncheck a page to hide it. Sources and Settings stay in Management."
        )
        instructions.setObjectName("workspaceNavigationEditorHelp")
        instructions.setWordWrap(True)
        instructions.setAccessibleName("Workspace page customization instructions")
        layout.addWidget(instructions)

        self.items = QListWidget()
        self.items.setObjectName("workspaceNavigationEditorItems")
        self.items.setAccessibleName("Reorder, rename, and show workspace pages")
        self.items.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.items.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.items.setDropIndicatorShown(True)
        self.items.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.items.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        for entry in entries[:_MAX_NAVIGATION_ITEMS]:
            item = QListWidgetItem(entry.label)
            item.setData(_ROUTE_ROLE, entry.route_id)
            item.setFlags(
                item.flags()
                | Qt.ItemFlag.ItemIsEditable
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsDragEnabled
            )
            item.setCheckState(
                Qt.CheckState.Checked
                if entry.route_id in visible_routes
                else Qt.CheckState.Unchecked
            )
            item.setData(
                Qt.ItemDataRole.AccessibleDescriptionRole,
                "Drag to reorder, double-click to rename, or uncheck to hide.",
            )
            self.items.addItem(item)
        self.items.setCurrentRow(0)
        layout.addWidget(self.items, 1)

        move_buttons = QHBoxLayout()
        self.move_up_button = QPushButton("Move up")
        self.move_up_button.setObjectName("workspaceNavigationMoveUpButton")
        self.move_up_button.setAccessibleName("Move selected workspace page up")
        self.move_up_button.clicked.connect(lambda: self._move_current(-1))
        move_buttons.addWidget(self.move_up_button)
        self.move_down_button = QPushButton("Move down")
        self.move_down_button.setObjectName("workspaceNavigationMoveDownButton")
        self.move_down_button.setAccessibleName("Move selected workspace page down")
        self.move_down_button.clicked.connect(lambda: self._move_current(1))
        move_buttons.addWidget(self.move_down_button)
        move_buttons.addStretch(1)
        layout.addLayout(move_buttons)

        self.status_label = QLabel("")
        self.status_label.setObjectName("workspaceNavigationEditorStatus")
        self.status_label.setAccessibleName("Workspace customization status")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.setObjectName("workspaceNavigationEditorButtons")
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setAccessibleName(
            "Save workspace customization"
        )
        self.buttons.accepted.connect(self._save)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def _move_current(self, offset: int) -> None:
        current_row = self.items.currentRow()
        target_row = current_row + offset
        if not 0 <= current_row < self.items.count() or not 0 <= target_row < self.items.count():
            return
        item = self.items.takeItem(current_row)
        self.items.insertItem(target_row, item)
        self.items.setCurrentItem(item)

    def _save(self) -> None:
        if not 1 <= self.items.count() <= _MAX_NAVIGATION_ITEMS:
            self.status_label.setText("Keep at least one workspace page.")
            return
        routes: list[str] = []
        labels: dict[str, str] = {}
        hidden: list[str] = []
        seen_labels: set[str] = set()
        for index in range(self.items.count()):
            item = self.items.item(index)
            route_id = item.data(_ROUTE_ROLE)
            if not isinstance(route_id, str) or route_id not in self._entries:
                self.status_label.setText("A workspace page could not be identified. Try again.")
                return
            label = " ".join(item.text().split())[:_MAX_LABEL_LENGTH]
            if not label:
                self.status_label.setText("Page names cannot be blank.")
                return
            label_key = label.casefold()
            if label_key in seen_labels:
                self.status_label.setText("Page names must be unique.")
                return
            seen_labels.add(label_key)
            routes.append(route_id)
            labels[route_id] = label
            if item.checkState() != Qt.CheckState.Checked:
                hidden.append(route_id)
        if len(hidden) == len(routes):
            self.status_label.setText("Keep at least one workspace page visible.")
            return
        self._preferences = {"version": 1, "order": routes, "labels": labels, "hidden": hidden}
        self.accept()

    @property
    def preferences(self) -> dict[str, object] | None:
        """Return the validated values after the user saves the dialog."""

        return None if self._preferences is None else dict(self._preferences)
