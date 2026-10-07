"""Presentation-only dialog for bounded CSV and text-list imports."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QDate, Qt, QTime
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from personal_hub.imports import (
    ImportFileError,
    calendar_template_csv,
    normalize_import_name,
    read_import_file,
    task_template_csv,
    text_list_instructions,
)


class ImportDialog(QDialog):
    """Collect one explicitly local CSV or text-list import."""

    def __init__(
        self,
        *,
        default_target: str = "tasks",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._payload: dict[str, object] | None = None
        self.setObjectName("importDialog")
        self.setWindowTitle("Import tasks or calendar items")
        self.setAccessibleName("Personal Hub import setup")
        self.setModal(True)
        self.setMinimumWidth(620)

        layout = QVBoxLayout(self)
        description = QLabel(
            "Import local tasks or calendar items from a CSV file or a pasted text list. "
            "Imported calendar items are read-only and never write to connected calendars."
        )
        description.setObjectName("importDescription")
        description.setAccessibleName("Import instructions")
        description.setTextFormat(Qt.TextFormat.PlainText)
        description.setWordWrap(True)
        layout.addWidget(description)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self.target_combo = QComboBox()
        self.target_combo.setObjectName("importTargetCombo")
        self.target_combo.setAccessibleName("Import destination")
        self.target_combo.addItem("Tasks", "tasks")
        self.target_combo.addItem("Calendar items", "calendar")
        target_index = self.target_combo.findData(default_target.strip().casefold())
        self.target_combo.setCurrentIndex(target_index if target_index >= 0 else 0)
        self.target_combo.currentIndexChanged.connect(self._refresh_instructions)
        form.addRow("Import into", self.target_combo)

        self.format_combo = QComboBox()
        self.format_combo.setObjectName("importFormatCombo")
        self.format_combo.setAccessibleName("Import format")
        self.format_combo.addItem("CSV file or pasted CSV", "csv")
        self.format_combo.addItem("Text list", "text")
        self.format_combo.currentIndexChanged.connect(self._refresh_instructions)
        form.addRow("Format", self.format_combo)

        self.name_input = QLineEdit()
        self.name_input.setObjectName("importNameInput")
        self.name_input.setAccessibleName("Import name")
        self.name_input.setPlaceholderText("For example, Project tasks or Family events")
        self.name_input.setMaxLength(160)
        form.addRow("Import name", self.name_input)
        layout.addLayout(form)

        file_row = QHBoxLayout()
        self.file_input = QLineEdit()
        self.file_input.setObjectName("importFileInput")
        self.file_input.setAccessibleName("Import file path")
        self.file_input.setPlaceholderText("Optional: choose a UTF-8 .csv or .txt file")
        self.file_input.setClearButtonEnabled(True)
        file_row.addWidget(self.file_input, 1)
        self.browse_button = QPushButton("Browse…")
        self.browse_button.setObjectName("importBrowseButton")
        self.browse_button.setAccessibleName("Browse for import file")
        self.browse_button.setAutoDefault(False)
        self.browse_button.clicked.connect(self._browse_file)
        file_row.addWidget(self.browse_button)
        layout.addLayout(file_row)

        self.content_input = QPlainTextEdit()
        self.content_input.setObjectName("importContentInput")
        self.content_input.setAccessibleName("Import content")
        self.content_input.setAccessibleDescription("Paste CSV rows or one task or event per line.")
        self.content_input.setPlaceholderText("Paste CSV rows or one item per line here")
        self.content_input.setMinimumHeight(170)
        self.content_input.setTabChangesFocus(False)
        layout.addWidget(self.content_input)

        self.instructions_label = QLabel()
        self.instructions_label.setObjectName("importInstructionsLabel")
        self.instructions_label.setAccessibleName("Import format instructions")
        self.instructions_label.setTextFormat(Qt.TextFormat.PlainText)
        self.instructions_label.setWordWrap(True)
        layout.addWidget(self.instructions_label)

        self.calendar_defaults_frame = QFrame()
        self.calendar_defaults_frame.setObjectName("calendarImportDefaults")
        defaults_layout = QFormLayout(self.calendar_defaults_frame)
        defaults_layout.setContentsMargins(0, 0, 0, 0)
        self.default_date_input = QDateEdit(QDate.currentDate())
        self.default_date_input.setObjectName("calendarImportDefaultDate")
        self.default_date_input.setAccessibleName("Default calendar date")
        self.default_date_input.setCalendarPopup(True)
        self.default_date_input.calendarWidget().setFirstDayOfWeek(Qt.DayOfWeek.Sunday)
        defaults_layout.addRow("Default date", self.default_date_input)
        self.default_time_input = QTimeEdit(QTime(9, 0))
        self.default_time_input.setObjectName("calendarImportDefaultTime")
        self.default_time_input.setAccessibleName("Default calendar start time")
        defaults_layout.addRow("Default start time", self.default_time_input)
        self.default_duration_input = QSpinBox()
        self.default_duration_input.setObjectName("calendarImportDefaultDuration")
        self.default_duration_input.setAccessibleName("Default calendar duration in minutes")
        self.default_duration_input.setRange(5, 1_440)
        self.default_duration_input.setValue(60)
        self.default_duration_input.setSuffix(" min")
        defaults_layout.addRow("Default duration", self.default_duration_input)
        self.default_all_day_checkbox = QCheckBox("Use all-day items when a row has no time")
        self.default_all_day_checkbox.setObjectName("calendarImportDefaultAllDay")
        self.default_all_day_checkbox.setAccessibleName("Default calendar all-day setting")
        defaults_layout.addRow("Calendar defaults", self.default_all_day_checkbox)
        layout.addWidget(self.calendar_defaults_frame)

        template_row = QHBoxLayout()
        self.template_hint_label = QLabel(
            "Need a starting point? Save the CSV header template and add one row per item."
        )
        self.template_hint_label.setObjectName("importTemplateHint")
        self.template_hint_label.setTextFormat(Qt.TextFormat.PlainText)
        self.template_hint_label.setWordWrap(True)
        template_row.addWidget(self.template_hint_label, 1)
        self.save_template_button = QPushButton()
        self.save_template_button.setObjectName("importSaveTemplateButton")
        self.save_template_button.setAccessibleName("Save import CSV template")
        self.save_template_button.setAutoDefault(False)
        self.save_template_button.clicked.connect(self._save_template)
        template_row.addWidget(self.save_template_button)
        layout.addLayout(template_row)

        self.status_label = QLabel()
        self.status_label.setObjectName("importValidationStatus")
        self.status_label.setAccessibleName("Import validation status")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Import")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._refresh_instructions()

    @property
    def target(self) -> str:
        return str(self.target_combo.currentData() or "tasks")

    @property
    def source_format(self) -> str:
        return str(self.format_combo.currentData() or "csv")

    def _refresh_instructions(self) -> None:
        is_calendar = self.target == "calendar"
        is_text = self.source_format == "text"
        self.calendar_defaults_frame.setVisible(is_calendar and is_text)
        self.save_template_button.setVisible(not is_text)
        self.save_template_button.setText(
            "Save calendar CSV template" if is_calendar else "Save task CSV template"
        )
        self.save_template_button.setAccessibleName(
            "Save calendar CSV template" if is_calendar else "Save task CSV template"
        )
        if is_text:
            self.instructions_label.setText(text_list_instructions(self.target))
        elif is_calendar:
            self.instructions_label.setText(
                "Use the columns in the template. Calendar rows need start_date or start_at; "
                "dates and times are interpreted in each row's timezone."
            )
        else:
            self.instructions_label.setText(
                "Use the columns in the task template. title is required; due dates, lists, "
                "tags, and reminders are optional."
            )

    def _browse_file(self) -> None:
        selected, _filter = QFileDialog.getOpenFileName(
            self,
            "Select import file",
            "",
            "CSV or text files (*.csv *.txt);;All files (*)",
        )
        if not selected:
            return
        try:
            content = read_import_file(selected)
        except (ImportFileError, OSError, ValueError):
            self.status_label.setText("The selected file could not be read as safe UTF-8 text.")
            return
        self.file_input.setText(selected)
        self.content_input.setPlainText(content)
        if not self.name_input.text().strip():
            self.name_input.setText(Path(selected).stem[:160])
        self.status_label.clear()

    def _save_template(self) -> None:
        suffix = "calendar" if self.target == "calendar" else "tasks"
        selected, _filter = QFileDialog.getSaveFileName(
            self,
            "Save import template",
            f"personal_hub_{suffix}_import_template.csv",
            "CSV files (*.csv);;All files (*)",
        )
        if not selected:
            return
        try:
            Path(selected).expanduser().resolve(strict=False).write_text(
                calendar_template_csv() if self.target == "calendar" else task_template_csv(),
                encoding="utf-8",
                newline="",
            )
        except OSError:
            self.status_label.setText("The template could not be saved to that location.")
            return
        self.status_label.setText(f"Template saved to {Path(selected).name}.")

    def _calendar_defaults(self) -> dict[str, object]:
        selected_date = self.default_date_input.date()
        selected_time = self.default_time_input.time()
        return {
            "default_calendar_date": selected_date.toPython(),
            "default_start_time": selected_time.toPython(),
            "default_duration_minutes": self.default_duration_input.value(),
            "default_all_day": self.default_all_day_checkbox.isChecked(),
        }

    def accept(self) -> None:
        content = self.content_input.toPlainText()
        selected_path = self.file_input.text().strip()
        if selected_path:
            try:
                content = read_import_file(selected_path)
            except (ImportFileError, OSError, ValueError):
                self.status_label.setText("The selected file could not be read as safe UTF-8 text.")
                return
        if not content.strip():
            self.status_label.setText("Choose a file or paste CSV/text-list content to import.")
            return
        try:
            import_name = normalize_import_name(
                self.name_input.text(),
                fallback="Task import" if self.target == "tasks" else "Calendar import",
            )
        except (TypeError, ValueError):
            self.status_label.setText("Enter a short, single-line import name.")
            return
        payload: dict[str, object] = {
            "content": content,
            "target": self.target,
            "source_format": self.source_format,
            "import_name": import_name,
        }
        if self.target == "calendar":
            payload.update(self._calendar_defaults())
        self._payload = payload
        super().accept()

    def payload(self) -> dict[str, object] | None:
        return None if self._payload is None else dict(self._payload)

    import_payload = payload


__all__ = ["ImportDialog"]
