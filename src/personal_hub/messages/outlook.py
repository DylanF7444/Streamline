"""Read-only Microsoft Graph mail summaries and explicit sanitized body reads."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

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

OUTLOOK_MAIL_READ_SCOPE = "Mail.Read"
OUTLOOK_MAIL_API_ROOT = "https://graph.microsoft.com/v1.0"


def _text(value: object, fallback: str, maximum: int) -> str:
    normalized = " ".join(str(value or "").split())
    return (normalized or fallback)[:maximum]


def _id(value: object) -> str | None:
    normalized = str(value or "").strip()
    return (
        normalized
        if normalized and len(normalized) <= 256 and all(ord(c) >= 32 for c in normalized)
        else None
    )


def _timestamp(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise MessageInvalidResponseError("Outlook message date was invalid.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MessageInvalidResponseError("Outlook message date was invalid.")
    return parsed.astimezone(UTC)


def _next_link(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    parts = urlsplit(text)
    if (
        parts.scheme.casefold() != "https"
        or parts.hostname != "graph.microsoft.com"
        or not parts.path.startswith("/v1.0/")
        or len(text) > 4_096
    ):
        raise MessageInvalidResponseError("Outlook pagination link was invalid.")
    return text


class OutlookMessageSource:
    source_id = "outlook-mail"
    provider_id = "outlook"
    display_name = "Outlook Mail"
    scopes = (OUTLOOK_MAIL_READ_SCOPE,)

    def __init__(
        self,
        token_provider: object,
        transport: object,
        *,
        source_id: str = "outlook-mail",
        account_id: str = "me",
        max_messages: int = 200,
        max_pages: int = 10,
        max_response_bytes: int = 2_000_000,
        offline: bool = False,
    ) -> None:
        self.source_id = source_id
        self.account_id = account_id
        self.token_provider = token_provider
        self.transport = transport
        self.max_messages = max_messages
        self.max_pages = max_pages
        self.max_response_bytes = max_response_bytes
        self.offline = bool(offline)

    def set_offline(self, value: bool) -> None:
        self.offline = bool(value)

    async def list_summaries(self, query: MessageQuery) -> MessageSnapshot:
        if not isinstance(query, MessageQuery):
            raise TypeError("Outlook Mail expects MessageQuery")
        token = await access_token(self.token_provider, self.scopes, offline=self.offline)
        summaries: list[MessageSummary] = []
        for folder in query.folders:
            url = f"{OUTLOOK_MAIL_API_ROOT}/me/mailFolders/{quote(folder, safe='')}/messages"
            filters: list[str] = []
            if query.unread_only:
                filters.append("isRead eq false")
            if query.important_only:
                filters.append("importance eq 'high'")
            if query.received_after:
                filters.append(f"receivedDateTime ge {query.received_after.isoformat()}")
            for _page in range(self.max_pages):
                payload = await request_json(
                    self.transport,
                    url,
                    token=token,
                    params={
                        "$select": (
                            "id,conversationId,from,subject,bodyPreview,receivedDateTime,"
                            "isRead,importance,webLink,categories,inferenceClassification"
                        ),
                        "$filter": " and ".join(filters),
                        "$top": min(100, query.limit - len(summaries)),
                        "$orderby": "receivedDateTime desc",
                    },
                    max_response_bytes=self.max_response_bytes,
                    offline=self.offline,
                )
                raw_values = payload.get("value", [])
                if not isinstance(raw_values, list):
                    raise MessageInvalidResponseError("Outlook message list was invalid.")
                for raw in raw_values:
                    if (
                        isinstance(raw, Mapping)
                        and (summary := self._summary(raw, folder=folder)) is not None
                    ):
                        summaries.append(summary)
                        if len(summaries) >= min(query.limit, self.max_messages):
                            break
                if len(summaries) >= min(query.limit, self.max_messages):
                    break
                next_url = _next_link(payload.get("@odata.nextLink"))
                if next_url is None:
                    break
                url = next_url
            if len(summaries) >= min(query.limit, self.max_messages):
                break
        summaries.sort(key=lambda item: (item.received_at, item.message_id), reverse=True)
        return MessageSnapshot(
            source_id=self.source_id,
            provider="outlook",
            account_id=self.account_id,
            collected_at=datetime.now(UTC),
            summaries=tuple(summaries[: query.limit]),
        )

    def _summary(
        self, value: Mapping[str, object], *, folder: str | None = None
    ) -> MessageSummary | None:
        message_id = _id(value.get("id"))
        if message_id is None:
            return None
        sender = value.get("from", {})
        email = sender.get("emailAddress", {}) if isinstance(sender, Mapping) else {}
        sender_text = ""
        if isinstance(email, Mapping):
            name = _text(email.get("name"), "", 160)
            address = _text(email.get("address"), "", 320)
            sender_text = f"{name} <{address}>" if name and address else name or address
        link = value.get("webLink")
        title = _text(value.get("subject"), "(No subject)", 500)
        preview = _text(value.get("bodyPreview"), "", 1_000) or None
        categories = value.get("categories", ())
        return MessageSummary(
            source_id=self.source_id,
            message_id=message_id,
            thread_id=_id(value.get("conversationId")),
            provider="outlook",
            account_id=self.account_id,
            sender=_text(sender_text, "Unknown sender", 320),
            title=title,
            preview=preview,
            received_at=_timestamp(value.get("receivedDateTime")),
            unread=not bool(value.get("isRead", False)),
            importance="important"
            if str(value.get("importance", "")).casefold() == "high"
            else "normal",
            category=classify_message(
                provider="outlook",
                categories=categories,
                folder=folder,
                inference_classification=(
                    value.get("inferenceClassification")
                    if isinstance(value.get("inferenceClassification"), str)
                    else None
                ),
                sender=sender_text,
                title=title,
                preview=preview,
            ),
            deep_link=str(link) if isinstance(link, str) and link else None,
        )

    async def get_body(self, message_id: str) -> MessageBody:
        identifier = _id(message_id)
        if identifier is None:
            raise ValueError("Outlook message_id is invalid")
        token = await access_token(self.token_provider, self.scopes, offline=self.offline)
        payload = await request_json(
            self.transport,
            f"{OUTLOOK_MAIL_API_ROOT}/me/messages/{quote(identifier, safe='')}",
            token=token,
            params={"$select": "id,body"},
            max_response_bytes=self.max_response_bytes,
            offline=self.offline,
        )
        body = payload.get("body", {})
        if not isinstance(body, Mapping):
            raise MessageInvalidResponseError("Outlook message body was invalid.")
        content_type = str(body.get("contentType", "text")).casefold()
        return sanitize_message_body(
            self.source_id,
            identifier,
            str(body.get("content", "")),
            content_type="text/html" if content_type == "html" else "text/plain",
        )


__all__ = ["OUTLOOK_MAIL_API_ROOT", "OUTLOOK_MAIL_READ_SCOPE", "OutlookMessageSource"]
