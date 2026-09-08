"""Hub-owned external-platform setup dialog.

The dialog collects only reviewed connection fields.  Secret values use password-mode
controls and are returned directly to the coordinator for immediate vault storage.
"""

from __future__ import annotations

from collections.abc import Mapping

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from personal_hub.integrations.platforms import (
    ExternalPlatformConfig,
    ExternalPlatformDefinition,
)


class ExternalPlatformDialog(QDialog):
    """Configure one reviewed external platform connection."""

    def __init__(
        self,
        definition: ExternalPlatformDefinition,
        current: ExternalPlatformConfig | Mapping[str, object] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.definition = definition
        self.current = (
            current
            if isinstance(current, ExternalPlatformConfig)
            else ExternalPlatformConfig.model_validate(current)
            if current is not None
            else None
        )
        self._payload: dict[str, object] | None = None
        self.setObjectName("externalPlatformDialog")
        self.setWindowTitle(f"Connect {definition.display_name}")
        self.setAccessibleName(f"{definition.display_name} connection setup")
        self.setModal(True)
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        description = QLabel(definition.description)
        description.setObjectName("pageSubtitle")
        description.setTextFormat(Qt.TextFormat.PlainText)
        description.setWordWrap(True)
        layout.addWidget(description)

        permissions = QLabel(self._permission_text())
        permissions.setAccessibleName("Requested read-only access")
        permissions.setTextFormat(Qt.TextFormat.PlainText)
        permissions.setWordWrap(True)
        layout.addWidget(permissions)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.account_input = QLineEdit(
            self.current.account_label if self.current is not None else definition.display_name
        )
        self.account_input.setAccessibleName("Connection display name")
        self.account_input.setMaxLength(160)
        form.addRow("Display name", self.account_input)

        self.client_id_input: QLineEdit | None = None
        self.client_secret_input: QLineEdit | None = None
        self.tenant_input: QLineEdit | None = None
        self.calendar_checkbox: QCheckBox | None = None
        self.messages_checkbox: QCheckBox | None = None
        self.token_input: QLineEdit | None = None
        self.canvas_feed_input: QLineEdit | None = None
        self.slack_kind_combo: QComboBox | None = None
        self.slack_selection_input: QLineEdit | None = None

        if definition.authentication == "ical_url":
            self.canvas_feed_input = QLineEdit()
            self.canvas_feed_input.setAccessibleName("Canvas iCalendar feed URL")
            self.canvas_feed_input.setEchoMode(QLineEdit.EchoMode.Password)
            self.canvas_feed_input.setPlaceholderText(
                "https://your-school.instructure.com/feeds/calendars/….ics"
                if self.current is None
                else "Leave blank to keep the current private feed URL"
            )
            self.canvas_feed_input.setMaxLength(4_096)
            form.addRow("iCalendar URL", self.canvas_feed_input)
        elif definition.authentication == "oauth_pkce":
            self.client_id_input = QLineEdit(
                self.current.client_id if self.current is not None else ""
            )
            self.client_id_input.setAccessibleName("Desktop OAuth client ID")
            self.client_id_input.setPlaceholderText("Desktop/public OAuth client ID")
            self.client_id_input.setMaxLength(512)
            form.addRow("OAuth client ID", self.client_id_input)

            self.client_secret_input = QLineEdit()
            self.client_secret_input.setAccessibleName("Optional OAuth client secret")
            self.client_secret_input.setEchoMode(QLineEdit.EchoMode.Password)
            self.client_secret_input.setPlaceholderText("Optional; leave blank to keep existing")
            self.client_secret_input.setMaxLength(4_096)
            form.addRow("Client secret", self.client_secret_input)

            if definition.platform_id == "microsoft":
                self.tenant_input = QLineEdit(
                    self.current.tenant if self.current is not None else "common"
                )
                self.tenant_input.setAccessibleName("Microsoft tenant")
                self.tenant_input.setMaxLength(64)
                form.addRow("Tenant", self.tenant_input)

            selected = set(
                self.current.capabilities if self.current is not None else definition.capabilities
            )
            self.calendar_checkbox = QCheckBox("Calendar (read only)")
            self.calendar_checkbox.setChecked("calendar" in selected)
            self.calendar_checkbox.setAccessibleName("Enable read-only calendar access")
            form.addRow("Calendar", self.calendar_checkbox)
            self.messages_checkbox = QCheckBox("Message summaries (read only)")
            self.messages_checkbox.setChecked("messages" in selected)
            self.messages_checkbox.setAccessibleName("Enable read-only message summaries")
            form.addRow("Messages", self.messages_checkbox)
        else:
            self.token_input = QLineEdit()
            self.token_input.setAccessibleName("Slack read-only API token")
            self.token_input.setEchoMode(QLineEdit.EchoMode.Password)
            self.token_input.setPlaceholderText("xoxp-… or xoxb-…; leave blank to keep existing")
            self.token_input.setMaxLength(32_768)
            form.addRow("Read-only token", self.token_input)

            self.slack_kind_combo = QComboBox()
            self.slack_kind_combo.setAccessibleName("Slack selected view type")
            self.slack_kind_combo.addItem("Channel ID", "channel")
            self.slack_kind_combo.addItem("Saved search", "query")
            if self.current is not None:
                index = self.slack_kind_combo.findData(self.current.slack_selection_kind)
                self.slack_kind_combo.setCurrentIndex(max(0, index))
            form.addRow("Read view", self.slack_kind_combo)

            self.slack_selection_input = QLineEdit(
                self.current.slack_selection or "" if self.current is not None else ""
            )
            self.slack_selection_input.setAccessibleName("Slack channel ID or saved search")
            self.slack_selection_input.setMaxLength(500)
            self._update_slack_placeholder()
            self.slack_kind_combo.currentIndexChanged.connect(self._update_slack_placeholder)
            form.addRow("Selection", self.slack_selection_input)

        layout.addLayout(form)
        self.status_label = QLabel()
        self.status_label.setObjectName("connectionValidationStatus")
        self.status_label.setAccessibleName("Connection validation status")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Connect")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _permission_text(self) -> str:
        if self.definition.platform_id == "canvas":
            return (
                "Uses only the private Canvas iCalendar link you paste. Each calendar item "
                "becomes a local due-dated to-do, which also appears in Calendar."
            )
        if self.definition.platform_id == "google":
            return (
                "Requests only Calendar read-only and/or Gmail read-only access. "
                "Personal Hub cannot send mail or change calendar events."
            )
        if self.definition.platform_id == "microsoft":
            return (
                "Requests only Calendars.Read and/or Mail.Read plus offline token renewal. "
                "Personal Hub cannot send mail or change calendar events."
            )
        return (
            "Stores one token locally and uses only channels:history or search:read for the "
            "selected view. Posting, reactions, and uploads are unavailable."
        )

    def _update_slack_placeholder(self, *_args: object) -> None:
        if self.slack_kind_combo is None or self.slack_selection_input is None:
            return
        self.slack_selection_input.setPlaceholderText(
            "C0123456789" if self.slack_kind_combo.currentData() == "channel" else "from:me"
        )

    def accept(self) -> None:
        account_label = " ".join(self.account_input.text().split())
        if not account_label:
            self.status_label.setText("Enter a display name for this connection.")
            return
        payload: dict[str, object] = {
            "account_label": account_label,
            "enabled": True,
        }
        if self.definition.authentication == "ical_url":
            assert self.canvas_feed_input is not None
            feed_url = self.canvas_feed_input.text().strip()
            if self.current is None and not feed_url:
                self.status_label.setText("Paste your private Canvas iCalendar feed URL.")
                return
            payload["capabilities"] = ("tasks", "calendar")
            if feed_url:
                payload["feed_url"] = feed_url
        elif self.definition.authentication == "oauth_pkce":
            assert self.client_id_input is not None
            client_id = self.client_id_input.text().strip()
            if not client_id:
                self.status_label.setText("Enter the desktop/public OAuth client ID.")
                return
            capabilities: list[str] = []
            if self.calendar_checkbox is not None and self.calendar_checkbox.isChecked():
                capabilities.append("calendar")
            if self.messages_checkbox is not None and self.messages_checkbox.isChecked():
                capabilities.append("messages")
            if not capabilities:
                self.status_label.setText("Select at least one read-only capability.")
                return
            payload.update({"client_id": client_id, "capabilities": tuple(capabilities)})
            if self.client_secret_input is not None and self.client_secret_input.text().strip():
                payload["client_secret"] = self.client_secret_input.text().strip()
            if self.tenant_input is not None:
                payload["tenant"] = self.tenant_input.text().strip()
        else:
            assert self.slack_kind_combo is not None
            assert self.slack_selection_input is not None
            kind = str(self.slack_kind_combo.currentData())
            selection = self.slack_selection_input.text().strip()
            if not selection:
                self.status_label.setText("Enter one Slack channel ID or saved search.")
                return
            payload.update(
                {
                    "capabilities": ("messages",),
                    "slack_selection_kind": kind,
                    "slack_selection": selection,
                    "slack_scopes": (
                        ("channels:history",) if kind == "channel" else ("search:read",)
                    ),
                }
            )
            if self.token_input is not None and self.token_input.text().strip():
                payload["token"] = self.token_input.text().strip()
            if self.current is None and "token" not in payload:
                self.status_label.setText("Enter a read-only Slack API token.")
                return
        self._payload = payload
        if self.client_secret_input is not None:
            self.client_secret_input.clear()
        if self.token_input is not None:
            self.token_input.clear()
        if self.canvas_feed_input is not None:
            self.canvas_feed_input.clear()
        super().accept()

    def connection_payload(self) -> dict[str, object] | None:
        return None if self._payload is None else dict(self._payload)


__all__ = ["ExternalPlatformDialog"]
