"""Accessible local metrics workspace and explicit device/transfer controls."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QDate, QSignalBlocker, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.exc import SQLAlchemyError

from personal_hub.metrics.contracts import MetricDefinition, MetricSource
from personal_hub.metrics.files import (
    MAX_FILE,
    decrypt_snapshot,
    encrypt_snapshot,
    export_snapshots,
    import_snapshot,
    parse_file,
)
from personal_hub.ui.desktop.readable import SummaryCards


class MeasurementValues(QTabWidget):
    """Exact values in labeled tables, keeping long record IDs out of prose."""

    def __init__(self):
        super().__init__()
        self.tables = []
        for title, headers in (
            ("Daily values", ("Date", "Value", "Unit")),
            (
                "Measurement records",
                ("Started", "Value", "Unit", "Timezone", "Record ID", "Revision"),
            ),
        ):
            table = QTableWidget(0, len(headers))
            table.setHorizontalHeaderLabels(headers)
            table.setAccessibleName(title)
            table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
            table.verticalHeader().hide()
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
            table.horizontalHeader().setStretchLastSection(True)
            table.setMinimumHeight(220)
            copy = QAction("Copy selection", table)
            copy.setShortcut(QKeySequence.StandardKey.Copy)
            copy.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
            copy.triggered.connect(lambda _checked=False, target=table: self.copy_selection(target))
            table.addAction(copy)
            table.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)
            self.addTab(table, title)
            self.tables.append(table)
        self.tables[0].horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for column in range(4):
            self.tables[1].horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        self.tables[1].setColumnWidth(4, 180)
        self.setAccessibleName("Daily metric values and measurement records")

    @staticmethod
    def copy_selection(table):
        indexes = table.selectedIndexes()
        if not indexes:
            return
        selected = {(index.row(), index.column()): index.data() for index in indexes}
        rows, columns = [index.row() for index in indexes], [index.column() for index in indexes]
        text = "\n".join(
            "\t".join(
                str(selected.get((row, column), ""))
                for column in range(min(columns), max(columns) + 1)
            )
            for row in range(min(rows), max(rows) + 1)
        )
        QApplication.clipboard().setText(text)

    def set_rows(self, daily, records):
        for table, rows in zip(self.tables, (daily, records), strict=True):
            table.setRowCount(len(rows))
            for row, values in enumerate(rows):
                for column, value in enumerate(values):
                    item = QTableWidgetItem(str(value))
                    item.setToolTip(str(value))
                    table.setItem(row, column, item)
            table.resizeRowsToContents()
        self.setTabText(1, f"Measurement records ({len(records)} shown, up to 200)")

    def clear(self):
        self.set_rows([], [])

    def toPlainText(self):
        return "\n".join(
            "\t".join(table.item(row, col).text() for col in range(table.columnCount()))
            for table in self.tables
            for row in range(table.rowCount())
        )


class Trend(QWidget):
    def __init__(self):
        super().__init__()
        self.points = []
        self.setMinimumHeight(120)
        self.setAccessibleName("Metric trend; exact daily values are listed below")

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setPen(QPen(self.palette().highlight().color(), 2))
        values = [p for p in self.points if p is not None]
        if not values:
            return
        low, high = min(values), max(values)
        previous = None
        for index, value in enumerate(self.points):
            if value is None:
                previous = None
                continue
            x = 12 + index * (self.width() - 24) / max(1, len(self.points) - 1)
            y = self.height() - 12 - (value - low) * (self.height() - 24) / (high - low or 1)
            if previous:
                painter.drawLine(*previous, int(x), int(y))
            painter.drawEllipse(int(x) - 2, int(y) - 2, 4, 4)
            previous = (int(x), int(y))


class MetricsPage(QScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.controller = self.receiver = None
        self.private = self.locked = False
        self.want_receiver = False
        self.dialogs = []
        self.setWidgetResizable(True)
        body = QWidget()
        self.setWidget(body)
        layout = QVBoxLayout(body)
        heading = QLabel("Metrics")
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        overview = self.tab("Overview")
        self.metric_choice = QComboBox()
        self.source = QComboBox()
        self.start = QDateEdit(QDate.currentDate().addDays(-6))
        self.end = QDateEdit(QDate.currentDate())
        filters = QGroupBox("Choose measurements")
        filter_form = QFormLayout(filters)
        filter_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        overview.addWidget(filters)
        for name, control in (
            ("Metric", self.metric_choice),
            ("Source", self.source),
            ("From", self.start),
            ("Through", self.end),
        ):
            control.setAccessibleName(name)
            filter_form.addRow(name, control)
        self.start.setCalendarPopup(True)
        self.end.setCalendarPopup(True)
        self.button(overview, "Use selected source for summaries", self.primary)
        self.summary = QLabel("No measurements")
        self.summary.setObjectName("readingSummaryValue")
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.summary.setWordWrap(True)
        overview.addWidget(self.summary)
        self.trend = Trend()
        overview.addWidget(self.trend)
        self.values = MeasurementValues()
        overview.addWidget(self.values)
        self.card = QCheckBox("Show this metric in Daily overview")
        overview.addWidget(self.card)
        self.card.toggled.connect(self.save_card)
        self.button(overview, "Preview deletion in selected range", self.delete_history)
        self.button(overview, "Allow a deleted record to be imported again", self.restore)

        transfer = self.tab("Devices and transfer")
        devices = self.group(transfer, "Source and permissions")
        self.devices = QComboBox()
        self.devices.setAccessibleName("Paired or imported metric source")
        devices.addWidget(self.devices)
        self.device_status = QLabel()
        self.device_status.setTextFormat(Qt.TextFormat.PlainText)
        self.device_status.setWordWrap(True)
        devices.addWidget(self.device_status)
        self.receiver_state = QLabel("LAN receiver: Disabled")
        self.receiver_state.setAccessibleName("LAN receiver status")
        self.receiver_state.setTextFormat(Qt.TextFormat.PlainText)
        devices.addWidget(self.receiver_state)
        self.button(devices, "Register source and permitted metrics", self.enroll)
        self.button(devices, "Edit selected source permissions", self.permissions)
        self.button(devices, "Revoke selected source", self.revoke)
        network = self.group(transfer, "Local network pairing")
        self.host = QLineEdit("127.0.0.1")
        self.host.setAccessibleName("Local receiving interface IP address")
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(8765)
        self.port.setAccessibleName("Metrics receiver port")
        network.addWidget(QLabel("Choose your PC's LAN address to receive from Android."))
        network_form = QFormLayout()
        network_form.addRow("Receiving address", self.host)
        network_form.addRow("Port", self.port)
        network.addLayout(network_form)
        self.button(network, "Start LAN receiver", self.start_receiver)
        self.button(network, "Stop LAN receiver", self.stop_receiver)
        self.button(network, "Create five-minute pairing string", self.pair)
        self.pairing = QPlainTextEdit()
        self.pairing.setReadOnly(True)
        self.pairing.setAccessibleName("Private single-use device pairing string")
        self.pairing.setMaximumHeight(110)
        network.addWidget(self.pairing)
        snapshots = self.group(transfer, "Encrypted snapshot files")
        self.button(snapshots, "Import encrypted snapshot file", self.snapshot_import)
        self.button(snapshots, "Import encrypted snapshots from folder", self.folder_import)
        self.button(
            snapshots, "Export selected source as encrypted snapshots", self.snapshot_export
        )

        custom = self.tab("Custom metrics and files")
        self.button(custom, "Define numeric metric or timed session", self.define)
        self.button(custom, "Import measurement JSON", lambda: self.raw_import(False))
        self.button(custom, "Import measurement CSV", lambda: self.raw_import(True))
        custom.addWidget(
            QLabel(
                "Custom senders use the same paired HTTPS API as Android. See the metrics guide."
            )
        )
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        for control in (self.metric_choice, self.source):
            control.currentIndexChanged.connect(self.refresh)
        for control in (self.start, self.end):
            control.dateChanged.connect(self.refresh)
        self.devices.currentIndexChanged.connect(self.refresh)
        self.timer = QTimer(self)
        self.timer.setInterval(5000)
        self.timer.timeout.connect(self.tick)

    def tab(self, name):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.tabs.addTab(page, name)
        return layout

    @staticmethod
    def group(layout, title):
        group = QGroupBox(title)
        layout.addWidget(group)
        return QVBoxLayout(group)

    def button(self, layout, name, handler):
        button = QPushButton(name)
        button.setAccessibleName(name)
        button.clicked.connect(lambda: self.safe(handler))
        layout.addWidget(button)

    def bind(self, controller, daily):
        self.controller, self.receiver = controller, controller.metrics_receiver
        self.daily = daily
        self.daily_label = SummaryCards()
        self.daily_label.setAccessibleName("Selected personal metrics for this day")
        daily.layout().insertWidget(2, self.daily_label)
        self.host.setText(controller.settings_repository.get("metrics.host", "127.0.0.1"))
        self.port.setValue(controller.settings_repository.get("metrics.port", 8765))
        self.timer.start()
        self.refresh()

    def safe(self, handler):
        if self.private or not self.controller or self.controller.is_closed:
            return
        try:
            handler()
            self.refresh()
        except (ValueError, KeyError, OSError, RuntimeError, ArithmeticError, SQLAlchemyError):
            self.status.setText(
                "Could not complete this action. Check the source, permissions, file format, and connection."
            )

    def ask(self, title, value="", *, secret=False):
        dialog = QInputDialog(self)
        self.dialogs.append(dialog)
        dialog.setWindowTitle(title)
        dialog.setLabelText(title)
        dialog.setTextValue(value)
        dialog.setTextEchoMode(QLineEdit.EchoMode.Password if secret else QLineEdit.EchoMode.Normal)
        try:
            return dialog.textValue() if dialog.exec() else None
        finally:
            self.dialogs.remove(dialog)
            dialog.deleteLater()

    def confirm(self, title, text):
        if self.private:
            return QMessageBox.StandardButton.No
        dialog = QMessageBox(self)
        dialog.setWindowTitle(title)
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setText(text)
        dialog.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        dialog.setDefaultButton(QMessageBox.StandardButton.No)
        self.dialogs.append(dialog)
        try:
            return dialog.exec()
        finally:
            self.dialogs.remove(dialog)
            dialog.deleteLater()

    def choose_file(self, title, *, directory=False, save=False, pattern="All files (*)"):
        if self.private:
            return None
        dialog = QFileDialog(self, title)
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog)
        dialog.setFileMode(
            QFileDialog.FileMode.Directory
            if directory
            else QFileDialog.FileMode.AnyFile
            if save
            else QFileDialog.FileMode.ExistingFile
        )
        dialog.setNameFilter(pattern)
        if save:
            dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
            dialog.setDefaultSuffix("phmetrics")
        self.dialogs.append(dialog)
        try:
            return dialog.selectedFiles()[0] if dialog.exec() else None
        finally:
            self.dialogs.remove(dialog)
            dialog.deleteLater()

    def set_state(self, *, private, locked):
        self.private, self.locked = private, locked
        self.pairing.clear()
        if self.controller:
            self.controller.metrics_locked = locked
        if private:
            for dialog in tuple(self.dialogs):
                dialog.reject()
            self.summary.clear()
            self.values.clear()
            self.device_status.clear()
            self.receiver_state.clear()
            self.status.clear()
            self.trend.points = []
            self.trend.update()
            for combo in (self.metric_choice, self.source, self.devices):
                with QSignalBlocker(combo):
                    combo.clear()
            if hasattr(self, "daily_label"):
                self.daily_label.clear()
            if self.receiver:
                with self.receiver.guard:
                    self.receiver.enrollments.clear()
        if locked and self.receiver:
            self.receiver.stop()
        self.tabs.setEnabled(not private)
        if not private:
            self.refresh()

    def tick(self):
        if not self.controller or self.controller.is_closed:
            self.timer.stop()
            return
        if self.receiver and self.receiver.blocked():
            if self.receiver.status == "Receiving":
                self.receiver.stop()
        elif self.want_receiver and self.receiver and self.receiver.status != "Receiving":
            self.safe(self.start_receiver)
        if not self.private:
            self.safe(self.refresh)

    def populate(self, combo, items):
        selected = combo.currentData()
        with QSignalBlocker(combo):
            combo.clear()
            for label, value in items:
                combo.addItem(label, value)
            index = combo.findData(selected)
            combo.setCurrentIndex(index if index >= 0 else 0)

    def refresh(self, *_):
        if self.private or not self.controller or self.controller.is_closed:
            return
        repo = self.controller.metrics_repository
        receiver_status = (
            "Paused or locked"
            if self.receiver and self.receiver.blocked()
            else self.receiver.status
            if self.receiver
            else "Unavailable"
        )
        self.receiver_state.setText(f"LAN receiver: {receiver_status}")
        self.populate(self.metric_choice, [(d.name, d.metric_id) for d in repo.definitions()])
        sources = repo.sources()
        self.populate(self.devices, [(s.name, s.source_id) for s in sources])
        metric = self.metric_choice.currentData()
        self.populate(
            self.source,
            [("Primary source", None)]
            + [(s.name, s.source_id) for s in sources if metric in s.metric_ids],
        )
        chosen = self.source.currentData() or repo.primary(metric)
        start, end = self.start.date().toPython(), self.end.date().toPython()
        if end < start:
            self.summary.setText("Choose an end date on or after the start date.")
            self.values.clear()
            self.trend.points = []
            self.trend.update()
            return
        summary = repo.summary(metric, start, end, chosen)
        self.summary.setText(
            f"{summary.value:g} {summary.unit} · {summary.count} records"
            if summary.value is not None
            else "No measurements available"
        )
        daily, points = [], []
        for index in range(min((end - start).days + 1, 366)):
            day = start + timedelta(days=index)
            item = repo.summary(metric, day, day, chosen)
            points.append(item.value)
            daily.append(
                (
                    str(day),
                    f"{item.value:g}" if item.value is not None else "Unavailable",
                    item.unit,
                )
            )
        records = repo.records(metric, chosen, start, end, limit=200) if chosen else []
        detail = [
            (
                r.start.isoformat(),
                r.value if r.value is not None else "Session",
                r.unit,
                r.timezone,
                r.record_id,
                r.revision,
            )
            for _, r in records
        ]
        self.values.set_rows(daily, detail)
        self.trend.points = points
        self.trend.update()
        selected = repo.source(self.devices.currentData())
        self.device_status.setText(
            f"{'Revoked' if selected.revoked else 'Approved'} · {', '.join(selected.metric_ids)} · Last received: {selected.last_received_at or 'never'}"
            if selected
            else "Register a source to begin."
        )
        cards = self.controller.settings_repository.get("metrics.daily_cards", [])
        with QSignalBlocker(self.card):
            self.card.setChecked(metric in cards)
        lines = []
        definitions = {definition.metric_id: definition.name for definition in repo.definitions()}
        for identifier in cards:
            try:
                item = repo.summary(identifier, self.daily.day, self.daily.day)
                lines.append(
                    (
                        definitions.get(identifier, identifier),
                        f"{item.value:g} {item.unit}" if item.value is not None else "Unavailable",
                    )
                )
            except (ValueError, StopIteration):
                continue
        self.daily_label.set_cards(lines)
        self.daily_label.set_compact(self.daily.width() < 650)

    def save_card(self, enabled):
        if self.private or not self.controller:
            return
        cards = set(self.controller.settings_repository.get("metrics.daily_cards", []))
        identifier = self.metric_choice.currentData()
        cards.add(identifier) if enabled else cards.discard(identifier)
        self.controller.settings_repository.set("metrics.daily_cards", sorted(cards))
        self.refresh()

    def primary(self):
        self.controller.metrics_repository.set_primary(
            self.metric_choice.currentData(), self.source.currentData()
        )

    def enroll(self):
        name = self.ask("Source display name")
        if not name:
            return
        identifier = self.ask("Source ID (use this same ID on your device)", uuid4().hex)
        allowed = self.ask("Permitted metric IDs, comma separated", "steps")
        if identifier and allowed:
            self.controller.metrics_repository.enroll(
                MetricSource(
                    source_id=identifier,
                    name=name,
                    metric_ids=tuple(s.strip() for s in allowed.split(",")),
                )
            )

    def define(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Define a personal metric")
        form = QFormLayout(dialog)
        identifier, name, unit = QLineEdit(), QLineEdit(), QLineEdit()
        kind, aggregation = QComboBox(), QComboBox()
        kind.addItem("Numeric reading at a time", "point")
        kind.addItem("Total over an interval", "interval")
        kind.addItem("Timed session", "session")
        for label, value in (
            ("Latest", "latest"),
            ("Sum", "sum"),
            ("Minimum", "min"),
            ("Maximum", "max"),
            ("Arithmetic mean", "mean"),
            ("Duration", "duration"),
        ):
            aggregation.addItem(label, value)
        for label, control in (
            ("Metric ID (simple lowercase name)", identifier),
            ("Display name", name),
            ("Unit (for example celsius)", unit),
            ("Measurement type", kind),
            ("Summary calculation", aggregation),
        ):
            control.setAccessibleName(label)
            form.addRow(label, control)

        def update_kind():
            session = kind.currentData() == "session"
            aggregation.setEnabled(not session)
            unit.setEnabled(not session)
            aggregation.setCurrentIndex(aggregation.findData("duration" if session else "latest"))
            if session:
                unit.setText("seconds")

        kind.currentIndexChanged.connect(update_kind)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        error = QLabel()
        error.setTextFormat(Qt.TextFormat.PlainText)
        form.addRow(error)

        def save():
            try:
                definition = MetricDefinition(
                    metric_id=identifier.text().strip(),
                    name=name.text().strip(),
                    unit=unit.text().strip(),
                    kind=kind.currentData(),
                    aggregation=aggregation.currentData(),
                )
                self.controller.metrics_repository.define(definition)
                dialog.accept()
            except (ValueError, SQLAlchemyError):
                error.setText(
                    "Check the ID, unit and calculation. Sessions use duration in seconds."
                )

        buttons.accepted.connect(save)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        self.dialogs.append(dialog)
        try:
            dialog.exec()
        finally:
            self.dialogs.remove(dialog)
            dialog.deleteLater()

    def permissions(self):
        source = self.controller.metrics_repository.source(self.devices.currentData())
        if source:
            allowed = self.ask("Permitted metric IDs, comma separated", ",".join(source.metric_ids))
            if allowed:
                self.controller.metrics_repository.update_permissions(
                    source.source_id, tuple(s.strip() for s in allowed.split(","))
                )

    def start_receiver(self):
        if self.receiver is None:
            raise ValueError("receiver unavailable")
        self.receiver.start(self.host.text().strip(), self.port.value())
        self.want_receiver = True
        self.controller.settings_repository.set("metrics.host", self.host.text().strip())
        self.controller.settings_repository.set("metrics.port", self.port.value())
        self.status.setText(f"Receiving on {self.host.text()}:{self.receiver.port}")

    def stop_receiver(self):
        self.want_receiver = False
        if self.receiver:
            self.receiver.stop()
        self.pairing.clear()
        self.status.setText("LAN receiver stopped")

    def pair(self):
        self.pairing.setPlainText(self.receiver.enrollment(self.devices.currentData()))

    def revoke(self):
        identifier = self.devices.currentData()
        if self.receiver:
            self.receiver.revoke(identifier)
        else:
            self.controller.metrics_repository.revoke(identifier)
        self.pairing.clear()

    def read_file(self, path, maximum=MAX_FILE):
        with Path(path).open("rb") as stream:
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError("file too large")
        return data

    def confirm_snapshot(self, data, passphrase):
        snapshot = decrypt_snapshot(data, passphrase)
        repo = self.controller.metrics_repository
        if repo.source(snapshot.source.source_id) is None:
            text = (
                f"Approve {snapshot.source.name} ({snapshot.source.source_id})?\nMetrics: {', '.join(snapshot.source.metric_ids)}\nRecords: {sum(len(b.records) for b in snapshot.batches)}\nDefinitions: "
                + ", ".join(f"{d.name}: {d.unit}, {d.aggregation}" for d in snapshot.definitions)
            )
            if self.confirm("Approve snapshot source", text) != QMessageBox.StandardButton.Yes:
                return
            for definition in snapshot.definitions:
                repo.define(definition)
            repo.enroll(snapshot.source)
        self.status.setText(str(import_snapshot(repo, snapshot)))

    def snapshot_import(self):
        path = self.choose_file("Import encrypted metrics", pattern="Metrics (*.phmetrics)")
        if path:
            passphrase = self.ask("Snapshot passphrase", secret=True)
            if passphrase:
                self.confirm_snapshot(self.read_file(path), passphrase)

    def folder_import(self):
        directory = self.choose_file("Metrics folder", directory=True)
        if not directory:
            return
        passphrase = self.ask("Snapshot passphrase", secret=True)
        if passphrase:
            paths = sorted(Path(directory).glob("*.phmetrics"))
            if len(paths) > 100:
                raise ValueError("import at most 100 files per folder operation")
            for path in paths:
                self.confirm_snapshot(self.read_file(path), passphrase)

    def snapshot_export(self):
        path = self.choose_file(
            "Export encrypted metrics", save=True, pattern="Metrics (*.phmetrics)"
        )
        if not path:
            return
        passphrase = self.ask("Export passphrase (at least 12 characters)", secret=True)
        if passphrase:
            for index, snapshot in enumerate(
                export_snapshots(self.controller.metrics_repository, self.devices.currentData()), 1
            ):
                target = Path(path).with_name(f"{Path(path).stem}-{index:04d}.phmetrics")
                with target.open("xb") as stream:
                    stream.write(encrypt_snapshot(snapshot, passphrase))
            self.status.setText("Encrypted snapshot parts exported.")

    def raw_import(self, csv_mode):
        source = self.devices.currentData()
        if not source:
            raise ValueError("register a source first")
        path = self.choose_file("Import measurements")
        if not path:
            return
        options = {}
        if csv_mode:
            saved = self.controller.settings_repository.get(
                "metrics.csv_mapping", {"start": "timestamp", "value": "value"}
            )
            mapping = self.ask("CSV field-to-column mapping JSON", json.dumps(saved))
            timezone = self.ask("CSV reporting timezone", self.controller.settings.default_timezone)
            if not mapping or not timezone:
                return
            definition = next(
                d
                for d in self.controller.metrics_repository.definitions()
                if d.metric_id == self.metric_choice.currentData()
            )
            options = dict(
                csv_mapping=json.loads(mapping),
                timezone=timezone,
                metric_id=definition.metric_id,
                unit=definition.unit,
            )
        batch = parse_file(self.read_file(path, 1024 * 1024), source_id=source, **options)
        if (
            self.confirm(
                "Import preview",
                f"Import {len(batch.records)} records for {source}?\nMetrics: {', '.join(sorted({r.metric_id for r in batch.records}))}",
            )
            == QMessageBox.StandardButton.Yes
        ):
            result = self.controller.metrics_repository.ingest(batch)
            if csv_mode:
                self.controller.settings_repository.set(
                    "metrics.csv_mapping", options["csv_mapping"]
                )
            self.status.setText(str(result))

    def delete_history(self):
        repo = self.controller.metrics_repository
        options = dict(
            metric_id=self.metric_choice.currentData(),
            source_id=self.source.currentData() or repo.primary(self.metric_choice.currentData()),
            start=self.start.date().toPython(),
            end=self.end.date().toPython(),
        )
        count = repo.delete_history(**options)
        if (
            count
            and self.confirm(
                "Delete metrics",
                f"Delete {count} records in this range? Old files will remain suppressed.",
            )
            == QMessageBox.StandardButton.Yes
        ):
            repo.delete_history(**options, apply=True)

    def restore(self):
        identifier = self.ask("Exact deleted record ID to allow again")
        source = self.source.currentData() or self.controller.metrics_repository.primary(
            self.metric_choice.currentData()
        )
        if identifier and source:
            self.controller.metrics_repository.restore_identity(source, identifier)
