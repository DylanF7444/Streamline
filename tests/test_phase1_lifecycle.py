from __future__ import annotations

from pathlib import Path

import pytest

from personal_hub import app
from personal_hub.config import HUB_STARTUP_ENTRY_NAME, HubSettings
from personal_hub.lifecycle import HubLifecycle, LifecycleEvent, LifecycleState
from personal_hub.startup import WindowsStartupRegistration


class _Lock:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_lifecycle_pauses_refresh_for_lock_sleep_and_user_pause(monkeypatch, tmp_path: Path):
    lock = _Lock()
    monkeypatch.setattr(
        "personal_hub.lifecycle.acquire_single_instance",
        lambda _settings: lock,
    )
    lifecycle = HubLifecycle(HubSettings(data_dir=tmp_path))
    events: list[LifecycleEvent] = []
    lifecycle.subscribe(events.append)

    lifecycle.start()
    assert lifecycle.state == LifecycleState.RUNNING
    assert lifecycle.network_refresh_allowed

    lifecycle.handle_session_lock()
    assert not lifecycle.network_refresh_allowed
    lifecycle.handle_session_unlock()
    assert lifecycle.network_refresh_allowed

    lifecycle.prepare_for_sleep()
    assert not lifecycle.network_refresh_allowed
    lifecycle.resume_from_sleep()
    assert lifecycle.network_refresh_allowed

    lifecycle.set_network_paused(True)
    assert not lifecycle.network_refresh_allowed
    lifecycle.set_network_paused(False)
    assert lifecycle.network_refresh_allowed

    lifecycle.shutdown()
    assert lifecycle.state == LifecycleState.STOPPED
    assert lock.closed
    assert events == [
        LifecycleEvent.STARTED,
        LifecycleEvent.SESSION_LOCKED,
        LifecycleEvent.SESSION_UNLOCKED,
        LifecycleEvent.SLEEPING,
        LifecycleEvent.RESUMED,
        LifecycleEvent.NETWORK_PAUSED,
        LifecycleEvent.NETWORK_RESUMED,
        LifecycleEvent.STOPPING,
        LifecycleEvent.STOPPED,
    ]


def test_quiet_hour_settings_are_strict_and_side_effect_free(tmp_path: Path):
    settings = HubSettings(
        data_dir=tmp_path / "not-created",
        notification_quiet_hours_start="9:05",
        notification_quiet_hours_end="17:30",
    )

    assert settings.notification_quiet_hours_start == "09:05"
    assert settings.notification_quiet_hours_end == "17:30"
    assert not settings.data_dir.exists()
    with pytest.raises(ValueError, match="HH:MM"):
        HubSettings(data_dir=tmp_path / "other", notification_quiet_hours_start="25:00")


def test_startup_registration_uses_only_the_hub_identity(tmp_path: Path):
    registration = WindowsStartupRegistration(tmp_path / "PersonalHub.exe")

    assert registration.value_name == HUB_STARTUP_ENTRY_NAME
    assert "PersonalHub.exe" in registration.command
    assert "LocalAssistant" not in registration.command


def test_app_main_builds_locked_container_and_closes_it(monkeypatch, tmp_path: Path):
    calls: list[object] = []

    class _Container:
        def close(self) -> None:
            calls.append("closed")

    def build(settings: HubSettings, *, acquire_instance_lock: bool):
        calls.append(settings)
        calls.append(acquire_instance_lock)
        return _Container()

    monkeypatch.setattr(app, "build_hub_container", build)
    monkeypatch.setattr(app, "_run_desktop", lambda _container: 7)

    assert app.main(["--data-dir", str(tmp_path / "hub")]) == 7
    assert isinstance(calls[0], HubSettings)
    assert calls[1:] == [True, "closed"]
