"""Deterministic foreground usage, gaps, dates and local data boundaries."""

import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from personal_hub.bootstrap import build_hub_container
from personal_hub.config import HubSettings
from personal_hub.screen_time.collector import Observation, ScreenTimeRecorder
from personal_hub.screen_time.contracts import ScreenTimeSession, ScreenTimeSettings
from personal_hub.screen_time.repository import ScreenTimeRepository
from personal_hub.storage.database import HubDatabase


@pytest.fixture
def repo(tmp_path):
    db = HubDatabase(tmp_path / "screen.db")
    db.migrate()
    yield ScreenTimeRepository(db)
    db.close()


def test_consent_checkpoints_switch_idle_and_exclusions(repo):
    recorder = ScreenTimeRecorder(repo, ScreenTimeSettings(excluded_apps=("secret.exe",)))
    start = datetime(2026, 11, 9, tzinfo=UTC)

    def sample(seconds, app="editor.exe", idle=0, locked=False):
        recorder.observe(
            Observation(app, idle),
            now=start + timedelta(seconds=seconds),
            monotonic=seconds,
            locked=locked,
        )

    sample(0)
    sample(5)
    assert not repo.records()
    recorder.running = True
    sample(10)
    sample(15)
    sample(20)
    assert len(repo.records()) == 1
    assert (repo.records()[0].end - repo.records()[0].start).total_seconds() == 10
    sample(25, "browser.exe")
    sample(30, "browser.exe")
    sample(35, "secret.exe")
    sample(40, idle=180)
    sample(45, locked=True)
    assert set(repo.summary(start.date(), start.date(), "UTC")) == {"editor.exe", "browser.exe"}
    assert sum(repo.summary(start.date(), start.date(), "UTC").values()) == 15
    recorder.pause()
    sample(50)
    assert len(repo.records()) == 2


def test_sleep_clock_changes_and_restart_never_extend_history(repo):
    start = datetime(2026, 11, 9, tzinfo=UTC)
    recorder = ScreenTimeRecorder(repo)
    recorder.running = True
    for wall, mono in [(0, 0), (5, 5), (3600, 3600), (3605, 3605), (7200, 3610)]:
        recorder.observe(
            Observation("editor.exe"), now=start + timedelta(seconds=wall), monotonic=mono
        )
    assert sum(repo.summary(start.date(), start.date(), "UTC").values()) == 10
    fresh = ScreenTimeRecorder(repo)
    assert not fresh.running


@pytest.mark.parametrize("day,hours", [(date(2026, 3, 8), 23), (date(2026, 11, 1), 25)])
def test_midnight_and_dst_clip_intervals(repo, day, hours):
    zone = ZoneInfo("America/Chicago")
    start = datetime.combine(day, datetime.min.time(), zone).astimezone(UTC)
    for offset in range(hours):
        repo.save(
            ScreenTimeSession(
                app="editor.exe",
                start=start + timedelta(hours=offset),
                end=start + timedelta(hours=offset + 1),
            )
        )
    assert repo.summary(day, day, "America/Chicago") == {"editor.exe": hours * 3600}


def test_summary_is_complete_beyond_detail_limit_and_deletion_preview(repo):
    start = datetime(2026, 11, 9, tzinfo=UTC)
    for i in range(210):
        repo.save(
            ScreenTimeSession(
                app="editor.exe",
                start=start + timedelta(seconds=i * 10),
                end=start + timedelta(seconds=i * 10 + 5),
            )
        )
    assert len(repo.records()) == 200
    assert repo.summary(start.date(), start.date(), "UTC") == {"editor.exe": 1050}
    assert repo.delete_range(start.date(), start.date(), "UTC", preview=True) == 210
    assert repo.records()
    assert repo.delete_range(start.date(), start.date(), "UTC") == 210
    assert not repo.records()


def test_backup_restore_export_and_no_shared_state(repo, tmp_path):
    with build_hub_container(HubSettings(data_dir=tmp_path / "hub")) as container:
        hub = container.controller
        start = datetime(2026, 11, 9, tzinfo=UTC)
        record = ScreenTimeSession(
            app="private-app.exe", start=start, end=start + timedelta(seconds=5)
        )
        hub.screen_time_repository.save(record)
        hub.settings_repository.set("screen_time.settings", {"start_on_open": True})
        backup = hub.backup_database(tmp_path / "backup.db")
        hub.export_local_data(tmp_path / "export.json")
        payload = json.loads((tmp_path / "export.json").read_text())
        assert payload["screen_time"][0]["app"] == record.app
        assert record.app not in hub._build_sync_snapshot(now=start).model_dump_json()
        hub.screen_time_repository.delete_range(start.date(), start.date(), "UTC")
        hub.restore_database(backup)
        assert hub.screen_time_repository.records()[0] == record
        assert (
            "screen_time.settings"
            not in __import__(
                "personal_hub.controller", fromlist=["_SYNCABLE_HUB_SETTINGS"]
            )._SYNCABLE_HUB_SETTINGS
        )


def test_validation_rejects_content_and_unbounded_settings():
    with pytest.raises(ValidationError):
        ScreenTimeSettings(excluded_apps=("C:/private.exe",))
    with pytest.raises(ValidationError):
        ScreenTimeSettings(idle_seconds=0)
    with pytest.raises(ValidationError):
        ScreenTimeSettings(rules={"C:/private.exe": {}})
    with pytest.raises(ValidationError):
        ScreenTimeSession(app="app.exe", start=datetime.now(), end=datetime.now())


def test_schema17_migration_and_backup_validation(repo, tmp_path):
    from sqlalchemy import text

    with repo.database.engine.begin() as connection:
        connection.execute(text("DROP TABLE hub_screen_time_sessions"))
        connection.execute(text("UPDATE hub_schema_migrations SET version=17"))
    repo.database.migrate()
    with repo.database.engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version FROM hub_schema_migrations"))
            == HubDatabase.CURRENT_SCHEMA_VERSION
        )
    repo.database.backup(tmp_path / "migrated.db")
