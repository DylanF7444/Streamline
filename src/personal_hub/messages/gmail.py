"""Bounded Gmail attention summaries and explicit in-memory body reads."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote

from personal_hub.messages.categories import classify_message
from personal_hub.messages.contracts import (
    MessageBody,
    MessageInvalidResponseError,
    MessageQuery,
    MessageSnapshot,
    MessageSummary,
)
from personal_hub.messages.provider_utils import access_token, request_json
from personal_hub.messages.sanitizer import sanitize_message_body

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_API_ROOT = "https://gmail.googleapis.com/gmail/v1/users/me"


def _safe(value: object, fallback: str, maximum: int) -> str:
    normalized = " ".join(str(value or "").split())
    return (normalized or fallback)[:maximum]


def _message_id(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized if normalized and len(normalized) <= 256 and normalized.isascii() else None


def _received(value: object, headers: Mapping[str, str]) -> datetime:
    try:
        return datetime.fromtimestamp(int(str(value)) / 1_000, tz=UTC)
    except (TypeError, ValueError, OSError):
        try:
            parsed = parsedate_to_datetime(headers.get("date", ""))
            return parsed.astimezone(UTC)
        except (TypeError, ValueError, OverflowError):
            raise MessageInvalidResponseError("Gmail message date was invalid.") from None


class GmailMessageSource:
    source_id = "gmail"
    provider_id = "gmail"
    display_name = "Gmail"
    scopes = (GMAIL_READONLY_SCOPE,)

    def __init__(
        self,
        token_provider: object,
        transport: object,
        *,
        source_id: str = "gmail",
        account_id: str = "me",
        max_messages: int = 200,
        max_response_bytes: int = 2_000_000,
        offline: bool = False,
    ) -> None:
        if not 1 <= max_messages <= 200:
            raise ValueError("Gmail summary limit must be between 1 and 200")
        self.source_id = source_id
        self.account_id = account_id
        self.token_provider = token_provider
        self.transport = transport
        self.max_messages = max_messages
        self.max_response_bytes = max_response_bytes
        self.offline = bool(offline)

    def set_offline(self, value: bool) -> None:
        self.offline = bool(value)

    async def list_summaries(self, query: MessageQuery) -> MessageSnapshot:
        if not isinstance(query, MessageQuery):
            raise TypeError("Gmail expects MessageQuery")
        token = await access_token(self.token_provider, self.scopes, offline=self.offline)
        terms: list[str] = []
        terms.extend(f"in:{folder}" for folder in query.folders)
        if query.query:
            terms.append(f"({query.query})")
        if query.unread_only:
            terms.append("is:unread")
        if query.important_only:
            terms.append("is:important")
        if query.received_after:
            terms.append(f"after:{int(query.received_after.timestamp())}")
        payload = await request_json(
            self.transport,
            f"{GMAIL_API_ROOT}/messages",
            token=token,
            params={"q": " ".join(terms), "maxResults": min(query.limit, self.max_messages)},
            max_response_bytes=self.max_response_bytes,
            offline=self.offline,
        )
        raw_messages = payload.get("messages", [])
        if not isinstance(raw_messages, list):
            raise MessageInvalidResponseError("Gmail message list was invalid.")
        summaries: list[MessageSummary] = []
        for raw in raw_messages[: min(query.limit, self.max_messages)]:
            if not isinstance(raw, Mapping) or (message_id := _message_id(raw.get("id"))) is None:
                continue
            resource = await request_json(
                self.transport,
                f"{GMAIL_API_ROOT}/messages/{quote(message_id, safe='')}",
                token=token,
                params={
                    "format": "metadata",
                    "metadataHeaders": "From,Subject,Date",
                },
                max_response_bytes=self.max_response_bytes,
                offline=self.offline,
            )
            summary = self._summary(resource, message_id)
            if summary is not None:
                summaries.append(summary)
        summaries.sort(key=lambda item: (item.received_at, item.message_id), reverse=True)
        return MessageSnapshot(
            source_id=self.source_id,
            provider="gmail",
            account_id=self.account_id,
            collected_at=datetime.now(UTC),
            summaries=tuple(summaries[: query.limit]),
        )

    def _summary(self, value: Mapping[str, object], fallback_id: str) -> MessageSummary | None:
        payload = value.get("payload", {})
        if not isinstance(payload, Mapping):
            return None
        raw_headers = payload.get("headers", [])
        headers: dict[str, str] = {}
        if isinstance(raw_headers, list):
            for item in raw_headers[:100]:
                if isinstance(item, Mapping):
                    name = str(item.get("name", "")).casefold()
                    if name in {"from", "subject", "date"}:
                        headers[name] = _safe(item.get("value"), "", 500)
        message_id = _message_id(value.get("id")) or fallback_id
        thread_id = _message_id(value.get("threadId"))
        labels = value.get("labelIds", [])
        label_set = {str(item).upper() for item in labels} if isinstance(labels, list) else set()
        sender = _safe(headers.get("from"), "Unknown sender", 320)
        title = _safe(headers.get("subject"), "(No subject)", 500)
        preview = _safe(value.get("snippet"), "", 1_000) or None
        return MessageSummary(
            source_id=self.source_id,
            message_id=message_id,
            thread_id=thread_id,
            provider="gmail",
            account_id=self.account_id,
            sender=sender,
            title=title,
            preview=preview,
            received_at=_received(value.get("internalDate"), headers),
            unread="UNREAD" in label_set,
            importance="important" if "IMPORTANT" in label_set else "normal",
            category=classify_message(
                provider="gmail",
                labels=label_set,
                sender=sender,
                title=title,
                preview=preview,
            ),
            deep_link=f"https://mail.google.com/mail/u/0/#all/{quote(message_id, safe='')}",
        )

    async def get_body(self, message_id: str) -> MessageBody:
        identifier = _message_id(message_id)
        if identifier is None:
            raise ValueError("Gmail message_id is invalid")
        token = await access_token(self.token_provider, self.scopes, offline=self.offline)
        payload = await request_json(
            self.transport,
            f"{GMAIL_API_ROOT}/messages/{quote(identifier, safe='')}",
            token=token,
            params={"format": "full"},
            max_response_bytes=self.max_response_bytes,
            offline=self.offline,
        )
        selected = _gmail_body(payload.get("payload"))
        if selected is None:
            selected = ("text/plain", "")
        return sanitize_message_body(
            self.source_id,
            identifier,
            selected[1],
            content_type=selected[0],
        )


def _gmail_body(value: object) -> tuple[str, str] | None:
    queue: list[tuple[object, int]] = [(value, 0)]
    html: tuple[str, str] | None = None
    seen = 0
    while queue:
        part, depth = queue.pop(0)
        if not isinstance(part, Mapping) or depth > 20 or seen >= 200:
            continue
        seen += 1
        mime = str(part.get("mimeType", "text/plain")).casefold()
        body = part.get("body", {})
        if isinstance(body, Mapping) and isinstance(body.get("data"), str):
            encoded = str(body["data"])
            if len(encoded) <= 2_800_000:
                try:
                    decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
                    text = decoded.decode("utf-8", errors="replace")
                except (ValueError, TypeError):
                    text = ""
                if mime == "text/plain":
                    return mime, text
                if mime == "text/html" and html is None:
                    html = (mime, text)
        children = part.get("parts", [])
        if isinstance(children, list):
            queue.extend((child, depth + 1) for child in children[:100])
    return html


__all__ = ["GMAIL_API_ROOT", "GMAIL_READONLY_SCOPE", "GmailMessageSource"]
