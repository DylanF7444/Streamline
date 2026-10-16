"""Local workspace navigation entries and their user customization dialog."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

_ROUTE_ROLE: Final = int(Qt.ItemDataRole.UserRole)
_MAX_NAVIGATION_ITEMS: Final = 512
_MAX_LABEL_LENGTH: Final = 64


@dataclass(frozen=True, slots=True)
class WorkspaceNavigationEntry:
    """One visible workspace route, optionally scoped to a local collection."""

    route_id: str
    label: str
    destination: str
    collection_kind: str = ""
    collection_id: str = ""


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
