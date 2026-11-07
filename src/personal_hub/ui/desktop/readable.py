"""Shared, wrapping presentation for read-only lists, details and small totals.

Display text stays on each item for accessibility and native keyboard behavior.
Only escaped text is passed to QTextDocument; it never loads external content.
"""

from __future__ import annotations

from html import escape

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import (
    QAbstractTextDocumentLayout,
    QPalette,
    QTextBlockFormat,
    QTextCursor,
    QTextDocument,
    QTextOption,
)
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QLabel,
    QListWidget,
    QPlainTextEdit,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

PRESENTATION_ROLE = int(Qt.ItemDataRole.UserRole) + 70


def present_item(item, title, metadata="", *, body="", section=False):
    """Add visual hierarchy without changing the item's identity or plain text."""
    item.setData(PRESENTATION_ROLE, (str(title), str(metadata), str(body), bool(section)))
    item.setData(
        Qt.ItemDataRole.AccessibleTextRole,
        "\n".join(part for part in (str(title), str(metadata), str(body)) if part),
    )
    item.setToolTip("<qt>" + escape(item.text()).replace("\n", "<br>") + "</qt>")


class ReadableItemDelegate(QStyledItemDelegate):
    def _document(self, option, index):
        data = index.data(PRESENTATION_ROLE)
        if data:
            title, metadata, body, section = data
        else:
            title, _, metadata = str(index.data(Qt.ItemDataRole.DisplayRole) or "").partition("\n")
            body, section = "", False
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        foreground = option.palette.color(
            QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text
        ).name()
        document = QTextDocument()
        document.setDefaultFont(option.font)
        document.setDocumentMargin(0)
        text_option = document.defaultTextOption()
        text_option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        document.setDefaultTextOption(text_option)
        document.setDefaultStyleSheet(f"body {{ color: {foreground}; }} p {{ margin: 0; }}")
        content = f"<p><b>{escape(title)}</b></p>"
        if metadata:
            content += (
                f'<p style="margin-top: 4px;">{escape(metadata).replace(chr(10), "<br>")}</p>'
            )
        if body:
            content += f'<p style="margin-top: 7px;">{escape(body).replace(chr(10), "<br>")}</p>'
        document.setHtml(f"<body>{content}</body>")
        # Measure against the viewport, including at narrow widths and large text sizes.
        view = self.parent()
        width = view.viewport().width() if isinstance(view, QListWidget) else option.rect.width()
        check_width = 28 if index.data(Qt.ItemDataRole.CheckStateRole) is not None else 0
        document.setTextWidth(max(40, width - 32 - check_width))
        return document, section, check_width

    def sizeHint(self, option, index):
        document, section, _ = self._document(option, index)
        return QSize(0, int(document.size().height()) + (24 if section else 28))

    def paint(self, painter, option, index):
        styled = QStyleOptionViewItem(option)
        self.initStyleOption(styled, index)
        document, section, check_width = self._document(styled, index)
        styled.text = ""
        style = styled.widget.style()
        painter.save()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, styled, painter, styled.widget)
        if section:
            tint = option.palette.color(QPalette.ColorRole.Highlight)
            tint.setAlpha(35)
            painter.fillRect(styled.rect, tint)
        rect = styled.rect.adjusted(14 + check_width, 12, -14, -12)
        painter.setClipRect(rect)
        painter.translate(rect.topLeft())
        context = QAbstractTextDocumentLayout.PaintContext()
        document.documentLayout().draw(painter, context)
        painter.restore()
        if not section:
            painter.save()
            separator = option.palette.color(QPalette.ColorRole.Text)
            separator.setAlpha(45)
            painter.setPen(separator)
            painter.drawLine(option.rect.bottomLeft(), option.rect.bottomRight())
            painter.restore()


class ReadableList(QListWidget):
    """Native list with a bold title, wrapping context, and comfortable row spacing."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setItemDelegate(ReadableItemDelegate(self))
        self.setWordWrap(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.setUniformItemSizes(False)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.scheduleDelayedItemsLayout()


class ReadableDetails(QTextBrowser):
    """Selectable read-only details with labeled fields and preserved paragraphs."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.setTabChangesFocus(True)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)

    def show_details(self, title, fields=(), notes=""):
        content = f'<h3 style="margin: 0 0 12px;">{escape(str(title))}</h3>'
        for label, value in fields:
            if value:
                content += f"<p><b>{escape(str(label))}:</b> {escape(str(value))}</p>"
        if notes:
            content += "<p><b>Notes</b></p>" + "".join(
                f"<p>{escape(paragraph).replace(chr(10), '<br>')}</p>"
                for paragraph in notes.split("\n\n")
            )
        self.setHtml(content)


class ReadableText(QPlainTextEdit):
    """Plain, inert message text with breathing room between lines and paragraphs."""

    def setPlainText(self, text):
        super().setPlainText(text)
        cursor = QTextCursor(self.document())
        cursor.select(QTextCursor.SelectionType.Document)
        block = QTextBlockFormat()
        block.setLineHeight(135, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
        block.setBottomMargin(6)
        cursor.mergeBlockFormat(block)


class SummaryCards(QWidget):
    """Responsive factual totals; plain text remains available to assistive clients."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(10)
        self.cards = []
        self._text = ""
        self._compact = False

    def set_cards(self, values):
        values = tuple(values)
        text = "\n".join(f"{label}: {value}" for label, value in values)
        if text == self._text:
            return
        self.clear()
        self._text = text
        self.setAccessibleDescription(self._text)
        for title, value in values:
            card = QFrame()
            card.setObjectName("readingSummaryCard")
            layout = QVBoxLayout(card)
            layout.setContentsMargins(14, 12, 14, 12)
            caption = QLabel(str(title))
            caption.setObjectName("readingSummaryCaption")
            caption.setTextFormat(Qt.TextFormat.PlainText)
            caption.setWordWrap(True)
            number = QLabel(str(value))
            number.setObjectName("readingSummaryValue")
            number.setTextFormat(Qt.TextFormat.PlainText)
            number.setWordWrap(True)
            layout.addWidget(caption)
            layout.addWidget(number)
            self.cards.append(card)
        self.set_compact(self._compact)

    def set_compact(self, compact):
        self._compact = compact
        self._reflow()

    def _reflow(self):
        columns = 1 if self._compact or self.width() < 600 else 3
        for index, card in enumerate(self.cards):
            self.grid.removeWidget(card)
            self.grid.addWidget(card, index // columns, index % columns)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reflow()

    def clear(self):
        for card in self.cards:
            self.grid.removeWidget(card)
            for label in card.findChildren(QLabel):
                label.clear()
            card.hide()
            card.deleteLater()
        self.cards = []
        self._text = ""
        self.setAccessibleDescription("")

    def setPlainText(self, text):
        self.set_cards((("Overview", text),))

    def toPlainText(self):
        return self._text
