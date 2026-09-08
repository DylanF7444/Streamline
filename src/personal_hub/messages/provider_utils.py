"""Shared bounded GET/token helpers for message adapters."""

from __future__ import annotations

import inspect
import json
from collections.abc import Mapping

from personal_hub.messages.contracts import (
    MessageDisconnectedError,
    MessageInvalidResponseError,
    MessageOfflineError,
    MessagePermissionRequiredError,
    MessageUnavailableError,
)


async def access_token(provider: object, scopes: tuple[str, ...], *, offline: bool) -> str:
    if offline:
        raise MessageOfflineError("Message refresh is paused by offline mode.")
    callback = getattr(provider, "get_token", None)
    if callback is None:
        callback = getattr(provider, "access_token", None)
    if callback is None and callable(provider):
        callback = provider
    if not callable(callback):
        raise MessageDisconnectedError("Message source is disconnected.")
    try:
        signature = inspect.signature(callback)
        if "scopes" in signature.parameters:
            value = callback(scopes=scopes)
        elif signature.parameters:
            value = callback(scopes)
        else:
            value = callback()
        if inspect.isawaitable(value):
            value = await value
    except MessageOfflineError:
        raise
    except Exception as error:
        raise MessageDisconnectedError("Message source is disconnected.") from error
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 16_384
        or any(item in value for item in "\r\n\x00")
    ):
        raise MessageDisconnectedError("Message source is disconnected.")
    return value.strip()


async def request_json(
    transport: object,
    url: str,
    *,
    token: str,
    params: Mapping[str, object],
    max_response_bytes: int,
    offline: bool,
) -> dict[str, object]:
    if offline:
        raise MessageOfflineError("Message refresh is paused by offline mode.")
    callback = getattr(transport, "request", None)
    get_only = False
    if callback is None:
        callback = getattr(transport, "get", None)
        get_only = True
    if not callable(callback):
        raise MessageUnavailableError("Message provider transport is unavailable.")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Accept-Encoding": "identity",
    }
    try:
        signature = inspect.signature(callback)
        kwargs = {"headers": headers, "params": dict(params), "timeout": 20}
        accepts_kwargs = any(
            item.kind is inspect.Parameter.VAR_KEYWORD for item in signature.parameters.values()
        )
        selected = (
            kwargs
            if accepts_kwargs
            else {key: value for key, value in kwargs.items() if key in signature.parameters}
        )
        value = callback(url, **selected) if get_only else callback("GET", url, **selected)
        if inspect.isawaitable(value):
            value = await value
    except MessageOfflineError:
        raise
    except Exception as error:
        raise MessageUnavailableError("Message provider request failed safely.") from error
    status = getattr(value, "status_code", getattr(value, "status", 200))
    body = getattr(value, "body", getattr(value, "content", None))
    if isinstance(value, Mapping):
        status = value.get("status_code", value.get("status", status))
        is_transport_envelope = any(
            key in value for key in ("status", "status_code", "content")
        ) or isinstance(value.get("body"), (str, bytes, bytearray))
        if not is_transport_envelope:
            payload: object = value
        else:
            body = value.get("body", value.get("content", body))
            payload = None
    else:
        payload = None
    try:
        status_code = int(status)
    except (TypeError, ValueError) as error:
        raise MessageInvalidResponseError("Message provider returned an invalid status.") from error
    if status_code in {401, 403}:
        raise MessagePermissionRequiredError("Message source permission is required.")
    if status_code == 429 or status_code >= 500:
        raise MessageUnavailableError("Message provider is temporarily unavailable.")
    if not 200 <= status_code < 300:
        raise MessageInvalidResponseError("Message provider returned an invalid response.")
    if payload is None:
        if isinstance(body, str):
            encoded = body.encode()
        elif isinstance(body, bytes):
            encoded = body
        elif body is None:
            encoded = b"{}"
        else:
            payload = body
            encoded = b""
        if len(encoded) > max_response_bytes:
            raise MessageInvalidResponseError("Message provider response exceeded its bound.")
        if payload is None:
            try:
                payload = json.loads(encoded)
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise MessageInvalidResponseError(
                    "Message provider returned malformed JSON."
                ) from error
    if not isinstance(payload, Mapping):
        raise MessageInvalidResponseError("Message provider returned an invalid JSON object.")
    try:
        encoded_size = len(json.dumps(payload, ensure_ascii=False).encode())
    except (TypeError, ValueError, RecursionError) as error:
        raise MessageInvalidResponseError(
            "Message provider returned invalid JSON values."
        ) from error
    if encoded_size > max_response_bytes:
        raise MessageInvalidResponseError("Message provider response exceeded its bound.")
    return {str(key): item for key, item in payload.items()}


__all__ = ["access_token", "request_json"]
