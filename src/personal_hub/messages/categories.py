"""Deterministic, local-only categories for attention-message summaries.

The category is a presentation aid, not a mailbox mutation.  Provider labels are
trusted only as bounded hints and the fallback rules intentionally stay conservative:
when a message does not have a strong signal it remains in ``primary``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final, Literal

type MessageCategory = Literal["primary", "promotions", "social", "updates", "spam"]

MESSAGE_CATEGORY_VALUES: Final[tuple[MessageCategory, ...]] = (
    "primary",
    "promotions",
    "social",
    "updates",
    "spam",
)
MESSAGE_CATEGORY_LABELS: Final[dict[MessageCategory, str]] = {
    "primary": "Primary",
    "promotions": "Promotions",
    "social": "Social",
    "updates": "Updates",
    "spam": "Spam",
}

_SPAM_MARKERS: Final[frozenset[str]] = frozenset(
    {
        "spam",
        "category_spam",
        "junk",
        "junk_mail",
        "junk_email",
        "junkemail",
        "quarantine",
    }
)
_PROMOTION_MARKERS: Final[frozenset[str]] = frozenset(
    {
        "category_promotions",
        "promotion",
        "promotions",
        "marketing",
        "advertising",
        "advertisement",
        "commercial",
    }
)
_SOCIAL_MARKERS: Final[frozenset[str]] = frozenset(
    {
        "category_social",
        "social",
        "social_updates",
        "forums",
        "category_forums",
    }
)
_UPDATE_MARKERS: Final[frozenset[str]] = frozenset(
    {
        "category_updates",
        "updates",
        "notifications",
        "notification",
        "transactional",
    }
)
_PRIMARY_MARKERS: Final[frozenset[str]] = frozenset(
    {
        "category_personal",
        "category_primary",
        "personal",
        "primary",
        "focused",
    }
)

_SPAM_TERMS: Final[tuple[str, ...]] = (
    "phishing",
    "scam",
    "lottery winner",
    "claim your prize",
    "you have won",
)
_PROMOTION_TERMS: Final[tuple[str, ...]] = (
    "unsubscribe",
    "sale",
    "discount",
    "coupon",
    "promo code",
    "promotion",
    "clearance",
    "limited time",
    "free shipping",
    "shop now",
    "register today",
    "save ",
)
_SOCIAL_TERMS: Final[tuple[str, ...]] = (
    "friend request",
    "new follower",
    "mentioned you",
    "commented on",
    "liked your",
    "shared a post",
    "sent you a message",
)
_UPDATE_TERMS: Final[tuple[str, ...]] = (
    "receipt",
    "invoice",
    "order confirmation",
    "shipping confirmation",
    "delivery update",
    "security alert",
    "verification code",
    "password reset",
    "account notification",
    "subscription renewal",
)
_SOCIAL_BRANDS: Final[tuple[str, ...]] = (
    "facebook",
    "instagram",
    "linkedin",
    "pinterest",
    "reddit",
    "snapchat",
    "tiktok",
    "discord",
    "x.com",
)
_UPDATE_BRANDS: Final[tuple[str, ...]] = (
    "github",
    "gitlab",
    "microsoft account",
    "google account",
    "apple id",
    "dropbox",
)


def is_message_category(value: object) -> bool:
    """Return whether ``value`` is one of the persisted display categories."""

    return isinstance(value, str) and value in MESSAGE_CATEGORY_VALUES


def message_category_label(value: object) -> str:
    """Return a safe human-readable label, defaulting unknown values to Primary."""

    return MESSAGE_CATEGORY_LABELS.get(value, "Primary") if isinstance(value, str) else "Primary"


def _normalize_marker(value: object) -> str:
    text = str(value or "").casefold()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")[:96]


def _markers(values: Iterable[object] | object | None) -> frozenset[str]:
    if values is None:
        return frozenset()
    if isinstance(values, (str, bytes, bytearray)):
        candidates: Iterable[object] = (values,)
    else:
        try:
            candidates = values  # type: ignore[assignment]
            iter(candidates)
        except TypeError:
            candidates = (values,)
    return frozenset(
        normalized for value in tuple(candidates)[:100] if (normalized := _normalize_marker(value))
    )


def _has_marker(markers: frozenset[str], candidates: frozenset[str]) -> bool:
    aliases = set(markers)
    aliases.update(
        value.removeprefix("category_") for value in markers if value.startswith("category_")
    )
    compact = {value.replace("_", "") for value in aliases}
    return any(value in aliases or value.replace("_", "") in compact for value in candidates)


def _normalized_text(*values: object) -> str:
    text = " ".join(str(value or "") for value in values)
    text = re.sub(r"[^a-z0-9]+", " ", text.casefold())
    return " ".join(text.split())[:4_000]


def _has_term(text: str, terms: tuple[str, ...]) -> bool:
    padded = f" {text} "
    return any(
        f" {re.sub(r'[^a-z0-9]+', ' ', term.casefold()).strip()} " in padded for term in terms
    )


def classify_message(
    *,
    provider: str = "",
    labels: Iterable[object] | object | None = None,
    categories: Iterable[object] | object | None = None,
    folder: str | None = None,
    inference_classification: str | None = None,
    sender: str = "",
    title: str = "",
    preview: str | None = None,
) -> MessageCategory:
    """Assign a Gmail-style category without sending content anywhere.

    Explicit provider labels win over heuristics.  The order is spam, promotions,
    social, updates, then primary so a malformed or conflicting provider response
    cannot make a spam item visible in a normal category.
    """

    marker_values: list[object] = []
    for value in (labels, categories, folder, inference_classification):
        if value is None:
            continue
        if isinstance(value, (str, bytes, bytearray)):
            marker_values.append(value)
        else:
            try:
                marker_values.extend(tuple(value))  # type: ignore[arg-type]
            except TypeError:
                marker_values.append(value)
    markers = _markers(marker_values)
    if _has_marker(markers, _SPAM_MARKERS):
        return "spam"
    if _has_marker(markers, _PROMOTION_MARKERS):
        return "promotions"
    if _has_marker(markers, _SOCIAL_MARKERS):
        return "social"
    if _has_marker(markers, _UPDATE_MARKERS):
        return "updates"
    if _has_marker(markers, _PRIMARY_MARKERS):
        return "primary"

    text = _normalized_text(sender, title, preview)
    provider_text = _normalized_text(provider, sender, title)
    if _has_term(text, _SPAM_TERMS):
        return "spam"
    if _has_term(text, _PROMOTION_TERMS):
        return "promotions"
    if _has_term(text, _SOCIAL_TERMS) or _has_term(provider_text, _SOCIAL_BRANDS):
        return "social"
    if _has_term(text, _UPDATE_TERMS) or _has_term(provider_text, _UPDATE_BRANDS):
        return "updates"
    return "primary"


# ``categorize_message`` reads naturally for callers that use the noun "category".
categorize_message = classify_message


__all__ = [
    "MESSAGE_CATEGORY_LABELS",
    "MESSAGE_CATEGORY_VALUES",
    "MessageCategory",
    "categorize_message",
    "classify_message",
    "is_message_category",
    "message_category_label",
]
