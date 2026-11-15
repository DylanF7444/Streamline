"""Deterministic encrypted cross-device synchronization coverage."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from personal_hub.bootstrap import build_hub_container
from personal_hub.calendar.local import ImportedCalendarEvent
from personal_hub.config import HubSettings
from personal_hub.feeds.contracts import FeedEntry, FeedSyncState
from personal_hub.sync import (
    FileSyncEngine,
    SyncBundleError,
    SyncError,
    SyncPassphraseError,
    SyncRecord,
    SyncSnapshot,
    SyncTombstone,
    decrypt_snapshot,
    encrypt_snapshot,
    merge_snapshots,
)
from personal_hub.sync.crypto import decrypt_provider_relay, encrypt_provider_relay
from personal_hub.sync.relay_contracts import ProviderRelaySnapshot

PASSPHRASE = "correct horse battery staple"
STAMP = datetime(2026, 1, 1, tzinfo=UTC)


def _snapshot(
    device_id: str,
    *,
    title: str = "Shared task",
    updated_at: datetime = STAMP,
    tombstones: tuple[SyncTombstone, ...] = (),
) -> SyncSnapshot:
    return SyncSnapshot(
        profile_id="profile-1",
        device_id=device_id,
        generated_at=updated_at,
        records=(
            SyncRecord(
                collection="tasks",
                record_id="task-1",
                updated_at=updated_at,
                payload={"title": title},
            ),
        ),
        tombstones=tombstones,
    )


def test_sync_bundle_is_authenticated_and_contains_no_plaintext_payload() -> None:
    snapshot = _snapshot("a" * 32, title="Private task text")
    # Snapshot device IDs are intentionally constrained to real shard-safe IDs.
    snapshot = snapshot.model_copy(update={"device_id": "a" * 32})
    bundle = encrypt_snapshot(snapshot, PASSPHRASE)

    assert b"Private task text" not in bundle
    assert decrypt_snapshot(bundle, PASSPHRASE) == snapshot
    with pytest.raises(SyncPassphraseError):
        decrypt_snapshot(bundle, "wrong passphrase")
    with pytest.raises(SyncBundleError):
        decrypt_snapshot(bundle[:-1] + bytes([bundle[-1] ^ 1]), PASSPHRASE)


def test_sync_merge_is_deterministic_and_tombstones_win_on_ties() -> None:
    first = _snapshot("a" * 32, title="first")
    second = _snapshot("b" * 32, title="second")
    merged, conflicts = merge_snapshots(
        (first, second),
        profile_id="profile-1",
        device_id="a" * 32,
        generated_at=STAMP,
    )

    assert conflicts == 1
    assert merged.records[0].payload["title"] == "second"

    tombstone = SyncTombstone(
        collection="tasks",
        record_id="task-1",
        deleted_at=STAMP,
    )
    deleted, _ = merge_snapshots(
        (first, _snapshot("b" * 32, tombstones=(tombstone,))),
        profile_id="profile-1",
        device_id="a" * 32,
        generated_at=STAMP,
    )
    assert deleted.records == ()
    assert deleted.tombstones == (tombstone,)


def test_file_sync_engine_publishes_one_encrypted_shard_per_device(tmp_path: Path) -> None:
    endpoint = tmp_path / "shared"
    local = _snapshot("a" * 32)
    engine = FileSyncEngine(endpoint, device_id="a" * 32)
    result = engine.synchronize(local, PASSPHRASE, now=STAMP)

    assert result.record_count == 1
    assert result.profile_id == "profile-1"
    assert engine.shard_path.is_file()
    assert list((endpoint / ".personal-hub-sync").glob("*.sync")) == [engine.shard_path]
    assert decrypt_snapshot(engine.shard_path.read_bytes(), PASSPHRASE).records == local.records


def test_controller_sync_round_trip_settings_and_deletion(tmp_path: Path) -> None:
    endpoint = tmp_path / "shared"
    first = build_hub_container(HubSettings(data_dir=tmp_path / "first"))
    second = build_hub_container(HubSettings(data_dir=tmp_path / "second"))
    try:
        first.controller.configure_sync(endpoint)
        first.controller.update_settings(default_timezone="America/Chicago")
        first_setting_timestamp = first.controller.settings_repository.all_with_timestamps()[
            "default_timezone"
        ][1]
        task = first.controller.create_task(
            title="Travel checklist",
            created_at=STAMP,
            updated_at=STAMP,
        )
        first.controller.sync_now(PASSPHRASE, now=STAMP)

        second.controller.configure_sync(endpoint)
        second.controller.sync_now(PASSPHRASE, now=STAMP.replace(hour=1))
        assert second.controller.settings.default_timezone == "America/Chicago"
        assert (
            second.controller.settings_repository.all_with_timestamps()["default_timezone"][1]
            == first_setting_timestamp
        )
        assert second.controller.get_task(str(task.task_id)) is not None

        first.controller.task_repository.delete(task.task_id)
        first.controller.sync_now(PASSPHRASE, now=STAMP.replace(hour=2))
        second.controller.sync_now(PASSPHRASE, now=STAMP.replace(hour=3))
        assert second.controller.get_task(str(task.task_id)) is None
    finally:
        first.close()
        second.close()


def test_controller_rejects_a_different_profile_in_the_shared_folder(tmp_path: Path) -> None:
    endpoint = tmp_path / "shared"
    first = build_hub_container(HubSettings(data_dir=tmp_path / "first"))
    second = build_hub_container(HubSettings(data_dir=tmp_path / "second"))
    try:
        first.controller.configure_sync(endpoint)
        first.controller.sync_now(PASSPHRASE, now=STAMP)
        second.controller.configure_sync(endpoint)
        second.controller.settings_repository.set("sync.profile_id", "different-profile")
        with pytest.raises(SyncError):
            second.controller.sync_now(PASSPHRASE, now=STAMP.replace(hour=1))
    finally:
        first.close()
        second.close()


def test_controller_syncs_local_definitions_without_provider_state(tmp_path: Path) -> None:
    endpoint = tmp_path / "shared"
    first = build_hub_container(HubSettings(data_dir=tmp_path / "first"))
    second = build_hub_container(HubSettings(data_dir=tmp_path / "second"))
    import_id = uuid4()
    try:
        first.controller.configure_sync(endpoint)
        subscription = first.controller.create_feed_subscription(
            title="Example feed",
            feed_url="https://example.com/feed.xml",
        )
        watch = first.controller.create_watch(
            name="Example page",
            kind="page_text",
            source_url="https://example.com/",
            condition={"kind": "changed"},
            now=STAMP,
        )
        event = ImportedCalendarEvent(
            import_id=import_id,
            stable_key="event-1",
            title="Imported appointment",
            starts_at=STAMP + timedelta(hours=2),
            ends_at=STAMP + timedelta(hours=3),
            created_at=STAMP,
            updated_at=STAMP,
        )
        first.controller.calendar_import_repository.replace_import(
            name="Local calendar",
            source_format="text",
            events=(event,),
            import_id=import_id,
            now=STAMP,
        )
        first.controller.sync_now(PASSPHRASE, now=STAMP)

        second.controller.configure_sync(endpoint)
        second.controller.sync_now(PASSPHRASE, now=STAMP.replace(hour=1))

        assert second.controller.list_feed_subscriptions()[0] == subscription
        assert second.controller.watch_repository.get(watch.watch_id) == watch
        assert second.controller.calendar_import_repository.get(import_id) is not None
        assert second.controller.calendar_import_repository.list_events(import_id) == [event]
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("passphrase", ["", "short", "x" * 4097])
def test_invalid_passphrase_is_reported_before_applying_data(tmp_path, passphrase):
    applied = []
    engine = FileSyncEngine(tmp_path / "shared", device_id="a" * 32)
    with pytest.raises(SyncPassphraseError):
        engine.synchronize(_snapshot("a" * 32), passphrase, apply=applied.append)
    assert applied == []
    assert not engine.shard_path.exists()

    container = build_hub_container(HubSettings(data_dir=tmp_path / "hub"))
    try:
        container.controller.configure_sync(tmp_path / "shared")
        with pytest.raises(SyncError, match="passphrase") as caught:
            container.controller.sync_now(passphrase)
        assert "Hub validation" not in str(caught.value)
        assert container.controller.sync_status().last_error == str(caught.value)
        assert not container.controller.sync_status().profile_paired
    finally:
        container.close()


def test_wrong_passphrase_preserves_local_data_and_reports_authentication_error(tmp_path):
    container = build_hub_container(HubSettings(data_dir=tmp_path / "hub"))
    try:
        container.controller.configure_sync(tmp_path / "shared")
        task = container.controller.create_task(title="Keep my task")
        container.controller.sync_now(PASSPHRASE)
        shard = next((tmp_path / "shared" / ".personal-hub-sync").glob("*.sync"))
        original = shard.read_bytes()
        with pytest.raises(SyncError, match="passphrase was rejected"):
            container.controller.sync_now("wrong but long enough")
        assert container.controller.get_task(str(task.task_id)) == task
        assert shard.read_bytes() == original
        assert "passphrase was rejected" in container.controller.sync_status().last_error
    finally:
        container.close()


def test_none_cannot_bypass_the_default_encryption_setting(tmp_path):
    container = build_hub_container(HubSettings(data_dir=tmp_path / "hub"))
    try:
        container.controller.configure_sync(tmp_path / "shared")
        with pytest.raises(SyncError, match="passphrase is required"):
            container.controller.sync_now(None)
        assert not list((tmp_path / "shared").rglob("*.sync"))
        assert container.controller.sync_status().passphrase_required
    finally:
        container.close()


def test_unencrypted_sync_requires_explicit_mode_and_retains_schema_validation():
    snapshot = _snapshot("a" * 32, title="Shared task with café")
    bundle = encrypt_snapshot(snapshot, None)
    assert b"Shared task" in bundle
    assert decrypt_snapshot(bundle, None) == snapshot
    with pytest.raises(SyncBundleError, match="uses no passphrase"):
        decrypt_snapshot(bundle, PASSPHRASE)
    with pytest.raises(SyncBundleError, match="protection does not match"):
        decrypt_snapshot(encrypt_snapshot(snapshot, PASSPHRASE), None)
    envelope = json.loads(bundle)
    envelope["payload"]["records"][0]["collection"] = "credentials"
    with pytest.raises(SyncBundleError, match="snapshot is invalid"):
        decrypt_snapshot(json.dumps(envelope).encode(), None)


@pytest.mark.parametrize("passphrase", [None, PASSPHRASE])
def test_relay_bundle_round_trip_and_format_separation(passphrase):
    relay = ProviderRelaySnapshot(
        profile_id="profile-1",
        collector_device_id="a" * 32,
        generated_at=STAMP,
        expires_at=STAMP + timedelta(days=1),
    )
    bundle = encrypt_provider_relay(relay, passphrase)
    assert decrypt_provider_relay(bundle, passphrase) == relay
    with pytest.raises(SyncBundleError):
        decrypt_snapshot(bundle, passphrase)
    with pytest.raises(SyncBundleError):
        decrypt_provider_relay(encrypt_snapshot(_snapshot("a" * 32), passphrase), passphrase)
    with pytest.raises(SyncBundleError):
        decrypt_provider_relay(bundle, PASSPHRASE if passphrase is None else None)


def test_no_passphrase_sync_round_trip_relay_deletion_and_saved_choice(tmp_path):
    endpoint = tmp_path / "shared"
    first = build_hub_container(HubSettings(data_dir=tmp_path / "first"))
    second = build_hub_container(HubSettings(data_dir=tmp_path / "second"))
    try:
        assert first.controller.sync_status().passphrase_required
        for container, role in ((first, "collector"), (second, "consumer")):
            container.controller.configure_sync(endpoint)
            container.controller.set_sync_passphrase_required(False)
            container.controller.set_provider_relay_role(role)
        task = first.controller.create_task(title="No passphrase task")
        subscription = first.controller.create_feed_subscription(
            title="Shared news", feed_url="https://example.com/feed.xml"
        )
        timestamp = datetime.now(UTC)
        entry = FeedEntry(
            entry_id="shared-news-item",
            subscription_id=subscription.subscription_id,
            title="Shared news item",
            url="https://example.com/news",
            first_seen_at=timestamp,
        )
        first.controller.feed_entry_repository.add(entry)
        first.controller.feed_subscription_repository.save_sync_state(
            FeedSyncState(
                subscription_id=subscription.subscription_id,
                state="ready",
                last_success_at=timestamp,
            )
        )
        first.controller.sync_now()
        second.controller.sync_now()
        assert second.controller.get_task(str(task.task_id)) == task
        assert second.controller.feed_entry_repository.list(
            subscription_id=subscription.subscription_id
        ) == [entry]
        assert second.controller.sync_status().relay_item_count == 1
        assert second.controller.sync_status().relay_last_sync_at is not None
        assert second.controller.sync_status().last_error is None
        first.controller.task_repository.delete(task.task_id)
        first.controller.sync_now()
        second.controller.sync_now()
        assert second.controller.get_task(str(task.task_id)) is None
        first.controller.set_sync_passphrase_required(True)
        with pytest.raises(SyncError, match="uses no passphrase"):
            first.controller.sync_now(PASSPHRASE)
        first.controller.set_sync_passphrase_required(False)
    finally:
        first.close()
        second.close()
    reopened = build_hub_container(HubSettings(data_dir=tmp_path / "first"))
    try:
        assert not reopened.controller.sync_status().passphrase_required
        reopened.controller.sync_now()
    finally:
        reopened.close()
