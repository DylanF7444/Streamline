"""Locally generated icons for app and website usage rows."""

from __future__ import annotations

import hashlib
import ntpath
from collections import OrderedDict

from PySide6.QtCore import QFileInfo, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QFileIconProvider

_COLORS = ("#427a72", "#526b9a", "#9a6849", "#80649a", "#4c7c92", "#92734a")


def _monogram(value: str, *, circle: bool) -> QIcon:
    digest = hashlib.blake2s(value.casefold().encode("utf-8"), digest_size=2).digest()
    color = QColor(_COLORS[int.from_bytes(digest, "big") % len(_COLORS)])
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    shape = QRectF(1, 1, 30, 30)
    if circle:
        painter.drawEllipse(shape)
    else:
        painter.drawRoundedRect(shape, 7, 7)
    painter.setPen(QColor("#ffffff"))
    font = QFont(painter.font())
    font.setBold(True)
    font.setPixelSize(13)
    painter.setFont(font)
    painter.drawText(shape, Qt.AlignmentFlag.AlignCenter, value[:2].upper())
    painter.end()
    return QIcon(pixmap)


class UsageIconCache:
    """Keep a bounded, in-memory icon cache; never fetch icons over the network."""

    def __init__(self) -> None:
        self._file_provider = QFileIconProvider()
        self._apps: OrderedDict[str, tuple[QIcon, bool]] = OrderedDict()
        self._sites: OrderedDict[str, QIcon] = OrderedDict()

    @staticmethod
    def _remember(cache: OrderedDict, key: str, value, limit: int) -> None:
        cache.pop(key, None)
        cache[key] = value
        while len(cache) > limit:
            cache.popitem(last=False)

    def application_icon(self, app: str, executable_path: str | None = None) -> QIcon:
        key = (app or "application").casefold()
        cached = self._apps.get(key)
        if cached is not None and (cached[1] or not executable_path):
            self._apps.move_to_end(key)
            return cached[0]

        if executable_path:
            file_info = QFileInfo(executable_path)
            if file_info.isFile():
                icon = self._file_provider.icon(file_info)
                if not icon.isNull():
                    self._remember(self._apps, key, (icon, True), 256)
                    return icon

        if cached is not None:
            self._apps.move_to_end(key)
            return cached[0]
        stem = ntpath.splitext(ntpath.basename(key))[0]
        letters = "".join(character for character in stem if character.isalnum()) or "app"
        icon = _monogram(letters, circle=False)
        self._remember(self._apps, key, (icon, False), 256)
        return icon

    def website_icon(self, hostname: str) -> QIcon:
        key = hostname.casefold().rstrip(".")
        cached = self._sites.get(key)
        if cached is not None:
            self._sites.move_to_end(key)
            return cached
        labels = key.split(".")
        if labels and labels[0] == "www" and len(labels) > 1:
            labels = labels[1:]
        label = labels[0] if labels else "site"
        letters = "".join(character for character in label if character.isalnum()) or "web"
        icon = _monogram(letters, circle=True)
        self._remember(self._sites, key, icon, 512)
        return icon

    def clear(self) -> None:
        self._apps.clear()
        self._sites.clear()
