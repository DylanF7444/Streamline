"""Read-only Microsoft Graph Outlook Calendar source for Personal Hub.

The adapter deliberately owns a very small boundary: it obtains a delegated token for
the exact calendar-read scope, performs bounded ``GET`` requests against fixed Graph
calendar-view paths, and maps only validated event metadata into Hub calendar contracts.
It has no mutation methods and does not import another product's integration layer.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol, runtime_checkable
from urllib.parse import parse_qs, quote, urlsplit, urlunsplit
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
    CalendarResult,
    CalendarSnapshot,
    CalendarStatus,
    CalendarUnavailableError,
)
from personal_hub.contracts import SourceRef

OUTLOOK_CALENDAR_READ_SCOPE = "Calendars.Read"
OUTLOOK_CALENDAR_SCOPE = OUTLOOK_CALENDAR_READ_SCOPE
CALENDARS_READ_SCOPE = OUTLOOK_CALENDAR_READ_SCOPE
OUTLOOK_REQUIRED_SCOPES = (OUTLOOK_CALENDAR_READ_SCOPE,)
REQUIRED_SCOPES = OUTLOOK_REQUIRED_SCOPES
OUTLOOK_PROVIDER_ID = "outlook-calendar"
OUTLOOK_SOURCE_ID = "outlook-calendar"
GRAPH_ORIGIN = "https://graph.microsoft.com"
OUTLOOK_GRAPH_API_ROOT = f"{GRAPH_ORIGIN}/v1.0"
MICROSOFT_GRAPH_API_ROOT = OUTLOOK_GRAPH_API_ROOT
OUTLOOK_CALENDAR_API_ROOT = OUTLOOK_GRAPH_API_ROOT
OUTLOOK_CALENDAR_ORIGIN = "https://outlook.office.com"
OUTLOOK_CALENDAR_READONLY_SCOPE = OUTLOOK_CALENDAR_READ_SCOPE

_GRAPH_SELECT = (
    "id,subject,start,end,isAllDay,webLink,location,showAs,responseStatus,type,"
    "seriesMasterId,isCancelled"
)
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z", re.ASCII)
_OUTLOOK_LINK_HOSTS = frozenset(
    {
        "outlook.office.com",
        "outlook.live.com",
        "outlook.office365.com",
        "outlook.office.de",
        "outlook.office365.us",
        "outlook.office365.cn",
        "outlook.cloud.microsoft",
    }
)
_WINDOWS_TIMEZONES = {
    "Dateline Standard Time": "Etc/GMT+12",
    "UTC": "UTC",
    "GMT Standard Time": "Europe/London",
    "W. Europe Standard Time": "Europe/Berlin",
    "Central Europe Standard Time": "Europe/Budapest",
    "Romance Standard Time": "Europe/Paris",
    "Russian Standard Time": "Europe/Moscow",
    "Arabian Standard Time": "Asia/Dubai",
    "India Standard Time": "Asia/Kolkata",
    "China Standard Time": "Asia/Shanghai",
    "Tokyo Standard Time": "Asia/Tokyo",
    "AUS Eastern Standard Time": "Australia/Sydney",
    "New Zealand Standard Time": "Pacific/Auckland",
    "Pacific Standard Time": "America/Los_Angeles",
    "Mountain Standard Time": "America/Denver",
    "Central Standard Time": "America/Chicago",
    "Eastern Standard Time": "America/New_York",
    "Atlantic Standard Time": "America/Halifax",
    "Newfoundland Standard Time": "America/St_Johns",
    "E. South America Standard Time": "America/Sao_Paulo",
}
_STATE_MESSAGES = {
    "ready": "Outlook Calendar is ready.",
    "empty": "Outlook Calendar has no events in the selected range.",
    "stale": "Showing the last successful Outlook Calendar snapshot.",
    "offline": "Outlook Calendar refresh is paused by offline mode.",
    "permission_required": "Outlook Calendar permission is required.",
    "disconnected": "Outlook Calendar is disconnected.",
    "error": "Outlook Calendar refresh failed safely.",
}


@runtime_checkable
class OutlookTokenProvider(Protocol):
    """Injected source of a Hub-scoped Microsoft access token."""

    async def get_token(self, scopes: tuple[str, ...]) -> str: ...


@runtime_checkable
class OutlookCalendarTransport(Protocol):
    """Injected async Graph GET transport used by the adapter."""

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str | int],
    ) -> object: ...


class OutlookCalendarOfflineError(CalendarOfflineError):
    """Raised when a direct event read is attempted while Hub offline mode is enabled."""


class OutlookCalendarSource:
    """Bounded, read-only Outlook Calendar source.

    ``transport`` is an injected async Graph transport.  It may expose
    ``request(method, path, ...)`` or ``get(path, ...)``; the adapter always supplies
    ``GET`` and a fixed Graph calendar-view path.  ``token_provider`` may expose an
    asynchronous ``access_token``/``get_access_token`` method, be an async callable,
    or return a token value directly.  The adapter never persists either object or
    remote event content.
    """

    source_id = OUTLOOK_SOURCE_ID
    provider_id = OUTLOOK_PROVIDER_ID
    display_name = "Outlook Calendar"
    required_scopes = OUTLOOK_REQUIRED_SCOPES

    def __init__(
        self,
        token_provider: Any,
        transport: Any,
        *,
        source_id: str = OUTLOOK_SOURCE_ID,
        provider_id: str = OUTLOOK_PROVIDER_ID,
        display_name: str = "Outlook Calendar",
        connection_id: str | None = None,
        timeout_seconds: float = 20.0,
        max_pages: int = 20,
        max_response_bytes: int = 2_000_000,
        max_events: int = 1_000,
        max_events_per_page: int = 250,
        max_calendars: int = 50,
        max_window: timedelta = timedelta(days=366),
        offline: bool = False,
        offline_getter: Callable[[], bool] | None = None,
    ) -> None:
        # Google and Microsoft callers commonly choose opposite positional orders.
        # Detect the unambiguous injected boundaries while accepting either order.
        if _looks_like_transport(token_provider) and not _looks_like_transport(transport):
            transport, token_provider = token_provider, transport
        if transport is None:
            raise TypeError("Outlook Calendar requires an injected Graph transport")
        if token_provider is None:
            raise TypeError("Outlook Calendar requires an injected token provider")
        if not _looks_like_transport(transport) and not callable(transport):
            raise TypeError("Outlook Calendar transport must provide an async request boundary")
        if not _looks_like_token_provider(token_provider):
            raise TypeError("Outlook Calendar token provider must provide an access-token boundary")
        self.source_id = _stable_config_id(source_id, "source_id")
        self.provider_id = _stable_config_id(provider_id, "provider_id")
        self.display_name = _bounded_label(display_name)
        if connection_id is not None:
            self.connection_id = _stable_config_id(connection_id, "connection_id")
        else:
            self.connection_id = None
        self.transport = transport
        self.token_provider = token_provider
        self.timeout_seconds = _validate_timeout(timeout_seconds)
        self.max_pages = _validate_bound(max_pages, 1, 20, "max_pages")
        self.max_response_bytes = _validate_bound(
            max_response_bytes, 1_024, 20_000_000, "max_response_bytes"
        )
        self.max_events = _validate_bound(max_events, 1, 5_000, "max_events")
        self.max_events_per_page = _validate_bound(
            max_events_per_page, 1, 250, "max_events_per_page"
        )
        self.max_calendars = _validate_bound(max_calendars, 1, 100, "max_calendars")
        if max_window <= timedelta(0) or max_window > timedelta(days=366):
            raise ValueError("max_window must be positive and at most 366 days")
        self.max_window = max_window
        self.scopes = OUTLOOK_REQUIRED_SCOPES
        self._offline_getter = offline_getter
        self._offline_value = bool(offline)
        self._last_good: dict[tuple[Any, ...], CalendarSnapshot] = {}

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

    def set_offline(self, enabled: bool) -> None:
        self._offline_getter = None
        self._offline_value = bool(enabled)

    set_offline_mode = set_offline
    set_offline_lock = set_offline

    def validate_query(self, query: CalendarQuery) -> CalendarQuery:
        """Validate and enforce the adapter's stricter network bounds."""

        try:
            validated = CalendarQuery.model_validate(query)
        except ValidationError as error:
            raise CalendarInvalidResponseError("Invalid calendar query") from error
        if len(validated.calendar_ids) > self.max_calendars:
            raise CalendarInvalidResponseError("Too many selected calendars")
        if validated.ends_at - validated.starts_at > self.max_window:
            raise CalendarInvalidResponseError("Calendar query window is too large")
        return validated

    async def status(self, query: CalendarQuery | None = None) -> CalendarStatus:
        """Return a safe connection status without exposing provider response text."""

        checked_at = datetime.now(UTC)
        if self.offline:
            return self._status("offline", checked_at=checked_at)
        try:
            token = await self._access_token()
            if not token:
                return self._status("disconnected", checked_at=checked_at)
            self._ensure_online()
            await self._request(
                "/me/calendars",
                {"$select": "id", "$top": 1},
                token,
            )
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            state = _state_for_error(error)
            return self._status(state, checked_at=checked_at)
        count = 0
        if query is not None:
            with_context = self._try_validate_query(query)
            if with_context is not None:
                previous = self._last_good.get(self._cache_key(with_context))
                count = len(previous.events) if previous is not None else 0
        return self._status("ready", item_count=count, checked_at=checked_at)

    health = status

    async def list_events(
        self,
        query: CalendarQuery,
        *,
        timeout_seconds: float | None = None,
    ) -> CalendarSnapshot:
        """Fetch selected calendar views with bounded GET-only pagination."""

        validated = self.validate_query(query)
        self._ensure_online()
        token = await self._access_token(timeout_seconds=timeout_seconds)
        if not token:
            raise CalendarDisconnectedError("Outlook Calendar is disconnected")
        self._ensure_online()
        selected = validated.calendar_ids or (None,)
        events: list[CalendarEvent] = []
        seen_events: set[tuple[str, str]] = set()
        is_complete = True
        for calendar_id in selected:
            page_token: str | None = None
            seen_tokens: set[str] = set()
            for page_number in range(self.max_pages):
                self._ensure_online()
                event_limit = min(validated.limit, self.max_events)
                remaining = event_limit - len(events)
                if remaining <= 0:
                    is_complete = False
                    break
                params: dict[str, str | int] = {
                    "$select": _GRAPH_SELECT,
                    "startDateTime": _rfc3339(validated.starts_at),
                    "endDateTime": _rfc3339(validated.ends_at),
                    "$top": min(self.max_events_per_page, remaining),
                }
                if page_token is not None:
                    params["$skiptoken"] = page_token
                payload = await self._request(
                    _calendar_path(calendar_id),
                    params,
                    token,
                    timeout_seconds=timeout_seconds,
                )
                values = payload.get("value", [])
                if not isinstance(values, list):
                    raise CalendarInvalidResponseError(
                        "Outlook Calendar returned an invalid item list"
                    )
                for resource in values[:remaining]:
                    if not isinstance(resource, Mapping):
                        continue
                    event = self._event_from_resource(resource, calendar_id, validated)
                    if event is None:
                        continue
                    identity = (event.calendar_id, event.event_id)
                    if identity in seen_events:
                        continue
                    seen_events.add(identity)
                    events.append(event)
                    if len(events) >= event_limit:
                        break
                if len(events) >= event_limit:
                    is_complete = False
                    break
                next_token = _continuation_token(payload)
                if next_token is None:
                    break
                if next_token in seen_tokens:
                    is_complete = False
                    break
                if page_number + 1 >= self.max_pages:
                    is_complete = False
                    break
                seen_tokens.add(next_token)
                page_token = next_token
        events.sort(key=_event_sort_key)
        return CalendarSnapshot(
            source_id=self.source_id,
            provider_id=self.provider_id,
            collected_at=datetime.now(UTC),
            events=tuple(events[: min(validated.limit, self.max_events)]),
            is_complete=is_complete,
            selected_calendar_ids=validated.calendar_ids,
        )

    async def collect(
        self,
        query: CalendarQuery,
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[CalendarEvent, ...]:
        """Compatibility projection for provider registries that consume event tuples."""

        snapshot = await self.list_events(query, timeout_seconds=timeout_seconds)
        return snapshot.events

    async def refresh(
        self,
        query: CalendarQuery,
        *,
        timeout_seconds: float | None = None,
    ) -> CalendarResult:
        """Return a safe state envelope, retaining a last-good snapshot in memory."""

        validated = self.validate_query(query)
        key = self._cache_key(validated)
        checked_at = datetime.now(UTC)
        previous = self._last_good.get(key)
        if self.offline:
            return self._result(
                "offline",
                checked_at=checked_at,
                snapshot=previous,
                last_success_at=previous.collected_at if previous is not None else None,
            )
        try:
            snapshot = await self.list_events(validated, timeout_seconds=timeout_seconds)
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            state = _state_for_error(error)
            if state == "offline":
                return self._result(
                    state,
                    checked_at=checked_at,
                    snapshot=previous,
                    last_success_at=previous.collected_at if previous is not None else None,
                )
            if previous is not None and state == "error":
                state = "stale"
            return self._result(
                state,
                checked_at=checked_at,
                snapshot=previous,
                last_success_at=previous.collected_at if previous is not None else None,
            )
        self._last_good[key] = snapshot
        state = "ready" if snapshot.events else "empty"
        return self._result(
            state,
            checked_at=checked_at,
            snapshot=snapshot,
            last_success_at=snapshot.collected_at,
        )

    get_events = list_events
    refresh_events = refresh

    async def _access_token(self, *, timeout_seconds: float | None = None) -> str | None:
        self._ensure_online()
        provider = self.token_provider
        method = None
        for name in ("access_token", "get_access_token", "token", "get_token"):
            candidate = getattr(provider, name, None)
            if candidate is not None:
                method = candidate
                break
        if method is None:
            method = provider if callable(provider) else None
        if method is None:
            return _token_value(provider)
        try:
            result = _call_with_scope(method)
            if inspect.isawaitable(result):
                async with asyncio.timeout(self._timeout(timeout_seconds)):
                    result = await result
        except CalendarError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise _map_outlook_error(error) from None
        self._ensure_online()
        return _token_value(result)

    async def _request(
        self,
        path: str,
        query: Mapping[str, str | int],
        token: str,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        self._ensure_online()
        request = getattr(self.transport, "request", None)
        request_kind = "request"
        if not callable(request):
            request = getattr(self.transport, "get", None)
            request_kind = "get"
        if not callable(request) and callable(self.transport):
            request = self.transport
            request_kind = "callable"
        if not callable(request):
            raise CalendarDisconnectedError("Outlook Calendar transport is unavailable")
        target = _transport_target(request, path)
        args: tuple[Any, ...] = ("GET", target) if request_kind == "request" else (target,)
        kwargs = _transport_kwargs(request, query=query, token=token)
        try:
            async with asyncio.timeout(self._timeout(timeout_seconds)):
                result = request(*args, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
                result = await _response_payload(result)
        except CalendarError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise _map_outlook_error(error) from None
        self._ensure_online()
        return _bounded_payload(result, self.max_response_bytes)

    def _event_from_resource(
        self,
        resource: Mapping[str, Any],
        calendar_id: str | None,
        query: CalendarQuery,
    ) -> CalendarEvent | None:
        if resource.get("isCancelled") is True or resource.get("status") == "cancelled":
            return None
        raw_event_id = resource.get("id")
        if not isinstance(raw_event_id, str) or not raw_event_id.strip():
            return None
        event_id = _stable_event_id(raw_event_id)
        selected_calendar_id = calendar_id or "default"
        start_resource = resource.get("start")
        end_resource = resource.get("end")
        if not isinstance(start_resource, Mapping) or not isinstance(end_resource, Mapping):
            return None
        start_zone, timezone_name = _graph_timezone(
            start_resource.get("timeZone") or end_resource.get("timeZone"),
            fallback=query.timezone,
        )
        all_day = bool(resource.get("isAllDay")) or (
            isinstance(start_resource.get("date"), str)
            and isinstance(end_resource.get("date"), str)
        )
        if all_day:
            starts_at = _graph_date_or_datetime(start_resource, start_zone)
            ends_at = _graph_date_or_datetime(end_resource, start_zone)
        else:
            starts_at = _graph_datetime(start_resource.get("dateTime"), start_zone)
            ends_at = _graph_datetime(end_resource.get("dateTime"), start_zone)
        if starts_at is None or ends_at is None or ends_at <= starts_at:
            return None
        if ends_at.astimezone(UTC) <= query.starts_at or starts_at.astimezone(UTC) >= query.ends_at:
            return None
        title = _bounded_event_text(resource.get("subject"), default="(No title)", limit=240)
        location_value = resource.get("location")
        location = None
        if isinstance(location_value, Mapping):
            location = _bounded_optional_text(location_value.get("displayName"), limit=512)
        elif location_value is not None:
            location = _bounded_optional_text(location_value, limit=512)
        deep_link = _safe_https_link(resource.get("webLink"))
        try:
            return CalendarEvent(
                event_id=event_id,
                calendar_id=selected_calendar_id,
                title=title,
                starts_at=starts_at,
                ends_at=ends_at,
                all_day=all_day,
                timezone=timezone_name,
                source=SourceRef(
                    provider_id=self.provider_id,
                    connection_id=self.connection_id,
                    resource_id=event_id,
                    display_name=self.display_name,
                    origin=GRAPH_ORIGIN,
                ),
                deep_link=deep_link,
                location=location,
                sensitive=True,
            )
        except (ValidationError, ValueError):
            return None

    def _status(
        self,
        state: str,
        *,
        item_count: int = 0,
        checked_at: datetime | None = None,
    ) -> CalendarStatus:
        return CalendarStatus(
            source_id=self.source_id,
            provider_id=self.provider_id,
            state=state,
            checked_at=checked_at or datetime.now(UTC),
            item_count=item_count,
            safe_message=_state_message(state),
        )

    def _result(
        self,
        state: str,
        *,
        checked_at: datetime,
        snapshot: CalendarSnapshot | None,
        last_success_at: datetime | None,
    ) -> CalendarResult:
        return CalendarResult(
            source_id=self.source_id,
            provider_id=self.provider_id,
            state=state,
            checked_at=checked_at,
            snapshot=snapshot,
            last_success_at=last_success_at,
            safe_message=_state_message(state),
        )

    def _cache_key(self, query: CalendarQuery) -> tuple[Any, ...]:
        return (
            query.starts_at,
            query.ends_at,
            query.calendar_ids,
            query.timezone,
            query.limit,
        )

    def _try_validate_query(self, query: CalendarQuery) -> CalendarQuery | None:
        try:
            return self.validate_query(query)
        except CalendarError:
            return None

    def _timeout(self, timeout_seconds: float | None) -> float:
        return (
            self.timeout_seconds if timeout_seconds is None else _validate_timeout(timeout_seconds)
        )

    def _ensure_online(self) -> None:
        if self.offline:
            raise OutlookCalendarOfflineError("Outlook Calendar is offline")


# Discoverable aliases for composition roots and provider registries.
OutlookCalendarAdapter = OutlookCalendarSource
OutlookCalendarProvider = OutlookCalendarSource
MicrosoftOutlookCalendarSource = OutlookCalendarSource


def _state_message(state: str) -> str:
    return _STATE_MESSAGES.get(state, _STATE_MESSAGES["error"])


def _state_for_error(error: BaseException) -> str:
    if isinstance(error, OutlookCalendarOfflineError):
        return "offline"
    if isinstance(error, (CalendarPermissionRequiredError, PermissionError)):
        return "permission_required"
    status = getattr(error, "status_code", getattr(error, "status", None))
    if status in {401, 403}:
        return "permission_required"
    if status == 404:
        return "disconnected"
    if isinstance(status, int) and (status in {408, 409, 425, 429} or status >= 500):
        return "error"
    if isinstance(error, CalendarDisconnectedError):
        return "disconnected"
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return "error"
    if isinstance(error, (ConnectionError, OSError)):
        return "disconnected"
    if isinstance(error, CalendarUnavailableError):
        return "error"
    return "error"


def _map_outlook_error(error: Exception) -> CalendarError:
    if isinstance(error, CalendarError):
        return error
    state = _state_for_error(error)
    if state == "permission_required":
        return CalendarPermissionRequiredError("Outlook Calendar permission is required")
    if state == "disconnected":
        return CalendarDisconnectedError("Outlook Calendar is disconnected")
    if state == "offline":
        return OutlookCalendarOfflineError("Outlook Calendar is offline")
    return CalendarUnavailableError("Outlook Calendar request failed safely")


def _looks_like_transport(value: Any) -> bool:
    return callable(getattr(value, "request", None)) or callable(getattr(value, "get", None))


def _looks_like_token_provider(value: Any) -> bool:
    if callable(value):
        return True
    if isinstance(value, Mapping):
        return _token_value(value) is not None
    return any(
        callable(getattr(value, name, None))
        for name in ("access_token", "get_access_token", "token", "get_token")
    ) or isinstance(getattr(value, "access_token", None), str)


def _transport_target(method: Callable[..., Any], path: str) -> str:
    try:
        names = inspect.signature(method).parameters
    except (TypeError, ValueError):
        names = {}
    if "url" in names and "path" not in names:
        return f"{OUTLOOK_GRAPH_API_ROOT}{path}"
    return path


def _call_with_scope(method: Callable[..., Any]) -> Any:
    """Call token providers with a signature-aware exact scope argument."""

    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return method(OUTLOOK_REQUIRED_SCOPES)
    parameters = signature.parameters
    if "scopes" in parameters:
        return method(scopes=OUTLOOK_REQUIRED_SCOPES)
    if "required_scopes" in parameters:
        return method(required_scopes=OUTLOOK_REQUIRED_SCOPES)
    if "scope" in parameters:
        return method(scope=OUTLOOK_CALENDAR_READ_SCOPE)
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        return method(scopes=OUTLOOK_REQUIRED_SCOPES)
    positional = tuple(
        parameter
        for parameter in parameters.values()
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        and parameter.default is inspect.Parameter.empty
    )
    if positional or any(
        parameter.kind is inspect.Parameter.VAR_POSITIONAL for parameter in parameters.values()
    ):
        return method(OUTLOOK_REQUIRED_SCOPES)
    return method()


def _transport_kwargs(
    method: Callable[..., Any], *, query: Mapping[str, str | int], token: str
) -> dict[str, Any]:
    try:
        signature = inspect.signature(method)
        parameters = signature.parameters
    except (TypeError, ValueError):
        parameters = {}
    names = set(parameters)
    has_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    )
    kwargs: dict[str, Any] = {}
    if "query" in names or (has_kwargs and "params" not in names):
        kwargs["query"] = dict(query)
    elif "params" in names:
        kwargs["params"] = dict(query)
    if "access_token" in names:
        kwargs["access_token"] = token
    elif "token" in names:
        kwargs["token"] = token
    elif "bearer" in names:
        kwargs["bearer"] = token
    elif "headers" in names or has_kwargs:
        kwargs["headers"] = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
    return kwargs


async def _response_payload(value: Any) -> Any:
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], int):
        status, payload = value
        if status >= 400:
            error = (
                CalendarPermissionRequiredError()
                if status in {401, 403}
                else CalendarUnavailableError()
            )
            error.status_code = status
            raise error
        return payload
    if isinstance(value, (Mapping, bytes, bytearray, str)):
        return value
    status = getattr(value, "status_code", getattr(value, "status", None))
    if isinstance(status, int) and status >= 400:
        error = (
            CalendarPermissionRequiredError()
            if status in {401, 403}
            else CalendarUnavailableError()
        )
        error.status_code = status
        raise error
    json_method = getattr(value, "json", None)
    if callable(json_method):
        payload = json_method()
        return await payload if inspect.isawaitable(payload) else payload
    read_method = getattr(value, "read", None)
    if callable(read_method):
        payload = read_method()
        return await payload if inspect.isawaitable(payload) else payload
    return value


def _bounded_payload(value: Any, maximum: int) -> dict[str, Any]:
    if isinstance(value, (bytes, bytearray)):
        if len(value) > maximum:
            raise CalendarInvalidResponseError("Outlook Calendar response is too large")
        try:
            value = json.loads(bytes(value).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CalendarInvalidResponseError("Outlook Calendar response is invalid") from error
    elif isinstance(value, str):
        encoded = value.encode("utf-8")
        if len(encoded) > maximum:
            raise CalendarInvalidResponseError("Outlook Calendar response is too large")
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise CalendarInvalidResponseError("Outlook Calendar response is invalid") from error
    if not isinstance(value, Mapping):
        raise CalendarInvalidResponseError("Outlook Calendar response is not an object")
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise CalendarInvalidResponseError("Outlook Calendar response is invalid") from error
    if len(encoded) > maximum:
        raise CalendarInvalidResponseError("Outlook Calendar response is too large")
    payload = dict(value)
    remote_error = payload.get("error")
    if isinstance(remote_error, Mapping):
        code = str(remote_error.get("code", "")).casefold()
        if any(marker in code for marker in ("auth", "permission", "access")):
            raise CalendarPermissionRequiredError("Outlook Calendar permission is required")
        raise CalendarUnavailableError("Outlook Calendar returned an error")
    return payload


def _continuation_token(payload: Mapping[str, Any]) -> str | None:
    value = (
        payload.get("@odata.nextLink") or payload.get("nextLink") or payload.get("next_page_token")
    )
    if not isinstance(value, str) or not value or len(value) > 4_096:
        return None
    if value.startswith("https://"):
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.hostname not in {
            "graph.microsoft.com",
            "graph.microsoft.us",
        }:
            return None
        query = parse_qs(parsed.query, keep_blank_values=False)
        candidates = query.get("$skiptoken") or query.get("skiptoken")
        value = candidates[0] if candidates else ""
    if not value or len(value) > 2_048 or any(character in value for character in "\r\n"):
        return None
    return value


def _calendar_path(calendar_id: str | None) -> str:
    if calendar_id is None or calendar_id == "":
        return "/me/calendarView"
    return f"/me/calendars/{quote(calendar_id, safe='')}/calendarView"


def _graph_timezone(value: Any, *, fallback: str = "UTC") -> tuple[ZoneInfo, str]:
    normalized = str(value).strip() if value is not None else fallback
    if not normalized:
        normalized = fallback
    mapped = _WINDOWS_TIMEZONES.get(normalized, normalized)
    try:
        zone = ZoneInfo(mapped)
    except (ZoneInfoNotFoundError, ValueError):
        try:
            zone = ZoneInfo(fallback)
            mapped = fallback
        except (ZoneInfoNotFoundError, ValueError):
            zone = ZoneInfo("UTC")
            mapped = "UTC"
    return zone, mapped


def _graph_date_or_datetime(resource: Mapping[str, Any], zone: ZoneInfo) -> datetime | None:
    date_value = resource.get("date")
    if isinstance(date_value, str):
        try:
            parsed = date.fromisoformat(date_value)
        except ValueError:
            return None
        return datetime.combine(parsed, datetime.min.time(), tzinfo=zone)
    return _graph_datetime(resource.get("dateTime"), zone)


def _graph_datetime(value: Any, zone: ZoneInfo) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace("Z", "+00:00")
    normalized = re.sub(r"\.(\d{6})\d+(?=(?:[+-]\d\d:\d\d)?$)", r".\1", normalized)
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed


def _safe_https_link(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 1_024
        or any(ord(character) < 32 for character in normalized)
    ):
        return None
    try:
        parsed = urlsplit(normalized)
        hostname = parsed.hostname
        if parsed.scheme.casefold() != "https" or not hostname:
            return None
        hostname = hostname.casefold()
        if hostname not in _OUTLOOK_LINK_HOSTS:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        if "\\" in normalized:
            return None
        port = parsed.port
        netloc = hostname if port in (None, 443) else f"{hostname}:{port}"
    except ValueError:
        return None
    return urlunsplit(("https", netloc, parsed.path or "/", parsed.query, ""))


def _stable_event_id(value: str) -> str:
    normalized = value.strip()
    if len(normalized) <= 128 and _IDENTIFIER_RE.fullmatch(normalized):
        return normalized
    digest = hashlib.sha256(normalized.encode("utf-8", "replace")).hexdigest()[:32]
    return f"outlook-event-{digest}"


def _event_sort_key(event: CalendarEvent) -> tuple[Any, ...]:
    return (
        event.starts_at,
        event.ends_at,
        event.calendar_id.casefold(),
        event.event_id.casefold(),
        event.title.casefold(),
    )


def _bounded_event_text(value: Any, *, default: str, limit: int) -> str:
    if value is None:
        return default
    normalized = " ".join(str(value).split())[:limit]
    return normalized or default


def _bounded_optional_text(value: Any, *, limit: int) -> str | None:
    if value is None:
        return None
    normalized = " ".join(str(value).split())[:limit]
    return normalized or None


def _stable_config_id(value: str, field_name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value.strip()) is None:
        raise ValueError(f"{field_name} must be a bounded identifier")
    return value.strip()


def _bounded_label(value: str) -> str:
    normalized = " ".join(str(value).split())
    if not normalized or len(normalized) > 160:
        raise ValueError("display_name must be a bounded non-empty string")
    return normalized


def _token_value(value: Any) -> str | None:
    if isinstance(value, Mapping):
        value = value.get("access_token") or value.get("token") or value.get("value")
    else:
        value = getattr(value, "access_token", value)
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 8_192
        or any(ord(character) < 32 for character in normalized)
    ):
        return None
    return normalized


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _validate_timeout(value: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0 or value > 300:
        raise ValueError("timeout_seconds must be greater than 0 and at most 300")
    return float(value)


def _validate_bound(value: int, minimum: int, maximum: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


__all__ = [
    "CALENDARS_READ_SCOPE",
    "GRAPH_ORIGIN",
    "MICROSOFT_GRAPH_API_ROOT",
    "MICROSOFT_OUTLOOK_CALENDAR_SOURCE",
    "OUTLOOK_CALENDAR_ADAPTER",
    "OUTLOOK_CALENDAR_API_ROOT",
    "OUTLOOK_CALENDAR_ORIGIN",
    "OUTLOOK_GRAPH_API_ROOT",
    "OUTLOOK_CALENDAR_READ_SCOPE",
    "OUTLOOK_CALENDAR_READONLY_SCOPE",
    "OUTLOOK_CALENDAR_SCOPE",
    "OUTLOOK_PROVIDER_ID",
    "OUTLOOK_REQUIRED_SCOPES",
    "OUTLOOK_SOURCE_ID",
    "OutlookCalendarAdapter",
    "OutlookCalendarOfflineError",
    "OutlookCalendarProvider",
    "OutlookCalendarSource",
    "OutlookCalendarTransport",
    "OutlookTokenProvider",
    "MicrosoftOutlookCalendarSource",
    "REQUIRED_SCOPES",
]


OUTLOOK_CALENDAR_ADAPTER = OutlookCalendarAdapter
MICROSOFT_OUTLOOK_CALENDAR_SOURCE = OutlookCalendarSource
