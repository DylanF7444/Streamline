"""Hardened RSS/Atom parsing and untrusted-content normalization.

The parser accepts only bounded bytes and emits normalized :class:`FeedEntry`
contracts.  XML declarations, entities, external references, scripts, remote
image markup, and custom navigation schemes are never passed downstream.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

try:  # pragma: no cover - the fallback is exercised when the optional package is absent
    from defusedxml import ElementTree as _ElementTree
    from defusedxml.common import DefusedXmlException as _DefusedXmlException
except ImportError:  # pragma: no cover - depends on the runtime dependency set
    import xml.etree.ElementTree as _ElementTree

    class _DefusedXmlException(Exception):
        pass


from pydantic import ValidationError

from personal_hub.feeds.contracts import (
    FeedEntry,
    FeedParseError,
    FeedSnapshot,
    FeedSubscription,
    canonical_url,
)
from personal_hub.feeds.identity import stable_feed_entry_id

_DECLARATION_RE = re.compile(
    rb"<!\s*(?:DOCTYPE|ENTITY|ELEMENT|ATTLIST)\b|\b(?:SYSTEM|PUBLIC)\b\s+['\"]",
    re.I,
)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TAG_MAX_DEPTH = 64
_NODE_MAX = 50_000
_TEXT_MAX = 32_000
_FEED_TITLE_MAX = 500


class _SafeTextParser(HTMLParser):
    """Extract visible text while dropping active or remote HTML elements."""

    _blocked_tags = frozenset(
        {
            "script",
            "style",
            "noscript",
            "template",
            "iframe",
            "object",
            "embed",
            "form",
            "svg",
            "math",
        }
    )

    def __init__(self, *, max_chars: int) -> None:
        super().__init__(convert_charrefs=True)
        self.max_chars = max_chars
        self.parts: list[str] = []
        self._blocked_depth = 0

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in self._blocked_tags:
            self._blocked_depth += 1

    def handle_startendtag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        # A self-closing blocked tag has no visible content, so no state needs
        # to be retained for its end tag.
        _ = tag

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in self._blocked_tags and self._blocked_depth:
            self._blocked_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._blocked_depth or len("".join(self.parts)) >= self.max_chars:
            return
        self.parts.append(data[: self.max_chars])

    def text(self) -> str:
        return " ".join("".join(self.parts).split())[: self.max_chars]


def sanitize_html(value: str | None, *, max_chars: int = 8_000) -> str | None:
    """Convert HTML-ish feed content to bounded plain text.

    Links are represented by their visible labels and all attributes are
    discarded.  Remote images and active content therefore cannot load when a
    caller displays the normalized value.
    """

    if value is None:
        return None
    if not isinstance(value, str):
        return None
    value = _CONTROL_RE.sub(" ", value)
    parser = _SafeTextParser(max_chars=max(1, max_chars))
    try:
        parser.feed(value[: max_chars * 4])
        parser.close()
    except Exception:
        # A malformed HTML fragment is still safe to display as bounded text;
        # returning the unparsed text is preferable to exposing the markup.
        parser = _SafeTextParser(max_chars=max(1, max_chars))
        parser.feed(re.sub(r"<[^>]{0,512}>", " ", value[: max_chars * 4]))
    result = parser.text()
    return result or None


def sanitize_text(value: str | None, *, max_chars: int = 8_000) -> str | None:
    """Normalize a plain or HTML feed field to safe bounded single-line text."""

    return sanitize_html(value, max_chars=max_chars)


def stable_entry_id(
    identity: str,
    canonical_link: str,
    *,
    subscription_id: object | None = None,
) -> str:
    """Derive a deterministic storage-sized identity from GUID or canonical URL.

    Parser callers pass the subscription UUID so the globally keyed Hub entry
    table cannot collide when two subscriptions contain the same GUID.  The
    two-argument form remains useful for pure parser callers that only need a
    deterministic local key.
    """

    candidate = " ".join(identity.split()) if isinstance(identity, str) else ""
    if subscription_id is not None:
        return stable_feed_entry_id(subscription_id, guid=candidate or None, url=canonical_link)
    if (
        candidate
        and len(candidate) <= 128
        and not any(character in candidate for character in "\r\n\x00")
    ):
        return candidate
    # A digest keeps untrusted, overlong GUIDs out of storage while remaining
    # stable across item reorderings and process restarts.
    return hashlib.sha256((candidate or canonical_link).encode("utf-8")).hexdigest()


def parse_published_at(value: str | None) -> datetime | None:
    """Parse common RSS/Atom date forms and normalize to UTC."""

    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    parsed: datetime | None = None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _local_name(tag: object) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].casefold()


def _children(element: Any, name: str) -> list[Any]:
    wanted = name.casefold()
    return [child for child in list(element) if _local_name(child.tag) == wanted]


def _first_child(element: Any, *names: str) -> Any | None:
    wanted = {name.casefold() for name in names}
    for child in list(element):
        if _local_name(child.tag) in wanted:
            return child
    return None


def _node_text(element: Any | None, *, max_chars: int = _TEXT_MAX) -> str | None:
    if element is None:
        return None
    try:
        blocked = _SafeTextParser._blocked_tags

        def visible(node: Any) -> str:
            if _local_name(node.tag) in blocked:
                return ""
            pieces = [node.text or ""]
            for child in list(node):
                pieces.append(visible(child))
                pieces.append(child.tail or "")
            return "".join(pieces)

        text = visible(element)
    except (AttributeError, TypeError):
        return None
    return text[:max_chars] if text else None


def _safe_feed_link(value: str | None, *, base_url: str) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return canonical_url(value, base_url=base_url)
    except (TypeError, ValueError):
        return None


def _rss_link(element: Any, *, base_url: str) -> str | None:
    child = _first_child(element, "link")
    return _safe_feed_link(_node_text(child), base_url=base_url)


def _atom_link(element: Any, *, base_url: str) -> str | None:
    candidates: list[tuple[int, str]] = []
    for child in _children(element, "link"):
        href = child.attrib.get("href") if hasattr(child, "attrib") else None
        if not isinstance(href, str):
            href = _node_text(child)
        if not href:
            continue
        rel = str(child.attrib.get("rel", "alternate")).casefold()
        if rel == "self":
            rank = 2
        elif rel in {"alternate", ""}:
            rank = 0
        else:
            rank = 1
        safe = _safe_feed_link(href, base_url=base_url)
        if safe is not None:
            candidates.append((rank, safe))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][1]


def _author(element: Any) -> str | None:
    author = _first_child(element, "author", "creator")
    if author is None:
        return None
    name = _first_child(author, "name")
    return sanitize_text(
        _node_text(name) if name is not None else _node_text(author), max_chars=240
    )


def _summary(element: Any) -> str | None:
    child = _first_child(element, "description", "summary", "content", "encoded", "subtitle")
    return sanitize_html(_node_text(child), max_chars=8_000)


def _entry_from_element(
    element: Any,
    subscription: FeedSubscription,
    *,
    atom: bool,
    first_seen_at: datetime,
) -> FeedEntry | None:
    title = sanitize_text(_node_text(_first_child(element, "title")), max_chars=500)
    if not title:
        return None
    link = (
        _atom_link(element, base_url=str(subscription.feed_url))
        if atom
        else _rss_link(element, base_url=str(subscription.feed_url))
    )
    if link is None:
        return None
    identity_value = _node_text(_first_child(element, "id", "guid"))
    entry_id = stable_entry_id(
        identity_value or link,
        link,
        subscription_id=subscription.subscription_id,
    )
    published = parse_published_at(
        _node_text(_first_child(element, "published", "updated", "pubdate", "date"))
    )
    try:
        return FeedEntry(
            entry_id=entry_id,
            subscription_id=subscription.subscription_id,
            title=title,
            url=link,
            author=_author(element),
            published_at=published,
            summary_text=_summary(element),
            first_seen_at=first_seen_at,
        )
    except (TypeError, ValueError, ValidationError):
        # One malformed item must not make an otherwise healthy feed unusable.
        return None


def _validate_tree(root: Any, *, max_nodes: int, max_depth: int) -> None:
    nodes = 0
    stack: list[tuple[Any, int]] = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        nodes += 1
        if nodes > max_nodes:
            raise FeedParseError("Feed XML contains too many elements.")
        if depth > max_depth:
            raise FeedParseError("Feed XML nesting is too deep.")
        if not hasattr(element, "attrib") or len(element.attrib) > 128:
            raise FeedParseError("Feed XML contains too many attributes.")
        for value in getattr(element, "attrib", {}).values():
            if isinstance(value, str) and len(value) > _TEXT_MAX:
                raise FeedParseError("Feed XML attribute is too large.")
        for child in reversed(list(element)):
            stack.append((child, depth + 1))


class FeedParser:
    """Parse RSS 2.x or Atom 1.x into bounded Hub feed contracts."""

    def __init__(
        self,
        *,
        max_items: int = 100,
        max_response_bytes: int = 5_000_000,
        max_nodes: int = _NODE_MAX,
        max_depth: int = _TAG_MAX_DEPTH,
    ) -> None:
        if not 1 <= max_items <= 1_000:
            raise ValueError("Feed max_items must be between 1 and 1000")
        if not 1_024 <= max_response_bytes <= 20_000_000:
            raise ValueError("Feed max_response_bytes is out of bounds")
        if not 100 <= max_nodes <= 500_000:
            raise ValueError("Feed max_nodes is out of bounds")
        if not 8 <= max_depth <= 256:
            raise ValueError("Feed max_depth is out of bounds")
        self.max_items = max_items
        self.max_response_bytes = max_response_bytes
        self.max_nodes = max_nodes
        self.max_depth = max_depth

    def parse(
        self,
        body: bytes,
        subscription: FeedSubscription,
        *,
        collected_at: datetime | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FeedSnapshot:
        if not isinstance(subscription, FeedSubscription):
            raise TypeError("FeedParser expects a FeedSubscription")
        if isinstance(body, str):
            body = body.encode("utf-8")
        if not isinstance(body, bytes):
            raise TypeError("Feed response body must be bytes")
        if not body or len(body) > self.max_response_bytes:
            raise FeedParseError("Feed response is empty or too large.")
        if b"\x00" in body or _DECLARATION_RE.search(body):
            raise FeedParseError("Feed XML declarations and entities are not allowed.")
        try:
            root = _ElementTree.fromstring(body)
            _validate_tree(root, max_nodes=self.max_nodes, max_depth=self.max_depth)
        except (FeedParseError, _DefusedXmlException) as error:
            if isinstance(error, FeedParseError):
                raise
            raise FeedParseError("Feed XML was rejected by the safe parser.") from None
        except Exception:
            # XML ParseError subclasses vary between stdlib and defusedxml.
            raise FeedParseError("Feed XML could not be parsed safely.") from None

        root_name = _local_name(root.tag)
        atom = root_name == "feed"
        if not atom and root_name not in {"rss", "rdf", "rdf:rdf"}:
            raise FeedParseError("Feed format is not RSS or Atom.")
        if atom:
            container = root
            raw_entries = _children(container, "entry")
            title_node = _first_child(container, "title")
            feed_title = sanitize_text(_node_text(title_node), max_chars=_FEED_TITLE_MAX)
            site_link = _atom_link(container, base_url=str(subscription.feed_url))
        else:
            container = _first_child(root, "channel")
            if container is None:
                container = root
            raw_entries = _children(container, "item")
            # RDF feeds commonly place item elements beside channel; include
            # them when the channel itself has no entries.
            if not raw_entries:
                raw_entries = _children(root, "item")
            feed_title = sanitize_text(
                _node_text(_first_child(container, "title")), max_chars=_FEED_TITLE_MAX
            )
            site_link = _rss_link(container, base_url=str(subscription.feed_url))

        if not feed_title:
            feed_title = subscription.title
        observed_at = collected_at or datetime.now(UTC)
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("collected_at must be timezone-aware")
        observed_at = observed_at.astimezone(UTC)
        entries: list[FeedEntry] = []
        seen: set[str] = set()
        for element in raw_entries:
            if len(entries) >= self.max_items:
                break
            entry = _entry_from_element(
                element,
                subscription,
                atom=atom,
                first_seen_at=observed_at,
            )
            if entry is None or entry.entry_id in seen:
                continue
            seen.add(entry.entry_id)
            entries.append(entry)
        try:
            return FeedSnapshot(
                subscription_id=subscription.subscription_id,
                feed_url=subscription.feed_url,
                title=feed_title,
                site_url=site_link or subscription.site_url,
                entries=tuple(entries),
                collected_at=observed_at,
                etag=etag,
                last_modified=last_modified,
            )
        except (TypeError, ValueError, ValidationError) as error:
            raise FeedParseError("Feed metadata exceeded a safe bound.") from error


def parse_feed(
    body: bytes,
    subscription: FeedSubscription,
    *,
    max_items: int = 100,
    max_response_bytes: int = 5_000_000,
    collected_at: datetime | None = None,
    etag: str | None = None,
    last_modified: str | None = None,
) -> FeedSnapshot:
    """Convenience wrapper around :class:`FeedParser`."""

    return FeedParser(
        max_items=max_items,
        max_response_bytes=max_response_bytes,
    ).parse(
        body,
        subscription,
        collected_at=collected_at,
        etag=etag,
        last_modified=last_modified,
    )


def matches_filters(entry: FeedEntry, subscription: FeedSubscription) -> bool:
    """Apply case-insensitive include-any and exclude-any keyword filters."""

    haystack = " ".join(
        value for value in (entry.title, entry.summary_text or "", entry.author or "") if value
    ).casefold()
    if subscription.include_terms and not any(
        term in haystack for term in subscription.include_terms
    ):
        return False
    return not any(term in haystack for term in subscription.exclude_terms)


def filter_entries(
    entries: Iterable[FeedEntry], subscription: FeedSubscription
) -> tuple[FeedEntry, ...]:
    """Return only entries matching a subscription's include/exclude policy."""

    return tuple(entry for entry in entries if matches_filters(entry, subscription))


RSSAtomParser = FeedParser
RssAtomParser = FeedParser


__all__ = [
    "FeedParser",
    "RSSAtomParser",
    "RssAtomParser",
    "filter_entries",
    "matches_filters",
    "parse_feed",
    "parse_published_at",
    "sanitize_html",
    "sanitize_text",
    "stable_entry_id",
]
