"""GET-only, fixed-origin basemap resources. No personal records enter requests."""

from __future__ import annotations

import ssl
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from collections.abc import Callable
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import certifi

from personal_hub.feeds.security import validate_host_addresses

STYLE_URL = "https://tiles.openfreemap.org/styles/liberty"
MAP_HOST = "tiles.openfreemap.org"
MAX_RESOURCE_BYTES = 5_000_000
MAX_CACHE_BYTES = 32_000_000


class MapNetworkError(RuntimeError):
    """A fixed, content-free map failure suitable for the UI."""


def validate_resource_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 2_048:
        raise MapNetworkError("Map resource address is invalid.")
    try:
        parts = urlsplit(url)
        valid = (
            parts.scheme == "https"
            and parts.netloc == MAP_HOST
            and parts.port is None
            and not parts.query
            and not parts.fragment
            and parts.path.startswith(
                ("/styles/", "/sprites/", "/fonts/", "/planet", "/natural_earth/")
            )
            and not any(char in unquote(parts.path) for char in "\\\r\n\x00")
            and ".." not in unquote(parts.path).split("/")
        )
    except ValueError:
        valid = False
    if not valid:
        raise MapNetworkError("Map resource address is not allowed.")
    return urlunsplit(
        (parts.scheme, parts.netloc, quote(unquote(parts.path), safe="/,()@-._~"), "", "")
    )


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class MapResourceClient:
    """Four-worker callers share a bounded, memory-only LRU. Never prefetches."""

    def __init__(
        self,
        *,
        allowed: Callable[[], bool],
        opener=None,
        resolver=None,
    ) -> None:
        self.allowed = allowed
        self.resolver = resolver
        self._opener = (
            opener
            or urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                _NoRedirects(),
                urllib.request.HTTPSHandler(
                    context=ssl.create_default_context(cafile=certifi.where())
                ),
            ).open
        )
        self._cache: OrderedDict[str, bytes] = OrderedDict()
        self._cache_size = 0
        self._lock = threading.Lock()

    def get(self, url: str, *, cancelled: Callable[[], bool] = lambda: False) -> bytes:
        def check() -> None:
            if cancelled() or not self.allowed():
                raise MapNetworkError("Online map is paused.")

        check()  # Before URL handling, cache access, DNS, or transport.
        target = validate_resource_url(url)
        with self._lock:
            cached = self._cache.get(target)
            if cached is not None:
                self._cache.move_to_end(target)
                return cached
        check()
        deadline = time.monotonic() + 20
        try:
            validate_host_addresses(MAP_HOST, 443, resolver=self.resolver)
            check()
            request = urllib.request.Request(
                target,
                headers={
                    "User-Agent": "PersonalHub/0.1.0 (Windows desktop map)",
                    "Accept-Encoding": "identity",
                    "Accept": "*/*",
                },
            )
            with self._opener(request, timeout=15) as response:
                if getattr(response, "status", 200) != 200:
                    raise MapNetworkError("Map provider is unavailable. Try Reload map.")
                length = response.headers.get("Content-Length")
                if length and int(length) > MAX_RESOURCE_BYTES:
                    raise MapNetworkError("Map resource exceeded its size limit.")
                data = bytearray()
                while True:
                    check()
                    if time.monotonic() > deadline:
                        raise MapNetworkError("Map request timed out. Try Reload map.")
                    chunk = response.read(min(65_536, MAX_RESOURCE_BYTES + 1 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > MAX_RESOURCE_BYTES:
                        raise MapNetworkError("Map resource exceeded its size limit.")
        except MapNetworkError:
            raise
        except (OSError, ValueError, RuntimeError, urllib.error.URLError) as error:
            raise MapNetworkError("Map provider is unavailable. Try Reload map.") from error
        check()
        body = bytes(data)
        with self._lock:
            # Another worker may have completed the same resource in the meantime.
            previous = self._cache.pop(target, b"")
            self._cache_size -= len(previous)
            while self._cache and self._cache_size + len(body) > MAX_CACHE_BYTES:
                _, removed = self._cache.popitem(last=False)
                self._cache_size -= len(removed)
            self._cache[target] = body
            self._cache_size += len(body)
        return body

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
            self._cache_size = 0
