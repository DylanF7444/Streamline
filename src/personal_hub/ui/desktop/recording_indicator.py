"""Privacy-safe recording status visible while the main window is hidden."""

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap


class RecordingIndicator:
    def __init__(self, page, tray):
        self.page = page
        self.tray = tray
        self.base = tray.tray_icon.icon() if tray.tray_icon else QIcon()
        self.key = None
        self.app_status = QAction("Apps: paused", tray.menu)
        self.site_status = QAction("Sites: paused", tray.menu)
        self.open_action = QAction("Review screen time", tray.menu)
        self.browser_action = QAction("Start browser recording", tray.menu)
        self.app_status.setEnabled(False)
        self.site_status.setEnabled(False)
        self.open_action.triggered.connect(self.open)
        self.browser_action.triggered.connect(page.browser.toggle_recording)
        for action in (self.app_status, self.site_status, self.open_action, self.browser_action):
            tray.menu.insertAction(tray.quit_action, action)

    def open(self):
        self.tray.open_requested.emit()
        self.page.window.show_destination("Screen time")

    def update(self):
        page = self.page
        locked = page.locked
        lines, states = [], []
        for label, recorder in (("Apps", page.recorder), ("Sites", page.browser.runtime.recorder)):
            state = "paused" if locked else recorder.state
            detail = "Locked" if locked else recorder.status
            if not locked and not recorder.running and state != "error":
                state, detail = "paused", "Paused"
            saved = recorder.last_saved_at
            if saved and not page.private and not locked:
                detail += f" · Last saved {saved.astimezone().strftime('%H:%M:%S')}"
            lines.append(f"{label}: {detail}")
            states.append(state)
        self.app_status.setText(lines[0])
        self.site_status.setText(lines[1])
        self.browser_action.setText(
            "Pause browser recording" if page.browser.runtime.running else "Start browser recording"
        )
        self.browser_action.setEnabled(not locked and not page.private)
        page.tray_action.setEnabled(not locked)
        if self.tray.tray_icon is None:
            return
        # Windows truncates long tray tooltips; full reasons stay in the menu.
        captions = ["Locked" if locked else state.capitalize() for state in states]
        self.tray.tray_icon.setToolTip(
            f"Personal Hub\nApps (left): {captions[0]}\nSites (right): {captions[1]}\n"
            "Right-click for details"
        )
        key = tuple(states)
        if key == self.key:
            return
        self.key = key
        icon = QIcon()
        palette = {
            "recording": ("#53bda4", "R"),
            "waiting": ("#f4bd50", "W"),
            "paused": ("#b0b5bc", "P"),
            "error": ("#ff7373", "!"),
        }
        for size in (16, 24, 32, 48):
            pixmap = QPixmap(size, size)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.drawPixmap(0, 0, self.base.pixmap(size, size))
            font = QFont("Segoe UI")
            font.setBold(True)
            font.setPixelSize(max(7, size // 3))
            painter.setFont(font)
            for index, state in enumerate(states):
                color, mark = palette.get(state, palette["waiting"])
                rect = QRect(index * size // 2, size // 2, size // 2, size // 2)
                painter.fillRect(rect, QColor(color))
                painter.setPen(QColor("#111111"))
                painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, mark)
            painter.end()
            icon.addPixmap(pixmap)
        self.tray.tray_icon.setIcon(icon)
