"""Bounded asynchronous HTTP collection for RSS and Atom feeds.

The default transport uses ``urllib`` in a worker thread and never follows
redirects itself.  :class:`FeedFetcher` performs redirect revalidation, DNS
private-address checks, conditional-request headers, and response-size limits
before handing a response to the parser.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Iterable, Mapping
from http.client import HTTPMessage
from typing import Any, Protocol, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from personal_hub.feeds.contracts import (
    FeedOfflineError,
    FeedPolicyError,
    FeedRedirectError,
    FeedResponse,
    FeedResponseTooLargeError,
    FeedSubscription,
    FeedUnavailableError,
)
from personal_hub.feeds.security import (
    HostResolver,
    normalize_origins,
    origin_for_url,
    validate_redirect,
    validate_url_for_request,
)

_MAX_HARD_RESPONSE_BYTES = 20_000_000
_MAX_HEADER_VALUE = 1_024
_USER_AGENT = "PersonalHub/1.0 (+https://openai.com/personal-hub)"
_RSS_ACCEPT = (
    "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.1"
)


@runtime_checkable
class FeedTransport(Protocol):
    """Injected network boundary used by deterministic tests and adapters."""

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
        max_bytes: int,
    ) -> object: ...


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


def _header(headers: Mapping[str, object], name: str) -> str | None:
    wanted = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() != wanted:
            continue
        text = str(value).strip()
        if len(text) > _MAX_HEADER_VALUE or any(character in text for character in "\r\n\x00"):
            return None
        return text or None
    return None


def _headers(value: object) -> dict[str, str]:
    if isinstance(value, Mapping):
        result: dict[str, str] = {}
        for key, item in value.items():
            name = str(key).strip()
            text = str(item).strip()
            if (
                not name
                or len(name) > 128
                or len(text) > _MAX_HEADER_VALUE
                or any(character in name + text for character in "\r\n\x00")
            ):
                continue
            result[name] = text
        return result
    if isinstance(value, HTTPMessage):
        return _headers(dict(value.items()))
    items = getattr(value, "items", None)
    if callable(items):
        try:
            return _headers(dict(items()))
        except (TypeError, ValueError):
            return {}
    return {}


def _coerce_response(value: object, *, fallback_url: str) -> FeedResponse:
    """Accept common fake/http-client response shapes without leaking them."""

    if isinstance(value, FeedResponse):
        return value
    if isinstance(value, Mapping):
        status = value.get("status_code", value.get("status", 200))
        headers = value.get("headers", {})
        body = value.get("body", value.get("content", b""))
        response_url = value.get("url", fallback_url)
    else:
        status = getattr(value, "status_code", getattr(value, "status", 200))
        headers = getattr(value, "headers", {})
        body = getattr(value, "content", getattr(value, "body", b""))
        response_url = getattr(value, "url", fallback_url)
    try:
        status = int(status)
    except (TypeError, ValueError) as error:
        raise FeedUnavailableError("Feed transport returned an invalid status.") from error
    if isinstance(body, str):
        body = body.encode("utf-8")
    if not isinstance(body, bytes):
        raise FeedUnavailableError("Feed transport returned an invalid response body.")
    return FeedResponse(
        status_code=status,
        headers=_headers(headers),
        body=body,
        url=str(response_url) if response_url is not None else fallback_url,
    )


async def _call_transport(
    callback: object,
    method: str,
    url: str,
    kwargs: Mapping[str, object],
    *,
    get_only: bool = False,
) -> object:
    if not callable(callback):
        raise FeedUnavailableError("Feed transport is unavailable.")
    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        result = (
            callback(url, **dict(kwargs)) if get_only else callback(method, url, **dict(kwargs))
        )
    else:
        parameters = signature.parameters
        accepts_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
        )
        selected = (
            dict(kwargs)
            if accepts_kwargs
            else {name: value for name, value in kwargs.items() if name in parameters}
        )
        try:
            result = callback(url, **selected) if get_only else callback(method, url, **selected)
        except TypeError:
            # A small number of GET-only test transports expose ``get(url)``
            # instead of ``request(method, url)``.  This fallback only applies
            # to the shape mismatch at the boundary; transport errors are not
            # retried or exposed as raw text.
            if get_only or method != "GET":
                raise
            get_callback = getattr(callback, "get", None)
            if not callable(get_callback):
                raise
            result = get_callback(url, **selected)
    if inspect.isawaitable(result):
        return await result
    return result


def _bounded_body(response: FeedResponse, maximum: int) -> FeedResponse:
    declared = _header(response.headers, "content-length")
    if declared is not None:
        try:
            declared_size = int(declared)
            if declared_size < 0 or declared_size > maximum:
                raise FeedResponseTooLargeError("Feed response exceeded the byte limit.")
        except ValueError:
            raise FeedUnavailableError("Feed response had an invalid size header.") from None
    encoding = (_header(response.headers, "content-encoding") or "identity").casefold()
    if encoding not in {"", "identity"}:
        # Compression is intentionally not decompressed in the generic feed
        # client: a small compressed body can expand without a safe bound.
        raise FeedResponseTooLargeError("Compressed feed responses are not supported.")
    if len(response.body) > maximum:
        raise FeedResponseTooLargeError("Feed response exceeded the byte limit.")
    return response


class UrllibFeedTransport:
    """Default GET-only transport with automatic redirect handling disabled."""

    def __init__(self) -> None:
        self._opener = build_opener(_NoRedirectHandler())

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
        max_bytes: int,
    ) -> FeedResponse:
        if method.upper() != "GET":
            raise FeedPolicyError("Feed transport supports GET only.")
        return await asyncio.to_thread(self._request_sync, url, headers, timeout, max_bytes)

    def _request_sync(
        self,
        url: str,
        headers: Mapping[str, str],
        timeout: float,
        max_bytes: int,
    ) -> FeedResponse:
        request = Request(url, headers=dict(headers), method="GET")
        try:
            response = self._opener.open(request, timeout=timeout)
        except HTTPError as error:
            body = _read_response_body(error, max_bytes)
            return FeedResponse(
                status_code=int(error.code),
                headers=_headers(error.headers),
                body=body,
                url=error.geturl(),
            )
        except (URLError, TimeoutError, OSError) as error:
            raise FeedUnavailableError("Feed origin could not be reached.") from error
        try:
            body = _read_response_body(response, max_bytes)
            return FeedResponse(
                status_code=int(response.status),
                headers=_headers(response.headers),
                body=body,
                url=response.geturl(),
            )
        finally:
            response.close()


def _read_response_body(response: Any, maximum: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(65_536, maximum - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > maximum:
            raise FeedResponseTooLargeError("Feed response exceeded the byte limit.")
        chunks.append(chunk)
    return b"".join(chunks)


class FeedFetcher:
    """Validate and fetch one feed, including bounded redirect handling."""

    def __init__(
        self,
        transport: FeedTransport | object | None = None,
        *,
        timeout_seconds: float = 20.0,
        max_response_bytes: int = 5_000_000,
        max_redirects: int = 3,
        allow_http: bool = False,
        reviewed_origins: Iterable[str] = (),
        resolver: HostResolver | None = None,
        resolve_host: bool | None = None,
        offline: bool = False,
        offline_getter: object | None = None,
    ) -> None:
        if timeout_seconds <= 0 or timeout_seconds > 300:
            raise ValueError("Feed timeout must be greater than 0 and at most 300 seconds")
        if not 1_024 <= max_response_bytes <= _MAX_HARD_RESPONSE_BYTES:
            raise ValueError("Feed response limit is out of bounds")
        if not 0 <= max_redirects <= 10:
            raise ValueError("Feed redirect limit must be between 0 and 10")
        if offline_getter is not None and not callable(offline_getter):
            raise TypeError("offline_getter must be callable")
        self.transport = transport or UrllibFeedTransport()
        self.timeout_seconds = float(timeout_seconds)
        self.max_response_bytes = max_response_bytes
        self.max_redirects = max_redirects
        self.allow_http = bool(allow_http)
        self.reviewed_origins = normalize_origins(reviewed_origins)
        self.resolver = resolver
        # Injected transports are normally deterministic fakes or a separately
        # reviewed connector.  Literal private addresses are still blocked;
        # hostname resolution can be explicitly enabled with ``resolve_host``
        # or a resolver for tests and custom transports.
        self.resolve_host = (transport is None) if resolve_host is None else bool(resolve_host)
        self._offline_getter = offline_getter
        self._offline_value = bool(offline) if offline_getter is None else False

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

    async def fetch(
        self,
        subscription: FeedSubscription,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FeedResponse:
        """Fetch a subscription with conditional headers and safe redirects."""

        if not isinstance(subscription, FeedSubscription):
            raise TypeError("FeedFetcher expects a FeedSubscription")
        if self.offline:
            raise FeedOfflineError("Feed refresh is paused by offline mode.")
        # Plain HTTP is a two-key opt-in: the process policy must permit it and
        # this particular subscription must have been explicitly reviewed.
        allow_http = self.allow_http and bool(getattr(subscription, "allow_http", False))
        reviewed_origins = list(self.reviewed_origins)
        reviewed_origin = getattr(subscription, "reviewed_origin", None)
        if reviewed_origin:
            reviewed_origins.append(reviewed_origin)
        return await self.fetch_url(
            str(subscription.feed_url),
            allow_http=allow_http,
            reviewed_origins=reviewed_origins,
            etag=etag,
            last_modified=last_modified,
            accept=_RSS_ACCEPT,
        )

    async def fetch_url(
        self,
        url: str,
        *,
        allow_http: bool = False,
        reviewed_origins: Iterable[str] = (),
        etag: str | None = None,
        last_modified: str | None = None,
        accept: str = _RSS_ACCEPT,
    ) -> FeedResponse:
        """Fetch one reviewed HTTP(S) URL for another bounded text protocol.

        RSS/Atom subscriptions continue to use :meth:`fetch`; this lower-level
        entry point lets other Hub-owned read-only sources reuse the same URL,
        redirect, response-size, and offline policy without duplicating network
        security code.
        """

        if not isinstance(url, str):
            raise TypeError("FeedFetcher URL must be a string")
        if not isinstance(accept, str) or not accept.strip() or len(accept) > _MAX_HEADER_VALUE:
            raise FeedPolicyError("Feed response type is invalid.")
        if any(character in accept for character in "\r\n\x00"):
            raise FeedPolicyError("Feed response type is invalid.")
        if self.offline:
            raise FeedOfflineError("Feed refresh is paused by offline mode.")
        reviewed = tuple(reviewed_origins)
        url = await validate_url_for_request(
            url,
            allow_http=allow_http,
            reviewed_origins=reviewed,
            resolver=self.resolver,
            resolve_host=self.resolve_host,
        )
        approved_origins = set(normalize_origins(reviewed))
        approved_origins.add(origin_for_url(url))
        headers = {
            "Accept": accept.strip(),
            "Accept-Encoding": "identity",
            "User-Agent": _USER_AGENT,
        }
        if etag is not None:
            _add_conditional_header(headers, "If-None-Match", etag)
        if last_modified is not None:
            _add_conditional_header(headers, "If-Modified-Since", last_modified)

        for redirect_count in range(self.max_redirects + 1):
            if self.offline:
                raise FeedOfflineError("Feed refresh is paused by offline mode.")
            response = await self._request(url, headers)
            # A custom transport must not silently follow a redirect outside
            # this client's policy.  The built-in transport returns each 3xx
            # response explicitly, but this check covers injected HTTP clients
            # that expose a final ``url``.
            if response.url and response.url != url:
                validate_redirect(
                    url,
                    response.url,
                    allow_http=allow_http,
                    approved_origins=approved_origins,
                    resolver=self.resolver,
                    resolve_host=self.resolve_host,
                )
            if response.status_code not in {301, 302, 303, 307, 308}:
                return _bounded_body(response, self.max_response_bytes)
            location = _header(response.headers, "location")
            if not location:
                raise FeedRedirectError("Feed redirect did not provide a location.")
            if redirect_count >= self.max_redirects:
                raise FeedRedirectError("Feed redirect limit was exceeded.")
            url = validate_redirect(
                url,
                location,
                allow_http=allow_http,
                approved_origins=approved_origins,
                resolver=self.resolver,
                resolve_host=self.resolve_host,
            )
        raise FeedRedirectError("Feed redirect limit was exceeded.")

    fetch_response = fetch
    get = fetch

    async def _request(self, url: str, headers: Mapping[str, str]) -> FeedResponse:
        transport = self.transport
        callback = getattr(transport, "request", None)
        get_only = False
        if callback is None:
            callback = getattr(transport, "get", None)
            get_only = callback is not None
        if callback is None and callable(transport):
            callback = transport
        if callback is None:
            raise FeedUnavailableError("Feed transport is unavailable.")
        try:
            raw = await _call_transport(
                callback,
                "GET",
                url,
                {
                    "headers": headers,
                    "timeout": self.timeout_seconds,
                    "max_bytes": self.max_response_bytes,
                    "params": {},
                },
                get_only=get_only,
            )
            response = _coerce_response(raw, fallback_url=url)
            if response.status_code in {301, 302, 303, 307, 308}:
                # Redirect bodies are discarded and therefore cannot consume
                # unbounded memory before the next policy check.
                return FeedResponse(
                    status_code=response.status_code,
                    headers=response.headers,
                    body=b"",
                    url=response.url,
                )
            return _bounded_body(response, self.max_response_bytes)
        except (FeedPolicyError, FeedResponseTooLargeError, FeedOfflineError):
            raise
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise FeedUnavailableError("Feed request timed out.") from None
        except (ConnectionError, OSError, URLError):
            raise FeedUnavailableError("Feed origin could not be reached.") from None
        except Exception:
            raise FeedUnavailableError("Feed origin could not be reached.") from None


def _add_conditional_header(headers: dict[str, str], name: str, value: str) -> None:
    if not isinstance(value, str):
        return
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > _MAX_HEADER_VALUE
        or any(character in normalized for character in "\r\n\x00")
    ):
        return
    headers[name] = normalized


# Compatibility aliases used by integrations and contract tests.
SafeFeedFetcher = FeedFetcher
RSSAtomFetcher = FeedFetcher
RssAtomFetcher = FeedFetcher
FeedHttpClient = FeedFetcher
FeedFetchResponse = FeedResponse
FeedHttpResponse = FeedResponse
UrllibTransport = UrllibFeedTransport


__all__ = [
    "FeedFetcher",
    "FeedFetchResponse",
    "FeedHttpClient",
    "FeedHttpResponse",
    "FeedTransport",
    "RSSAtomFetcher",
    "RssAtomFetcher",
    "SafeFeedFetcher",
    "UrllibFeedTransport",
    "UrllibTransport",
]
