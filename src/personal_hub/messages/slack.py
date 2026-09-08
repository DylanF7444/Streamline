"""Selected-channel/query Slack attention summaries with no mutation surface."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from urllib.parse import quote

from personal_hub.messages.categories import classify_message
from personal_hub.messages.contracts import (
    MessageBody,
    MessageInvalidResponseError,
    MessagePermissionRequiredError,
    MessageQuery,
    MessageSnapshot,
    MessageSummary,
    MessageUnavailableError,
)
from personal_hub.messages.provider_utils import access_token, request_json
from personal_hub.messages.sanitizer import sanitize_message_body

SLACK_CHANNEL_HISTORY_SCOPE = "channels:history"
SLACK_SEARCH_SCOPE = "search:read"
SLACK_API_ROOT = "https://slack.com/api"
_ID = re.compile(r"^[A-Za-z0-9._-]{1,160}$")
_MRKDOWN_LINK = re.compile(r"<(?P<url>https?://[^|>]+)(?:\|(?P<label>[^>]+))?>")


def _text(value: object, fallback: str, maximum: int) -> str:
    normalized = " ".join(str(value or "").split())
    normalized = _MRKDOWN_LINK.sub(
        lambda item: item.group("label") or item.group("url"), normalized
    )
    return (normalized or fallback)[:maximum]


def _timestamp(value: object) -> datetime:
    try:
        return datetime.fromtimestamp(float(str(value)), tz=UTC)
    except (TypeError, ValueError, OSError) as error:
        raise MessageInvalidResponseError("Slack message timestamp was invalid.") from error


def _validate_response(payload: Mapping[str, object]) -> None:
    if payload.get("ok", True) is True:
        return
    code = str(payload.get("error", "")).casefold()
    if code in {"missing_scope", "not_authed", "invalid_auth", "channel_not_found"}:
        raise MessagePermissionRequiredError("Slack message permission is required.")
    if code == "ratelimited":
        raise MessageUnavailableError("Slack is temporarily unavailable.")
    raise MessageInvalidResponseError("Slack returned an invalid response.")


class SlackMessageSource:
    source_id = "slack"
    provider_id = "slack"
    display_name = "Slack"

    def __init__(
        self,
        token_provider: object,
        transport: object,
        *,
        source_id: str = "slack",
        account_id: str,
        max_messages: int = 200,
        max_response_bytes: int = 2_000_000,
        offline: bool = False,
    ) -> None:
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
            raise TypeError("Slack expects MessageQuery")
        summaries: list[MessageSummary] = []
        if query.channels:
            token = await access_token(
                self.token_provider, (SLACK_CHANNEL_HISTORY_SCOPE,), offline=self.offline
            )
            for channel in query.channels:
                if not _ID.fullmatch(channel):
                    raise ValueError("Slack channel identity is invalid")
                payload = await request_json(
                    self.transport,
                    f"{SLACK_API_ROOT}/conversations.history",
                    token=token,
                    params={"channel": channel, "limit": min(query.limit, 100)},
                    max_response_bytes=self.max_response_bytes,
                    offline=self.offline,
                )
                _validate_response(payload)
                raw_messages = payload.get("messages", [])
                if not isinstance(raw_messages, list):
                    raise MessageInvalidResponseError("Slack message list was invalid.")
                for raw in raw_messages:
                    if isinstance(raw, Mapping) and (summary := self._summary(raw, channel)):
                        summaries.append(summary)
                        if len(summaries) >= min(query.limit, self.max_messages):
                            break
        elif query.query:
            token = await access_token(
                self.token_provider, (SLACK_SEARCH_SCOPE,), offline=self.offline
            )
            payload = await request_json(
                self.transport,
                f"{SLACK_API_ROOT}/search.messages",
                token=token,
                params={"query": query.query, "count": min(query.limit, 100)},
                max_response_bytes=self.max_response_bytes,
                offline=self.offline,
            )
            _validate_response(payload)
            matches = payload.get("messages", {})
            raw_messages = matches.get("matches", []) if isinstance(matches, Mapping) else []
            for raw in raw_messages if isinstance(raw_messages, list) else []:
                if isinstance(raw, Mapping):
                    channel_value = raw.get("channel", {})
                    channel = (
                        str(channel_value.get("id", ""))
                        if isinstance(channel_value, Mapping)
                        else str(channel_value)
                    )
                    if _ID.fullmatch(channel) and (summary := self._summary(raw, channel)):
                        summaries.append(summary)
        summaries.sort(key=lambda item: (item.received_at, item.message_id), reverse=True)
        return MessageSnapshot(
            source_id=self.source_id,
            provider="slack",
            account_id=self.account_id,
            collected_at=datetime.now(UTC),
            summaries=tuple(summaries[: query.limit]),
        )

    def _summary(self, value: Mapping[str, object], channel: str) -> MessageSummary | None:
        timestamp = str(value.get("ts", ""))
        if not timestamp or len(timestamp) > 32 or not _ID.fullmatch(channel):
            return None
        identifier = f"{channel}:{timestamp}"
        sender = _text(value.get("user", value.get("username")), "Unknown sender", 320)
        preview = _text(value.get("text"), "", 1_000)
        permalink = value.get("permalink")
        deep_link = (
            str(permalink)
            if isinstance(permalink, str) and permalink.startswith("https://")
            else f"https://app.slack.com/client/{quote(self.account_id, safe='')}/{quote(channel, safe='')}"
        )
        return MessageSummary(
            source_id=self.source_id,
            message_id=identifier,
            thread_id=str(value.get("thread_ts")) if value.get("thread_ts") else None,
            provider="slack",
            account_id=self.account_id,
            sender=sender,
            title=f"Message in {channel}",
            preview=preview or None,
            received_at=_timestamp(timestamp),
            unread=True,
            importance="important" if bool(value.get("is_starred", False)) else "normal",
            category=classify_message(
                provider="slack",
                sender=sender,
                title=f"Message in {channel}",
                preview=preview,
            ),
            deep_link=deep_link,
        )

    async def get_body(self, message_id: str) -> MessageBody:
        if ":" not in message_id:
            raise ValueError("Slack message identity is invalid")
        channel, timestamp = message_id.split(":", 1)
        if not _ID.fullmatch(channel) or len(timestamp) > 32:
            raise ValueError("Slack message identity is invalid")
        token = await access_token(
            self.token_provider, (SLACK_CHANNEL_HISTORY_SCOPE,), offline=self.offline
        )
        payload = await request_json(
            self.transport,
            f"{SLACK_API_ROOT}/conversations.replies",
            token=token,
            params={"channel": channel, "ts": timestamp, "limit": 1},
            max_response_bytes=self.max_response_bytes,
            offline=self.offline,
        )
        _validate_response(payload)
        values = payload.get("messages", [])
        if not isinstance(values, list) or not values or not isinstance(values[0], Mapping):
            raise MessageInvalidResponseError("Slack message body was unavailable.")
        return sanitize_message_body(
            self.source_id,
            message_id,
            str(values[0].get("text", "")),
            content_type="text/plain",
        )


__all__ = [
    "SLACK_API_ROOT",
    "SLACK_CHANNEL_HISTORY_SCOPE",
    "SLACK_SEARCH_SCOPE",
    "SlackMessageSource",
]
