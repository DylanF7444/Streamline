"""Strict contracts for Personal Hub RSS and Atom subscriptions.

The feed domain stores only the bounded, normalized representation needed by the
News and Today surfaces.  Fetching and parsing live behind separate adapters; no
raw XML, response headers, cookies, or provider credentials belong in these
contracts.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Literal, Protocol, runtime_checkable
from urllib.parse import urljoin, urlsplit
from uuid import UUID, uuid4

from pydantic import AnyHttpUrl, Field, field_validator, model_validator

from personal_hub.contracts import HubContract

FeedReadStateValue = Literal["unread", "read"]
FeedSyncStateValue = Literal[
    "ready",
    "empty",
    "stale",
    "offline",
    "permission_required",
    "disconnected",
    "error",
]


class FeedError(RuntimeError):
    """Base exception for bounded feed collection failures."""


class FeedPolicyError(FeedError):
    """The configured origin or request violates the Hub network policy."""


class FeedSSRFError(FeedPolicyError):
    """The host or redirect resolves to a local/private address."""


class FeedRedirectError(FeedPolicyError):
    """A redirect exceeds the approved redirect policy."""


class FeedResponseTooLargeError(FeedError):
    """The response exceeded the configured byte bound."""


class FeedParseError(FeedError):
    """The response was malformed, hostile, or not a supported feed."""


class FeedOfflineError(FeedError):
    """Collection was prevented by the Hub offline gate."""


class FeedUnavailableError(FeedError):
    """The origin or transport was unavailable."""


class FeedPermissionRequiredError(FeedError):
    """The feed requires a permission or authentication change."""


_HEADER_VALUE_RE: Final[re.Pattern[str]] = re.compile(r"[^\r\n]*\Z")
_TERM_MAX_LENGTH: Final[int] = 128
_MAX_TERMS: Final[int] = 64


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("feed timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _terms(value: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    for term in value:
        item = " ".join(term.split()).casefold()
        if not item:
            raise ValueError("feed filter terms cannot be empty")
        if len(item) > _TERM_MAX_LENGTH:
            raise ValueError("feed filter terms must be bounded")
        if item not in normalized:
            normalized.append(item)
    if len(normalized) > _MAX_TERMS:
        raise ValueError("feed filter terms are limited to 64 values")
    return tuple(normalized)


def _header(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if not _HEADER_VALUE_RE.fullmatch(normalized) or len(normalized) > 512:
        raise ValueError("feed cache validators must be bounded single-line values")
    return normalized


def _http_url(value: AnyHttpUrl | None) -> AnyHttpUrl | None:
    if value is None:
        return None
    # AnyHttpUrl already limits the scheme to HTTP(S).  Explicitly reject
    # embedded credentials because feed URLs are sent to external adapters and
    # credentials must never be accepted as part of a subscription URL.
    if value.username is not None or value.password is not None:
        raise ValueError("feed URLs cannot contain user information")
    return value


def _reviewed_origin(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().rstrip("/")
    parts = urlsplit(normalized)
    if parts.scheme.casefold() not in {"http", "https"} or not parts.hostname:
        raise ValueError("reviewed_origin must be an HTTP(S) origin")
    if parts.username is not None or parts.password is not None:
        raise ValueError("reviewed_origin cannot contain credentials")
    if parts.path not in {"", "/"} or parts.query or parts.fragment:
        raise ValueError("reviewed_origin must not include a path or query")
    try:
        port = parts.port
    except ValueError as error:
        raise ValueError("reviewed_origin contains an invalid port") from error
    default_port = 443 if parts.scheme.casefold() == "https" else 80
    host = parts.hostname.encode("idna").decode("ascii").casefold()
    return (
        f"{parts.scheme.casefold()}://{host}{f':{port}' if port and port != default_port else ''}"
    )


def canonical_url(value: str, *, base_url: str | None = None) -> str:
    """Resolve and normalize an HTTP(S) feed link for parser output."""

    if not isinstance(value, str):
        raise ValueError("feed links must be strings")
    candidate = urljoin(base_url or "", value.strip())
    if any(character in candidate for character in "\r\n\x00"):
        raise ValueError("feed links contain unsafe characters")
    # Keep this helper deliberately small and deterministic.  The fetcher’s
    # network policy performs DNS/private-address checks separately.
    from personal_hub.feeds.identity import canonicalize_entry_url

    return canonicalize_entry_url(candidate)


class FeedSubscription(HubContract):
    """User-selected RSS/Atom source and its local filtering policy."""

    subscription_id: UUID = Field(default_factory=uuid4)
    title: str = Field(min_length=1, max_length=240)
    feed_url: AnyHttpUrl
    site_url: AnyHttpUrl | None = None
    enabled: bool = True
    refresh_minutes: int = Field(default=30, ge=15, le=24 * 60)
    include_terms: tuple[str, ...] = Field(default=(), max_length=_MAX_TERMS)
    exclude_terms: tuple[str, ...] = Field(default=(), max_length=_MAX_TERMS)
    digest_only: bool = False
    # Plain HTTP is intentionally a per-subscription opt-in in addition to the
    # process-level policy on FeedFetcher.  ``reviewed_origin`` is an exact
    # redirect allow-list entry, never a hostname wildcard.
    allow_http: bool = False
    reviewed_origin: str | None = Field(default=None, max_length=512)

    _feed_url = field_validator("feed_url", "site_url")(_http_url)
    _include_terms = field_validator("include_terms")(_terms)
    _exclude_terms = field_validator("exclude_terms")(_terms)
    _reviewed_origin = field_validator("reviewed_origin")(_reviewed_origin)

    @model_validator(mode="after")
    def _http_requires_explicit_review(self) -> FeedSubscription:
        feed_parts = urlsplit(str(self.feed_url))
        if feed_parts.scheme.casefold() == "http":
            if not self.allow_http:
                raise ValueError("HTTP feed subscriptions require allow_http=True")
            if self.reviewed_origin is None:
                raise ValueError("HTTP feed subscriptions require a reviewed_origin")
        if self.reviewed_origin is not None:
            exact_origin = _reviewed_origin(f"{feed_parts.scheme.casefold()}://{feed_parts.netloc}")
            if self.reviewed_origin != exact_origin:
                raise ValueError("reviewed_origin must exactly match the feed URL origin")
        return self


class FeedSyncState(HubContract):
    """Bounded conditional-request and backoff metadata for one subscription."""

    subscription_id: UUID
    state: FeedSyncStateValue = "stale"
    etag: str | None = Field(default=None, max_length=512)
    last_modified: str | None = Field(default=None, max_length=512)
    last_checked_at: datetime | None = None
    last_success_at: datetime | None = None
    failure_count: int = Field(default=0, ge=0, le=1_000_000)
    backoff_until: datetime | None = None
    safe_error: str | None = Field(default=None, max_length=512)

    _etag = field_validator("etag", "last_modified")(_header)

    @field_validator("last_checked_at", "last_success_at", "backoff_until")
    @classmethod
    def _timestamps_are_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @field_validator("safe_error")
    @classmethod
    def _safe_error_is_single_line(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        return normalized or None


class FeedEntry(HubContract):
    """Bounded normalized feed entry content.

    ``entry_id`` is generated from a feed GUID when present, otherwise from a
    canonical URL.  See :mod:`personal_hub.feeds.identity`.
    """

    entry_id: str = Field(min_length=1, max_length=128)
    subscription_id: UUID
    title: str = Field(min_length=1, max_length=500)
    url: AnyHttpUrl
    author: str | None = Field(default=None, max_length=240)
    published_at: datetime | None = None
    summary_text: str | None = Field(default=None, max_length=8_000)
    first_seen_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    _url = field_validator("url")(_http_url)

    @field_validator("published_at", "first_seen_at")
    @classmethod
    def _timestamps_are_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @field_validator("author", "summary_text")
    @classmethod
    def _optional_text_is_bounded(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        return normalized or None


class FeedReadState(HubContract):
    """Local read/unread state kept separate from provider content."""

    entry_id: str = Field(min_length=1, max_length=128)
    state: FeedReadStateValue = "unread"
    read_at: datetime | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("read_at", "updated_at")
    @classmethod
    def _timestamps_are_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)


class FeedEntryRecord(HubContract):
    """A feed entry joined with its local read state for display."""

    entry: FeedEntry
    state: FeedReadStateValue = "unread"
    read_at: datetime | None = None

    @field_validator("read_at")
    @classmethod
    def _read_at_is_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)


class FeedSnapshot(HubContract):
    """A successful bounded parse, held in memory or passed to persistence."""

    subscription_id: UUID
    feed_url: AnyHttpUrl
    title: str | None = Field(default=None, max_length=500)
    site_url: AnyHttpUrl | None = None
    entries: tuple[FeedEntry, ...] = Field(default=(), max_length=1_000)
    collected_at: datetime
    etag: str | None = Field(default=None, max_length=512)
    last_modified: str | None = Field(default=None, max_length=512)
    not_modified: bool = False

    _feed_url = field_validator("feed_url", "site_url")(_http_url)

    @field_validator("collected_at")
    @classmethod
    def _collected_at_is_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("title")
    @classmethod
    def _snapshot_title_is_bounded(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if len(normalized) > 500:
            raise ValueError("feed title must be bounded")
        return normalized or None

    _etag = field_validator("etag", "last_modified")(_header)

    @property
    def item_count(self) -> int:
        return len(self.entries)


class FeedResult(HubContract):
    """Independent refresh outcome; one failed feed cannot erase another."""

    subscription_id: UUID
    feed_url: AnyHttpUrl
    title: str = Field(min_length=1, max_length=500)
    state: FeedSyncStateValue
    checked_at: datetime
    snapshot: FeedSnapshot | None = None
    last_success_at: datetime | None = None
    item_count: int = Field(default=0, ge=0, le=1_000)
    duration_ms: float = Field(default=0.0, ge=0.0, le=300_000.0)
    safe_message: str = Field(min_length=1, max_length=512)
    status_code: int | None = Field(default=None, ge=100, le=599)
    not_modified: bool = False
    backoff_until: datetime | None = None

    _feed_url = field_validator("feed_url")(_http_url)

    @field_validator("checked_at", "last_success_at", "backoff_until")
    @classmethod
    def _result_timestamps_are_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @field_validator("title", "safe_message")
    @classmethod
    def _result_text_is_safe(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("feed result text cannot be blank")
        return normalized


@dataclass(frozen=True, slots=True)
class FeedResponse:
    """Bounded transport response passed from the HTTP client to the parser."""

    status_code: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""
    url: str | None = None

    def __post_init__(self) -> None:
        if not 100 <= self.status_code <= 599:
            raise ValueError("feed response status_code must be an HTTP status")
        if not isinstance(self.body, bytes):
            raise TypeError("feed response body must be bytes")
        if len(self.body) > 20_000_000:
            raise ValueError("feed response body exceeds the transport hard limit")


@dataclass(frozen=True, slots=True)
class FeedBackoff:
    """Per-origin retry state kept in memory by the refresh coordinator."""

    origin: str
    failure_count: int = 0
    next_allowed_at: datetime | None = None


@runtime_checkable
class FeedSource(Protocol):
    """Minimal Hub-owned source protocol for a feed subscription."""

    async def fetch(
        self,
        subscription: FeedSubscription,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FeedSnapshot: ...


# Friendly aliases for adapters and callers that use shorter names.
FeedReadStatus = FeedReadState
FeedEntryView = FeedEntryRecord
FeedRefreshResult = FeedResult
FeedSnapshotResult = FeedSnapshot
FeedSourceProtocol = FeedSource


__all__ = [
    "FeedBackoff",
    "canonical_url",
    "FeedError",
    "FeedEntry",
    "FeedEntryRecord",
    "FeedEntryView",
    "FeedOfflineError",
    "FeedParseError",
    "FeedPermissionRequiredError",
    "FeedPolicyError",
    "FeedRedirectError",
    "FeedRefreshResult",
    "FeedResponse",
    "FeedResponseTooLargeError",
    "FeedReadState",
    "FeedReadStatus",
    "FeedReadStateValue",
    "FeedResult",
    "FeedSnapshot",
    "FeedSnapshotResult",
    "FeedSource",
    "FeedSourceProtocol",
    "FeedSSRFError",
    "FeedSubscription",
    "FeedSyncState",
    "FeedSyncStateValue",
    "FeedUnavailableError",
]
