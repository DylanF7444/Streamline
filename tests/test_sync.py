"""Deterministic encrypted cross-device synchronization coverage."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from personal_hub.bootstrap import build_hub_container
from personal_hub.calendar.local import ImportedCalendarEvent
from personal_hub.config import HubSettings
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
