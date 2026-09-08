from __future__ import annotations

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QLabel

from personal_hub.ui.desktop.dashboard import DashboardBoard


def test_header_drag_moves_across_columns_and_click_does_not_move(qtbot):
    board = DashboardBoard()
    qtbot.addWidget(board)
    board.set_sections(
        [
            ("schedule", "Calendar", QLabel("one"), True),
            ("attention", "Inbox", QLabel("two"), True),
            ("watchlist", "Watchlist", QLabel("three"), True),
        ]
    )
    board.resize(900, 700)
    board.set_reduced_motion(True)
    board.set_editing(True)
    board.show()
    qtbot.wait(30)
    before = board.export_layout()["order"]
    qtbot.mouseClick(board.widget("attention").header, Qt.MouseButton.LeftButton)
    assert board.export_layout()["order"] == before
    header = board.widget("schedule").header
    target_card = board.widget("attention")
    target = target_card.mapToGlobal(QPoint(target_card.width() - 25, 45))
    start = header.rect().center()
    qtbot.mousePress(header, Qt.MouseButton.LeftButton, pos=start)
    move = QMouseEvent(
        QEvent.Type.MouseMove,
        QPointF(header.mapFromGlobal(target)),
        QPointF(target),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    header.mouseMoveEvent(move)
    assert not board._drag_indicator.isHidden()
    release = QMouseEvent(
        QEvent.Type.MouseButtonRelease,
        QPointF(header.mapFromGlobal(target)),
        QPointF(target),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    header.mouseReleaseEvent(release)
    assert board.export_layout()["order"] == ["attention", "schedule", "watchlist"]
    assert board._drag_indicator.isHidden()


def _sections():
    return [
        ("schedule", "Schedule", QLabel("Calendar content"), True),
        ("attention", "Attention", QLabel("Inbox content"), False),
        ("watchlist", "Watchlist", QLabel("Watch content"), True),
    ]


@pytest.mark.hub_ui
def test_board_keeps_content_and_emits_allowlisted_layout(qtbot):
    board = DashboardBoard()
    qtbot.addWidget(board)
    board.set_sections(_sections())
    board.show()
    qtbot.wait(20)

    changed: list[object] = []
    board.layout_changed.connect(changed.append)
    assert board.widget("schedule") is not None
    assert board.widget("schedule").content.text() == "Calendar content"

    assert board.remove_widget("schedule")
    assert board.add_menu.actions()[0].data() == "schedule"
    assert board.add_widget("schedule")
    assert board.move_widget_later("schedule")
    assert board.set_widget_size("schedule", "wide")

    layout = board.export_layout()
    assert layout["version"] == 1
    assert layout["order"] == ["watchlist", "attention", "schedule"]
    assert layout["sizes"]["schedule"] == "wide"
    assert layout["hidden"] == ["attention"]
    assert changed
    assert all(
        set(value) == {"version", "order", "hidden", "sizes"}
        for value in changed
        if isinstance(value, dict)
    )
    board.reset_layout()
    assert board.export_layout()["order"] == ["schedule", "attention", "watchlist"]
    assert board.export_layout()["hidden"] == ["attention"]
    assert set(board.export_layout()["sizes"].values()) == {"comfortable"}


@pytest.mark.hub_ui
def test_restore_filters_unknown_ids_and_preserves_state_on_refresh(qtbot):
    board = DashboardBoard()
    qtbot.addWidget(board)
    board.set_sections(_sections())
    board.set_reduced_motion(True)

    assert board.restore_layout(
        {
            "version": 1,
            "order": ["watchlist", "unknown", "schedule"],
            "hidden": ["attention", "unknown"],
            "sizes": {"watchlist": "tall", "schedule": "compact", "unknown": "wide"},
        }
    )
    before = board.export_layout()
    board.set_sections(
        [
            ("schedule", "Updated schedule", QLabel("new calendar"), True),
            ("attention", "Updated attention", QLabel("new inbox"), True),
            ("watchlist", "Updated watchlist", QLabel("new watch"), True),
            ("news", "News", QLabel("new news"), False),
        ]
    )
    after = board.export_layout()
    assert after["order"][:3] == before["order"]
    assert after["hidden"][:1] == before["hidden"]
    assert after["sizes"]["watchlist"] == "tall"
    assert after["sizes"]["schedule"] == "compact"
    assert "news" in after["hidden"]

    unchanged = board.export_layout()
    assert not board.restore_layout({"version": 2, "order": [], "hidden": [], "sizes": {}})
    assert board.export_layout() == unchanged


@pytest.mark.hub_ui
def test_refresh_reuses_card_shells_and_allows_content_replacement(qtbot):
    board = DashboardBoard()
    qtbot.addWidget(board)
    original = _sections()
    board.set_sections(original)
    board.set_editing(True)
    board.set_widget_size("schedule", "wide")
    original_card = board.widget("schedule")

    board.set_sections(
        [
            ("schedule", "New schedule", QLabel("fresh calendar"), True),
            ("attention", "Attention", QLabel("fresh inbox"), False),
            ("watchlist", "Watchlist", QLabel("fresh watch"), True),
        ]
    )

    assert board.widget("schedule") is original_card
    assert board.widget("schedule").content.text() == "fresh calendar"
    assert board.widget("schedule").size_preset == "wide"
    assert board.editing
    assert not board.widget("schedule").edit_controls.isHidden()


@pytest.mark.hub_ui
def test_responsive_grid_and_open_destinations(qtbot):
    board = DashboardBoard()
    qtbot.addWidget(board)
    board.set_sections(
        [
            ("schedule", "Schedule", QLabel("one"), True),
            ("attention", "Attention", QLabel("two"), True),
            ("watchlist", "Watchlist", QLabel("three"), True),
        ]
    )
    board.set_reduced_motion(True)
    board.resize(800, 600)
    board.show()
    qtbot.wait(20)
    assert board.column_count == 2
    first = board.widget("schedule").geometry()
    second = board.widget("attention").geometry()
    assert first.intersected(second).isEmpty()

    board.resize(600, 600)
    qtbot.wait(20)
    assert board.column_count == 1
    assert board.widget("schedule").geometry().width() == board.grid.width()
    assert board.widget("watchlist").geometry().width() == board.grid.width()

    opened: list[str] = []
    board.open_requested.connect(opened.append)
    board.widget("schedule").open_button.click()
    board.widget("attention").open_button.click()
    board.widget("watchlist").open_button.click()
    assert opened == ["Calendar", "Inbox", "Watchlists"]


@pytest.mark.hub_ui
def test_edit_toggle_and_reduced_motion_settle_resize_grip(qtbot):
    board = DashboardBoard()
    qtbot.addWidget(board)
    board.set_sections(_sections())
    board.resize(800, 600)
    board.show()
    qtbot.wait(20)

    board.set_editing(True)
    card = board.widget("schedule")
    assert card.resize_grip.isVisible()
    assert card.resize_grip.geometry().bottomRight() == card.rect().bottomRight() - QPoint(4, 4)

    board.set_reduced_motion(True)
    start = card.geometry()
    board._begin_resize("schedule", QPoint(10, 10))
    board._preview_resize("schedule", QPoint(10, 10 + 5 * 24))
    board._finish_resize("schedule", QPoint(10, 10 + 5 * 24))
    assert board.export_layout()["sizes"]["schedule"] in {"comfortable", "tall"}
    assert card.geometry() != start or board.export_layout()["sizes"]["schedule"] != "comfortable"

    board.edit_button.click()
    assert not board.editing
    assert not card.resize_grip.isVisible()
