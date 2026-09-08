"""Plain-text message-body sanitization with remote content blocked by construction."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit

from personal_hub.messages.contracts import MessageBody, MessageInvalidResponseError

_BLOCKED = {"script", "style", "form", "iframe", "object", "embed", "svg", "canvas"}
_BREAKS = {"p", "div", "br", "li", "tr", "blockquote", "h1", "h2", "h3", "h4"}
_SPACE = re.compile(r"[ \t\f\v]+")


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str] = []
        self._blocked_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag in _BLOCKED:
            self._blocked_depth += 1
            return
        if self._blocked_depth:
            return
        if tag in _BREAKS:
            self.parts.append("\n")
        if tag == "a":
            href = next((value for key, value in attrs if key.casefold() == "href"), None)
            if href is not None:
                parts = urlsplit(href.strip())
                if (
                    parts.scheme.casefold() in {"http", "https"}
                    and parts.hostname
                    and parts.username is None
                    and parts.password is None
                    and len(href) <= 2_048
                    and href not in self.links
                    and len(self.links) < 50
                ):
                    self.links.append(href)
        # img/source/video/audio are intentionally ignored: no URL is retained or loaded.

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in _BLOCKED and self._blocked_depth:
            self._blocked_depth -= 1
            return
        if not self._blocked_depth and tag in _BREAKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._blocked_depth:
            self.parts.append(data)


def _plain(value: str) -> str:
    lines = []
    for raw in value.replace("\r", "\n").split("\n"):
        line = _SPACE.sub(" ", "".join(item for item in raw if item >= " " or item == "\t")).strip()
        if line:
            lines.append(line)
        if sum(map(len, lines)) >= 100_000:
            break
    return "\n".join(lines)[:100_000]


def sanitize_message_body(
    source_id: str,
    message_id: str,
    content: str | bytes,
    *,
    content_type: str = "text/plain",
    fetched_at: datetime | None = None,
) -> MessageBody:
    if isinstance(content, bytes):
        if len(content) > 2_000_000:
            raise MessageInvalidResponseError("Message body exceeded the byte limit.")
        text = content.decode("utf-8", errors="replace")
    elif isinstance(content, str):
        if len(content.encode("utf-8", errors="ignore")) > 2_000_000:
            raise MessageInvalidResponseError("Message body exceeded the byte limit.")
        text = content
    else:
        raise TypeError("message body content must be text or bytes")
    links: tuple[str, ...] = ()
    if "html" in content_type.casefold():
        parser = _Sanitizer()
        try:
            parser.feed(text)
            parser.close()
        except (AssertionError, ValueError) as error:
            raise MessageInvalidResponseError("Message HTML could not be sanitized.") from error
        text = "".join(parser.parts)
        links = tuple(parser.links)
    timestamp = fetched_at or datetime.now(UTC)
    return MessageBody(
        source_id=source_id,
        message_id=message_id,
        plain_text=_plain(text),
        links=links,
        fetched_at=timestamp,
    )


__all__ = ["sanitize_message_body"]
