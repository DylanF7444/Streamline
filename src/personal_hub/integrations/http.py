"""Bounded, fixed-provider HTTP transport for Personal Hub connections.

Only reviewed provider origins are accepted.  The transport never follows a request to an
unlisted host, never returns provider error bodies, and performs blocking ``urllib`` work on
an asyncio worker thread so Qt's event loop remains responsive.
"""

from __future__ import annotations

import asyncio
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any, Final

import certifi

from personal_hub.integrations.oauth import OAuthConnectionError

_ALLOWED_PROVIDER_HOSTS: Final[frozenset[str]] = frozenset(
    {
        "accounts.google.com",
        "gmail.googleapis.com",
        "graph.microsoft.com",
        "login.microsoftonline.com",
        "oauth2.googleapis.com",
        "slack.com",
        "www.googleapis.com",
    }
)
_MAX_ERROR_BYTES: Final = 8 * 1024


class ProviderHttpError(RuntimeError):
    """Safe provider transport failure without response content."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = int(status_code)
        super().__init__(message[:200])


class BoundedProviderTransport:
    """GET-only JSON and OAuth form transport for reviewed provider endpoints."""

    def __init__(
        self,
        *,
        allowed_hosts: frozenset[str] = _ALLOWED_PROVIDER_HOSTS,
        timeout_seconds: float = 20.0,
        max_response_bytes: int = 2_000_000,
        opener: Any | None = None,
    ) -> None:
        if not 1 <= timeout_seconds <= 60:
            raise ValueError("provider timeout must be between 1 and 60 seconds")
        if not 1_024 <= max_response_bytes <= 20_000_000:
            raise ValueError("provider response bound is out of range")
        normalized_hosts = frozenset(host.casefold().strip(".") for host in allowed_hosts)
        if not normalized_hosts or any(not host for host in normalized_hosts):
            raise ValueError("at least one reviewed provider host is required")
        self.allowed_hosts = normalized_hosts
        self.timeout_seconds = float(timeout_seconds)
        self.max_response_bytes = int(max_response_bytes)
        self._ssl_context = ssl.create_default_context(cafile=certifi.where())
        self._opener = opener or urllib.request.urlopen

    def scoped_to_hosts(self, allowed_hosts: frozenset[str]) -> BoundedProviderTransport:
        """Return the same bounded transport policy scoped to another reviewed host set."""

        return BoundedProviderTransport(
            allowed_hosts=allowed_hosts,
            timeout_seconds=self.timeout_seconds,
            max_response_bytes=self.max_response_bytes,
            opener=self._opener,
        )

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, object] | None = None,
        query: Mapping[str, object] | None = None,
        timeout: float | None = None,
    ) -> dict[str, object]:
        """Return one bounded JSON object or a content-free HTTP status envelope."""

        if method.upper() != "GET":
            raise ValueError("Personal Hub provider transport is read-only")
        target = self._target(url, params if params is not None else query)
        safe_headers = self._headers(headers)
        selected_timeout = self._timeout(timeout)
        return await asyncio.to_thread(
            self._request_json_sync,
            target,
            safe_headers,
            selected_timeout,
        )

    async def post_form(
        self,
        url: str,
        data: dict[str, str],
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> dict[str, Any]:
        """Exchange an OAuth form at one reviewed HTTPS token endpoint."""

        target = self._target(url, None)
        if any(
            not isinstance(key, str)
            or not isinstance(value, str)
            or any(character in key + value for character in "\r\n\x00")
            for key, value in data.items()
        ):
            raise OAuthConnectionError("OAuth request fields are invalid")
        encoded = urllib.parse.urlencode(data).encode("utf-8")
        selected_timeout = self._timeout(timeout_seconds)
        selected_bound = min(max(1_024, int(max_response_bytes)), self.max_response_bytes)
        return await asyncio.to_thread(
            self._post_form_sync,
            target,
            encoded,
            selected_timeout,
            selected_bound,
        )

    def _request_json_sync(
        self,
        target: str,
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> dict[str, object]:
        request = urllib.request.Request(target, headers=headers, method="GET")
        try:
            with self._open(request, timeout_seconds) as response:
                status = int(getattr(response, "status", 200))
                body = self._read_bounded(response, self.max_response_bytes)
        except urllib.error.HTTPError as error:
            # Provider error content is untrusted and may include private account data.
            with error:
                error.read(_MAX_ERROR_BYTES)
            return {"status_code": int(error.code), "body": b""}
        except (TimeoutError, urllib.error.URLError, OSError) as error:
            raise ProviderHttpError(0, "Provider request failed safely") from error
        if not 200 <= status < 300:
            return {"status_code": status, "body": b""}
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ProviderHttpError(status, "Provider returned malformed JSON") from error
        if not isinstance(payload, dict):
            raise ProviderHttpError(status, "Provider returned an invalid JSON object")
        return {str(key): value for key, value in payload.items()}

    def _post_form_sync(
        self,
        target: str,
        encoded: bytes,
        timeout_seconds: float,
        maximum: int,
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            target,
            data=encoded,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        try:
            with self._open(request, timeout_seconds) as response:
                body = self._read_bounded(response, maximum)
        except urllib.error.HTTPError as error:
            with error:
                error.read(_MAX_ERROR_BYTES)
            raise OAuthConnectionError("OAuth provider rejected the token request") from error
        except (TimeoutError, urllib.error.URLError, OSError) as error:
            raise OAuthConnectionError("OAuth provider request failed safely") from error
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise OAuthConnectionError("OAuth provider returned malformed JSON") from error
        if not isinstance(payload, dict):
            raise OAuthConnectionError("OAuth provider returned an invalid response")
        return payload

    def _open(self, request: urllib.request.Request, timeout_seconds: float) -> Any:
        try:
            return self._opener(request, timeout=timeout_seconds, context=self._ssl_context)
        except TypeError:
            # Deterministic test openers commonly expose only request and timeout.
            return self._opener(request, timeout=timeout_seconds)

    @staticmethod
    def _read_bounded(response: Any, maximum: int) -> bytes:
        length = response.headers.get("Content-Length") if hasattr(response, "headers") else None
        if isinstance(length, str) and length.isdigit() and int(length) > maximum:
            raise ProviderHttpError(0, "Provider response exceeded its size bound")
        body = response.read(maximum + 1)
        if not isinstance(body, bytes) or len(body) > maximum:
            raise ProviderHttpError(0, "Provider response exceeded its size bound")
        return body

    def _target(self, url: str, params: Mapping[str, object] | None) -> str:
        if not isinstance(url, str) or len(url) > 8_192:
            raise ValueError("provider URL is invalid")
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").casefold().strip(".")
        if (
            parsed.scheme.casefold() != "https"
            or host not in self.allowed_hosts
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError("provider URL is outside the reviewed HTTPS origins")
        if parsed.port not in {None, 443}:
            raise ValueError("provider URL uses an unreviewed port")
        query_values = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if params:
            for key, value in params.items():
                if value is None or value == "":
                    continue
                if not isinstance(key, str) or any(character in key for character in "\r\n\x00"):
                    raise ValueError("provider query field is invalid")
                query_values.append((key, str(value)))
        query = urllib.parse.urlencode(query_values)
        return urllib.parse.urlunsplit(("https", parsed.netloc, parsed.path or "/", query, ""))

    @staticmethod
    def _headers(headers: Mapping[str, str] | None) -> dict[str, str]:
        values = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": "PersonalHub/1.0",
        }
        for key, value in (headers or {}).items():
            if (
                not isinstance(key, str)
                or not isinstance(value, str)
                or len(key) > 128
                or len(value) > 32_768
                or any(character in key + value for character in "\r\n\x00")
            ):
                raise ValueError("provider header is invalid")
            values[key] = value
        return values

    def _timeout(self, value: float | None) -> float:
        selected = self.timeout_seconds if value is None else float(value)
        if not 1 <= selected <= 60:
            raise ValueError("provider timeout must be between 1 and 60 seconds")
        return selected


__all__ = ["BoundedProviderTransport", "ProviderHttpError"]
