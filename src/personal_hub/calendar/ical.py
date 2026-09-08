"""Secure, read-only iCalendar feeds for Personal Hub.

The iCalendar adapter deliberately keeps feed definitions and conditional-request
metadata local while keeping event snapshots in memory only.  It accepts the
small interoperable subset needed by calendar feeds (VEVENT, time zones,
all-day events, exclusions, and bounded common recurrence rules) and treats all
remote text as untrusted input.
"""

from __future__ import annotations

import calendar as calendar_module
import hashlib
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Final, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AnyHttpUrl, Field, ValidationError, field_validator, model_validator

from personal_hub.calendar.contracts import (
    CalendarDisconnectedError,
    CalendarEvent,
    CalendarInvalidResponseError,
    CalendarOfflineError,
    CalendarPermissionRequiredError,
    CalendarQuery,
    CalendarSnapshot,
    CalendarStatus,
    CalendarUnavailableError,
)
from personal_hub.contracts import HubContract, SourceRef
from personal_hub.feeds.contracts import (
    FeedOfflineError,
    FeedParseError,
    FeedPolicyError,
    FeedResponse,
    FeedResponseTooLargeError,
    FeedUnavailableError,
)
from personal_hub.feeds.fetcher import FeedFetcher
from personal_hub.feeds.security import origin_for_url

if TYPE_CHECKING:
    from personal_hub.storage.repositories import SettingsRepository

IcalSyncStateValue = Literal[
    "ready",
    "empty",
    "stale",
    "offline",
    "permission_required",
    "disconnected",
    "error",
]

ICAL_PROVIDER_ID: Final = "ical"
ICAL_FEED_SETTINGS_KEY: Final = "calendar.ical.feeds.v1"
ICAL_RESPONSE_ACCEPT: Final = "text/calendar, application/ics, text/plain;q=0.9, */*;q=0.1"
_MAX_FEEDS: Final = 100
_MAX_BODY_BYTES: Final = 20_000_000
_MAX_LINES: Final = 100_000
_MAX_LINE_LENGTH: Final = 16_384
_MAX_PROPERTIES_PER_EVENT: Final = 256
_MAX_PROPERTY_VALUE_LENGTH: Final = 16_384
_MAX_RECURRENCE_OCCURRENCES: Final = 10_000
_MAX_RRULE_LENGTH: Final = 2_048
_MAX_TEXT_LENGTH: Final = 4_096
_ICAL_DATE_RE: Final = re.compile(r"^\d{8}$")
_ICAL_DATETIME_RE: Final = re.compile(r"^\d{8}T\d{4,6}Z?$", re.IGNORECASE)
_DURATION_RE: Final = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$",
    re.IGNORECASE,
)
_WEEKDAY_INDEX: Final[dict[str, int]] = {
    "MO": 0,
    "TU": 1,
    "WE": 2,
    "TH": 3,
    "FR": 4,
    "SA": 5,
    "SU": 6,
}
_FREQUENCIES: Final[frozenset[str]] = frozenset({"DAILY", "WEEKLY", "MONTHLY", "YEARLY"})


class IcalError(RuntimeError):
    """Base error for bounded iCalendar configuration and parsing."""


class IcalParseError(IcalError):
    """The downloaded body is not a supported, bounded iCalendar document."""


def _safe_header(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or len(text) > 512 or any(character in text for character in "\r\n\x00"):
        return None
    return text


def _response_header(response: FeedResponse, name: str) -> str | None:
    wanted = name.casefold()
    for key, value in response.headers.items():
        if str(key).casefold() == wanted:
            return _safe_header(value)
    return None


def _normalized_text(value: str, *, limit: int = _MAX_TEXT_LENGTH) -> str:
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > limit:
        raise IcalParseError("iCalendar text is missing or too large")
    return normalized


def _unescape_text(value: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character == "\\" and index + 1 < len(value):
            next_character = value[index + 1]
            if next_character in {"n", "N"}:
                result.append(" ")
            elif next_character in {"\\", ";", ","}:
                result.append(next_character)
            else:
                result.append(next_character)
            index += 2
            continue
        result.append(character)
        index += 1
    return "".join(result)


def _safe_text(value: str, *, limit: int = _MAX_TEXT_LENGTH) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        normalized = _normalized_text(_unescape_text(value), limit=limit)
    except IcalParseError:
        return None
    return normalized


def _safe_event_url(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = _unescape_text(value).strip()
    if not candidate or len(candidate) > 1_024:
        return None
    if any(character in candidate for character in "\r\n\x00"):
        return None
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None
    if (
        parts.scheme.casefold() not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or "\\" in candidate
    ):
        return None
    return candidate


class IcalCalendarFeed(HubContract):
    """A user-selected, read-only HTTP(S) iCalendar feed."""

    feed_id: UUID = Field(default_factory=uuid4)
    title: str = Field(min_length=1, max_length=240)
    feed_url: AnyHttpUrl
    enabled: bool = True
    refresh_minutes: int = Field(default=30, ge=15, le=24 * 60)
    allow_http: bool = False
    reviewed_origin: str | None = Field(default=None, max_length=512)

    @field_validator("feed_url")
    @classmethod
    def _feed_url_has_no_credentials(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.username is not None or value.password is not None:
            raise ValueError("iCalendar feed URLs cannot contain user information")
        return value

    @field_validator("reviewed_origin")
    @classmethod
    def _reviewed_origin_is_exactly_normalized(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return origin_for_url(value)
        except FeedPolicyError as error:
            raise ValueError("reviewed_origin must be an HTTP(S) origin") from error

    @model_validator(mode="after")
    def _feed_url_policy_is_explicit(self) -> IcalCalendarFeed:
        try:
            feed_origin = origin_for_url(str(self.feed_url))
            scheme = urlsplit(str(self.feed_url)).scheme.casefold()
        except FeedPolicyError as error:
            raise ValueError("iCalendar feed URL is invalid") from error
        if scheme == "http" and (not self.allow_http or self.reviewed_origin is None):
            raise ValueError("HTTP iCalendar feeds require explicit review")
        if self.reviewed_origin is not None and self.reviewed_origin != feed_origin:
            raise ValueError("reviewed_origin must exactly match the feed URL origin")
        return self


class IcalSyncState(HubContract):
    """Durable conditional-request and safe health metadata for one iCal feed."""

    feed_id: UUID
    state: IcalSyncStateValue = "stale"
    etag: str | None = Field(default=None, max_length=512)
    last_modified: str | None = Field(default=None, max_length=512)
    last_checked_at: datetime | None = None
    last_success_at: datetime | None = None
    failure_count: int = Field(default=0, ge=0, le=1_000_000)
    safe_error: str | None = Field(default=None, max_length=512)

    @field_validator("etag", "last_modified")
    @classmethod
    def _validators_are_safe(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or len(normalized) > 512:
            return None
        if any(character in normalized for character in "\r\n\x00"):
            return None
        return normalized

    @field_validator("last_checked_at", "last_success_at")
    @classmethod
    def _timestamps_are_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("iCalendar sync timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("safe_error")
    @classmethod
    def _safe_error_is_single_line(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        return normalized[:512] or None


@dataclass(frozen=True, slots=True)
class _IcalDefinition:
    uid: str
    starts_at: datetime | date
    ends_at: datetime | date
    all_day: bool
    timezone: str
    title: str
    description: str | None
    location: str | None
    url: str | None
    rrule: str | None
    exdates: tuple[datetime | date, ...]


def _unfold_lines(body: bytes | str) -> tuple[str, ...]:
    if isinstance(body, bytes):
        if len(body) > _MAX_BODY_BYTES:
            raise IcalParseError("iCalendar response exceeded the byte limit")
        try:
            text = body.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise IcalParseError("iCalendar response is not valid UTF-8") from error
    elif isinstance(body, str):
        text = body
        if len(text.encode("utf-8")) > _MAX_BODY_BYTES:
            raise IcalParseError("iCalendar response exceeded the byte limit")
    else:
        raise IcalParseError("iCalendar response body is invalid")
    if "\x00" in text:
        raise IcalParseError("iCalendar response contains unsafe characters")
    raw_lines = text.splitlines()
    if len(raw_lines) > _MAX_LINES:
        raise IcalParseError("iCalendar response contains too many lines")
    unfolded: list[str] = []
    for raw_line in raw_lines:
        if len(raw_line) > _MAX_LINE_LENGTH:
            raise IcalParseError("iCalendar line is too long")
        if raw_line.startswith((" ", "\t")):
            if not unfolded:
                raise IcalParseError("iCalendar continuation has no property")
            unfolded[-1] += raw_line[1:]
        else:
            unfolded.append(raw_line)
    for line in unfolded:
        if any(ord(character) < 0x20 and character != "\t" for character in line):
            raise IcalParseError("iCalendar property contains unsafe control characters")
    return tuple(unfolded)


def _property(line: str) -> tuple[str, dict[str, str], str]:
    if ":" not in line:
        raise IcalParseError("iCalendar property is missing a value")
    name_and_parameters, value = line.split(":", 1)
    parts = name_and_parameters.split(";")
    name = parts[0].strip().upper()
    if not re.fullmatch(r"[A-Z0-9-]{1,64}", name):
        raise IcalParseError("iCalendar property name is invalid")
    parameters: dict[str, str] = {}
    for part in parts[1:]:
        key, separator, parameter_value = part.partition("=")
        key = key.strip().upper()
        if not separator or not re.fullmatch(r"[A-Z0-9-]{1,64}", key):
            raise IcalParseError("iCalendar parameter is invalid")
        normalized_value = parameter_value.strip().strip('"')
        if not normalized_value or len(normalized_value) > 256:
            raise IcalParseError("iCalendar parameter is invalid")
        parameters[key] = normalized_value
    if len(value) > _MAX_PROPERTY_VALUE_LENGTH:
        raise IcalParseError("iCalendar property value is too large")
    return name, parameters, value


def _zone(value: str, fallback: str) -> ZoneInfo:
    candidate = value.strip().strip('"') or fallback
    try:
        return ZoneInfo(candidate)
    except ZoneInfoNotFoundError as error:
        raise IcalParseError("iCalendar event time zone is unsupported") from error


def _parse_datetime_value(
    value: str,
    parameters: Mapping[str, str],
    *,
    fallback_timezone: str,
) -> tuple[datetime | date, bool, str]:
    normalized = value.strip()
    value_kind = parameters.get("VALUE", "").upper()
    if value_kind == "DATE" or _ICAL_DATE_RE.fullmatch(normalized):
        try:
            return datetime.strptime(normalized, "%Y%m%d").date(), True, fallback_timezone
        except ValueError as error:
            raise IcalParseError("iCalendar date is invalid") from error
    if _ICAL_DATETIME_RE.fullmatch(normalized) is None:
        raise IcalParseError("iCalendar date-time is invalid")
    suffix_utc = normalized[-1:].upper() == "Z"
    digits = normalized[:-1] if suffix_utc else normalized
    formats = ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M")
    parsed: datetime | None = None
    for format_string in formats:
        try:
            parsed = datetime.strptime(digits, format_string)
            break
        except ValueError:
            continue
    if parsed is None:
        raise IcalParseError("iCalendar date-time is invalid")
    if suffix_utc:
        return parsed.replace(tzinfo=UTC), False, "UTC"
    timezone = _zone(parameters.get("TZID", ""), fallback_timezone)
    return parsed.replace(tzinfo=timezone), False, timezone.key


def _parse_duration(value: str) -> timedelta:
    match = _DURATION_RE.fullmatch(value.strip())
    if match is None or value.strip().upper() == "P":
        raise IcalParseError("iCalendar duration is invalid")
    duration = timedelta(
        days=int(match.group("days") or 0),
        hours=int(match.group("hours") or 0),
        minutes=int(match.group("minutes") or 0),
        seconds=int(match.group("seconds") or 0),
    )
    if duration <= timedelta(0) or duration > timedelta(days=366):
        raise IcalParseError("iCalendar duration is out of bounds")
    return duration


def _first_property(
    properties: Mapping[str, list[tuple[dict[str, str], str]]],
    name: str,
) -> tuple[dict[str, str], str] | None:
    values = properties.get(name)
    return None if not values else values[0]


def _parse_exdates(
    properties: Mapping[str, list[tuple[dict[str, str], str]]],
    *,
    fallback_timezone: str,
) -> tuple[datetime | date, ...]:
    result: list[datetime | date] = []
    for parameters, raw_value in properties.get("EXDATE", []):
        for value in raw_value.split(","):
            try:
                parsed, _all_day, _timezone = _parse_datetime_value(
                    value,
                    parameters,
                    fallback_timezone=fallback_timezone,
                )
            except IcalParseError:
                continue
            if parsed not in result:
                result.append(parsed)
            if len(result) >= 1_000:
                return tuple(result)
    return tuple(result)


def _definition_from_properties(
    properties: Mapping[str, list[tuple[dict[str, str], str]]],
    *,
    sequence: int,
    fallback_timezone: str,
) -> _IcalDefinition | None:
    status = _first_property(properties, "STATUS")
    if status is not None and status[1].strip().upper() == "CANCELLED":
        return None
    start_property = _first_property(properties, "DTSTART")
    if start_property is None:
        return None
    try:
        starts_at, all_day, timezone = _parse_datetime_value(
            start_property[1],
            start_property[0],
            fallback_timezone=fallback_timezone,
        )
    except IcalParseError:
        return None
    end_property = _first_property(properties, "DTEND")
    duration_property = _first_property(properties, "DURATION")
    try:
        if end_property is not None:
            ends_at, end_all_day, _end_timezone = _parse_datetime_value(
                end_property[1],
                end_property[0],
                fallback_timezone=timezone,
            )
            if end_all_day != all_day:
                return None
        elif duration_property is not None:
            duration = _parse_duration(duration_property[1])
            if all_day:
                assert isinstance(starts_at, date) and not isinstance(starts_at, datetime)
                ends_at = starts_at + duration
            else:
                assert isinstance(starts_at, datetime)
                ends_at = starts_at + duration
        elif all_day:
            assert isinstance(starts_at, date) and not isinstance(starts_at, datetime)
            ends_at = starts_at + timedelta(days=1)
        else:
            assert isinstance(starts_at, datetime)
            ends_at = starts_at + timedelta(hours=1)
    except (IcalParseError, TypeError, OverflowError):
        return None
    if all_day:
        if not isinstance(ends_at, date) or isinstance(ends_at, datetime):
            return None
        if ends_at <= starts_at:
            return None
    elif (
        not isinstance(ends_at, datetime)
        or not isinstance(starts_at, datetime)
        or ends_at <= starts_at
    ):
        return None

    uid_property = _first_property(properties, "UID")
    uid = _safe_text(uid_property[1], limit=256) if uid_property is not None else None
    if not uid:
        uid = hashlib.sha256(
            f"{sequence}:{start_property[1]}:{_first_property(properties, 'SUMMARY')}".encode(
                "utf-8", "replace"
            )
        ).hexdigest()
    summary_property = _first_property(properties, "SUMMARY")
    title = (
        _safe_text(summary_property[1], limit=240) if summary_property is not None else None
    ) or "Untitled event"
    location_property = _first_property(properties, "LOCATION")
    location = (
        _safe_text(location_property[1], limit=512) if location_property is not None else None
    )
    description_property = _first_property(properties, "DESCRIPTION")
    description = (
        _safe_text(description_property[1], limit=_MAX_TEXT_LENGTH)
        if description_property is not None
        else None
    )
    url_property = _first_property(properties, "URL")
    event_url = _safe_event_url(url_property[1] if url_property is not None else None)
    rrule_property = _first_property(properties, "RRULE")
    rrule = None
    if rrule_property is not None:
        candidate = rrule_property[1].strip()
        if candidate and len(candidate) <= _MAX_RRULE_LENGTH:
            rrule = candidate
    return _IcalDefinition(
        uid=uid,
        starts_at=starts_at,
        ends_at=ends_at,
        all_day=all_day,
        timezone=timezone,
        title=title,
        description=description,
        location=location,
        url=event_url,
        rrule=rrule,
        exdates=_parse_exdates(properties, fallback_timezone=timezone),
    )


class IcalCalendarItem(HubContract):
    """One bounded VEVENT projected for task-oriented importers.

    Unlike :class:`CalendarEvent`, this value is not filtered through a display
    window.  It represents each concrete VEVENT in the downloaded document once,
    which lets sources such as Canvas import every assignment/event as a due task.
    """

    item_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=240)
    due_at: datetime
    due_is_all_day: bool = False
    timezone: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=_MAX_TEXT_LENGTH)
    location: str | None = Field(default=None, max_length=512)
    deep_link: str | None = Field(default=None, max_length=1_024)

    @field_validator("due_at")
    @classmethod
    def _due_at_is_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("iCalendar item due time must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("timezone")
    @classmethod
    def _timezone_is_known(cls, value: str) -> str:
        return _zone(value, "UTC").key

    @field_validator("deep_link")
    @classmethod
    def _deep_link_is_safe(cls, value: str | None) -> str | None:
        return _safe_event_url(value)


def _parse_definitions(body: bytes | str, *, fallback_timezone: str) -> tuple[_IcalDefinition, ...]:
    lines = _unfold_lines(body)
    stack: list[str] = []
    current: dict[str, list[tuple[dict[str, str], str]]] | None = None
    definitions: list[_IcalDefinition] = []
    saw_calendar = False
    for line in lines:
        if not line:
            continue
        name, parameters, value = _property(line)
        if name == "BEGIN":
            component = value.strip().upper()
            if not component or len(component) > 64:
                raise IcalParseError("iCalendar component is invalid")
            if not stack and component != "VCALENDAR":
                raise IcalParseError("iCalendar document must begin with VCALENDAR")
            if component == "VEVENT":
                if not stack or stack[-1] != "VCALENDAR":
                    raise IcalParseError("VEVENT must be inside VCALENDAR")
                if current is not None:
                    raise IcalParseError("nested VEVENT is not supported")
                current = {}
            stack.append(component)
            saw_calendar = saw_calendar or component == "VCALENDAR"
            continue
        if name == "END":
            component = value.strip().upper()
            if not stack or component != stack[-1]:
                raise IcalParseError("iCalendar component boundaries are invalid")
            if component == "VEVENT":
                if current is None:
                    raise IcalParseError("iCalendar VEVENT is incomplete")
                definition = _definition_from_properties(
                    current,
                    sequence=len(definitions),
                    fallback_timezone=fallback_timezone,
                )
                if definition is not None:
                    definitions.append(definition)
                    if len(definitions) >= 5_000:
                        raise IcalParseError("iCalendar contains too many events")
                current = None
            stack.pop()
            continue
        if current is not None and stack and stack[-1] == "VEVENT":
            if sum(len(items) for items in current.values()) >= _MAX_PROPERTIES_PER_EVENT:
                raise IcalParseError("iCalendar event contains too many properties")
            current.setdefault(name, []).append((parameters, value))
    if not saw_calendar or stack or current is not None:
        raise IcalParseError("iCalendar document is incomplete")
    return tuple(definitions)


def _parse_rrule(value: str, definition: _IcalDefinition) -> dict[str, object] | None:
    fields: dict[str, str] = {}
    for part in value.split(";"):
        key, separator, raw = part.partition("=")
        key = key.strip().upper()
        raw = raw.strip()
        if not separator or not key or not raw or len(key) > 32 or len(raw) > 512:
            return None
        fields[key] = raw
    frequency = fields.get("FREQ", "").upper()
    if frequency not in _FREQUENCIES:
        return None
    try:
        interval = int(fields.get("INTERVAL", "1"))
    except ValueError:
        return None
    if not 1 <= interval <= 366:
        return None
    try:
        count = int(fields["COUNT"]) if "COUNT" in fields else None
    except ValueError:
        return None
    if count is not None and not 1 <= count <= _MAX_RECURRENCE_OCCURRENCES:
        return None
    byday: tuple[int, ...] = ()
    if "BYDAY" in fields:
        weekdays: list[int] = []
        for token in fields["BYDAY"].upper().split(","):
            if len(token) not in {2, 3, 4}:
                return None
            day_token = token[-2:]
            if day_token not in _WEEKDAY_INDEX or token[:-2] not in {"", "+1", "-1"}:
                return None
            if token[:-2] and frequency not in {"MONTHLY", "YEARLY"}:
                return None
            if _WEEKDAY_INDEX[day_token] not in weekdays:
                weekdays.append(_WEEKDAY_INDEX[day_token])
        byday = tuple(weekdays)
    bymonthday: tuple[int, ...] = ()
    if "BYMONTHDAY" in fields:
        values: list[int] = []
        for token in fields["BYMONTHDAY"].split(","):
            try:
                item = int(token)
            except ValueError:
                return None
            if not -31 <= item <= 31 or item == 0:
                return None
            values.append(item)
        bymonthday = tuple(dict.fromkeys(values))
    bymonth: tuple[int, ...] = ()
    if "BYMONTH" in fields:
        try:
            bymonth = tuple(dict.fromkeys(int(token) for token in fields["BYMONTH"].split(",")))
        except ValueError:
            return None
        if any(not 1 <= item <= 12 for item in bymonth):
            return None
    until: datetime | date | None = None
    if "UNTIL" in fields:
        try:
            until, _all_day, _timezone = _parse_datetime_value(
                fields["UNTIL"],
                {},
                fallback_timezone=definition.timezone,
            )
        except IcalParseError:
            return None
    return {
        "frequency": frequency,
        "interval": interval,
        "count": count,
        "byday": byday,
        "bymonthday": bymonthday,
        "bymonth": bymonth,
        "until": until,
    }


def _matches_by_month_day(value: date, bymonthday: tuple[int, ...]) -> bool:
    if not bymonthday:
        return True
    last_day = calendar_module.monthrange(value.year, value.month)[1]
    valid = {item if item > 0 else last_day + item + 1 for item in bymonthday}
    return value.day in valid


def _occurrence_starts(
    definition: _IcalDefinition,
    query: CalendarQuery,
) -> tuple[datetime | date, ...]:
    if definition.rrule is None:
        return (definition.starts_at,)
    rule = _parse_rrule(definition.rrule, definition)
    if rule is None:
        return (definition.starts_at,)
    frequency = str(rule["frequency"])
    interval = int(rule["interval"])
    count = rule["count"]
    byday = rule["byday"]
    bymonthday = rule["bymonthday"]
    bymonth = rule["bymonth"]
    until = rule["until"]
    start = definition.starts_at
    all_day = definition.all_day
    if all_day:
        assert isinstance(start, date) and not isinstance(start, datetime)
        query_start: date = query.starts_at.astimezone(ZoneInfo(definition.timezone)).date()
        query_end: date = query.ends_at.astimezone(ZoneInfo(definition.timezone)).date()
    else:
        assert isinstance(start, datetime)
        query_start_datetime = query.starts_at.astimezone(start.tzinfo or UTC)
        query_end_datetime = query.ends_at.astimezone(start.tzinfo or UTC)
        query_start = query_start_datetime
        query_end = query_end_datetime
    candidates: list[datetime | date] = []
    generated = 0

    def add_candidate(candidate: datetime | date) -> bool:
        nonlocal generated
        if count is not None and generated >= int(count):
            return False
        generated += 1
        if until is not None and (
            (isinstance(candidate, datetime) and isinstance(until, datetime) and candidate > until)
            or (
                isinstance(candidate, date)
                and not isinstance(candidate, datetime)
                and isinstance(until, date)
                and candidate > until
            )
        ):
            return False
        if candidate >= query_start:
            candidates.append(candidate)
        return True

    # Recurrence dates are intentionally generated only through the requested
    # window and a fixed safety cap.  An invalid/unbounded hostile rule cannot
    # consume the UI thread or allocate an unbounded event list.
    if frequency == "DAILY":
        cursor = start
        while cursor < query_end and len(candidates) < _MAX_RECURRENCE_OCCURRENCES:
            value_date = (
                cursor
                if isinstance(cursor, date) and not isinstance(cursor, datetime)
                else cursor.date()
            )  # type: ignore[union-attr]
            if (
                (not bymonth or value_date.month in bymonth)
                and (not bymonthday or _matches_by_month_day(value_date, bymonthday))
                and (not byday or value_date.weekday() in byday)
            ) and not add_candidate(cursor):
                break
            cursor = cursor + timedelta(days=interval)
    elif frequency == "WEEKLY":
        cursor = start
        week_days = tuple(byday) or (start.weekday(),)  # type: ignore[union-attr]
        period_start = start if all_day else start
        while period_start < query_end and len(candidates) < _MAX_RECURRENCE_OCCURRENCES:
            period_date = (
                period_start
                if isinstance(period_start, date) and not isinstance(period_start, datetime)
                else period_start.date()  # type: ignore[union-attr]
            )
            monday = period_date - timedelta(days=period_date.weekday())
            for weekday in week_days:
                candidate_date = monday + timedelta(days=weekday)
                if (
                    candidate_date < start.date()
                    if isinstance(start, datetime)
                    else candidate_date < start
                ):
                    continue
                candidate: datetime | date
                if all_day:
                    candidate = candidate_date
                else:
                    candidate = datetime.combine(
                        candidate_date,
                        start.timetz(),  # type: ignore[union-attr]
                    )
                if candidate >= query_end:
                    continue
                if not add_candidate(candidate):
                    return tuple(candidates)
            period_start = period_start + timedelta(weeks=interval)
    elif frequency == "MONTHLY":
        cursor_date = start if all_day else start.date()  # type: ignore[union-attr]
        period_index = 0
        while period_index < _MAX_RECURRENCE_OCCURRENCES:
            month_offset = period_index * interval
            year = cursor_date.year + (cursor_date.month - 1 + month_offset) // 12
            month = (cursor_date.month - 1 + month_offset) % 12 + 1
            if year > query_end.year + 1:
                break
            if bymonth and month not in bymonth:
                period_index += 1
                continue
            last_day = calendar_module.monthrange(year, month)[1]
            days = tuple(item for item in bymonthday) or (cursor_date.day,)
            if byday:
                days = tuple(
                    day
                    for day in range(1, last_day + 1)
                    if date(year, month, day).weekday() in byday
                )
            for day_value in days:
                day_value = day_value if day_value > 0 else last_day + day_value + 1
                if not 1 <= day_value <= last_day:
                    continue
                candidate_date = date(year, month, day_value)
                candidate = (
                    candidate_date if all_day else datetime.combine(candidate_date, start.timetz())  # type: ignore[union-attr]
                )
                if candidate < start or candidate >= query_end:
                    continue
                if not add_candidate(candidate):
                    return tuple(candidates)
            period_index += 1
    else:  # YEARLY
        assert isinstance(start, (datetime, date))
        start_date = start if all_day else start.date()  # type: ignore[union-attr]
        for year_offset in range(_MAX_RECURRENCE_OCCURRENCES):
            year = start_date.year + year_offset * interval
            if year > query_end.year + 1:
                break
            months = tuple(bymonth) or (start_date.month,)
            for month in months:
                last_day = calendar_module.monthrange(year, month)[1]
                days = tuple(bymonthday) or (start_date.day,)
                if byday:
                    days = tuple(
                        day
                        for day in range(1, last_day + 1)
                        if date(year, month, day).weekday() in byday
                    )
                for day_value in days:
                    day_value = day_value if day_value > 0 else last_day + day_value + 1
                    if not 1 <= day_value <= last_day:
                        continue
                    candidate_date = date(year, month, day_value)
                    candidate = (
                        candidate_date
                        if all_day
                        else datetime.combine(candidate_date, start.timetz())  # type: ignore[union-attr]
                    )
                    if candidate < start or candidate >= query_end:
                        continue
                    if not add_candidate(candidate):
                        return tuple(candidates)
    return tuple(candidates)


def _materialize_events(
    definitions: Iterable[_IcalDefinition],
    *,
    feed: IcalCalendarFeed,
    query: CalendarQuery,
) -> tuple[CalendarEvent, ...]:
    source_origin = origin_for_url(str(feed.feed_url))
    feed_url = _safe_event_url(str(feed.feed_url))
    events: list[CalendarEvent] = []
    for definition in definitions:
        if definition.all_day:
            assert isinstance(definition.starts_at, date) and not isinstance(
                definition.starts_at, datetime
            )
            assert isinstance(definition.ends_at, date) and not isinstance(
                definition.ends_at, datetime
            )
            duration_days = max(1, (definition.ends_at - definition.starts_at).days)
        else:
            assert isinstance(definition.starts_at, datetime)
            assert isinstance(definition.ends_at, datetime)
            duration = definition.ends_at - definition.starts_at
        for occurrence in _occurrence_starts(definition, query):
            if occurrence in definition.exdates:
                continue
            if definition.all_day:
                assert isinstance(occurrence, date) and not isinstance(occurrence, datetime)
                starts_at = datetime.combine(
                    occurrence,
                    datetime.min.time(),
                    tzinfo=ZoneInfo(definition.timezone),
                )
                ends_at = starts_at + timedelta(days=duration_days)
            else:
                assert isinstance(occurrence, datetime)
                starts_at = occurrence
                ends_at = occurrence + duration
            if (
                starts_at.astimezone(UTC) >= query.ends_at
                or ends_at.astimezone(UTC) <= query.starts_at
            ):
                continue
            occurrence_key = occurrence.isoformat()
            event_digest = hashlib.sha256(
                f"{definition.uid}|{occurrence_key}".encode("utf-8", "replace")
            ).hexdigest()[:40]
            try:
                events.append(
                    CalendarEvent(
                        event_id=f"ical-{event_digest}",
                        calendar_id=f"ical.{feed.feed_id.hex}",
                        title=definition.title,
                        starts_at=starts_at,
                        ends_at=ends_at,
                        all_day=definition.all_day,
                        timezone=definition.timezone,
                        source=SourceRef(
                            provider_id=ICAL_PROVIDER_ID,
                            connection_id=f"ical.{feed.feed_id.hex}",
                            resource_id=f"ical-{hashlib.sha256(definition.uid.encode('utf-8', 'replace')).hexdigest()[:40]}",
                            display_name=feed.title,
                            origin=source_origin,
                        ),
                        deep_link=definition.url or feed_url,
                        location=definition.location,
                        sensitive=True,
                    )
                )
            except ValidationError:
                continue
            if len(events) >= 5_000:
                return tuple(events)
    events.sort(key=lambda event: (event.starts_at, event.ends_at, event.event_id))
    return tuple(events)


def parse_ical_calendar(
    body: bytes | str,
    *,
    feed: IcalCalendarFeed,
    query: CalendarQuery,
) -> tuple[CalendarEvent, ...]:
    """Parse and filter one iCalendar response into provider-neutral events."""

    if not isinstance(feed, IcalCalendarFeed):
        raise TypeError("parse_ical_calendar expects an IcalCalendarFeed")
    if not isinstance(query, CalendarQuery):
        raise TypeError("parse_ical_calendar expects a CalendarQuery")
    definitions = _parse_definitions(body, fallback_timezone=query.timezone)
    return _materialize_events(definitions, feed=feed, query=query)


def parse_ical_calendar_items(
    body: bytes | str,
    *,
    fallback_timezone: str = "UTC",
) -> tuple[IcalCalendarItem, ...]:
    """Return every bounded VEVENT once without applying a calendar view window.

    This is intentionally a task-import boundary rather than another calendar
    snapshot API. Recurrence rules are not expanded: a recurring VEVENT remains
    one source item, while feeds that publish concrete occurrences continue to
    produce one item per VEVENT.
    """

    timezone = _zone(fallback_timezone, "UTC").key
    definitions = _parse_definitions(body, fallback_timezone=timezone)
    items: list[IcalCalendarItem] = []
    seen: set[str] = set()
    for definition in definitions:
        item_id = hashlib.sha256(definition.uid.encode("utf-8", "replace")).hexdigest()[:40]
        if item_id in seen:
            continue
        seen.add(item_id)
        starts_at = definition.starts_at
        if definition.all_day:
            assert isinstance(starts_at, date) and not isinstance(starts_at, datetime)
            due_at = datetime.combine(
                starts_at,
                datetime.min.time(),
                tzinfo=ZoneInfo(definition.timezone),
            )
        else:
            assert isinstance(starts_at, datetime)
            due_at = starts_at
        try:
            items.append(
                IcalCalendarItem(
                    item_id=item_id,
                    title=definition.title,
                    due_at=due_at,
                    due_is_all_day=definition.all_day,
                    timezone=definition.timezone,
                    description=definition.description,
                    location=definition.location,
                    deep_link=definition.url,
                )
            )
        except ValidationError:
            continue
    items.sort(key=lambda item: (item.due_at, item.title.casefold(), item.item_id))
    return tuple(items)


parse_ical_feed = parse_ical_calendar
parse_ical_events = parse_ical_calendar


class IcalFeedFetcher:
    """Reuse the bounded FeedFetcher network policy for calendar MIME types."""

    def __init__(
        self,
        fetcher: FeedFetcher | None = None,
        *,
        transport: object | None = None,
        allow_http: bool = False,
        offline: bool = False,
        **kwargs: object,
    ) -> None:
        if fetcher is not None and transport is not None:
            raise ValueError("pass either fetcher or transport, not both")
        self.fetcher = fetcher or FeedFetcher(
            transport,
            allow_http=allow_http,
            offline=offline,
            **kwargs,
        )

    @property
    def offline(self) -> bool:
        return self.fetcher.offline

    @property
    def allow_http(self) -> bool:
        return self.fetcher.allow_http

    @allow_http.setter
    def allow_http(self, value: bool) -> None:
        self.fetcher.allow_http = bool(value)

    def set_offline(self, value: bool) -> None:
        self.fetcher.set_offline(value)

    set_offline_mode = set_offline

    async def fetch(
        self,
        feed: IcalCalendarFeed,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FeedResponse:
        if not isinstance(feed, IcalCalendarFeed):
            raise TypeError("IcalFeedFetcher expects an IcalCalendarFeed")
        reviewed = () if feed.reviewed_origin is None else (feed.reviewed_origin,)
        return await self.fetcher.fetch_url(
            str(feed.feed_url),
            allow_http=self.fetcher.allow_http and feed.allow_http,
            reviewed_origins=reviewed,
            etag=etag,
            last_modified=last_modified,
            accept=ICAL_RESPONSE_ACCEPT,
        )


class IcalCalendarSource:
    """One conditional, read-only iCalendar source registered in CalendarService."""

    def __init__(
        self,
        feed: IcalCalendarFeed,
        fetcher: IcalFeedFetcher,
        *,
        sync_state: IcalSyncState | None = None,
        on_state_change: Callable[[IcalSyncState], None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(feed, IcalCalendarFeed):
            raise TypeError("IcalCalendarSource expects an IcalCalendarFeed")
        if not isinstance(fetcher, IcalFeedFetcher):
            raise TypeError("IcalCalendarSource expects an IcalFeedFetcher")
        self.feed = feed
        self.fetcher = fetcher
        self.source_id = f"ical.{feed.feed_id.hex}"
        self.provider_id = ICAL_PROVIDER_ID
        self.display_name = feed.title
        self.calendar_id = f"ical.{feed.feed_id.hex}"
        self._state = sync_state or IcalSyncState(feed_id=feed.feed_id)
        if self._state.feed_id != feed.feed_id:
            raise ValueError("iCalendar sync state identity does not match the feed")
        self._on_state_change = on_state_change
        self._clock = clock or (lambda: datetime.now(UTC))
        self._raw_body: bytes | None = None
        self._last_event_count = 0

    @property
    def sync_state(self) -> IcalSyncState:
        return self._state

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("iCalendar source clock must return an aware timestamp")
        return value.astimezone(UTC)

    def _set_state(self, **changes: object) -> None:
        self._state = self._state.model_copy(update=changes)
        if self._on_state_change is not None:
            self._on_state_change(self._state)

    def _mark_failure(self, state: IcalSyncStateValue, safe_error: str | None = None) -> None:
        now = self._now()
        self._set_state(
            state=state,
            last_checked_at=now,
            failure_count=min(self._state.failure_count + 1, 1_000_000),
            safe_error=safe_error,
        )

    async def status(self) -> CalendarStatus:
        now = self._now()
        if self.fetcher.offline:
            state: IcalSyncStateValue = "offline"
            message = "Calendar refresh is paused by offline mode."
        else:
            state = self._state.state
            message = {
                "ready": "iCalendar feed is ready.",
                "empty": "iCalendar feed is ready but has no events.",
                "stale": "Showing the last successful iCalendar snapshot.",
                "offline": "Calendar refresh is paused by offline mode.",
                "permission_required": "iCalendar feed permission is required.",
                "disconnected": "iCalendar feed is disconnected.",
                "error": "iCalendar feed refresh failed safely.",
            }[state]
        return CalendarStatus(
            source_id=self.source_id,
            provider_id=self.provider_id,
            state=state,
            checked_at=self._state.last_checked_at or now,
            item_count=self._last_event_count,
            safe_message=message,
        )

    async def list_events(self, query: CalendarQuery) -> CalendarSnapshot:
        if not isinstance(query, CalendarQuery):
            raise TypeError("iCalendar source expects a CalendarQuery")
        try:
            response = await self.fetcher.fetch(
                self.feed,
                etag=self._state.etag if self._raw_body is not None else None,
                last_modified=self._state.last_modified if self._raw_body is not None else None,
            )
            if response.status_code == 304:
                if self._raw_body is None:
                    raise FeedUnavailableError("iCalendar cache is unavailable")
                body = self._raw_body
            elif response.status_code == 200:
                body = response.body
            elif response.status_code in {401, 403}:
                raise CalendarPermissionRequiredError("iCalendar feed permission is required")
            elif response.status_code == 404:
                raise CalendarDisconnectedError("iCalendar feed was not found")
            elif response.status_code in {408, 425, 429} or response.status_code >= 500:
                raise CalendarUnavailableError("iCalendar feed is temporarily unavailable")
            else:
                raise CalendarInvalidResponseError("iCalendar feed returned an invalid response")
            events = parse_ical_calendar(body, feed=self.feed, query=query)
            if response.status_code == 200:
                # Cache only a body that passed the bounded parser. A malformed
                # 200 response must not become the cache used by a later 304.
                self._raw_body = body
            now = self._now()
            self._last_event_count = len(events)
            self._set_state(
                state="ready" if events else "empty",
                etag=_response_header(response, "etag") or self._state.etag,
                last_modified=_response_header(response, "last-modified")
                or self._state.last_modified,
                last_checked_at=now,
                last_success_at=now,
                failure_count=0,
                safe_error=None,
            )
            return CalendarSnapshot(
                source_id=self.source_id,
                provider_id=self.provider_id,
                collected_at=now,
                events=events,
                is_complete=len(events) < query.limit,
                selected_calendar_ids=(self.calendar_id,),
            )
        except CalendarPermissionRequiredError:
            self._mark_failure("permission_required", "iCalendar feed permission is required.")
            raise
        except CalendarDisconnectedError:
            self._mark_failure("disconnected", "iCalendar feed is disconnected.")
            raise
        except CalendarOfflineError:
            self._mark_failure("offline", "Calendar refresh is paused by offline mode.")
            raise
        except FeedOfflineError as error:
            self._mark_failure("offline", "Calendar refresh is paused by offline mode.")
            raise CalendarOfflineError("Calendar refresh is paused by offline mode.") from error
        except (
            FeedParseError,
            FeedPolicyError,
            FeedResponseTooLargeError,
            IcalParseError,
        ) as error:
            self._mark_failure("error", "iCalendar feed response could not be read safely.")
            raise CalendarInvalidResponseError(
                "iCalendar feed response could not be read safely"
            ) from error
        except (FeedUnavailableError, TimeoutError, OSError, ConnectionError) as error:
            self._mark_failure("error", "iCalendar feed is temporarily unavailable.")
            raise CalendarUnavailableError("iCalendar feed is temporarily unavailable") from error


class IcalFeedRepository:
    """Persist iCal definitions and sync metadata in the Hub settings store."""

    def __init__(self, settings_repository: SettingsRepository) -> None:
        self.settings_repository = settings_repository

    def _records(self) -> list[dict[str, object]]:
        value = self.settings_repository.get(ICAL_FEED_SETTINGS_KEY, [])
        if not isinstance(value, list):
            return []
        records: list[dict[str, object]] = []
        for item in value[:_MAX_FEEDS]:
            if isinstance(item, Mapping):
                records.append(dict(item))
        return records

    @staticmethod
    def _feed_from_value(value: object) -> IcalCalendarFeed:
        if not isinstance(value, Mapping):
            raise ValueError("stored iCalendar feed is not an object")
        payload = dict(value)
        if not isinstance(payload.get("feed_id"), UUID):
            try:
                payload["feed_id"] = UUID(str(payload["feed_id"]))
            except (AttributeError, TypeError, ValueError) as error:
                raise ValueError("stored iCalendar feed ID is invalid") from error
        return IcalCalendarFeed.model_validate(payload)

    @staticmethod
    def _state_from_value(value: object) -> IcalSyncState:
        if not isinstance(value, Mapping):
            raise ValueError("stored iCalendar sync state is not an object")
        payload = dict(value)
        if not isinstance(payload.get("feed_id"), UUID):
            try:
                payload["feed_id"] = UUID(str(payload["feed_id"]))
            except (AttributeError, TypeError, ValueError) as error:
                raise ValueError("stored iCalendar sync ID is invalid") from error
        for field_name in ("last_checked_at", "last_success_at"):
            value = payload.get(field_name)
            if isinstance(value, str):
                try:
                    payload[field_name] = datetime.fromisoformat(value)
                except ValueError as error:
                    raise ValueError("stored iCalendar sync timestamp is invalid") from error
        return IcalSyncState.model_validate(payload)

    def list(self, *, enabled: bool | None = None) -> tuple[IcalCalendarFeed, ...]:
        feeds: list[IcalCalendarFeed] = []
        for record in self._records():
            raw_feed = record.get("feed", record)
            try:
                feed = self._feed_from_value(raw_feed)
            except (TypeError, ValueError, ValidationError):
                continue
            if enabled is None or feed.enabled is enabled:
                feeds.append(feed)
        feeds.sort(key=lambda feed: (feed.title.casefold(), str(feed.feed_id)))
        return tuple(feeds)

    def get(self, feed_id: UUID | str) -> IcalCalendarFeed | None:
        try:
            selected = UUID(str(feed_id))
        except (ValueError, AttributeError, TypeError):
            return None
        return next((feed for feed in self.list() if feed.feed_id == selected), None)

    def sync_state(self, feed_id: UUID | str) -> IcalSyncState | None:
        try:
            selected = UUID(str(feed_id))
        except (ValueError, AttributeError, TypeError):
            return None
        for record in self._records():
            try:
                feed = self._feed_from_value(record.get("feed", record))
                if feed.feed_id != selected:
                    continue
                raw_state = record.get("sync")
                if raw_state is None:
                    return IcalSyncState(feed_id=selected)
                state = self._state_from_value(raw_state)
                return state if state.feed_id == selected else IcalSyncState(feed_id=selected)
            except (TypeError, ValueError, ValidationError):
                continue
        return None

    def add(self, feed: IcalCalendarFeed) -> IcalCalendarFeed:
        if not isinstance(feed, IcalCalendarFeed):
            raise TypeError("IcalFeedRepository expects an IcalCalendarFeed")
        records = self._records()
        record = {"feed": feed.model_dump(mode="json")}
        replaced = False
        for index, existing in enumerate(records):
            try:
                existing_feed = self._feed_from_value(existing.get("feed", existing))
            except (TypeError, ValueError, ValidationError):
                continue
            if existing_feed.feed_id == feed.feed_id:
                record["sync"] = existing.get("sync")
                records[index] = record
                replaced = True
                break
        if not replaced:
            if len(records) >= _MAX_FEEDS:
                raise ValueError("Personal Hub supports at most 100 iCalendar feeds")
            records.append(record)
        self.settings_repository.set(ICAL_FEED_SETTINGS_KEY, records)
        return feed

    upsert = add

    def save_sync_state(self, state: IcalSyncState) -> IcalSyncState:
        if not isinstance(state, IcalSyncState):
            raise TypeError("IcalFeedRepository expects an IcalSyncState")
        records = self._records()
        for record in records:
            try:
                feed = self._feed_from_value(record.get("feed", record))
            except (TypeError, ValueError, ValidationError):
                continue
            if feed.feed_id == state.feed_id:
                record["sync"] = state.model_dump(mode="json")
                self.settings_repository.set(ICAL_FEED_SETTINGS_KEY, records)
                return state
        return state

    def reset_sync_state(self, feed_id: UUID | str) -> IcalSyncState:
        try:
            selected = UUID(str(feed_id))
        except (ValueError, AttributeError, TypeError) as error:
            raise KeyError("Unknown iCalendar feed") from error
        state = IcalSyncState(feed_id=selected)
        self.save_sync_state(state)
        return state

    def delete(self, feed_id: UUID | str) -> bool:
        try:
            selected = UUID(str(feed_id))
        except (ValueError, AttributeError, TypeError):
            return False
        records = self._records()
        retained: list[dict[str, object]] = []
        deleted = False
        for record in records:
            try:
                feed = self._feed_from_value(record.get("feed", record))
            except (TypeError, ValueError, ValidationError):
                retained.append(record)
                continue
            if feed.feed_id == selected:
                deleted = True
            else:
                retained.append(record)
        if deleted:
            self.settings_repository.set(ICAL_FEED_SETTINGS_KEY, retained)
        return deleted


class IcalFeedManager:
    """Own persistent iCal feed definitions and calendar source registration."""

    def __init__(
        self,
        settings_repository: SettingsRepository,
        calendar_service: object,
        *,
        fetcher: IcalFeedFetcher | None = None,
    ) -> None:
        from personal_hub.calendar.service import CalendarService

        if not isinstance(calendar_service, CalendarService):
            raise TypeError("IcalFeedManager expects a CalendarService")
        self.repository = IcalFeedRepository(settings_repository)
        self.calendar_service = calendar_service
        self.fetcher = fetcher or IcalFeedFetcher(offline=calendar_service.offline)
        self._sources: dict[UUID, IcalCalendarSource] = {}
        self._restore()

    @staticmethod
    def source_id_for(feed_id: UUID | str) -> str:
        return f"ical.{UUID(str(feed_id)).hex}"

    def _restore(self) -> None:
        for feed in self.repository.list(enabled=True):
            self._register(feed)

    def reload(self) -> None:
        """Reconcile registered sources with the current local definitions."""

        for feed_id in tuple(self._sources):
            self._unregister(feed_id)
        self._restore()

    def _register(self, feed: IcalCalendarFeed) -> IcalCalendarSource:
        source_id = self.source_id_for(feed.feed_id)
        if source_id in self.calendar_service.source_ids:
            self.calendar_service.unregister(source_id)
        source = IcalCalendarSource(
            feed,
            self.fetcher,
            sync_state=self.repository.sync_state(feed.feed_id),
            on_state_change=self.repository.save_sync_state,
        )
        self.calendar_service.register(source, source_id=source_id)
        self._sources[feed.feed_id] = source
        return source

    def _unregister(self, feed_id: UUID) -> None:
        source_id = self.source_id_for(feed_id)
        if source_id in self.calendar_service.source_ids:
            self.calendar_service.unregister(source_id)
        self._sources.pop(feed_id, None)

    def list(self, *, enabled: bool | None = None) -> tuple[IcalCalendarFeed, ...]:
        return self.repository.list(enabled=enabled)

    feeds = list
    subscriptions = list

    def get(self, feed_id: UUID | str) -> IcalCalendarFeed | None:
        return self.repository.get(feed_id)

    def create(
        self,
        *,
        title: str,
        feed_url: str,
        feed_id: UUID | None = None,
        enabled: bool = True,
        refresh_minutes: int = 30,
        allow_http: bool = False,
    ) -> IcalCalendarFeed:
        normalized_url = feed_url.strip()
        reviewed_origin = (
            origin_for_url(normalized_url)
            if urlsplit(normalized_url).scheme.casefold() == "http" and allow_http
            else None
        )
        feed = IcalCalendarFeed(
            feed_id=feed_id or uuid4(),
            title=" ".join(title.split()),
            feed_url=normalized_url,
            enabled=enabled,
            refresh_minutes=refresh_minutes,
            allow_http=allow_http,
            reviewed_origin=reviewed_origin,
        )
        self.repository.add(feed)
        if feed.enabled:
            self._register(feed)
        return feed

    add = create
    import_feed = create

    def update(self, feed_id: UUID | str, **changes: object) -> IcalCalendarFeed | None:
        current = self.repository.get(feed_id)
        if current is None:
            return None
        values = current.model_dump(mode="python")
        values.update(changes)
        values["feed_id"] = current.feed_id
        if "feed_url" in values:
            values["feed_url"] = str(values["feed_url"]).strip()
        allow_http = bool(values.get("allow_http", False))
        feed_url = str(values["feed_url"])
        values["reviewed_origin"] = (
            origin_for_url(feed_url)
            if urlsplit(feed_url).scheme.casefold() == "http" and allow_http
            else None
        )
        updated = IcalCalendarFeed.model_validate(values)
        changed_origin = (
            str(current.feed_url) != str(updated.feed_url)
            or current.allow_http != updated.allow_http
        )
        self.repository.add(updated)
        if changed_origin:
            self.repository.reset_sync_state(updated.feed_id)
        self._unregister(updated.feed_id)
        if updated.enabled:
            self._register(updated)
        return updated

    update_feed = update

    def delete(self, feed_id: UUID | str) -> bool:
        try:
            selected = UUID(str(feed_id))
        except (ValueError, AttributeError, TypeError):
            return False
        self._unregister(selected)
        return self.repository.delete(selected)

    delete_feed = delete

    def set_enabled(self, feed_id: UUID | str, enabled: bool) -> IcalCalendarFeed | None:
        if type(enabled) is not bool:
            raise TypeError("iCalendar feed enabled state must be a bool")
        return self.update(feed_id, enabled=enabled)

    set_feed_enabled = set_enabled

    def set_offline(self, value: bool) -> None:
        self.fetcher.set_offline(value)

    set_offline_mode = set_offline

    def set_allow_http(self, value: bool) -> None:
        self.fetcher.allow_http = bool(value)


IcalFeed = IcalCalendarFeed
ICalFeed = IcalCalendarFeed
IcalCalendarAdapter = IcalCalendarSource
ICalendarSource = IcalCalendarSource
IcalFeedSource = IcalCalendarSource
IcalCalendarManager = IcalFeedManager
IcalCalendarService = IcalFeedManager


__all__ = [
    "ICAL_FEED_SETTINGS_KEY",
    "ICAL_PROVIDER_ID",
    "ICAL_RESPONSE_ACCEPT",
    "ICalFeed",
    "ICalendarSource",
    "IcalCalendarAdapter",
    "IcalCalendarFeed",
    "IcalCalendarItem",
    "IcalCalendarManager",
    "IcalCalendarService",
    "IcalCalendarSource",
    "IcalError",
    "IcalFeed",
    "IcalFeedFetcher",
    "IcalFeedManager",
    "IcalFeedRepository",
    "IcalFeedSource",
    "IcalParseError",
    "IcalSyncState",
    "parse_ical_calendar",
    "parse_ical_calendar_items",
    "parse_ical_events",
    "parse_ical_feed",
]
