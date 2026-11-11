"""Drive local task timers with a monotonic clock, independent of network refresh."""

from __future__ import annotations

from time import monotonic

from PySide6.QtCore import QObject, QTimer
from sqlalchemy.exc import SQLAlchemyError

from personal_hub.tasks.focus import FocusRepository


class FocusCoordinator(QObject):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.window = owner.window
        self.repository = FocusRepository(owner.container.controller.task_repository.database)
        self.repository.recover()
        self.completed_state = None
        self.clock = monotonic
        self.last_tick = self.clock()
        self.window.focus_pause_requested.connect(self.pause_all)
        self.window.focus_start_requested.connect(self.start)
        self.window.focus_panel.action_requested.connect(self.action)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.refresh()

    def refresh(self):
        if self.completed_state is not None:
            task = self.owner.container.controller.get_task(self.completed_state.task_id)
            if task is None or task.state != "completed":
                self.completed_state = None
        state = self.repository.current()
        self.window.focus_panel.render(
            state or self.completed_state,
            completed=state is None and self.completed_state is not None,
        )

    def start(self, task_id):
        if self.window.locked or self.window.privacy_masked:
            return
        self.tick()
        try:
            state = self.repository.start(task_id)
        except (ValueError, SQLAlchemyError):
            self.window.task_status_label.setText(
                "Unable to start this timer. Refresh the task and try again."
            )
            return
        self.completed_state = None
        self.timer.start()
        self.last_tick = self.clock()
        self.window.focus_panel.render(state)

    def action(self, action):
        if self.window.locked or self.window.privacy_masked:
            return
        task_id = self.window.focus_panel.task_id
        if task_id is None:
            return
        self.tick()
        try:
            if action == "dismiss":
                self.completed_state = None
            elif action == "pause":
                self.repository.pause(task_id)
            elif action == "resume":
                self.repository.start(task_id)
                self.timer.start()
                self.last_tick = self.clock()
            elif action == "complete":
                self.window.complete_task(task_id)
            elif action in {"stop", "cancel"}:
                self.repository.finish(task_id, discard=action == "cancel")
                self.owner.render_task_surfaces()
            self.refresh()
        except (ValueError, SQLAlchemyError):
            self.window.task_status_label.setText("Timer changes could not be saved. Try again.")

    def tick(self):
        now = self.clock()
        elapsed = now - self.last_tick
        if elapsed < 1:
            return
        seconds = int(elapsed)
        self.last_tick += seconds
        try:
            if seconds > 10 or self.window.locked:
                self.repository.recover()
                self.last_tick = now
                self.refresh()
                return
            result = self.repository.advance(seconds)
            if result is not None and result.reached and not result.running:
                if result.time_mode == "goal":
                    # Reuse the visible completion path to retain its normal Undo action.
                    self.completed_state = result
                    self.window.complete_task(result.task_id)
                    self.owner.render_task_surfaces()
                    self.owner.render_calendar()
                    self.window.focus_panel.render(result, completed=True)
                    self.window.statusBar().showMessage(
                        "Task time goal reached and completed.", 10000
                    )
                else:
                    self.window.focus_panel.render(result)
            else:
                self.refresh()
        except (ValueError, SQLAlchemyError):
            self.window.focus_panel.hide()
            self.window.task_status_label.setText(
                "Timer progress could not be saved. Resume after storage is available."
            )
            self.timer.stop()

    def pause_all(self):
        self.repository.recover()
        self.last_tick = self.clock()

    def close(self):
        self.tick()
        self.timer.stop()
        self.repository.recover()
