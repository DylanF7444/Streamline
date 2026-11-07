from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from personal_hub.metrics.contracts import (
    STEPS,
    Measurement,
    MeasurementBatch,
    MetricDefinition,
    MetricSource,
    Snapshot,
)
from personal_hub.metrics.files import (
    decrypt_snapshot,
    encrypt_snapshot,
    import_snapshot,
    parse_file,
)
from personal_hub.metrics.repository import MetricsRepository
from personal_hub.storage.database import HubDatabase


@pytest.fixture
def repo(tmp_path):
    database = HubDatabase(tmp_path / "metrics.db")
    database.migrate()
    repo = MetricsRepository(database)
    repo.enroll(MetricSource(source_id="phone", name="Phone", metric_ids=("steps",)))
    yield repo
    database.close()


def steps(value=100, revision=1, day=date(2026, 11, 1), timezone="America/Chicago"):
    return Measurement(
        record_id=f"steps:{timezone}:{day}",
        metric_id="steps",
        revision=revision,
        value=value,
        unit="count",
        start=datetime.combine(day, datetime.min.time(), ZoneInfo(timezone)),
        end=datetime.combine(day + timedelta(days=1), datetime.min.time(), ZoneInfo(timezone)),
        timezone=timezone,
        reporting_date=day,
    )


def batch(*records, source="phone"):
    return MeasurementBatch(source_id=source, records=records)


def test_steps_revision_retries_zero_and_missing(repo):
    record = steps()
    assert repo.ingest(batch(record))["accepted"] == 1
    assert repo.ingest(batch(record))["unchanged"] == 1
    repo.ingest(batch(steps(150, 2)))
    repo.ingest(batch(record))
    assert repo.summary("steps", record.reporting_date, record.reporting_date).value == 150
    repo.ingest(batch(steps(0, 3)))
    assert repo.summary("steps", record.reporting_date, record.reporting_date).value == 0
    assert repo.summary("steps", date(2020, 1, 1), date(2020, 1, 1)).value is None


def test_conflict_rolls_back_entire_batch(repo):
    repo.ingest(batch(steps()))
    with pytest.raises(ValueError):
        repo.ingest(batch(steps(day=date(2026, 11, 2)), steps(120)))
    assert len(repo.records()) == 1


def test_source_permissions_revocation_and_primary(repo):
    repo.enroll(MetricSource(source_id="watch", name="Watch", metric_ids=("steps",)))
    repo.ingest(batch(steps(100)))
    repo.ingest(batch(steps(200), source="watch"))
    day = date(2026, 11, 1)
    assert repo.summary("steps", day, day).value == 100
    repo.set_primary("steps", "watch")
    assert repo.summary("steps", day, day).value == 200
    repo.revoke("watch")
    with pytest.raises(ValueError):
        repo.ingest(batch(steps(300, 2), source="watch"))


def test_deletion_erases_payload_blocks_old_and_new_revisions(repo):
    repo.ingest(batch(steps()))
    assert repo.delete_history(source_id="phone") == 1
    assert repo.delete_history(source_id="phone", apply=True) == 1
    assert repo.ingest(batch(steps(200, 2)))["suppressed"] == 1
    assert repo.records() == []
    repo.restore_identity("phone", steps().record_id)
    assert repo.ingest(batch(steps()))["accepted"] == 1


def test_snapshot_authentication_atomicity_and_transport_dedup(repo):
    snapshot = Snapshot(
        source=repo.source("phone"), definitions=(STEPS,), batches=(batch(steps()),)
    )
    encrypted = encrypt_snapshot(snapshot, "a long test passphrase")
    assert b"phone" not in encrypted and b"steps" not in encrypted
    restored = decrypt_snapshot(encrypted, "a long test passphrase")
    import_snapshot(repo, restored)
    assert repo.ingest(batch(steps()))["unchanged"] == 1
    with pytest.raises(ValueError):
        decrypt_snapshot(encrypted, "another long passphrase")
    with pytest.raises(ValueError):
        decrypt_snapshot(encrypted[:-10], "a long test passphrase")


@pytest.mark.parametrize(
    "timezone,day,hours",
    [
        ("America/Chicago", date(2026, 11, 1), 25),
        ("America/Chicago", date(2026, 3, 8), 23),
        ("UTC", date(2026, 11, 1), 24),
    ],
)
def test_reporting_day_handles_dst(repo, timezone, day, hours):
    record = steps(timezone=timezone, day=day)
    assert (record.end - record.start).total_seconds() == hours * 3600
    repo.ingest(batch(record))
    assert repo.summary("steps", day, day).value == 100


def test_custom_metric_csv_mapping_and_repeated_file(repo):
    repo.define(MetricDefinition(metric_id="temperature", name="Temperature", unit="celsius"))
    repo.enroll(MetricSource(source_id="sensor", name="Sensor", metric_ids=("temperature",)))
    data = b"timestamp,temp\n2026-11-01T12:00:00Z,23.5\n"
    parsed = parse_file(
        data,
        source_id="sensor",
        csv_mapping={"start": "timestamp", "value": "temp"},
        metric_id="temperature",
        unit="celsius",
    )
    repo.ingest(parsed)
    assert repo.ingest(parsed)["unchanged"] == 1
    assert repo.summary("temperature", date(2026, 11, 1), date(2026, 11, 1)).value == 23.5


@pytest.mark.parametrize(
    "aggregation,expected", [("mean", 3), ("min", 2), ("max", 4), ("sum", 6), ("latest", 4)]
)
def test_numeric_aggregations(repo, aggregation, expected):
    definition = MetricDefinition(
        metric_id="custom", name="Custom", unit="units", aggregation=aggregation
    )
    repo.define(definition)
    repo.enroll(MetricSource(source_id="custom", name="Custom", metric_ids=("custom",)))
    records = tuple(
        Measurement(
            record_id=str(i),
            metric_id="custom",
            revision=1,
            value=v,
            unit="units",
            start=datetime(2026, 11, 1, i, tzinfo=UTC),
        )
        for i, v in enumerate((2, 4))
    )
    repo.ingest(batch(*records, source="custom"))
    assert repo.summary("custom", date(2026, 11, 1), date(2026, 11, 1)).value == expected


def test_session_and_backup_restore(repo, tmp_path):
    repo.define(
        MetricDefinition(
            metric_id="practice",
            name="Practice",
            unit="seconds",
            kind="session",
            aggregation="duration",
        )
    )
    repo.enroll(MetricSource(source_id="timer", name="Timer", metric_ids=("practice",)))
    repo.ingest(
        batch(
            Measurement(
                record_id="one",
                metric_id="practice",
                unit="seconds",
                revision=1,
                start=datetime(2026, 11, 1, tzinfo=UTC),
                end=datetime(2026, 11, 1, 0, 30, tzinfo=UTC),
            ),
            source="timer",
        )
    )
    backup = repo.database.backup(tmp_path / "backup.db")
    restored = HubDatabase(backup)
    restored.migrate()
    try:
        assert (
            MetricsRepository(restored)
            .summary("practice", date(2026, 11, 1), date(2026, 11, 1))
            .value
            == 1800
        )
    finally:
        restored.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"value": float("nan")},
        {"value": -1},
        {"revision": 0},
        {"start": datetime(2026, 11, 1)},
        {"record_id": "unstable"},
    ],
)
def test_reject_invalid_steps(changes):
    with pytest.raises(ValueError):
        Measurement.model_validate(steps().model_dump() | changes)


def test_step_source_timezone_cannot_silently_change(repo):
    repo.ingest(batch(steps()))
    with pytest.raises(ValueError, match="timezone"):
        repo.ingest(batch(steps(timezone="UTC")))
    assert len(repo.records()) == 1


def test_schema15_upgrade_adds_metrics_without_erasing_other_state(tmp_path):
    import sqlite3

    database = HubDatabase(tmp_path / "older.db")
    database.migrate()
    with sqlite3.connect(database.path) as connection:
        for table in ("hub_measurements", "hub_metric_sources", "hub_metric_definitions"):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("UPDATE hub_schema_migrations SET version=15")
    assert database._validate_database_file(database.path) == 15
    database.migrate()
    assert database._validate_database_file(database.path) == 16
    assert MetricsRepository(database).definitions() == [STEPS]
    database.close()


def test_permission_narrowing_blocks_future_readings(repo):
    repo.define(MetricDefinition(metric_id="custom", name="Custom", unit="units"))
    repo.update_permissions("phone", ("custom",))
    with pytest.raises(ValueError, match="cannot report"):
        repo.ingest(batch(steps()))


def test_shared_android_python_fixture_and_public_schema(repo):
    import json
    from pathlib import Path

    from personal_hub.metrics.schema import schema

    root = Path(__file__).resolve().parents[1]
    fixture = json.loads((root / "contracts/metrics-v1.fixture.json").read_text())
    snapshot = decrypt_snapshot(json.dumps(fixture["envelope"]).encode(), fixture["passphrase"])
    assert snapshot == Snapshot.model_validate_json(fixture["plaintext"])
    repo.enroll(snapshot.source)
    import_snapshot(repo, snapshot)
    assert (
        repo.summary("steps", date(2026, 11, 1), date(2026, 11, 1), snapshot.source.source_id).value
        == 1234
    )
    assert json.loads((root / "docs/metrics-v1.schema.json").read_text()) == schema()


def test_extreme_numeric_values_are_rejected_before_charting():
    with pytest.raises(ValueError):
        Measurement.model_validate(steps().model_dump() | {"value": 1e300})


def test_restoring_pre_metrics_backup_seeds_builtin_definition(tmp_path):
    import sqlite3

    from personal_hub.bootstrap import build_hub_container
    from personal_hub.config import HubSettings

    with build_hub_container(HubSettings(data_dir=tmp_path / "old")) as old:
        task = old.controller.create_task(title="Preserved task")
        backup = old.database.backup(tmp_path / "older.db")
    with sqlite3.connect(backup) as connection:
        for table in ("hub_measurements", "hub_metric_sources", "hub_metric_definitions"):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("UPDATE hub_schema_migrations SET version=15")
    with build_hub_container(HubSettings(data_dir=tmp_path / "root")) as root:
        root.controller.restore_database(backup)
        assert root.controller.metrics_repository.definitions() == [STEPS]
        assert root.controller.get_task(str(task.task_id)).title == "Preserved task"
