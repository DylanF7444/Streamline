"""Presentation-only controls for explicitly started task timers."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QGridLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout


class FocusPanel(QFrame):
    action_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("taskFocusPanel")
        self.task_id = None
        self.private = False
        layout = QVBoxLayout(self)
        self.title = QLabel("Start a timed task to focus.")
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setWordWrap(True)
        layout.addWidget(self.title)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setAccessibleName("Task time progress")
        layout.addWidget(self.progress)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        controls = QGridLayout()
        self.buttons = {}
        for action, label in (
            ("pause", "Pause"),
            ("resume", "Resume"),
            ("complete", "Finish & complete task"),
            ("stop", "Stop session"),
            ("cancel", "Discard session"),
            ("dismiss", "Dismiss"),
        ):
            button = QPushButton(label)
            button.setAccessibleName(label)
            button.clicked.connect(
                lambda checked=False, value=action: self.action_requested.emit(value)
            )
            index = len(self.buttons)
            controls.addWidget(button, index // 3, index % 3)
            self.buttons[action] = button
        layout.addLayout(controls)
        hint = QLabel(
            "Timers pause on exit, lock, and sleep. Restarting requires Resume. Only one timer runs at a time."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.hide()

    def set_private(self, private):
        self.private = private
        if private:
            self.title.clear()
            self.status.clear()
            self.task_id = None
            self.hide()

    def render(self, state, *, completed=False):
        if self.private:
            return
        if state is None:
            self.hide()
            self.task_id = None
            return
        self.task_id = state.task_id
        self.title.setText(state.title)
        self.progress.setValue(min(1000, state.elapsed_seconds * 1000 // state.target_seconds))
        remaining = max(0, state.target_seconds - state.elapsed_seconds)
        clock = f"{remaining // 3600:02}:{remaining // 60 % 60:02}:{remaining % 60:02}"
        self.status.setText(
            "Time goal reached. Task completed."
            if completed
            else "Time target reached. Complete the task, stop the session, or keep working."
            if state.reached
            else f"{clock} remaining · {state.elapsed_seconds // 60} minutes elapsed"
            + (" · Paused" if not state.running else "")
        )
        for button in self.buttons.values():
            button.setVisible(not completed)
        self.buttons["dismiss"].setVisible(completed)
        self.buttons["pause"].setVisible(state.running and not completed)
        self.buttons["resume"].setVisible(not state.running and not completed)
        self.buttons["resume"].setText("Keep working" if state.reached else "Resume")
        self.show()
