from datetime import UTC, datetime

from personal_hub.bootstrap import build_hub_container
from personal_hub.config import HubSettings
from personal_hub.metrics.contracts import (
    Measurement,
    MeasurementBatch,
    MetricDefinition,
    MetricSource,
)
from personal_hub.storage.secrets import DeterministicTestCipher, HubSecretVault
from personal_hub.ui.desktop.application import HubDesktopCoordinator
from personal_hub.ui.desktop.main_window import create_main_window


def test_metrics_navigation_privacy_and_listener_lock(qapp, qtbot, tmp_path):
    container = build_hub_container(HubSettings(data_dir=tmp_path / "hub"))
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    coordinator = HubDesktopCoordinator(container, window, qapp)
    container.controller.metrics_receiver.vault = HubSecretVault(
        container.settings, cipher=DeterministicTestCipher()
    )
    try:
        window.show_destination("Metrics")
        assert window.workspace_mode == "Logging"
        view = window.metrics_page
        repo = container.controller.metrics_repository
        repo.define(
            MetricDefinition(metric_id="private", name="Private temperature", unit="celsius")
        )
        repo.enroll(
            MetricSource(source_id="sensor", name="Private source", metric_ids=("private",))
        )
        repo.ingest(
            MeasurementBatch(
                source_id="sensor",
                records=(
                    Measurement(
                        record_id="test",
                        metric_id="private",
                        revision=1,
                        value=23.5,
                        unit="celsius",
                        start=datetime.now(UTC),
                    ),
                ),
            )
        )
        view.refresh()
        view.metric_choice.setCurrentIndex(view.metric_choice.findData("private"))
        assert "23.5" in view.summary.text()
        view.save_card(True)
        assert "23.5" in view.daily_label.toPlainText()
        view.receiver.start("127.0.0.1", 0)
        window.set_privacy_masked(True)
        assert not view.summary.text() and not view.values.toPlainText()
        assert view.metric_choice.count() == 0
        assert not view.daily_label.toPlainText()
        window.set_locked(True)
        assert view.receiver.thread is None
        window.set_privacy_masked(False)
        assert not view.summary.text()
        window.set_locked(False)
        assert view.metric_choice.count() > 0
    finally:
        coordinator.shutdown()
        container.close()
