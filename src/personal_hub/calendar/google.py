"""Bounded, read-only Google Calendar adapter owned by Personal Hub.

The adapter deliberately accepts an injected token provider and transport.  It does
not discover Assistant credentials, construct an HTTP client, or expose a write
operation.  Provider responses are reduced to the strict calendar contracts before
they leave this module.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, time
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError

from personal_hub.calendar.contracts import (
    CalendarDisconnectedError,
    CalendarError,
    CalendarEvent,
    CalendarInvalidResponseError,
    CalendarOfflineError,
    CalendarPermissionRequiredError,
    CalendarQuery,
    CalendarSnapshot,
    CalendarState,
    CalendarStatus,
    CalendarUnavailableError,
)
from personal_hub.contracts import SourceRef

GOOGLE_CALENDAR_READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
# Short aliases keep provider wiring readable while retaining one canonical scope.
GOOGLE_CALENDAR_SCOPE = GOOGLE_CALENDAR_READONLY_SCOPE
CALENDAR_SCOPE = GOOGLE_CALENDAR_READONLY_SCOPE
GOOGLE_CALENDAR_API_ROOT = "https://www.googleapis.com/calendar/v3"
GOOGLE_CALENDAR_ORIGIN = "https://calendar.google.com"
_SHORT_READONLY_SCOPE = "calendar.readonly"
_MAX_PAGE_TOKEN_LENGTH = 4_096
_MAX_DISPLAY_LENGTH = 240
_MAX_LOCATION_LENGTH = 512
_MAX_CALENDAR_NAME_LENGTH = 160
_MAX_TOKEN_LENGTH = 16_384
_MAX_REQUEST_ITEMS = 250
_MAX_PAGES = 50
_MAX_RESPONSE_BYTES = 2_000_000
_CALENDAR_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:%+@#=-"
)
_WRITE_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/calendar.app.created",
    }
)
_DELETED_EVENT_STATUSES = frozenset({"cancelled", "canceled", "deleted"})


@runtime_checkable
class GoogleTokenProvider(Protocol):
    """Injected source of a Hub-owned Google OAuth access token."""

    async def get_token(self, scopes: tuple[str, ...]) -> str: ...


@runtime_checkable
class GoogleCalendarTransport(Protocol):
    """Small injected GET transport boundary used by the adapter."""

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str | int | bool],
    ) -> object: ...


class GoogleCalendarAdapter:
    """Read selected Google Calendar events with bounded pagination.

    The constructor requires an explicit token provider and transport so callers
    can bind the adapter to the Personal Hub credential namespace and network
    policy.  No Assistant vault or transport is consulted implicitly.
    """

    source_id = "google-calendar"
    provider_id = "google-calendar"
    display_name = "Google Calendar"

    def __init__(
        self,
        token_provider: GoogleTokenProvider | Callable[..., object] | object,
        transport: GoogleCalendarTransport | Callable[..., object] | object,
        *,
        source_id: str = "google-calendar",
        provider_id: str = "google-calendar",
        display_name: str = "Google Calendar",
        connection_id: str | None = None,
        scopes: Sequence[str] = (GOOGLE_CALENDAR_READONLY_SCOPE,),
        max_calendars: int = 50,
        max_events: int = 1_000,
        max_pages: int = 20,
        max_response_bytes: int = _MAX_RESPONSE_BYTES,
        discover_calendars: bool = False,
        offline: bool = False,
        offline_getter: Callable[[], bool] | None = None,
    ) -> None:
        self.source_id = _bounded_identifier(source_id, "source_id")
        self.provider_id = _bounded_identifier(provider_id, "provider_id")
        self.display_name = _display_text(display_name, "Google Calendar")
        if connection_id is not None:
            connection_id = _bounded_identifier(connection_id, "connection_id")
        self.connection_id = connection_id
        self.scopes = _validate_scopes(scopes)
        if not 1 <= max_calendars <= 100:
            raise ValueError("Google Calendar max_calendars must be between 1 and 100")
        if not 1 <= max_events <= 5_000:
            raise ValueError("Google Calendar max_events must be between 1 and 5000")
        if not 1 <= max_pages <= _MAX_PAGES:
            raise ValueError("Google Calendar max_pages is out of bounds")
        if not 1_024 <= max_response_bytes <= 20_000_000:
            raise ValueError("Google Calendar max_response_bytes is out of bounds")
        if type(discover_calendars) is not bool:
            raise TypeError("Google Calendar discover_calendars must be a bool")
        if (
            not callable(getattr(transport, "request", None))
            and not callable(getattr(transport, "get", None))
            and not callable(transport)
        ):
            raise TypeError("Google Calendar transport must provide an async request boundary")
        if (
            not callable(getattr(token_provider, "get_token", None))
            and not callable(token_provider)
            and not isinstance(getattr(token_provider, "access_token", None), str)
        ):
            raise TypeError("Google Calendar token provider must provide an access-token boundary")
        self.token_provider = token_provider
        self.transport = transport
        self.max_calendars = max_calendars
        self.max_events = max_events
        self.max_pages = max_pages
        self.max_response_bytes = max_response_bytes
        self.discover_calendars = discover_calendars
        self._offline_getter = offline_getter
        self._offline_value = bool(offline) if offline_getter is None else False
        self._last_state: CalendarState | None = None
        self._last_status_message: str | None = None

    @property
    def offline(self) -> bool:
        return (
            bool(self._offline_getter())
            if self._offline_getter is not None
            else self._offline_value
        )

    @property
    def offline_mode(self) -> bool:
        return self.offline

    def set_offline(self, value: bool) -> None:
        self._offline_getter = None
        self._offline_value = bool(value)

    set_offline_mode = set_offline

    async def status(self) -> CalendarStatus:
        """Return adapter health without acquiring a token or calling Google."""

        if self.offline:
            state: CalendarState = "offline"
            message = "Calendar refresh is paused by offline mode."
        else:
            state = self._last_state or "ready"
            message = self._last_status_message or _state_message(state)
        return CalendarStatus(
            source_id=self.source_id,
            provider_id=self.provider_id,
            state=state,
            checked_at=datetime.now(UTC),
            item_count=0,
            safe_message=message,
        )

    async def list_events(self, query: CalendarQuery) -> CalendarSnapshot:
        """Fetch only event metadata for the selected calendars and time range."""

        if not isinstance(query, CalendarQuery):
            raise TypeError("Google Calendar expects CalendarQuery")
        if len(query.calendar_ids) > self.max_calendars:
            raise ValueError("Google Calendar query selects too many calendars")
        self._check_offline()
        try:
            token = await self._access_token()
            metadata: dict[str, _CalendarMetadata] = {}
            available_calendar_ids: tuple[str, ...] = ()
            calendar_display_names: dict[str, str] = {}
            if query.calendar_ids:
                selected = query.calendar_ids
                metadata = {
                    calendar_id: _CalendarMetadata(
                        calendar_id=calendar_id,
                        name=calendar_id,
                        timezone=query.timezone,
                    )
                    for calendar_id in selected
                }
                if self.discover_calendars:
                    try:
                        discovered = await self._list_calendars(token)
                    except CalendarError:
                        # Event refresh remains useful when calendar-list discovery is
                        # temporarily unavailable. The saved selection is still fetched.
                        discovered = {}
                    metadata.update(discovered)
                    for calendar_id in selected:
                        metadata.setdefault(
                            calendar_id,
                            _CalendarMetadata(
                                calendar_id=calendar_id,
                                name=calendar_id,
                                timezone=query.timezone,
                            ),
                        )
                    discovered_ids = tuple(sorted(discovered, key=str.casefold))
                    available_unselected = [
                        calendar_id for calendar_id in discovered_ids if calendar_id not in selected
                    ][: max(0, self.max_calendars - len(selected))]
                    available_calendar_ids = tuple(
                        sorted(
                            (*selected, *available_unselected),
                            key=str.casefold,
                        )
                    )
            else:
                metadata = await self._list_calendars(token)
                selected = tuple(sorted(metadata, key=str.casefold))[: self.max_calendars]
                available_calendar_ids = tuple(sorted(metadata, key=str.casefold))

            calendar_display_names = {
                calendar_id: metadata[calendar_id].name
                for calendar_id in available_calendar_ids
                if calendar_id in metadata
            }

            events: list[CalendarEvent] = []
            seen_events: set[tuple[str, str]] = set()
            # An unselected query is bounded to the first ``max_calendars``
            # calendars. It is not authoritative for completions in omitted
            # calendars, even when each fetched calendar is fully paginated.
            is_complete = bool(query.calendar_ids) or len(metadata) < self.max_calendars
            for calendar_id in selected:
                self._check_offline()
                calendar_events, calendar_complete = await self._list_calendar_events(
                    token,
                    query,
                    metadata.get(
                        calendar_id,
                        _CalendarMetadata(
                            calendar_id=calendar_id,
                            name=calendar_id,
                            timezone=query.timezone,
                        ),
                    ),
                )
                for event in calendar_events:
                    identity = (event.calendar_id, event.event_id)
                    if identity in seen_events:
                        continue
                    seen_events.add(identity)
                    events.append(event)
                    if len(events) >= min(query.limit, self.max_events):
                        is_complete = False
                        break
                is_complete = is_complete and calendar_complete
                if len(events) >= min(query.limit, self.max_events):
                    break

            ordered = tuple(sorted(events, key=_event_sort_key))[
                : min(query.limit, self.max_events)
            ]
            self._last_state = "ready" if ordered else "empty"
            self._last_status_message = _state_message(self._last_state)
            return CalendarSnapshot(
                source_id=self.source_id,
                provider_id=self.provider_id,
                collected_at=datetime.now(UTC),
                events=ordered,
                is_complete=is_complete,
                selected_calendar_ids=tuple(selected),
                available_calendar_ids=available_calendar_ids,
                calendar_display_names=calendar_display_names,
            )
        except CalendarError as error:
            self._remember_error(error)
            raise
        except Exception as error:
            mapped = _map_google_error(error)
            self._remember_error(mapped)
            raise mapped from None

    async def _list_calendars(self, token: str) -> dict[str, _CalendarMetadata]:
        metadata: dict[str, _CalendarMetadata] = {}
        page_token: str | None = None
        seen_tokens: set[str] = set()
        for _page_number in range(self.max_pages):
            self._check_offline()
            params: dict[str, str | int | bool] = {
                "maxResults": min(_MAX_REQUEST_ITEMS, self.max_calendars - len(metadata)),
                "showDeleted": False,
            }
            if page_token is not None:
                params["pageToken"] = page_token
            payload = await self._request_json(
                token,
                f"{GOOGLE_CALENDAR_API_ROOT}/users/me/calendarList",
                params,
            )
            raw_items = payload.get("items", [])
            if not isinstance(raw_items, list):
                raise CalendarInvalidResponseError("Google Calendar list response was invalid")
            for raw_item in raw_items:
                if not isinstance(raw_item, Mapping):
                    continue
                calendar_id = _calendar_identifier(raw_item.get("id"))
                if calendar_id is None or calendar_id in metadata:
                    continue
                name = _display_text(
                    raw_item.get("summaryOverride") or raw_item.get("summary"), calendar_id
                )
                timezone = _timezone_or_none(raw_item.get("timeZone"))
                metadata[calendar_id] = _CalendarMetadata(calendar_id, name, timezone)
                if len(metadata) >= self.max_calendars:
                    break
            if len(metadata) >= self.max_calendars:
                break
            page_token = _next_page_token(payload.get("nextPageToken"), seen_tokens)
            if page_token is None:
                break
        return metadata

    async def _list_calendar_events(
        self,
        token: str,
        query: CalendarQuery,
        metadata: _CalendarMetadata,
    ) -> tuple[tuple[CalendarEvent, ...], bool]:
        values: list[CalendarEvent] = []
        page_token: str | None = None
        seen_tokens: set[str] = set()
        is_complete = True
        for _page_number in range(self.max_pages):
            self._check_offline()
            remaining = min(query.limit, self.max_events) - len(values)
            if remaining <= 0:
                is_complete = False
                break
            params: dict[str, str | int | bool] = {
                "timeMin": query.starts_at.isoformat(),
                "timeMax": query.ends_at.isoformat(),
                "timeZone": query.timezone,
                "singleEvents": True,
                "showDeleted": False,
                "orderBy": "startTime",
                "maxResults": min(_MAX_REQUEST_ITEMS, remaining),
            }
            if page_token is not None:
                params["pageToken"] = page_token
            url = (
                f"{GOOGLE_CALENDAR_API_ROOT}/calendars/"
                f"{quote(metadata.calendar_id, safe='')}/events"
            )
            payload = await self._request_json(token, url, params)
            raw_items = payload.get("items", [])
            if not isinstance(raw_items, list):
                raise CalendarInvalidResponseError("Google Calendar event response was invalid")
            for raw_item in raw_items:
                if not isinstance(raw_item, Mapping):
                    continue
                event = _event_from_resource(
                    raw_item,
                    metadata,
                    query,
                    provider_id=self.provider_id,
                    connection_id=self.connection_id,
                )
                if event is None:
                    continue
                values.append(event)
                if len(values) >= min(query.limit, self.max_events):
                    is_complete = False
                    break
            if len(values) >= min(query.limit, self.max_events):
                break
            page_token = _next_page_token(payload.get("nextPageToken"), seen_tokens)
            if page_token is None:
                break
        else:
            if page_token is not None:
                is_complete = False
        return tuple(values), is_complete

    async def _access_token(self) -> str:
        self._check_offline()
        provider = self.token_provider
        callback = getattr(provider, "get_token", None)
        if callback is None and callable(provider):
            callback = provider
        if callback is None:
            token = getattr(provider, "access_token", None)
        else:
            token = _call_with_supported_kwargs(callback, (), {"scopes": self.scopes})
            if inspect.isawaitable(token):
                token = await token
        if not isinstance(token, str):
            raise CalendarDisconnectedError("Google Calendar token is unavailable")
        normalized = token.strip()
        if (
            not normalized
            or len(normalized) > _MAX_TOKEN_LENGTH
            or any(character in normalized for character in "\r\n")
        ):
            raise CalendarDisconnectedError("Google Calendar token is unavailable")
        self._check_offline()
        return normalized

    async def _request_json(
        self,
        token: str,
        url: str,
        params: Mapping[str, str | int | bool],
    ) -> Mapping[str, Any]:
        self._check_offline()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        transport = self.transport
        callback = getattr(transport, "request", None)
        mode = "request"
        if callback is None:
            callback = getattr(transport, "get", None)
            mode = "get"
        if callback is None and callable(transport):
            callback = transport
            mode = "request"
        if callback is None:
            raise CalendarDisconnectedError("Google Calendar transport is unavailable")
        try:
            if mode == "get":
                raw_response = _call_with_supported_kwargs(
                    callback, (url,), {"headers": headers, "params": params}
                )
            else:
                raw_response = _call_with_supported_kwargs(
                    callback,
                    ("GET", url),
                    {"headers": headers, "params": params, "query": params},
                )
            if inspect.isawaitable(raw_response):
                raw_response = await raw_response
        except CalendarError:
            raise
        except Exception as error:
            raise _map_google_error(error) from None
        self._check_offline()
        return await _decode_response(raw_response, self.max_response_bytes)

    def _check_offline(self) -> None:
        if self.offline:
            raise CalendarOfflineError("Google Calendar refresh is offline")

    def _remember_error(self, error: CalendarError) -> None:
        if isinstance(error, CalendarPermissionRequiredError):
            self._last_state = "permission_required"
        elif isinstance(error, CalendarDisconnectedError):
            self._last_state = "disconnected"
        elif isinstance(error, CalendarOfflineError):
            self._last_state = "offline"
        else:
            self._last_state = "error"
        self._last_status_message = _state_message(self._last_state)


class _CalendarMetadata:
    __slots__ = ("calendar_id", "name", "timezone")

    def __init__(self, calendar_id: str, name: str, timezone: str) -> None:
        self.calendar_id = calendar_id
        self.name = name
        self.timezone = timezone


def _validate_scopes(scopes: Sequence[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in scopes:
        if not isinstance(value, str):
            raise TypeError("Google Calendar scopes must be strings")
        scope = value.strip()
        if scope == _SHORT_READONLY_SCOPE:
            scope = GOOGLE_CALENDAR_READONLY_SCOPE
        if not scope or len(scope) > 256 or any(character in scope for character in "\r\n"):
            raise ValueError("Google Calendar scopes must be bounded")
        if scope in _WRITE_SCOPES:
            raise ValueError("Google Calendar adapter accepts read-only scopes only")
        if scope != GOOGLE_CALENDAR_READONLY_SCOPE:
            raise ValueError("Google Calendar accepts only the calendar.readonly scope")
        if scope not in normalized:
            normalized.append(scope)
    if GOOGLE_CALENDAR_READONLY_SCOPE not in normalized:
        raise ValueError("Google Calendar requires the calendar.readonly scope")
    return tuple(normalized)


def _bounded_identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 128
        or any(character not in _CALENDAR_ID_CHARS for character in normalized)
    ):
        raise ValueError(f"{field_name} must be a bounded identifier")
    return normalized


def _calendar_identifier(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 256
        or any(character not in _CALENDAR_ID_CHARS for character in normalized)
    ):
        return None
    return normalized


def _display_text(value: object, fallback: str) -> str:
    text = " ".join(value.split()) if isinstance(value, str) else ""
    return text[:_MAX_CALENDAR_NAME_LENGTH] or fallback[:_MAX_CALENDAR_NAME_LENGTH]


def _event_text(value: object, fallback: str, limit: int) -> str:
    text = " ".join(value.split()) if isinstance(value, str) else ""
    return text[:limit] or fallback


def _timezone_or_none(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > 128:
        return None
    try:
        ZoneInfo(normalized)
    except ZoneInfoNotFoundError:
        return None
    return normalized


def _resolve_timezone(*values: object) -> str:
    for value in values:
        timezone = _timezone_or_none(value)
        if timezone is not None:
            return timezone
    return "UTC"


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: object, timezone: str) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        try:
            parsed = parsed.replace(tzinfo=ZoneInfo(timezone))
        except ZoneInfoNotFoundError:
            return None
    return parsed


def _safe_google_link(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    try:
        parsed = urlsplit(normalized)
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() != "https"
        or parsed.hostname is None
        or parsed.hostname.casefold() not in {"calendar.google.com", "www.google.com"}
        or not parsed.path.startswith("/calendar")
        or parsed.username is not None
        or parsed.password is not None
        or "\\" in normalized
        or any(character in normalized for character in "\r\n")
    ):
        return None
    return normalized[:1_024]


def _event_from_resource(
    resource: Mapping[str, Any],
    metadata: _CalendarMetadata,
    query: CalendarQuery,
    *,
    provider_id: str = "google-calendar",
    connection_id: str | None = None,
) -> CalendarEvent | None:
    status = resource.get("status")
    if isinstance(status, str) and status.strip().casefold() in _DELETED_EVENT_STATUSES:
        return None
    event_id = resource.get("id")
    if not isinstance(event_id, str) or not event_id.strip():
        return None
    event_id = event_id.strip()
    start = resource.get("start")
    end = resource.get("end")
    if not isinstance(start, Mapping) or not isinstance(end, Mapping):
        return None
    timezone = _resolve_timezone(start.get("timeZone"), metadata.timezone, query.timezone)
    if isinstance(start.get("date"), str) and isinstance(end.get("date"), str):
        start_date = _parse_date(start.get("date"))
        end_date = _parse_date(end.get("date"))
        if start_date is None or end_date is None or end_date <= start_date:
            return None
        starts_at = datetime.combine(start_date, time.min, tzinfo=ZoneInfo(timezone))
        ends_at = datetime.combine(end_date, time.min, tzinfo=ZoneInfo(timezone))
        all_day = True
    else:
        starts_at = _parse_datetime(start.get("dateTime"), timezone)
        ends_at = _parse_datetime(end.get("dateTime"), timezone)
        if starts_at is None or ends_at is None:
            return None
        all_day = False
    try:
        source = SourceRef(
            provider_id=provider_id,
            connection_id=connection_id,
            resource_id=event_id,
            display_name=metadata.name,
            origin=GOOGLE_CALENDAR_ORIGIN,
        )
        return CalendarEvent(
            event_id=event_id,
            calendar_id=metadata.calendar_id,
            title=_event_text(resource.get("summary"), "Untitled event", _MAX_DISPLAY_LENGTH),
            starts_at=starts_at,
            ends_at=ends_at,
            all_day=all_day,
            timezone=timezone,
            source=source,
            deep_link=_safe_google_link(resource.get("htmlLink")),
            location=(
                _event_text(resource.get("location"), "", _MAX_LOCATION_LENGTH)
                if isinstance(resource.get("location"), str)
                and resource.get("location", "").strip()
                else None
            ),
            sensitive=True,
        )
    except (ValidationError, ValueError, TypeError):
        return None


async def _decode_response(raw_response: object, max_response_bytes: int) -> Mapping[str, Any]:
    """Decode a small JSON response while keeping body text out of exceptions."""

    if isinstance(raw_response, Mapping):
        if "status_code" in raw_response:
            status = raw_response.get("status_code")
            if isinstance(status, int):
                _raise_for_status(status)
            payload = raw_response.get("json", raw_response.get("payload"))
            if payload is None:
                raise CalendarInvalidResponseError("Google Calendar response was not JSON")
            _check_payload_size(payload, max_response_bytes)
            return _require_payload(payload)
        payload = raw_response
        _check_payload_size(payload, max_response_bytes)
        return _require_payload(payload)
    if isinstance(raw_response, tuple) and len(raw_response) == 2:
        status, payload = raw_response
        if isinstance(status, int):
            _raise_for_status(status)
        _check_payload_size(payload, max_response_bytes)
        return _require_payload(payload)

    status = _response_status(raw_response)
    _raise_for_status(status)
    headers = _response_headers(raw_response)
    content_length = _header_int(headers, "content-length")
    if content_length is not None and content_length > max_response_bytes:
        raise CalendarInvalidResponseError("Google Calendar response exceeded its size limit")
    content = getattr(raw_response, "content", None)
    if isinstance(content, (bytes, bytearray, memoryview)) and len(content) > max_response_bytes:
        raise CalendarInvalidResponseError("Google Calendar response exceeded its size limit")
    json_method = getattr(raw_response, "json", None)
    if not callable(json_method):
        raise CalendarInvalidResponseError("Google Calendar response was not JSON")
    try:
        payload = json_method()
        if inspect.isawaitable(payload):
            payload = await payload
    except Exception:
        raise CalendarInvalidResponseError("Google Calendar response was not JSON") from None
    _check_payload_size(payload, max_response_bytes)
    return _require_payload(payload)


def _response_status(response: object) -> int:
    value = getattr(response, "status_code", getattr(response, "status", 200))
    return value if isinstance(value, int) else 200


def _response_headers(response: object) -> Mapping[str, object]:
    headers = getattr(response, "headers", {})
    return headers if isinstance(headers, Mapping) else {}


def _header_int(headers: Mapping[str, object], key: str) -> int | None:
    for name, value in headers.items():
        if str(name).casefold() == key:
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                return None
            return parsed if parsed >= 0 else None
    return None


def _check_payload_size(payload: object, max_response_bytes: int) -> None:
    try:
        size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError, OverflowError):
        raise CalendarInvalidResponseError("Google Calendar response was not JSON") from None
    if size > max_response_bytes:
        raise CalendarInvalidResponseError("Google Calendar response exceeded its size limit")


def _require_payload(payload: object) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise CalendarInvalidResponseError("Google Calendar response was not an object")
    return payload


def _raise_for_status(status: int) -> None:
    if status < 400:
        return
    if status in {401, 403}:
        raise CalendarPermissionRequiredError("Google Calendar permission is required")
    if status == 404:
        raise CalendarDisconnectedError("Google Calendar is disconnected")
    if status in {408, 409, 425, 429} or status >= 500:
        raise CalendarUnavailableError("Google Calendar is temporarily unavailable")
    raise CalendarError("Google Calendar request failed")


def _map_google_error(error: Exception) -> CalendarError:
    status = getattr(error, "status_code", getattr(error, "status", None))
    if isinstance(status, int):
        try:
            _raise_for_status(status)
        except CalendarError as mapped:
            return mapped
    if isinstance(error, PermissionError):
        return CalendarPermissionRequiredError("Google Calendar permission is required")
    if isinstance(error, (TimeoutError,)):
        return CalendarUnavailableError("Google Calendar is temporarily unavailable")
    if isinstance(error, (ConnectionError, OSError)):
        return CalendarDisconnectedError("Google Calendar is disconnected")
    if isinstance(error, CalendarError):
        return error
    return CalendarUnavailableError("Google Calendar request failed")


def _state_message(state: CalendarState) -> str:
    return {
        "ready": "Calendar is ready.",
        "empty": "Calendar is ready but has no events.",
        "stale": "Showing the last successful calendar snapshot.",
        "offline": "Calendar refresh is paused by offline mode.",
        "permission_required": "Calendar permission is required.",
        "disconnected": "Calendar is disconnected.",
        "error": "Calendar refresh failed safely.",
    }[state]


def _next_page_token(value: object, seen_tokens: set[str]) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_PAGE_TOKEN_LENGTH
        or value in seen_tokens
    ):
        return None
    seen_tokens.add(value)
    return value


def _call_with_supported_kwargs(
    callback: Callable[..., object], positional: tuple[object, ...], kwargs: Mapping[str, object]
) -> object:
    """Call common injected fake/HTTP boundaries without leaking extra arguments."""

    try:
        parameters = inspect.signature(callback).parameters
    except (TypeError, ValueError):
        return callback(*positional, **dict(kwargs))
    positional_parameters = tuple(
        parameter
        for parameter in parameters.values()
        if parameter.kind
        in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
    )
    if (
        len(positional) >= 2
        and positional_parameters
        and positional_parameters[0].name.casefold() in {"url", "uri", "endpoint"}
    ):
        positional = positional[1:]
    accepts_kwargs = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    )
    selected = (
        dict(kwargs)
        if accepts_kwargs
        else {key: value for key, value in kwargs.items() if key in parameters}
    )
    if "params" not in parameters and not accepts_kwargs and "query" not in parameters:
        selected.pop("params", None)
        selected.pop("query", None)
    return callback(*positional, **selected)


def _event_sort_key(event: CalendarEvent) -> tuple[object, ...]:
    return (
        event.starts_at.astimezone(UTC),
        0 if event.all_day else 1,
        event.ends_at.astimezone(UTC),
        event.title.casefold(),
        event.calendar_id.casefold(),
        event.event_id,
    )


GoogleCalendarSource = GoogleCalendarAdapter
GoogleCalendar = GoogleCalendarAdapter


__all__ = [
    "GOOGLE_CALENDAR_API_ROOT",
    "GOOGLE_CALENDAR_ORIGIN",
    "CALENDAR_SCOPE",
    "GOOGLE_CALENDAR_SCOPE",
    "GOOGLE_CALENDAR_READONLY_SCOPE",
    "GoogleCalendar",
    "GoogleCalendarAdapter",
    "GoogleCalendarSource",
    "GoogleCalendarTransport",
    "GoogleTokenProvider",
]
