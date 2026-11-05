"""Small native line icons that do not depend on a symbol font or remote assets."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QIconEngine, QPainter, QPainterPath, QPen, QPixmap


class _LineIcon(QIconEngine):
    def __init__(self, name: str, color: str, selected_color: str) -> None:
        super().__init__()
        self.name = name
        self.color = color
        self.selected_color = selected_color

    def clone(self) -> _LineIcon:
        return _LineIcon(self.name, self.color, self.selected_color)

    def paint(self, painter: QPainter, rect: QRect, mode: QIcon.Mode, state: QIcon.State) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        side = min(rect.width(), rect.height())
        painter.translate(rect.center().x() - side / 2, rect.center().y() - side / 2)
        painter.scale(side / 24, side / 24)
        color = QColor(self.selected_color if state == QIcon.State.On else self.color)
        if mode == QIcon.Mode.Disabled:
            color.setAlpha(130)
        painter.setPen(
            QPen(
                color,
                1.7,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
        )
        painter.setBrush(Qt.BrushStyle.NoBrush)

        def line(*points: tuple[float, float]) -> None:
            path = QPainterPath(QPointF(*points[0]))
            for point in points[1:]:
                path.lineTo(QPointF(*point))
            painter.drawPath(path)

        if self.name == "Home":
            line(
                (3, 10),
                (12, 3),
                (21, 10),
                (21, 21),
                (15, 21),
                (15, 14),
                (9, 14),
                (9, 21),
                (3, 21),
                (3, 10),
            )
        elif self.name == "Calendar":
            painter.drawRoundedRect(QRectF(3, 5, 18, 16), 2, 2)
            line((3, 10), (21, 10))
            line((8, 3), (8, 7))
            line((16, 3), (16, 7))
            for x in (8, 13, 18):
                painter.drawPoint(QPointF(x, 15))
        elif self.name in {"Tasks", "check"}:
            line((3, 7), (5, 9), (8, 5))
            line((11, 7), (21, 7))
            line((3, 16), (5, 18), (8, 14))
            line((11, 16), (21, 16))
        elif self.name == "Watchlists":
            painter.drawEllipse(QRectF(3, 3, 18, 18))
            painter.drawEllipse(QRectF(7, 7, 10, 10))
            line((12, 12), (20, 4))
        elif self.name == "Inbox":
            line(
                (3, 15),
                (6, 5),
                (18, 5),
                (21, 15),
                (21, 21),
                (3, 21),
                (3, 15),
                (8, 15),
                (10, 18),
                (14, 18),
                (16, 15),
                (21, 15),
            )
        elif self.name == "Sources":
            painter.drawRoundedRect(QRectF(7, 7, 10, 10), 2, 2)
            line((10, 3), (10, 7))
            line((14, 3), (14, 7))
            line((12, 17), (12, 22))
        elif self.name == "Settings":
            for y, x in ((6, 9), (12, 16), (18, 7)):
                line((3, y), (x - 2, y))
                line((x + 2, y), (21, y))
                painter.drawEllipse(QRectF(x - 2, y - 2, 4, 4))
        elif self.name == "Activity":
            line((3, 13), (7, 13), (10, 5), (14, 20), (17, 10), (21, 10))
        elif self.name in {"Reflection", "Summary"}:
            painter.drawRoundedRect(QRectF(5, 3, 15, 18), 2, 2)
            line((3, 7), (7, 7))
            line((3, 12), (7, 12))
            line((3, 17), (7, 17))
            line((11, 8), (16, 8))
            line((11, 13), (16, 13))
        elif self.name == "Places":
            path = QPainterPath(QPointF(12, 22))
            path.cubicTo(0, 10, 5, 3, 12, 3)
            path.cubicTo(19, 3, 24, 10, 12, 22)
            painter.drawPath(path)
            painter.drawEllipse(QRectF(9, 7, 6, 6))
        elif self.name == "add":
            line((12, 4), (12, 20))
            line((4, 12), (20, 12))
        elif self.name == "refresh":
            painter.drawArc(QRectF(4, 4, 16, 16), 40 * 16, 285 * 16)
            line((21, 4), (20, 10), (14, 8))
        elif self.name == "undo":
            line((8, 4), (3, 9), (8, 14))
            path = QPainterPath(QPointF(3, 9))
            path.lineTo(14, 9)
            path.cubicTo(23, 9, 23, 21, 14, 21)
            painter.drawPath(path)
        else:
            painter.drawRoundedRect(QRectF(4, 4, 16, 16), 3, 3)
            line((8, 12), (11, 15), (17, 9))
        painter.restore()

    def pixmap(self, size, mode, state) -> QPixmap:
        pixmap = QPixmap(size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        self.paint(painter, QRect(0, 0, size.width(), size.height()), mode, state)
        painter.end()
        return pixmap


def hub_icon(name: str, color: str = "#aeb7c7", selected_color: str | None = None) -> QIcon:
    if name.startswith("collection.calendar-view."):
        name = "Calendar"
    elif name.startswith("collection.task-list."):
        name = "Tasks"
    return QIcon(_LineIcon(name, color, selected_color or color))
