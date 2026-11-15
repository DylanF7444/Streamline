"""Deterministic weekly facts, clipping, source selection and record lifecycle."""

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from personal_hub.browser_time.contracts import BrowserTimeSession
from personal_hub.config import HubSettings
from personal_hub.controller import HubController
from personal_hub.logging.contracts import ActivityEntry, ReflectionEntry
from personal_hub.logging.weekly_review import week_bounds
from personal_hub.metrics.contracts import (
    Measurement,
    MeasurementBatch,
    MetricDefinition,
    MetricSource,
)
from personal_hub.screen_time.contracts import ScreenTimeSession


@pytest.fixture
def hub(tmp_path):
    controller = HubController(
        settings=HubSettings(data_dir=tmp_path / "hub", default_timezone="America/Chicago")
    )
    yield controller
    controller.close()


@pytest.mark.parametrize(
    "day,start,end",
    [
        (date(2026, 11, 13), date(2026, 11, 9), date(2026, 11, 15)),
        (date(2027, 1, 1), date(2026, 12, 28), date(2027, 1, 3)),
        (date(2028, 2, 29), date(2028, 2, 28), date(2028, 3, 5)),
        (date(2026, 11, 15), date(2026, 11, 9), date(2026, 11, 15)),
    ],
)
def test_calendar_weeks(day, start, end):
    assert week_bounds(day) == (start, end)


def test_invalid_week_and_detail_bounds(hub):
    with pytest.raises(ValueError):
        hub.weekly_review(date.max)
    for limit in (0, 10001):
        with pytest.raises(ValueError):
            hub.weekly_review(date(2026, 11, 13), limit=limit)


def test_counts_and_durations_cover_history_beyond_display_limit(hub):
    start = datetime(2026, 11, 9, 6, tzinfo=UTC)
    for index in range(205):
        hub.activity_repository.add(
            ActivityEntry(
                description=f"Progress {index}",
                list_name="Writing",
                occurred_at=start,
                duration_minutes=2 if index % 2 else 1,
                duration_source="timer" if index % 2 else "entered",
            )
        )
    task = hub.create_task(title="Historical title", created_at=start, updated_at=start)
    hub.complete_task(str(task.task_id), completed_at=start)
    hub.update_task(str(task.task_id), title="Changed title")
    review = hub.weekly_review(date(2026, 11, 13), limit=1)
    assert len(review.entries) == 1
    assert review.facts["manual"] == 205 and review.facts["task_completion"] == 1
    assert review.facts["durations"] == {"entered": 103, "timer": 204}
    assert review.facts["lists"]["Writing"] == 205
    assert (review.days[0].completed, review.days[0].manual) == (1, 205)
    assert review.days[0].timed_minutes == 204
    assert all(day.app_seconds is None and day.site_seconds is None for day in review.days)
    assert all(day.entered_minutes is None for day in review.days[1:])
    hub.undo_task(str(task.task_id), at=start + timedelta(days=1))
    assert hub.weekly_review(date(2026, 11, 13)).facts["task_completion"] == 0


def test_dst_week_clips_usage_and_activity_to_local_midnights(hub):
    intervals = [
        (datetime(2026, 10, 26, 4, 59, tzinfo=UTC), datetime(2026, 10, 26, 5, 1, tzinfo=UTC)),
        (datetime(2026, 11, 1, 6, 30, tzinfo=UTC), datetime(2026, 11, 1, 8, 30, tzinfo=UTC)),
        (datetime(2026, 11, 2, 5, 59, tzinfo=UTC), datetime(2026, 11, 2, 6, 1, tzinfo=UTC)),
    ]
    for start, end in intervals:
        hub.screen_time_repository.save(ScreenTimeSession(app="editor.exe", start=start, end=end))
        hub.browser_time_repository.save(
            BrowserTimeSession(browser="brave", domain="example.com", start=start, end=end)
        )
    for at in (datetime(2026, 11, 2, 5, 59, tzinfo=UTC), datetime(2026, 11, 2, 6, tzinfo=UTC)):
        hub.log_progress(description="Boundary", occurred_at=at)
    review = hub.weekly_review(date(2026, 11, 1))
    assert (review.start, review.end) == (date(2026, 10, 26), date(2026, 11, 1))
    assert review.apps == {"editor.exe": 7320}
    assert review.sites == {"example.com": 7320}
    assert review.days[0].app_seconds == 60
    assert review.days[-1].app_seconds == 7260
    assert review.days[-1].manual == review.facts["manual"] == 1
    # Updating display timezone changes the week boundaries, not the stored intervals.
    hub.update_settings(default_timezone="UTC")
    assert hub.weekly_review(date(2026, 11, 1)).apps["editor.exe"] == 7320
    assert hub.weekly_review(date(2026, 11, 1)).facts["manual"] == 0


def test_metrics_use_primary_source_aggregation_and_missing_is_not_zero(hub):
    repo = hub.metrics_repository
    definitions = [
        MetricDefinition(metric_id="counted", name="Count", unit="count", aggregation="sum"),
        MetricDefinition(
            metric_id="temperature", name="Temperature", unit="celsius", aggregation="mean"
        ),
        MetricDefinition(metric_id="missing", name="Missing", unit="count", aggregation="latest"),
    ]
    for definition in definitions:
        repo.define(definition)
    ids = tuple(d.metric_id for d in definitions)
    for source_id in ("primary", "other"):
        repo.enroll(MetricSource(source_id=source_id, name=source_id, metric_ids=ids))
        repo.ingest(
            MeasurementBatch(
                source_id=source_id,
                records=tuple(
                    Measurement(
                        record_id=f"{metric}-{index}",
                        metric_id=metric,
                        revision=1,
                        value=value,
                        unit="count" if metric == "counted" else "celsius",
                        start=datetime(2026, 11, 9 + index, 12, tzinfo=UTC),
                    )
                    for metric, values in (("counted", (0, 5)), ("temperature", (10, 20)))
                    for index, value in enumerate(
                        values if source_id == "primary" else (1000, 1000)
                    )
                ),
            )
        )
    hub.settings_repository.set("logging.weekly_metrics", [*ids, "deleted-definition"])
    review = hub.weekly_review(date(2026, 11, 13))
    values = {item.definition.metric_id: item.summary for item in review.metrics}
    assert values["counted"].value == 5 and values["counted"].count == 2
    assert values["temperature"].value == 15
    assert values["missing"].value is None and values["missing"].count == 0
    assert all(item.source_id == "primary" for item in values.values())
    repo.set_primary("counted", "other")
    assert (
        next(
            m
            for m in hub.weekly_review(date(2026, 11, 13)).metrics
            if m.definition.metric_id == "counted"
        ).summary.value
        == 2000
    )
    assert hub.weekly_review(date(2026, 11, 23)).metrics[0].summary.value is None


def test_weekly_reflections_use_existing_backup_export_retention_and_sync_boundaries(hub, tmp_path):
    day = date(2026, 11, 9)
    saved = hub.save_reflection(
        ReflectionEntry(display_date=day, title="Week review", body="Private week")
    )
    hub.save_reflection(ReflectionEntry(display_date=day + timedelta(days=6), body="Sunday"))
    hub.save_reflection(ReflectionEntry(display_date=day - timedelta(days=1), body="Previous week"))
    assert {r.body for r in hub.weekly_review(day).reflections} == {"Private week", "Sunday"}
    backup = hub.backup_database(tmp_path / "backup.db")
    exported = json.loads(hub.export_local_data(tmp_path / "export.json").read_text("utf-8"))
    assert any(r["body"] == "Private week" for r in exported["reflections"])
    snapshot = hub._build_sync_snapshot(now=datetime.now(UTC))
    assert not any(r.collection == "reflections" for r in snapshot.records)
    hub.set_reflection_sync(True)
    assert any(
        r.record_id == str(saved.reflection_id)
        for r in hub._build_sync_snapshot(now=datetime.now(UTC)).records
    )
    hub.delete_reflection(str(saved.reflection_id))
    assert not any(r.body == "Private week" for r in hub.weekly_review(day).reflections)
    hub.restore_database(backup)
    assert any(r.body == "Private week" for r in hub.weekly_review(day).reflections)
