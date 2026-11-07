from datetime import UTC, datetime

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QListWidgetItem, QStyleOptionViewItem

from personal_hub.logging.contracts import ActivityEntry
from personal_hub.ui.desktop.logging_workspace import LoggingWorkspace
from personal_hub.ui.desktop.metrics_page import MeasurementValues
from personal_hub.ui.desktop.readable import (
    PRESENTATION_ROLE,
    ReadableDetails,
    ReadableList,
    ReadableText,
    SummaryCards,
    present_item,
)

pytestmark = pytest.mark.ui


def test_wrapping_rows_remeasure_and_preserve_native_selection(qtbot):
    view = ReadableList()
    qtbot.addWidget(view)
    item = QListWidgetItem("Accessible original text")
    item.setData(Qt.ItemDataRole.UserRole, "stable-identity")
    present_item(item, "A lengthy title about reading assignments " * 4, "Monday · Course name")
    view.addItem(item)
    view.resize(800, 450)
    view.show()
    qtbot.waitUntil(lambda: view.visualItemRect(item).height() > 0)
    wide = view.sizeHintForRow(0)
    view.resize(280, 450)
    view.setFont(QFont(view.font().family(), 18))
    qtbot.waitUntil(lambda: view.sizeHintForRow(0) > wide)
    assert view.horizontalScrollBar().maximum() == 0
    qtbot.keyClick(view, Qt.Key.Key_Home)
    assert view.currentItem() is item
    assert item.text() == "Accessible original text"
    assert item.data(Qt.ItemDataRole.UserRole) == "stable-identity"
    present_item(item, "x" * 240, "Long unbroken identifier")
    document, _, _ = view.itemDelegate()._document(QStyleOptionViewItem(), view.model().index(0, 0))
    assert document.idealWidth() <= view.viewport().width()


def test_external_markup_is_literal_in_rows_details_and_messages(qtbot):
    payload = '<img src="https://example.test/tracker"> & <b>private text</b>'
    view = ReadableList()
    qtbot.addWidget(view)
    item = QListWidgetItem(payload)
    present_item(item, payload, payload)
    view.addItem(item)
    document, _, _ = view.itemDelegate()._document(QStyleOptionViewItem(), view.model().index(0, 0))
    assert document.toPlainText() == f"{payload}\n{payload}"
    details = ReadableDetails()
    qtbot.addWidget(details)
    details.show_details(payload, [("List", payload)], payload)
    assert details.toPlainText().count(payload) == 3
    assert not details.openLinks() and not details.openExternalLinks()
    message = ReadableText()
    qtbot.addWidget(message)
    message.setPlainText(payload + "\n\nSecond paragraph")
    assert message.toPlainText() == payload + "\n\nSecond paragraph"


def test_logging_hierarchy_and_privacy_clear_all_presentations(qtbot):
    workspace = LoggingWorkspace()
    for page in workspace.pages.values():
        qtbot.addWidget(page)
    entry = ActivityEntry(
        description="Private assignment",
        list_name="Private course",
        notes="Private notes\n\nSecond paragraph",
        duration_minutes=25,
        occurred_at=datetime(2026, 11, 4, 19, 0, tzinfo=UTC),
    )
    workspace.render_activity([entry])
    assert workspace.activity_list.item(0).data(PRESENTATION_ROLE)[3] is True
    assert workspace.activity_list.item(1).data(PRESENTATION_ROLE)[0] == entry.description
    workspace.activity_list.setCurrentRow(1)
    assert "Private notes" in workspace.activity_detail.toPlainText()
    workspace.daily.render({"task_completion": 0, "manual": 1, "duration_minutes": 25}, [entry], [])
    assert len(workspace.daily.facts.cards) == 3
    workspace.set_private(True)
    assert workspace.activity_list.count() == 0
    assert not workspace.activity_detail.toPlainText()
    assert "25" not in workspace.daily.facts.toPlainText()
    assert workspace.daily.progress.count() == 0


def test_metric_tables_keep_exact_records_and_clear_private_values(qtbot):
    values = MeasurementValues()
    qtbot.addWidget(values)
    values.set_rows(
        [("2026-11-04", "23.5", "celsius")],
        [
            ("2026-11-04T19:00:00Z", "23.5", "celsius", "UTC", "record-123", 2),
        ],
    )
    assert values.tables[0].item(0, 1).text() == "23.5"
    assert values.tables[1].item(0, 4).text() == "record-123"
    assert values.tables[1].item(0, 5).text() == "2"
    values.tables[1].selectRow(0)
    values.copy_selection(values.tables[1])
    assert "record-123\t2" in QApplication.clipboard().text()
    values.clear()
    assert all(table.rowCount() == 0 for table in values.tables)
    assert not values.toPlainText()
    cards = SummaryCards()
    qtbot.addWidget(cards)
    cards.set_cards((("Tasks", 12), ("Time entered", "25 minutes")))
    cards.set_compact(True)
    assert cards.grid.itemAtPosition(1, 0).widget() is cards.cards[1]
    cards.clear()
    assert not cards.toPlainText() and not cards.accessibleDescription()
