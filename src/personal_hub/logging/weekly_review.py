"""Local weekly projections. Counts cover full history; displayed details are bounded."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from personal_hub.browser_time.repository import BrowserTimeRepository
from personal_hub.logging.contracts import ActivityEntry, LogFilter, ReflectionEntry
from personal_hub.logging.repository import ActivityRepository, ReflectionRepository
from personal_hub.metrics.contracts import MetricDefinition, MetricSummary
from personal_hub.metrics.repository import MetricsRepository
from personal_hub.screen_time.repository import ScreenTimeRepository


def week_bounds(day: date) -> tuple[date, date]:
    """Monday through Sunday; reject weeks whose exclusive upper bound cannot exist."""
    start = day - timedelta(days=day.weekday())
    try:
        end = start + timedelta(days=6)
        end + timedelta(days=1)
    except OverflowError as error:
        raise ValueError("Choose a week within the supported date range") from error
    return start, end


@dataclass(frozen=True)
class ReviewDay:
    day: date
    completed: int
    manual: int
    entered_minutes: int | None
    timed_minutes: int | None
    app_seconds: int | None
    site_seconds: int | None


@dataclass(frozen=True)
class ReviewMetric:
    definition: MetricDefinition
    summary: MetricSummary
    source_name: str | None
    reporting_timezone: str | None


@dataclass(frozen=True)
class WeeklyReview:
    start: date
    end: date
    timezone: str
    facts: dict
    days: tuple[ReviewDay, ...]
    entries: tuple[ActivityEntry, ...]
    reflections: tuple[ReflectionEntry, ...]
    apps: dict[str, int]
    sites: dict[str, int]
    metrics: tuple[ReviewMetric, ...]
    definitions: tuple[MetricDefinition, ...]
    selected_metrics: tuple[str, ...]


def build_weekly_review(
    activity: ActivityRepository,
    reflections: ReflectionRepository,
    screen_time: ScreenTimeRepository,
    browser_time: BrowserTimeRepository,
    metrics: MetricsRepository,
    *,
    day: date,
    timezone: str,
    metric_ids: tuple[str, ...] = (),
    limit: int = 200,
) -> WeeklyReview:
    start, end = week_bounds(day)
    options = LogFilter(start=start, end=end, timezone=timezone, limit=limit)
    facts = activity.summary(options)
    days = []
    for offset in range(7):
        current = start + timedelta(days=offset)
        detail = facts["daily"].get(current.isoformat(), {})
        apps = screen_time.summary(current, current, timezone)
        sites = browser_time.summary(current, current, timezone)
        days.append(
            ReviewDay(
                day=current,
                completed=detail.get("task_completion", 0),
                manual=detail.get("manual", 0),
                entered_minutes=detail.get("durations", {}).get("entered"),
                timed_minutes=detail.get("durations", {}).get("timer"),
                app_seconds=sum(apps.values()) if apps else None,
                site_seconds=sum(sites.values()) if sites else None,
            )
        )
    definitions = tuple(metrics.definitions())
    selected = tuple(d.metric_id for d in definitions if d.metric_id in metric_ids[:64])
    sources = {source.source_id: source for source in metrics.sources()}
    values = []
    for definition in definitions:
        if definition.metric_id not in selected:
            continue
        summary = metrics.summary(definition.metric_id, start, end)
        source = sources.get(summary.source_id)
        values.append(
            ReviewMetric(
                definition,
                summary,
                source.name if source else None,
                source.reporting_timezone if source else None,
            )
        )
    return WeeklyReview(
        start,
        end,
        timezone,
        facts,
        tuple(days),
        tuple(activity.list(options)),
        tuple(reflections.list(options.model_copy(update={"limit": 10_000}))),
        screen_time.summary(start, end, timezone),
        browser_time.summary(start, end, timezone),
        tuple(values),
        definitions,
        selected,
    )
