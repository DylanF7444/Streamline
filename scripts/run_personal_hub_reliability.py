"""Run deterministic bounded-growth evidence or a real-time Personal Hub soak."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import time
import tracemalloc
from datetime import UTC, datetime, timedelta
from pathlib import Path

from personal_hub.bootstrap import HubContainer, build_hub_container
from personal_hub.config import HubSettings
from personal_hub.feeds import FeedEntry
from personal_hub.messages import MessageQuery, MessageSummary
from personal_hub.watches import WatchObservation, WatchSyncState

START = datetime(2026, 1, 5, tzinfo=UTC)


def _settings(data_dir: Path) -> HubSettings:
    return HubSettings(
        data_dir=data_dir,
        default_timezone="UTC",
        feed_retention_days=1,
        feed_max_entries_per_subscription=20,
        watch_observation_retention_days=1,
        watch_max_observations_per_watch=20,
        message_summary_retention_days=7,
        message_max_summaries_per_source=20,
    )


def _prepare(container: HubContainer, now: datetime) -> None:
    if not container.controller.list_feed_subscriptions():
        container.controller.create_feed_subscription(
            title="Synthetic reliability feed",
            feed_url="https://reliability.example.test/feed.xml",
            refresh_minutes=15,
        )
    if not container.controller.list_watches():
        container.controller.create_watch(
            name="Synthetic reliability watch",
            kind="page_text",
            source_url="https://reliability.example.test/watch",
            condition={"kind": "changed"},
            refresh_minutes=30,
            cooldown_minutes=60,
            now=now,
        )
    if not container.controller.list_message_sources():
        container.controller.create_message_source(
            source_id="gmail-reliability",
            provider="gmail",
            account_id="synthetic-account",
            query=MessageQuery(folders=("INBOX",), limit=20),
            now=now,
        )


def _step(container: HubContainer, index: int, now: datetime) -> None:
    subscription = container.controller.list_feed_subscriptions()[0]
    for offset in range(4):
        identity = f"feed-{index:06d}-{offset}"
        container.feed_entry_repository.add(
            FeedEntry(
                entry_id=identity,
                subscription_id=subscription.subscription_id,
                title=f"Synthetic entry {index}-{offset}",
                url=f"https://reliability.example.test/entry/{identity}",
                published_at=now,
                first_seen_at=now,
            ),
            seen_at=now,
        )

    watch = container.controller.list_watches()[0]
    value = f"bounded-value-{index}"
    fingerprint = hashlib.sha256(value.encode()).hexdigest()
    container.watch_repository.persist_evaluation(
        WatchObservation(
            watch_id=watch.watch_id,
            checked_at=now,
            fingerprint=fingerprint,
            normalized_value=value,
        ),
        WatchSyncState(
            watch_id=watch.watch_id,
            state="ready",
            last_checked_at=now,
            last_success_at=now,
            baseline_fingerprint=fingerprint,
            baseline_value=value,
        ),
    )

    config = container.controller.list_message_sources()[0]
    summaries = tuple(
        MessageSummary(
            source_id=config.source_id,
            message_id=f"message-{index:06d}-{offset}",
            provider=config.provider,
            account_id=config.account_id,
            sender="Synthetic sender",
            title=f"Synthetic message {index}-{offset}",
            received_at=now,
            unread=True,
        )
        for offset in range(4)
    )
    container.message_repository.upsert_summaries(config, summaries, retained_at=now)


def _counts(container: HubContainer) -> dict[str, int]:
    _definitions, observations, alerts = container.watch_repository.counts()
    _sources, messages = container.message_repository.counts()
    return {
        "feed_entries": container.feed_entry_repository.count(),
        "watch_observations": observations,
        "watch_alerts": alerts,
        "message_summaries": messages,
    }


def _verify(container: HubContainer) -> tuple[dict[str, int], bool]:
    counts = _counts(container)
    bounded = (
        counts["feed_entries"] <= 20
        and counts["watch_observations"] <= 20
        and counts["message_summaries"] <= 20
        and counts["watch_alerts"] == 0
    )
    return counts, bounded and container.database.integrity_check()


def run_accelerated(data_dir: Path, *, hours: int = 24 * 7) -> dict[str, object]:
    if hours < 24 * 7:
        raise ValueError("accelerated bounded-growth evidence requires at least seven days")
    settings = _settings(data_dir)
    tracemalloc.start()
    container = build_hub_container(settings)
    maximum = {key: 0 for key in _counts(container)}
    try:
        _prepare(container, START)
        for index in range(hours):
            now = START + timedelta(hours=index)
            _step(container, index, now)
            counts = _counts(container)
            for key, value in counts.items():
                maximum[key] = max(maximum[key], value)
            if index and index % 24 == 0:
                container.close()
                container = build_hub_container(settings)
        final, passed = _verify(container)
        _current, peak = tracemalloc.get_traced_memory()
        return {
            "scenario": "accelerated-seven-day-bounded-growth",
            "evidence_kind": "deterministic_virtual_time",
            "virtual_start": START.isoformat(),
            "virtual_hours": hours,
            "restart_count": (hours - 1) // 24,
            "maximum_counts": maximum,
            "final_counts": final,
            "peak_traced_bytes": peak,
            "qualifies_as_seven_day_bounded_growth": hours >= 24 * 7,
            "qualifies_as_24_hour_wall_clock_soak": False,
            "passes": passed,
        }
    finally:
        container.close()
        tracemalloc.stop()


def run_wall_clock(
    data_dir: Path,
    *,
    hours: float,
    interval_seconds: float = 60.0,
) -> dict[str, object]:
    if hours <= 0 or interval_seconds < 1:
        raise ValueError("wall-clock duration and interval must be positive")
    settings = _settings(data_dir)
    started = datetime.now(UTC)
    deadline = time.monotonic() + hours * 3_600
    iterations = 0
    tracemalloc.start()
    container = build_hub_container(settings)
    try:
        _prepare(container, started)
        while True:
            now = datetime.now(UTC)
            _step(container, iterations, now)
            iterations += 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(interval_seconds, remaining))
        final, passed = _verify(container)
        _current, peak = tracemalloc.get_traced_memory()
        finished = datetime.now(UTC)
        elapsed = (finished - started).total_seconds()
        return {
            "scenario": "wall-clock-active-source-soak",
            "evidence_kind": "wall_clock",
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "elapsed_seconds": elapsed,
            "iterations": iterations,
            "final_counts": final,
            "peak_traced_bytes": peak,
            "qualifies_as_24_hour_wall_clock_soak": elapsed >= 24 * 3_600,
            "passes": passed,
        }
    finally:
        container.close()
        tracemalloc.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("accelerated", "wall-clock"), default="accelerated")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--hours", type=float)
    parser.add_argument("--interval-seconds", type=float, default=60.0)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".benchmark-data/personal-hub-reliability.json"),
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="personal-hub-reliability-") as temporary:
        data_dir = args.data_dir or Path(temporary)
        report = (
            run_accelerated(data_dir, hours=int(args.hours or 24 * 7))
            if args.mode == "accelerated"
            else run_wall_clock(
                data_dir,
                hours=args.hours or 24.0,
                interval_seconds=args.interval_seconds,
            )
        )
    report["generated_at"] = datetime.now(UTC).isoformat()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["passes"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
