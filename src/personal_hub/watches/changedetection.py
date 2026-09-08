"""Optional read-only changedetection.io adapter with an exact-origin boundary."""

from __future__ import annotations

import inspect
import re
from collections.abc import Mapping
from urllib.parse import urlsplit

from personal_hub.feeds.security import origin_for_url
from personal_hub.watches.contracts import (
    PageResponse,
    WatchDefinition,
    WatchOfflineError,
    WatchPolicyError,
    WatchUnavailableError,
)

_REMOTE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class ChangedetectionReadAdapter:
    """Read one configured remote watch snapshot; no create/update/delete API exists."""

    def __init__(
        self,
        api_origin: str,
        transport: object,
        api_token_getter: object,
        *,
        offline: bool = False,
        timeout_seconds: float = 20,
        max_response_bytes: int = 2_000_000,
    ) -> None:
        normalized = origin_for_url(api_origin)
        if normalized != api_origin.rstrip("/") or not normalized.startswith("https://"):
            raise ValueError("changedetection.io requires an exact HTTPS API origin")
        if not callable(api_token_getter):
            raise TypeError("changedetection.io token getter must be callable")
        self.api_origin = normalized
        self.transport = transport
        self.api_token_getter = api_token_getter
        self.offline = bool(offline)
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes

    def set_offline(self, value: bool) -> None:
        self.offline = bool(value)

    async def fetch(self, watch: WatchDefinition) -> PageResponse:
        if not isinstance(watch, WatchDefinition) or watch.kind != "changedetection":
            raise TypeError("changedetection.io expects a changedetection WatchDefinition")
        source = str(watch.source_url)
        if origin_for_url(source) != self.api_origin:
            raise WatchPolicyError("The remote watch is outside the configured API origin.")
        remote_watch_id = urlsplit(source).path.rstrip("/").rsplit("/", 1)[-1]
        return await self.fetch_snapshot(remote_watch_id)

    async def fetch_snapshot(self, remote_watch_id: str) -> PageResponse:
        if self.offline:
            raise WatchOfflineError("Watch refresh is paused by offline mode.")
        if not isinstance(remote_watch_id, str) or not _REMOTE_ID.fullmatch(remote_watch_id):
            raise ValueError("changedetection.io watch identity is invalid")
        token = self.api_token_getter()
        if inspect.isawaitable(token):
            token = await token
        if not isinstance(token, str) or not token or any(c in token for c in "\r\n\x00"):
            raise WatchPolicyError("changedetection.io credentials require attention.")
        url = f"{self.api_origin}/api/v1/watch/{remote_watch_id}"
        callback = getattr(self.transport, "request", None)
        if not callable(callback):
            raise WatchUnavailableError("changedetection.io transport is unavailable.")
        try:
            response = callback(
                "GET",
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                },
                timeout=self.timeout_seconds,
                max_bytes=self.max_response_bytes,
            )
            if inspect.isawaitable(response):
                response = await response
        except Exception as error:
            raise WatchUnavailableError("changedetection.io could not be reached.") from error
        status = getattr(response, "status_code", getattr(response, "status", None))
        headers = getattr(response, "headers", {})
        body = getattr(response, "body", getattr(response, "content", b""))
        response_url = getattr(response, "url", url)
        if isinstance(response, Mapping):
            status = response.get("status_code", response.get("status", status))
            headers = response.get("headers", headers)
            body = response.get("body", response.get("content", body))
            response_url = response.get("url", response_url)
        if not isinstance(body, bytes) or len(body) > self.max_response_bytes:
            raise WatchUnavailableError("changedetection.io response exceeded its bound.")
        if origin_for_url(str(response_url)) != self.api_origin:
            raise WatchPolicyError("changedetection.io response left the configured origin.")
        try:
            return PageResponse(
                status_code=int(status),
                headers={str(key): str(value) for key, value in dict(headers).items()},
                body=body,
                url=str(response_url),
            )
        except (TypeError, ValueError) as error:
            raise WatchUnavailableError(
                "changedetection.io returned an invalid response."
            ) from error


__all__ = ["ChangedetectionReadAdapter"]
