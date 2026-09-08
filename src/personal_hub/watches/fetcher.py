"""GET-only, SSRF-resistant, bounded webpage collection for watches."""

from __future__ import annotations

import inspect
from collections.abc import Mapping

from personal_hub.feeds.contracts import (
    FeedPolicyError,
    FeedRedirectError,
    FeedSSRFError,
    FeedUnavailableError,
)
from personal_hub.feeds.fetcher import UrllibFeedTransport
from personal_hub.feeds.security import (
    HostResolver,
    normalize_origins,
    origin_for_url,
    validate_redirect,
    validate_url_for_request,
)
from personal_hub.watches.contracts import (
    PageResponse,
    WatchDefinition,
    WatchOfflineError,
    WatchPolicyError,
    WatchUnavailableError,
)

_REDIRECTS = frozenset({301, 302, 303, 307, 308})


def _header(headers: Mapping[str, object], name: str) -> str | None:
    wanted = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() != wanted:
            continue
        text = str(value).strip()
        if len(text) > 1_024 or any(character in text for character in "\r\n\x00"):
            return None
        return text or None
    return None


def _headers(value: object) -> dict[str, str]:
    items = value.items() if hasattr(value, "items") else ()
    result: dict[str, str] = {}
    for key, item in items:
        name, text = str(key).strip(), str(item).strip()
        if (
            name
            and len(name) <= 128
            and len(text) <= 1_024
            and not any(character in name + text for character in "\r\n\x00")
        ):
            result[name] = text
    return result


def _response(value: object, *, fallback_url: str) -> PageResponse:
    if isinstance(value, PageResponse):
        return value
    status = getattr(value, "status_code", getattr(value, "status", None))
    headers = getattr(value, "headers", {})
    body = getattr(value, "body", getattr(value, "content", b""))
    url = getattr(value, "url", fallback_url)
    if isinstance(value, Mapping):
        status = value.get("status_code", value.get("status", status))
        headers = value.get("headers", headers)
        body = value.get("body", value.get("content", body))
        url = value.get("url", url)
    if isinstance(body, str):
        body = body.encode("utf-8")
    try:
        return PageResponse(
            status_code=int(status),
            headers=_headers(headers),
            body=body,
            url=str(url),
        )
    except (TypeError, ValueError) as error:
        raise WatchUnavailableError("The watched origin returned an invalid response.") from error


async def _call_transport(
    transport: object,
    url: str,
    *,
    headers: Mapping[str, str],
    timeout: float,
    max_bytes: int,
) -> object:
    callback = getattr(transport, "request", None)
    get_only = False
    if callback is None:
        callback = getattr(transport, "get", None)
        get_only = True
    if callback is None and callable(transport):
        callback = transport
        get_only = True
    if not callable(callback):
        raise WatchUnavailableError("The webpage transport is unavailable.")
    kwargs = {"headers": headers, "timeout": timeout, "max_bytes": max_bytes, "params": {}}
    try:
        signature = inspect.signature(callback)
        accepts_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
        selected = (
            kwargs
            if accepts_kwargs
            else {key: item for key, item in kwargs.items() if key in signature.parameters}
        )
        value = callback(url, **selected) if get_only else callback("GET", url, **selected)
        return await value if inspect.isawaitable(value) else value
    except WatchUnavailableError:
        raise
    except Exception as error:
        raise WatchUnavailableError("The watched origin could not be reached.") from error


class PageFetcher:
    """Collect bytes without a browser engine, scripts, cookies, or form support."""

    def __init__(
        self,
        transport: object | None = None,
        *,
        timeout_seconds: float = 20,
        max_response_bytes: int = 8_000_000,
        max_redirects: int = 3,
        allow_http: bool = False,
        resolver: HostResolver | None = None,
        resolve_host: bool | None = None,
        offline: bool = False,
    ) -> None:
        if not 0 < timeout_seconds <= 60:
            raise ValueError("watch timeout must be between 0 and 60 seconds")
        if not 1_024 <= max_response_bytes < 10_000_000:
            raise ValueError("watch response limit must be below 10 MB")
        if not 0 <= max_redirects <= 10:
            raise ValueError("watch redirect limit must be between 0 and 10")
        self.transport = transport or UrllibFeedTransport()
        self.timeout_seconds = float(timeout_seconds)
        self.max_response_bytes = max_response_bytes
        self.max_redirects = max_redirects
        self.allow_http = bool(allow_http)
        self.resolver = resolver
        self.resolve_host = (transport is None) if resolve_host is None else bool(resolve_host)
        self.offline = bool(offline)

    def set_offline(self, value: bool) -> None:
        self.offline = bool(value)

    async def fetch(self, watch: WatchDefinition) -> PageResponse:
        if not isinstance(watch, WatchDefinition):
            raise TypeError("PageFetcher expects a WatchDefinition")
        if self.offline:
            raise WatchOfflineError("Watch refresh is paused by offline mode.")
        allow_http = self.allow_http and watch.allow_http
        approved = set(normalize_origins(watch.approved_redirect_origins))
        approved.add(watch.reviewed_origin)
        try:
            url = await validate_url_for_request(
                str(watch.source_url),
                allow_http=allow_http,
                reviewed_origins=approved,
                resolver=self.resolver,
                resolve_host=self.resolve_host,
            )
            if origin_for_url(url) != watch.reviewed_origin:
                raise WatchPolicyError("The watched URL origin was not explicitly approved.")
            headers = {
                "Accept": "text/html, application/xhtml+xml, application/json;q=0.8, text/plain;q=0.7",
                "Accept-Encoding": "identity",
                "User-Agent": "PersonalHub/1.0 webpage-watch",
            }
            for redirect_count in range(self.max_redirects + 1):
                if self.offline:
                    raise WatchOfflineError("Watch refresh is paused by offline mode.")
                raw = await _call_transport(
                    self.transport,
                    url,
                    headers=headers,
                    timeout=self.timeout_seconds,
                    max_bytes=self.max_response_bytes,
                )
                response = _response(raw, fallback_url=url)
                if response.url and response.url != url:
                    validate_redirect(
                        url,
                        response.url,
                        allow_http=allow_http,
                        approved_origins=approved,
                        resolver=self.resolver,
                        resolve_host=self.resolve_host,
                    )
                if response.status_code not in _REDIRECTS:
                    return self._bounded(response)
                location = _header(response.headers, "location")
                if location is None or redirect_count >= self.max_redirects:
                    raise WatchPolicyError("The watch redirect could not be followed safely.")
                url = validate_redirect(
                    url,
                    location,
                    allow_http=allow_http,
                    approved_origins=approved,
                    resolver=self.resolver,
                    resolve_host=self.resolve_host,
                )
        except WatchOfflineError:
            raise
        except WatchPolicyError:
            raise
        except (FeedPolicyError, FeedRedirectError, FeedSSRFError) as error:
            raise WatchPolicyError("The watch URL failed network safety validation.") from error
        except FeedUnavailableError as error:
            raise WatchUnavailableError("The watched origin could not be reached.") from error
        raise WatchPolicyError("The watch redirect could not be followed safely.")

    def _bounded(self, response: PageResponse) -> PageResponse:
        length = _header(response.headers, "content-length")
        if length is not None:
            try:
                if int(length) < 0 or int(length) > self.max_response_bytes:
                    raise WatchUnavailableError("The watched page exceeded the byte limit.")
            except ValueError as error:
                raise WatchUnavailableError("The watched page returned an invalid size.") from error
        encoding = (_header(response.headers, "content-encoding") or "identity").casefold()
        if encoding not in {"", "identity"}:
            raise WatchUnavailableError("Compressed watched pages are not supported.")
        if len(response.body) > self.max_response_bytes:
            raise WatchUnavailableError("The watched page exceeded the byte limit.")
        return response


__all__ = ["PageFetcher"]
