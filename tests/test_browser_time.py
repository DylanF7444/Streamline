"""Offline browser protocol, consent, timing and local storage regression tests."""

import io
import json
import shutil
import struct
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from personal_hub.bootstrap import build_hub_container
from personal_hub.browser_time.bridge import BrowserTimeRuntime, read_json, receive, write_json
from personal_hub.browser_time.contracts import (
    BrowserSample,
    BrowserTimeSession,
    BrowserTimeSettings,
)
from personal_hub.browser_time.native_host import read_message, serve
from personal_hub.config import HubSettings
from personal_hub.screen_time.collector import Observation


@pytest.fixture
def hub(tmp_path):
    with build_hub_container(HubSettings(data_dir=tmp_path / "hub")) as container:
        yield container.controller


def test_hostname_boundary():
    assert BrowserSample(browser="chrome", domain="WWW.Example.COM.").domain == "www.example.com"
    for domain in (
        "https://example.com/private?q=secret",
        "example.com/path",
        "127.0.0.1",
        "localhost",
        "host.local",
        "a.internal",
        "example.com\n",
        "a..com",
        "-a.com",
    ):
        with pytest.raises(ValidationError):
            BrowserSample(browser="chrome", domain=domain)
    for extra in ({"url": "private"}, {"title": "private"}, {"duration": 9000}):
        with pytest.raises(ValidationError):
            BrowserSample(browser="chrome", **extra)
    with pytest.raises(ValidationError):
        BrowserTimeSettings(idle_seconds=0)


@pytest.mark.parametrize(
    "browser,executable", [("chrome", "chrome.exe"), ("edge", "msedge.exe"), ("brave", "brave.exe")]
)
def test_recording_requires_live_lease_and_new_samples(hub, browser, executable):
    runtime = BrowserTimeRuntime(
        hub.settings.data_dir, hub.browser_time_repository, BrowserTimeSettings()
    )
    root = runtime.root
    base = datetime(2026, 11, 10, tzinfo=UTC).timestamp()
    sample = {"browser": browser, "domain": "example.com"}
    with pytest.raises(FileNotFoundError):
        receive(root, sample, now=base)
    runtime.start()
    runtime.tick(Observation(executable), now=base, monotonic=0)
    for second in (5, 10, 15):
        assert receive(root, sample, now=base + second)["recording"]
        runtime.tick(Observation(executable), now=base + second, monotonic=second)
    assert hub.browser_time_repository.summary(date(2026, 11, 10), date(2026, 11, 10), "UTC") == {
        "example.com": 10
    }
    # The same mailbox observation cannot extend history a second time.
    runtime.tick(Observation(executable), now=base + 20, monotonic=20)
    runtime.tick(Observation(executable), now=base + 25, monotonic=25)
    assert (
        sum(
            hub.browser_time_repository.summary(
                date(2026, 11, 10), date(2026, 11, 10), "UTC"
            ).values()
        )
        == 10
    )
    assert not receive(root, sample, now=base + 100)["recording"]
    runtime.pause()
    assert not (root / "lease.json").exists()
    assert not (root / f"{browser}.json").exists()


def test_foreground_idle_exclusion_lock_and_sleep_gaps(hub):
    runtime = BrowserTimeRuntime(
        hub.settings.data_dir,
        hub.browser_time_repository,
        BrowserTimeSettings(excluded_domains=("blocked.com",)),
    )
    runtime.start()
    base = datetime(2026, 11, 10, tzinfo=UTC).timestamp()

    def tick(second, domain="example.com", app="chrome.exe", idle=0):
        runtime.tick(
            Observation(None), now=base + second - 1, monotonic=second - 1
        ) if second == 0 else None
        receive(runtime.root, {"browser": "chrome", "domain": domain}, now=base + second)
        runtime.tick(Observation(app, idle), now=base + second, monotonic=second)

    runtime.tick(Observation("chrome.exe"), now=base, monotonic=0)
    tick(5)
    tick(10)
    tick(15, app="editor.exe")
    tick(20)
    tick(25, idle=180)
    tick(30, domain="sub.blocked.com")
    tick(35)
    tick(3600)  # expired lease rejects the sample
    assert (
        sum(
            hub.browser_time_repository.summary(
                date(2026, 11, 10), date(2026, 11, 10), "UTC"
            ).values()
        )
        == 5
    )
    runtime.tick(Observation("chrome.exe"), locked=True)
    assert not runtime.running
    assert not (runtime.root / "lease.json").exists()


def test_generation_prevents_replay_after_delete_and_restart(hub):
    runtime = BrowserTimeRuntime(
        hub.settings.data_dir, hub.browser_time_repository, BrowserTimeSettings()
    )
    runtime.start()
    runtime.tick(Observation("chrome.exe"))
    receive(runtime.root, {"browser": "edge", "domain": "example.com"})
    old = read_json(runtime.root / "edge.json")
    runtime.pause()
    runtime.start()
    write_json(runtime.root / "edge.json", old)
    runtime.tick(Observation("msedge.exe"))
    assert runtime.recorder.app is None


def test_backup_restore_export_sync_and_migration(hub, tmp_path):
    start = datetime(2026, 11, 10, 23, 59, tzinfo=UTC)
    record = BrowserTimeSession(
        browser="edge", domain="example.com", start=start, end=start + timedelta(minutes=2)
    )
    hub.browser_time_repository.save(record)
    hub.settings_repository.set("browser_time.settings", {"excluded_domains": ["secret.com"]})
    assert hub.browser_time_repository.summary(start.date(), start.date(), "UTC") == {
        "example.com": 60
    }
    assert (
        hub.browser_time_repository.delete_range(start.date(), start.date(), "UTC", preview=True)
        == 1
    )
    backup = hub.backup_database(tmp_path / "backup.db")
    hub.export_local_data(tmp_path / "export.json")
    payload = json.loads((tmp_path / "export.json").read_text())
    assert payload["browser_time"][0]["domain"] == "example.com"
    assert "secret.com" not in hub._build_sync_snapshot(now=start).model_dump_json()
    assert "example.com" not in hub._build_sync_snapshot(now=start).model_dump_json()
    runtime = BrowserTimeRuntime(
        hub.settings.data_dir, hub.browser_time_repository, BrowserTimeSettings()
    )
    hub.browser_time_runtime = runtime
    runtime.start()
    hub.browser_time_repository.delete_range(start.date(), start.date(), "UTC")
    hub.restore_database(backup)
    assert not runtime.running
    assert hub.browser_time_repository.records()[0] == record
    with hub.database.engine.begin() as connection:
        connection.execute(text("DROP TABLE hub_browser_time_sessions"))
        connection.execute(text("UPDATE hub_schema_migrations SET version=18"))
    hub.database.migrate()
    assert hub.database.CURRENT_SCHEMA_VERSION == 19


def test_native_framing_and_rejection(hub):
    for data in (b"x", struct.pack("<I", 100000), struct.pack("<I", 4) + b"{}"):
        with pytest.raises(ValueError):
            read_message(io.BytesIO(data))
    assert read_message(io.BytesIO()) is None

    def framed(value):
        raw = json.dumps(value).encode()
        return struct.pack("<I", len(raw)) + raw

    runtime = BrowserTimeRuntime(
        hub.settings.data_dir, hub.browser_time_repository, BrowserTimeSettings()
    )
    runtime.start()
    runtime.tick(Observation("chrome.exe"))
    stream = io.BytesIO(
        framed({"browser": "chrome", "domain": "example.com", "url": "private"})
        + framed({"browser": "edge", "domain": "example.com"})
        + framed({"browser": "chrome", "domain": "example.com"})
    )
    output = io.BytesIO()
    serve(stream, output, runtime.root, ["chrome"])
    output.seek(0)
    assert read_message(output) == {"recording": False}
    assert read_message(output) == {"recording": False}
    response = read_message(output)
    assert response["recording"] is True
    assert response["state"] == "waiting"
    assert response["usage"]["total_seconds"] == 0
    assert "private" not in (runtime.root / "chrome.json").read_text()


def test_javascript_extension_offline():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for browser extension state-machine tests")
    subprocess.run(
        [node, "--test", str(Path(__file__).with_name("browser_time_extension.test.cjs"))],
        check=True,
        capture_output=True,
        text=True,
    )


def test_registration_allowlist_shared_ids_and_disconnect(tmp_path, monkeypatch):
    import sys
    from contextlib import nullcontext
    from types import SimpleNamespace

    from personal_hub.browser_time import setup

    registry = {}
    fake = SimpleNamespace(
        HKEY_CURRENT_USER=1,
        REG_SZ=1,
        CreateKey=lambda root, key: nullcontext(key),
        SetValueEx=lambda key, name, zero, kind, value: registry.update({key: value}),
        DeleteKey=lambda root, key: registry.pop(key, None),
    )
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(setup, "default_personal_hub_data_dir", lambda: tmp_path / "config")
    monkeypatch.setattr(setup.sys, "platform", "win32")
    helper = tmp_path / "PersonalHubBrowserHost.exe"
    helper.touch()
    for browser in ("chrome", "edge", "brave"):
        setup.register(browser, "a" * 32, helper, tmp_path / "hub")
    config = read_json(tmp_path / "config" / "browser-native-host.json")
    assert config["origins"]["chrome-extension://" + "a" * 32 + "/"] == ["brave", "chrome", "edge"]
    assert len(registry) == 3
    assert setup.REGISTRY["brave"] + "\\" + setup.HOST_NAME in registry
    with pytest.raises(ValueError):
        setup.register("chrome", "https://untrusted", helper, tmp_path / "hub")
    setup.unregister()
    assert not registry
    assert not (tmp_path / "config" / "browser-native-host.json").exists()


def test_complete_summary_and_dst(hub):
    from zoneinfo import ZoneInfo

    day = date(2026, 11, 1)
    start = datetime(2026, 11, 1, tzinfo=ZoneInfo("America/Chicago")).astimezone(UTC)
    repo = hub.browser_time_repository
    for hour in range(25):
        repo.save(
            BrowserTimeSession(
                browser="chrome",
                domain="example.com",
                start=start + timedelta(hours=hour),
                end=start + timedelta(hours=hour + 1),
            )
        )
    assert repo.summary(day, day, "America/Chicago") == {"example.com": 25 * 3600}
    # Totals are independent of the bounded interval query.
    assert len(repo.records(day, day, "America/Chicago", limit=1)) == 1
    assert repo.delete_range(day, day, "America/Chicago", preview=True) == 25
    assert repo.delete_range(day, day, "America/Chicago") == 25


def test_live_mailbox_exclusion_and_long_hostname(hub):
    runtime = BrowserTimeRuntime(
        hub.settings.data_dir,
        hub.browser_time_repository,
        BrowserTimeSettings(excluded_domains=("example.com",)),
    )
    runtime.start()
    now = datetime.now(UTC).timestamp()
    runtime.tick(Observation("chrome.exe"), now=now, monotonic=0)
    receive(runtime.root, {"browser": "chrome", "domain": "private.example.com"}, now=now)
    assert read_json(runtime.root / "chrome.json")["sample"]["domain"] is None
    long_domain = "a" * 60 + "." + "b" * 60 + "." + "c" * 60 + ".com"
    for second in (5, 10):
        receive(runtime.root, {"browser": "chrome", "domain": long_domain}, now=now + second)
        runtime.tick(Observation("chrome.exe"), now=now + second, monotonic=second)
    assert hub.browser_time_repository.records()[0].domain == long_domain
    runtime.tick(Observation("editor.exe"), now=now + 15, monotonic=15)
    assert not receive(
        runtime.root, {"browser": "chrome", "domain": "private.other.com"}, now=now + 16
    )["recording"]
    assert "private.other.com" not in (runtime.root / "chrome.json").read_text()


def test_disconnect_revokes_existing_native_connection(hub):
    message = json.dumps({"browser": "chrome", "domain": "example.com"}).encode()
    output = io.BytesIO()
    serve(
        io.BytesIO(struct.pack("<I", len(message)) + message),
        output,
        hub.settings.data_dir / "browser-time-live",
        ["chrome"],
        authorized=lambda: False,
    )
    assert not output.getvalue()
    assert not (hub.settings.data_dir / "browser-time-live" / "chrome.json").exists()


def test_brave_identity_requires_matching_foreground_and_survives_export_restore(hub, tmp_path):
    runtime = BrowserTimeRuntime(
        hub.settings.data_dir, hub.browser_time_repository, BrowserTimeSettings()
    )
    runtime.start()
    now = datetime(2026, 11, 12, tzinfo=UTC).timestamp()
    runtime.tick(Observation("brave.exe"), now=now, monotonic=0)
    for second in (5, 10):
        receive(runtime.root, {"browser": "brave", "domain": "example.com"}, now=now + second)
        runtime.tick(Observation("chrome.exe"), now=now + second, monotonic=second)
    assert not hub.browser_time_repository.records()
    for second in (15, 20):
        receive(runtime.root, {"browser": "brave", "domain": "example.com"}, now=now + second)
        runtime.tick(Observation("brave.exe"), now=now + second, monotonic=second)
    record = hub.browser_time_repository.records()[0]
    assert record.browser == "brave"
    backup = hub.backup_database(tmp_path / "brave.db")
    hub.export_local_data(tmp_path / "brave.json")
    assert (
        json.loads((tmp_path / "brave.json").read_text())["browser_time"][0]["browser"] == "brave"
    )
    hub.browser_time_repository.delete_range(record.start.date(), record.end.date(), "UTC")
    hub.restore_database(backup)
    assert hub.browser_time_repository.records()[0] == record
    runtime.pause()
    assert not (runtime.root / "brave.json").exists()


def test_brave_only_setup_registers_chromium_compatibility_key(tmp_path, monkeypatch):
    import sys
    from contextlib import nullcontext
    from types import SimpleNamespace

    from personal_hub.browser_time import setup

    registry = {}
    fake = SimpleNamespace(
        HKEY_CURRENT_USER=1,
        REG_SZ=1,
        CreateKey=lambda root, key: nullcontext(key),
        SetValueEx=lambda key, name, zero, kind, value: registry.update({key: value}),
        DeleteKey=lambda root, key: registry.pop(key, None),
    )
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(setup, "default_personal_hub_data_dir", lambda: tmp_path / "config")
    monkeypatch.setattr(setup.sys, "platform", "win32")
    helper = tmp_path / "PersonalHubBrowserHost.exe"
    helper.touch()
    setup.register("brave", "a" * 32, helper, tmp_path / "hub")
    chrome = setup.REGISTRY["chrome"] + "\\" + setup.HOST_NAME
    brave = setup.REGISTRY["brave"] + "\\" + setup.HOST_NAME
    assert registry[chrome] == registry[brave]
    config = read_json(tmp_path / "config" / "browser-native-host.json")
    assert list(config["origins"].values()) == [["brave"]]
    setup.unregister()
    assert not registry


def test_runtime_explains_missing_extension_without_exposing_sites(hub):
    runtime = BrowserTimeRuntime(
        hub.settings.data_dir, hub.browser_time_repository, BrowserTimeSettings()
    )
    runtime.start()
    runtime.tick(Observation("brave.exe"))
    assert "Waiting for browser extension" in runtime.recorder.status
    runtime.tick(Observation("brave.exe", 181))
    assert runtime.recorder.status == "Paused — no recent input"
    runtime.tick(Observation("editor.exe"))
    assert "focus Chrome, Edge or Brave" in runtime.recorder.status


def test_indicator_confirms_only_saved_matching_fresh_site(hub):
    runtime = BrowserTimeRuntime(
        hub.settings.data_dir, hub.browser_time_repository, BrowserTimeSettings()
    )
    runtime.start()
    base = datetime(2026, 11, 10, tzinfo=UTC).timestamp()
    sample = {"browser": "chrome", "domain": "example.com"}
    runtime.tick(Observation("chrome.exe"), now=base, monotonic=0)
    assert receive(runtime.root, sample, now=base)["state"] == "waiting"
    runtime.tick(Observation("chrome.exe"), now=base + 5, monotonic=5)
    assert receive(runtime.root, sample, now=base + 5)["state"] == "waiting"
    runtime.tick(Observation("chrome.exe"), now=base + 10, monotonic=10)
    assert receive(runtime.root, sample, now=base + 10)["state"] == "recording"
    assert (
        receive(runtime.root, {**sample, "domain": "other.org"}, now=base + 10)["state"]
        == "waiting"
    )
    assert receive(runtime.root, {**sample, "domain": None}, now=base + 10)["state"] == "ineligible"
    assert receive(runtime.root, sample, now=base + 21)["state"] != "recording"
    runtime.tick(Observation("editor.exe"), now=base + 22, monotonic=22)
    assert receive(runtime.root, sample, now=base + 22)["state"] == "focus"
    runtime.tick(Observation("chrome.exe", 181), now=base + 23, monotonic=23)
    assert receive(runtime.root, sample, now=base + 23)["state"] == "idle"
    runtime.pause()
    assert not (runtime.root / "lease.json").exists()


def test_toolbar_daily_totals_are_bounded_complete_and_use_hub_local_day(hub):
    runtime = BrowserTimeRuntime(
        hub.settings.data_dir, hub.browser_time_repository, BrowserTimeSettings()
    )
    runtime.timezone = "America/Chicago"
    # Midnight at the start of the DST fallback day is 05:00 UTC.
    midnight = datetime(2026, 11, 1, 5, tzinfo=UTC)
    repo = hub.browser_time_repository
    for i in range(10):
        repo.save(
            BrowserTimeSession(
                browser="chrome" if i % 2 else "brave",
                domain=f"site{i}.com",
                start=midnight - timedelta(seconds=30),
                end=midnight + timedelta(seconds=60 * (i + 1)),
            )
        )
    repo.save(
        BrowserTimeSession(
            browser="edge",
            domain="site0.com",
            start=midnight + timedelta(hours=24),
            end=midnight + timedelta(hours=24, minutes=1),
        )
    )
    usage = runtime.daily_usage(
        (midnight + timedelta(hours=24, minutes=30)).timestamp(), "brave|site0.com"
    )
    assert usage["date"] == "2026-11-01"
    assert usage["timezone"] == "America/Chicago"
    assert len(usage["sites"]) == 8
    assert usage["total_seconds"] == 3360
    assert usage["other_seconds"] == 240
    assert usage["current"] == {"domain": "site0.com", "seconds": 120}
    assert sum(site["seconds"] for site in usage["sites"]) + usage["other_seconds"] == 3360
    next_day = runtime.daily_usage((midnight + timedelta(hours=25)).timestamp(), "brave|site0.com")
    assert next_day["date"] == "2026-11-02"
    assert next_day["total_seconds"] == 0
    assert next_day["current"]["seconds"] == 0


def test_toolbar_usage_follows_saved_checkpoints_and_clears_for_privacy(hub):
    runtime = BrowserTimeRuntime(
        hub.settings.data_dir, hub.browser_time_repository, BrowserTimeSettings()
    )
    runtime.start()
    base = datetime(2026, 11, 10, tzinfo=UTC).timestamp()
    sample = {"browser": "chrome", "domain": "example.com"}
    for second in (0, 5, 10):
        runtime.tick(Observation("chrome.exe"), now=base + second, monotonic=second)
        response = receive(runtime.root, sample, now=base + second)
    assert response["usage"]["current"] == {"domain": "example.com", "seconds": 5}
    assert response["usage"]["total_seconds"] == 5
    other = receive(runtime.root, {**sample, "domain": "other.org"}, now=base + 10)
    assert other["usage"]["current"] is None
    assert "usage" not in receive(runtime.root, {**sample, "domain": None}, now=base + 10)
    runtime.set_private(True)
    assert "usage" not in read_json(runtime.root / "lease.json")
    assert "usage" not in receive(runtime.root, sample, now=base + 10)
    runtime.tick(Observation("chrome.exe"), now=base + 15, monotonic=15)
    assert runtime.running
    assert "usage" not in receive(runtime.root, sample, now=base + 15)
    runtime.set_private(False)
    runtime.tick(Observation("chrome.exe"), now=base + 20, monotonic=20)
    assert receive(runtime.root, sample, now=base + 20)["usage"]["total_seconds"] == 15
    runtime.tick(Observation("editor.exe"), now=base + 25, monotonic=25)
    assert "usage" not in receive(runtime.root, sample, now=base + 25)
    runtime.tick(Observation("chrome.exe", 181), now=base + 30, monotonic=30)
    assert "usage" not in receive(runtime.root, sample, now=base + 30)
    assert "usage" not in receive(runtime.root, sample, now=base + 43)


def test_excluded_site_never_receives_toolbar_usage(hub):
    runtime = BrowserTimeRuntime(
        hub.settings.data_dir,
        hub.browser_time_repository,
        BrowserTimeSettings(excluded_domains=("example.com",)),
    )
    runtime.start()
    runtime.tick(Observation("chrome.exe"))
    response = receive(runtime.root, {"browser": "chrome", "domain": "private.example.com"})
    assert response["state"] == "ineligible"
    assert "usage" not in response
