"""Read-only Canvas Student import through a private iCalendar feed URL."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final
from urllib.parse import urlsplit
from uuid import UUID, uuid5

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
    ) -> tuple[HubTask, ...]:
        """Import every concrete VEVENT as one stable local due-dated task."""

        origin = self.validate_feed_url(feed_url)
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
            items = parse_ical_calendar_items(body, fallback_timezone=fallback_timezone)
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

        now = datetime.now(UTC)
        imported: list[HubTask] = []
        for item in items:
            task_id = uuid5(CANVAS_TASK_NAMESPACE, f"{origin}|{item.item_id}")
            current = self.task_repository.get(task_id)
            notes = _task_notes(
                description=item.description,
                location=item.location,
                deep_link=item.deep_link,
            )
            if current is None:
                imported.append(
                    self.task_repository.create(
                        task_id=task_id,
                        title=item.title,
                        notes=notes,
                        due_at=item.due_at,
                        due_is_all_day=item.due_is_all_day,
                        timezone=item.timezone,
                        tags=("canvas",),
                        created_at=now,
                        updated_at=now,
                    )
                )
                continue
            tags = tuple(sorted({*current.tags, "canvas"}))
            updated = self.task_repository.update(
                task_id,
                title=item.title,
                notes=notes,
                due_at=item.due_at,
                due_is_all_day=item.due_is_all_day,
                timezone=item.timezone,
                tags=tags,
                updated_at=now,
            )
            if updated is not None:
                imported.append(updated)
        self.last_import_count = len(imported)
        return tuple(imported)

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


__all__ = ["CANVAS_TASK_NAMESPACE", "CanvasIcalTaskImporter"]
