"""Stable, deterministic identity helpers for RSS and Atom entries."""

from __future__ import annotations

import hashlib
import posixpath
import unicodedata
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID


def _subscription_text(subscription_id: UUID | str) -> str:
    if isinstance(subscription_id, UUID):
        return str(subscription_id)
    if not isinstance(subscription_id, str):
        raise TypeError("subscription_id must be a UUID or UUID string")
    try:
        return str(UUID(subscription_id))
    except ValueError as error:
        raise ValueError("subscription_id must be a UUID") from error


def canonicalize_entry_url(value: Any) -> str:
    """Return the URL form used for entry identity and safe navigation.

    Fragments are presentation-only and are removed.  Scheme/host casing,
    default ports, dot segments, and an empty path are normalized while query
    parameter order is intentionally preserved because ordering can be
    meaningful to a server.
    """

    text = str(value).strip()
    if not text or len(text) > 2_048:
        raise ValueError("feed entry URL must be a bounded HTTP(S) URL")
    parsed = urlsplit(text)
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise ValueError("feed entry URL must use HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("feed entry URL cannot contain user information")
    if not parsed.hostname:
        raise ValueError("feed entry URL must include a host")
    try:
        hostname = parsed.hostname.encode("idna").decode("ascii").casefold()
        port = parsed.port
    except (UnicodeError, ValueError) as error:
        raise ValueError("feed entry URL host or port is invalid") from error
    default_port = 80 if parsed.scheme.casefold() == "http" else 443
    netloc = hostname if port in (None, default_port) else f"{hostname}:{port}"
    path = parsed.path or "/"
    # Collapse only dot segments; preserve duplicate slashes and encoded data.
    normalized_path = posixpath.normpath(path)
    if path.startswith("/") and not normalized_path.startswith("/"):
        normalized_path = "/" + normalized_path
    if path.endswith("/") and not normalized_path.endswith("/"):
        normalized_path += "/"
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            netloc,
            normalized_path,
            parsed.query,
            "",
        )
    )


def entry_identity_key(*, guid: str | None = None, url: Any = None) -> str:
    """Build the stable source key used before hashing an entry ID.

    A non-empty provider GUID is authoritative.  Feeds without one fall back
    to their canonical link; callers must provide at least one identity input.
    """

    if guid is not None:
        normalized_guid = unicodedata.normalize("NFKC", str(guid)).strip()
        if normalized_guid:
            if len(normalized_guid) > 2_048:
                raise ValueError("feed entry GUID must be bounded")
            return f"guid:{normalized_guid}"
    if url is None:
        raise ValueError("feed entry identity requires a GUID or URL")
    return f"url:{canonicalize_entry_url(url)}"


def stable_feed_entry_id(
    subscription_id: UUID | str,
    guid: str | None = None,
    url: Any = None,
) -> str:
    """Return a stable, opaque ID scoped to one subscription.

    Scoping the digest to the subscription keeps the same article in two
    intentionally separate subscriptions from colliding while still making
    feed reorderings and process restarts idempotent.
    """

    subscription_text = _subscription_text(subscription_id)
    source_key = entry_identity_key(guid=guid, url=url)
    digest = hashlib.sha256(
        f"personal-hub:feed-entry:v1\x00{subscription_text}\x00{source_key}".encode()
    ).hexdigest()
    return f"feed-{digest}"


# Names used by provider adapters and tests across the implementation.
stable_entry_id = stable_feed_entry_id
feed_entry_id = stable_feed_entry_id
canonical_url = canonicalize_entry_url
canonical_entry_url = canonicalize_entry_url


__all__ = [
    "canonical_entry_url",
    "canonical_url",
    "canonicalize_entry_url",
    "entry_identity_key",
    "feed_entry_id",
    "stable_entry_id",
    "stable_feed_entry_id",
]
