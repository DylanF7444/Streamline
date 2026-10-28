"""Read-only Canvas Student import through a private iCalendar feed URL."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Final
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

from personal_hub.calendar.ical import (
    IcalCalendarFeed,
    IcalFeedFetcher,
    IcalParseError,
    parse_ical_calendar_items,
)
from personal_hub.feeds.contracts import (
    FeedOfflineError,
    FeedParseError,
    FeedPolicyError,
    FeedResponseTooLargeError,
    FeedUnavailableError,
)
from personal_hub.feeds.parser import sanitize_html
from personal_hub.feeds.security import origin_for_url
from personal_hub.sources import (
    SourceDisconnectedError,
    SourcePermissionRequiredError,
    SourceUnavailableError,
)
from personal_hub.tasks import HubTask, TaskRepository

CANVAS_TASK_NAMESPACE: Final = UUID("d966d9c9-95b1-4a7e-962a-93b00f9293f9")
_CANVAS_FEED_ID: Final = UUID("841a758a-cd91-438d-b5ed-966af3f5a2d6")
_CANVAS_ASSIGNMENT_FRAGMENT = re.compile(r"^assignment_(?P<assignment_id>[1-9][0-9]*)$", re.ASCII)
_CANVAS_ASSIGNMENT_PATH = re.compile(
    r"^/courses/(?P<course_id>[1-9][0-9]*)/assignments/(?P<assignment_id>[1-9][0-9]*)$",
    re.ASCII,
)
_CANVAS_COURSE_CONTEXT = re.compile(r"^course_(?P<course_id>[1-9][0-9]*)$", re.ASCII)


class CanvasIcalTaskImporter:
    """Fetch a bounded Canvas iCalendar document and idempotently upsert tasks."""

    def __init__(
        self,
        task_repository: TaskRepository,
        *,
        fetcher: IcalFeedFetcher | None = None,
    ) -> None:
        self.task_repository = task_repository
        self.fetcher = fetcher or IcalFeedFetcher()
        self._raw_body: bytes | None = None
        self._etag: str | None = None
        self._last_modified: str | None = None
        self._origin: str | None = None
        self.last_import_count: int | None = None

    def reset(self) -> None:
        """Forget response validators and content without changing imported tasks."""

        self._raw_body = None
        self._etag = None
        self._last_modified = None
        self._origin = None
        self.last_import_count = None

    async def sync(
        self,
        feed_url: str,
        *,
        account_label: str,
        fallback_timezone: str = "UTC",
        remove_completed_tasks_after_due_date: bool = False,
        remove_completed_assignments: bool | None = None,
        now: datetime | None = None,
    ) -> tuple[HubTask, ...]:
        """Import concrete VEVENTs and optionally clean completed past-due assignments."""

        origin = self.validate_feed_url(feed_url)
        sync_time = _aware_utc(now)
        cleanup_canvas_assignments = (
            remove_completed_tasks_after_due_date
            if remove_completed_assignments is None
            else remove_completed_assignments
        )
        if self._origin != origin:
            self.reset()
            self._origin = origin
        feed = IcalCalendarFeed(
            feed_id=_CANVAS_FEED_ID,
            title=account_label,
            feed_url=feed_url,
            reviewed_origin=origin,
        )
        try:
            response = await self.fetcher.fetch(
                feed,
                etag=self._etag if self._raw_body is not None else None,
                last_modified=self._last_modified if self._raw_body is not None else None,
            )
            if response.status_code == 304:
                if self._raw_body is None:
                    raise SourceUnavailableError("Canvas calendar cache is unavailable")
                body = self._raw_body
            elif response.status_code == 200:
                body = response.body
            elif response.status_code in {401, 403}:
                raise SourcePermissionRequiredError("Canvas calendar permission is required")
            elif response.status_code == 404:
                raise SourceDisconnectedError("Canvas calendar feed was not found")
            elif response.status_code in {408, 425, 429} or response.status_code >= 500:
                raise SourceUnavailableError("Canvas calendar is temporarily unavailable")
            else:
                raise ValueError("Canvas calendar returned an invalid response")
            items = parse_ical_calendar_items(
                body,
                fallback_timezone=fallback_timezone,
                expand_recurrence=True,
                reference_at=sync_time,
            )
        except (SourceDisconnectedError, SourcePermissionRequiredError, SourceUnavailableError):
            raise
        except (FeedOfflineError, FeedUnavailableError) as error:
            raise SourceUnavailableError("Canvas calendar is temporarily unavailable") from error
        except (
            FeedParseError,
            FeedPolicyError,
            FeedResponseTooLargeError,
            IcalParseError,
            TypeError,
            ValueError,
        ) as error:
            raise ValueError("Canvas calendar response could not be read safely") from error

        if response.status_code == 200:
            self._raw_body = body
            self._etag = _safe_response_header(response.headers, "etag")
            self._last_modified = _safe_response_header(response.headers, "last-modified")

        imported: list[HubTask] = []
        for item in items:
            task_id = uuid5(CANVAS_TASK_NAMESPACE, f"{origin}|{item.item_id}")
            current = self.task_repository.get(task_id)
            native_url = canvas_assignment_url(item.deep_link, canvas_origin=origin)
            if self.task_repository.has_external_deletion_marker(
                provider_id="canvas",
                external_key=native_url,
                task_id=task_id,
            ):
                # A marker is authoritative even if a stale/local copy happens to
                # remain after an interrupted cleanup.  The marker prevents a
                # successful refresh from recreating the task as open.
                if current is not None:
                    self.task_repository.delete(task_id)
                continue
            if cleanup_canvas_assignments and _should_remove_completed_assignment(
                current,
                assignment_url=native_url,
                now=sync_time,
                canvas_origin=origin,
            ):
                self.task_repository.mark_deleted_on_hub(
                    task_id,
                    provider_id="canvas",
                    external_key=native_url,
                    deleted_at=sync_time,
                )
                continue
            notes = _task_notes(
                description=item.description,
                location=item.location,
                deep_link=native_url,
            )
            if current is None:
                imported.append(
                    self.task_repository.create(
                        task_id=task_id,
                        title=item.title,
                        notes=notes,
                        external_url=native_url,
                        due_at=item.due_at,
                        due_is_all_day=item.due_is_all_day,
                        timezone=item.timezone,
                        tags=("canvas",),
                        created_at=sync_time,
                        updated_at=sync_time,
                    )
                )
                continue
            tags = tuple(sorted({*current.tags, "canvas"}))
            updated = self.task_repository.update(
                task_id,
                title=item.title,
                notes=notes,
                external_url=native_url,
                due_at=item.due_at,
                due_is_all_day=item.due_is_all_day,
                timezone=item.timezone,
                tags=tags,
                # A caller-provided clock may be backdated for deterministic
                # reconciliation tests.  Never move a persisted task's
                # updated_at backwards when that happens.
                updated_at=max(sync_time, current.updated_at),
            )
            if updated is not None:
                if cleanup_canvas_assignments and _should_remove_completed_assignment(
                    updated,
                    assignment_url=native_url,
                    now=sync_time,
                    canvas_origin=origin,
                ):
                    self.task_repository.mark_deleted_on_hub(
                        updated.task_id,
                        provider_id="canvas",
                        external_key=native_url,
                        deleted_at=sync_time,
                    )
                    continue
                imported.append(updated)
        if cleanup_canvas_assignments:
            self._remove_completed_assignments_past_due(origin=origin, now=sync_time)
        self.last_import_count = len(imported)
        return tuple(imported)

    def _remove_completed_assignments_past_due(
        self,
        *,
        origin: str,
        now: datetime,
    ) -> None:
        """Remove eligible local copies after a successful Canvas refresh.

        The feed can stop returning an old event before its local task is cleaned up,
        so inspect the bounded set of completed Canvas tasks as well as the current
        feed items.  A native same-origin assignment URL distinguishes assignments
        from generic Canvas calendar events.
        """

        candidates = self.task_repository.list(
            state="completed",
            tags=("canvas",),
            include_archived=True,
            limit=10_000,
        )
        for task in candidates:
            native_url = canvas_assignment_url(
                task.external_url,
                canvas_origin=origin,
            )
            if native_url is not None and _should_remove_completed_assignment(
                task,
                assignment_url=native_url,
                now=now,
                canvas_origin=origin,
            ):
                self.task_repository.mark_deleted_on_hub(
                    task.task_id,
                    provider_id="canvas",
                    external_key=native_url,
                    deleted_at=now,
                )

    @staticmethod
    def validate_feed_url(feed_url: str) -> str:
        if not isinstance(feed_url, str):
            raise ValueError("Canvas requires an HTTPS iCalendar feed URL")
        candidate = feed_url.strip()
        if (
            not candidate
            or len(candidate) > 4_096
            or any(character in candidate for character in "\r\n\x00\\")
        ):
            raise ValueError("Canvas requires a valid HTTPS iCalendar feed URL")
        try:
            parts = urlsplit(candidate)
            origin = origin_for_url(candidate)
        except (FeedPolicyError, ValueError) as error:
            raise ValueError("Canvas requires a valid HTTPS iCalendar feed URL") from error
        if (
            parts.scheme.casefold() != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or not parts.path.casefold().endswith(".ics")
        ):
            raise ValueError("Canvas requires an HTTPS iCalendar feed URL ending in .ics")
        return origin


def _task_notes(
    *,
    description: str | None,
    location: str | None,
    deep_link: str | None,
) -> str:
    parts = ["Imported from Canvas."]
    safe_description = sanitize_html(description, max_chars=8_000)
    if safe_description:
        parts.append(safe_description)
    if location:
        parts.append(f"Location: {location}")
    if deep_link:
        parts.append(f"Open in Canvas: {deep_link}")
    return "\n\n".join(parts)[:20_000]


def canvas_assignment_url(
    calendar_url: str | None,
    *,
    canvas_origin: str | None = None,
) -> str | None:
    """Return the native Canvas assignment URL represented by a VEVENT URL.

    Canvas emits a calendar URL for every iCalendar event. Assignment events
    identify the assignment in the URL fragment and the course in the
    ``include_contexts`` query parameter; ordinary calendar events do not.
    Already-native assignment URLs are returned in canonical form so tasks
    imported by an earlier version are repaired when they are displayed.
    """

    if not calendar_url:
        return None
    try:
        parts = urlsplit(calendar_url)
        event_origin = origin_for_url(calendar_url)
        if (
            parts.scheme.casefold() not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or (canvas_origin is not None and event_origin != canvas_origin)
        ):
            return None
        native_match = _CANVAS_ASSIGNMENT_PATH.fullmatch(parts.path.rstrip("/"))
        if native_match is not None:
            return urlunsplit(
                (
                    parts.scheme,
                    parts.netloc,
                    f"/courses/{native_match.group('course_id')}/assignments/"
                    f"{native_match.group('assignment_id')}",
                    "",
                    "",
                )
            )
        if parts.path.rstrip("/").casefold() != "/calendar":
            return None
        assignment_match = _CANVAS_ASSIGNMENT_FRAGMENT.fullmatch(unquote(parts.fragment).strip())
        if assignment_match is None:
            return None
        query = parse_qs(parts.query, keep_blank_values=False)
        course_id: str | None = None
        for raw_contexts in query.get("include_contexts", ()):
            for raw_context in unquote(raw_contexts).split(","):
                course_match = _CANVAS_COURSE_CONTEXT.fullmatch(raw_context.strip())
                if course_match is not None:
                    course_id = course_match.group("course_id")
                    break
            if course_id is not None:
                break
        if course_id is None:
            return None
        native_url = urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                f"/courses/{course_id}/assignments/{assignment_match.group('assignment_id')}",
                "",
                "",
            )
        )
        return native_url if origin_for_url(native_url) == event_origin else None
    except (FeedPolicyError, ValueError):
        return None


def _safe_response_header(headers: object, name: str) -> str | None:
    if not hasattr(headers, "items"):
        return None
    for key, value in headers.items():
        if str(key).casefold() != name.casefold():
            continue
        normalized = str(value).strip()
        if (
            normalized
            and len(normalized) <= 512
            and not any(character in normalized for character in "\r\n\x00")
        ):
            return normalized
    return None


def _aware_utc(value: datetime | None) -> datetime:
    timestamp = datetime.now(UTC) if value is None else value
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("Canvas sync time must be timezone-aware")
    return timestamp.astimezone(UTC)


def _should_remove_completed_assignment(
    task: HubTask | None,
    *,
    assignment_url: str | None,
    now: datetime,
    canvas_origin: str,
) -> bool:
    if task is None or task.state != "completed" or task.due_at is None:
        return False
    native_url = assignment_url or canvas_assignment_url(
        task.external_url,
        canvas_origin=canvas_origin,
    )
    if native_url is None:
        return False
    if task.due_is_all_day:
        task_zone = ZoneInfo(task.timezone)
        return task.due_at.astimezone(task_zone).date() < now.astimezone(task_zone).date()
    return task.due_at < now


__all__ = ["CANVAS_TASK_NAMESPACE", "CanvasIcalTaskImporter", "canvas_assignment_url"]
