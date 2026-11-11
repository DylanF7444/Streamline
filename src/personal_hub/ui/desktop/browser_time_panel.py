"""Browser consent, setup and hostname-only local review."""

import shutil
import sys
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)
from sqlalchemy.exc import SQLAlchemyError

from personal_hub.browser_time.bridge import BrowserTimeRuntime
from personal_hub.browser_time.contracts import BrowserTimeSettings
from personal_hub.browser_time.setup import register, unregister
from personal_hub.screen_time.contracts import AppRule
from personal_hub.screen_time.repository import bounds


def duration(seconds):
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m {seconds % 60}s"


class BrowserTimePanel(QGroupBox):
    def __init__(self, page):
        super().__init__("Browser sites", page)
        self.page = page
        self.private = False
        self.locked = False
        self.box = QVBoxLayout(self)
        self.status = QLabel("Browser recording paused")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.box.addWidget(self.status)
        help_text = QLabel(
            "Optional Chrome / Edge extension records the focused site's hostname while Hub "
            "runs. Private tabs, local addresses, page titles, full addresses and content are "
            "excluded. Site time is part of browser app time; do not add them together. "
            "The date range above applies to sites too."
        )
        help_text.setWordWrap(True)
        self.box.addWidget(help_text)
        self.setup_button = QPushButton("Set up browser extension")
        self.setup_button.clicked.connect(self.setup_extension)
        self.box.addWidget(self.setup_button)
        self.remove_button = QPushButton("Disconnect browser helper")
        self.remove_button.clicked.connect(self.disconnect)
        self.box.addWidget(self.remove_button)
        self.toggle = QPushButton("Start browser recording")
        self.toggle.clicked.connect(self.toggle_recording)
        self.box.addWidget(self.toggle)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.idle = QSpinBox()
        self.idle.setRange(30, 1800)
        self.idle.setSuffix(" seconds")
        self.exclusions = QLineEdit()
        self.exclusions.setMaxLength(65024)
        self.exclusions.setPlaceholderText("bank.example.com, private.example.org")
        self.filter = QLineEdit()
        self.filter.setMaxLength(253)
        self.filter.textChanged.connect(self.refresh)
        form.addRow("Pause sites after no input", self.idle)
        form.addRow("Exclude hosts and subdomains", self.exclusions)
        form.addRow("Filter site, alias or category", self.filter)
        self.box.addLayout(form)
        self.save = QPushButton("Save browser settings")
        self.save.clicked.connect(self.save_settings)
        self.box.addWidget(self.save)
        self.total = QLabel()
        self.total.setTextFormat(Qt.TextFormat.PlainText)
        self.total.setWordWrap(True)
        self.box.addWidget(self.total)
        self.sites = self.table("Site totals", ["Site", "Category", "Foreground time"])
        self.categories = self.table("Site category totals", ["Category", "Foreground time"])
        self.sessions = self.table("Site intervals", ["Started", "Ended", "Browser", "Host"])
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.box.addWidget(self.detail)
        self.limit = 200
        self.more = QPushButton("Load more site intervals")
        self.more.clicked.connect(self.load_more)
        self.box.addWidget(self.more)
        self.week = self.table("Site seven-day trend", ["Date", "Foreground time"])
        rules = QFormLayout()
        rules.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.domain, self.alias, self.category = QLineEdit(), QLineEdit(), QLineEdit()
        self.domain.setMaxLength(253)
        self.alias.setMaxLength(160)
        self.category.setMaxLength(64)
        rules.addRow("Rule hostname", self.domain)
        rules.addRow("Display name", self.alias)
        rules.addRow("Category", self.category)
        self.box.addLayout(rules)
        self.rule_save = QPushButton("Save site rule")
        self.rule_save.clicked.connect(self.save_rule)
        self.box.addWidget(self.rule_save)
        self.delete = QPushButton("Preview site history deletion")
        self.delete.clicked.connect(self.delete_history)
        self.box.addWidget(self.delete)

    def table(self, title, headers):
        table = QTableWidget(0, len(headers))
        table.setAccessibleName(title)
        table.setHorizontalHeaderLabels(headers)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setMinimumHeight(130)
        self.box.addWidget(QLabel(title))
        self.box.addWidget(table)
        return table

    def bind(self, controller):
        self.controller = controller
        self.load_settings()
        self.runtime = BrowserTimeRuntime(
            controller.settings.data_dir, controller.browser_time_repository, self.settings
        )
        controller.browser_time_runtime = self.runtime

    def load_settings(self):
        try:
            self.settings = BrowserTimeSettings.model_validate(
                self.controller.settings_repository.get("browser_time.settings", {})
            )
        except ValueError:
            self.settings = BrowserTimeSettings()
        self.idle.setValue(self.settings.idle_seconds)
        self.exclusions.setText(", ".join(self.settings.excluded_domains))

    def toggle_recording(self):
        if self.private or self.locked:
            return
        if self.runtime.running:
            self.runtime.pause()
        else:
            self.runtime.start()
        self.refresh()

    def tick(self, observation):
        self.runtime.tick(observation, locked=self.locked)
        self.refresh()

    def save_settings(self):
        if self.private:
            return
        try:
            settings = BrowserTimeSettings(
                idle_seconds=self.idle.value(),
                rules=self.settings.rules,
                excluded_domains=tuple(
                    v.strip() for v in self.exclusions.text().split(",") if v.strip()
                ),
            )
            self.persist(settings)
        except (ValueError, SQLAlchemyError):
            self.status.setText("Could not save browser settings. Use hostnames without paths.")

    def persist(self, settings):
        self.controller.settings_repository.set(
            "browser_time.settings", settings.model_dump(mode="json")
        )
        running = self.runtime.running
        self.runtime.pause()
        self.settings = self.runtime.settings = settings
        if running:
            self.runtime.start()
        self.refresh()

    def save_rule(self):
        if self.private:
            return
        try:
            rules = dict(self.settings.rules)
            rules[self.domain.text().strip()] = AppRule(
                name=self.alias.text().strip(),
                category=self.category.text().strip() or "Uncategorized",
            )
            self.persist(
                BrowserTimeSettings.model_validate({**self.settings.model_dump(), "rules": rules})
            )
        except (ValueError, SQLAlchemyError):
            self.status.setText("Could not save rule. Check the hostname and category.")

    def load_more(self):
        self.limit = min(10000, self.limit + 200)
        self.refresh()

    def refresh(self, *_):
        if not hasattr(self, "runtime") or self.controller.is_closed:
            return
        self.toggle.setText(
            "Pause browser recording" if self.runtime.running else "Start browser recording"
        )
        self.status.setText(
            "Browser details hidden"
            if self.private
            else (
                self.runtime.recorder.status if self.runtime.running else "Browser recording paused"
            )
        )
        if self.private:
            return
        try:
            stored = BrowserTimeSettings.model_validate(
                self.controller.settings_repository.get("browser_time.settings", {})
            )
            if stored != self.settings:
                self.load_settings()
                self.runtime.settings = self.settings
                self.runtime.recorder.reset()
            start, end = (
                self.page.start_date.date().toPython(),
                self.page.end_date.date().toPython(),
            )
            if end < start:
                self.clear()
                self.total.setText("Choose an end date on or after the start date.")
                return
            zone_name = self.controller.settings.default_timezone
            zone = ZoneInfo(zone_name)
            repo = self.controller.browser_time_repository
            query = self.filter.text().casefold()

            def label(domain):
                rule = self.settings.rules.get(domain, AppRule())
                return rule.name or domain, rule.category

            def matches(domain):
                return query in f"{domain} {' '.join(label(domain))}".casefold()

            summary = {d: v for d, v in repo.summary(start, end, zone_name).items() if matches(d)}
            self.total.setText(
                f"Sites · {duration(sum(summary.values()))} · included in browser app time"
            )
            self.fill(
                self.sites,
                [(*label(d), duration(v)) for d, v in sorted(summary.items(), key=lambda p: -p[1])],
            )
            totals = {}
            for d, v in summary.items():
                category = label(d)[1]
                totals[category] = totals.get(category, 0) + v
            self.fill(self.categories, [(c, duration(v)) for c, v in sorted(totals.items())])
            lower, upper = bounds(start, end, zone_name)
            records = repo.records(start, end, zone_name, limit=self.limit)
            self.fill(
                self.sessions,
                [
                    (
                        max(r.start, lower).astimezone(zone).isoformat(timespec="seconds"),
                        min(r.end, upper).astimezone(zone).isoformat(timespec="seconds"),
                        r.browser,
                        r.domain,
                    )
                    for r in records
                    if matches(r.domain)
                ],
            )
            self.detail.setText(
                f"Intervals use the first {self.limit} records; totals cover all matching history. Gaps are unrecorded."
            )
            self.more.setVisible(len(records) == self.limit and self.limit < 10000)
            days = [
                end - timedelta(days=i) for i in reversed(range(min(7, (end - start.min).days + 1)))
            ]
            self.fill(
                self.week,
                [
                    (
                        d.isoformat(),
                        duration(
                            sum(
                                v
                                for host, v in repo.summary(d, d, zone_name).items()
                                if matches(host)
                            )
                        ),
                    )
                    for d in days
                ],
            )
        except (OSError, ValueError, SQLAlchemyError):
            self.runtime.pause()
            self.clear()
            self.status.setText("Browser history unavailable. Check local storage and retry.")

    @staticmethod
    def fill(table, rows):
        table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j, value in enumerate(row):
                table.setItem(i, j, QTableWidgetItem(str(value)))

    def clear(self):
        for table in (self.sites, self.categories, self.sessions, self.week):
            table.setRowCount(0)
        self.total.clear()
        self.detail.clear()

    def set_state(self, *, private=False, locked=False):
        self.private, self.locked = private, locked
        if locked and hasattr(self, "runtime"):
            self.runtime.pause()
        self.clear()
        for editor in (self.filter, self.domain, self.alias, self.category, self.exclusions):
            editor.clear()
        for control in (
            self.toggle,
            self.setup_button,
            self.remove_button,
            self.save,
            self.rule_save,
            self.delete,
            self.idle,
            self.exclusions,
            self.filter,
            self.domain,
            self.alias,
            self.category,
            self.more,
        ):
            control.setEnabled(not private and not locked)
        if not private and hasattr(self, "controller"):
            self.load_settings()
        self.refresh()

    def delete_history(self):
        if self.private:
            return
        start, end = self.page.start_date.date().toPython(), self.page.end_date.date().toPython()
        running = self.runtime.running
        self.runtime.pause()
        generation = self.runtime.generation
        try:
            repo = self.controller.browser_time_repository
            zone = self.controller.settings.default_timezone
            count = repo.delete_range(start, end, zone, preview=True)
            answer = QMessageBox.question(
                self,
                "Delete browser site history",
                f"Delete {count} complete site intervals overlapping these dates? Boundary-crossing intervals are deleted in full.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes and not self.private:
                repo.delete_range(start, end, zone)
        except (ValueError, SQLAlchemyError):
            self.status.setText("Could not delete browser history. Check the dates and retry.")
        finally:
            if (
                running
                and not self.private
                and not self.locked
                and generation == self.runtime.generation
            ):
                self.runtime.start()
            self.refresh()

    def disconnect(self):
        if self.private:
            return
        self.runtime.pause()
        try:
            unregister()
            self.status.setText(
                "Helper disconnected. Remove the extension in your browser if desired. Saved history is retained."
            )
        except OSError:
            self.status.setText("Could not unregister helper. Browser recording is paused.")

    def setup_extension(self):
        if self.private:
            return
        directory = QFileDialog.getExistingDirectory(
            self, "Choose a folder for the browser extension"
        )
        if not directory or self.private:
            return
        try:
            source = Path(__file__).parents[2] / "browser_time" / "extension"
            target = Path(directory) / "PersonalHubBrowserTime"
            target.mkdir(exist_ok=True)
            for name in ("manifest.json", "background.js", "popup.html", "popup.js"):
                shutil.copyfile(source / name, target / name)
            QMessageBox.information(
                self,
                "Load browser extension",
                f"Open chrome://extensions or edge://extensions, turn on Developer mode, choose Load unpacked and select:\n{target}\n\nCopy the extension ID, then continue setup here.",
            )
            browser, ok = QInputDialog.getItem(
                self, "Browser", "Browser to connect", ["chrome", "edge"], editable=False
            )
            if not ok or self.private:
                return
            extension_id, ok = QInputDialog.getText(
                self, "Extension ID", "32-letter ID shown by the browser"
            )
            if not ok or self.private:
                return
            executable = Path(sys.executable).parent / "PersonalHubBrowserHost.exe"
            if not executable.exists():
                candidate = Path(__file__).parents[4] / "dist" / "PersonalHubBrowserHost.exe"
                executable = candidate if candidate.exists() else executable
            if not executable.exists():
                chosen, _ = QFileDialog.getOpenFileName(
                    self, "Choose PersonalHubBrowserHost.exe", "", "Helper executable (*.exe)"
                )
                if not chosen or self.private:
                    return
                executable = Path(chosen)
            register(browser, extension_id.strip(), executable, self.controller.settings.data_dir)
            self.status.setText(
                "Connected. Enable tracking in the extension, then choose Start browser recording here."
            )
        except (OSError, ValueError):
            self.status.setText(
                "Setup failed. Check the extension ID and helper executable, then retry."
            )
