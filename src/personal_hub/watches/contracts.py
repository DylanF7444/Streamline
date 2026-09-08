"""Strict contracts for inert webpage watches and explainable alerts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import AnyHttpUrl, Field, field_validator, model_validator

from personal_hub.contracts import HubContract
from personal_hub.feeds.security import normalize_origins, origin_for_url

type WatchKind = Literal[
    "feed_match",
    "page_text",
    "page_selector",
    "product_price",
    "product_restock",
    "changedetection",
]
type ConditionKind = Literal[
    "changed",
    "contains",
    "price_below",
    "percentage_decrease",
    "restocked",
]
type NotificationPolicy = Literal["immediate", "digest", "inbox_only"]
type WatchValue = str | float | bool | None
type WatchState = Literal[
    "ready",
    "stale",
    "offline",
    "permission_required",
    "disconnected",
    "error",
]

_SELECTOR = re.compile(
    r"(?:[a-zA-Z][a-zA-Z0-9_-]*)?(?:#[a-zA-Z][a-zA-Z0-9_-]*|\.[a-zA-Z][a-zA-Z0-9_-]*|\[(?:itemprop|property|name)=(?:[a-zA-Z0-9:_-]+)\])?\Z"
)


class WatchError(RuntimeError):
    """Base exception for safe watch collection failures."""


class WatchPolicyError(WatchError):
    """A watch URL or outbound operation violates network policy."""


class WatchParseError(WatchError):
    """A page could not produce the requested bounded value."""


class WatchUnavailableError(WatchError):
    """A watched origin is temporarily unavailable."""


class WatchPermissionRequiredError(WatchError):
    """A watch needs authentication or permission review."""


class WatchOfflineError(WatchError):
    """Network collection was blocked by offline mode."""


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("watch timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _safe_line(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split())
    return normalized or None


class WatchCondition(HubContract):
    """Typed, non-executable change condition."""

    kind: ConditionKind = "changed"
    threshold: float | None = Field(default=None, ge=0, le=1_000_000_000)
    percentage: float | None = Field(default=None, gt=0, le=100)
    term: str | None = Field(default=None, max_length=240)
    selector: str | None = Field(default=None, max_length=240)
    alert_on_current_match: bool = False

    _term = field_validator("term")(_safe_line)

    @field_validator("selector")
    @classmethod
    def _safe_selector(cls, value: str | None) -> str | None:
        normalized = _safe_line(value)
        if normalized is not None and not _SELECTOR.fullmatch(normalized):
            raise ValueError("only a single inert tag, id, class, or metadata selector is allowed")
        return normalized

    @model_validator(mode="after")
    def _required_operand(self) -> WatchCondition:
        if self.kind == "price_below" and self.threshold is None:
            raise ValueError("price_below requires a threshold")
        if self.kind == "percentage_decrease" and self.percentage is None:
            raise ValueError("percentage_decrease requires a percentage")
        if self.kind == "contains" and self.term is None:
            raise ValueError("contains requires a term")
        return self


class WatchDefinition(HubContract):
    watch_id: UUID = Field(default_factory=uuid4)
    name: str = Field(min_length=1, max_length=240)
    kind: WatchKind
    source_url: AnyHttpUrl
    condition: WatchCondition
    refresh_minutes: int = Field(default=60, ge=15, le=1_440)
    enabled: bool = True
    notification_policy: NotificationPolicy = "immediate"
    cooldown_minutes: int = Field(default=60, ge=0, le=43_200)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    allow_http: bool = False
    reviewed_origin: str = Field(max_length=512)
    approved_redirect_origins: tuple[str, ...] = Field(default=(), max_length=8)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("created_at", "updated_at")
    @classmethod
    def _timestamps(cls, value: datetime) -> datetime:
        return _aware(value)

    @field_validator("approved_redirect_origins")
    @classmethod
    def _origins(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(normalize_origins(value)))

    @model_validator(mode="after")
    def _network_and_kind_policy(self) -> WatchDefinition:
        source = str(self.source_url)
        exact_origin = origin_for_url(source)
        if self.reviewed_origin != exact_origin:
            raise ValueError("reviewed_origin must exactly match the watched URL origin")
        if urlsplit(source).scheme.casefold() == "http" and not self.allow_http:
            raise ValueError("HTTP watches require explicit per-watch approval")
        minimum = 15 if self.kind in {"product_price", "product_restock"} else 30
        if self.kind == "feed_match":
            minimum = 15
        if self.refresh_minutes < minimum:
            raise ValueError(f"{self.kind} watches require at least {minimum} minutes")
        if self.kind == "page_selector" and not self.condition.selector:
            raise ValueError("page_selector watches require an inert selector")
        if self.kind == "product_price" and self.condition.kind not in {
            "changed",
            "price_below",
            "percentage_decrease",
        }:
            raise ValueError("product_price watch condition is incompatible")
        if self.kind == "product_restock" and self.condition.kind != "restocked":
            raise ValueError("product_restock watches require a restocked condition")
        return self


class WatchObservation(HubContract):
    observation_id: UUID = Field(default_factory=uuid4)
    watch_id: UUID
    checked_at: datetime
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    normalized_value: WatchValue = None
    metadata: dict[str, WatchValue] = Field(default_factory=dict, max_length=32)

    @field_validator("checked_at")
    @classmethod
    def _timestamp(cls, value: datetime) -> datetime:
        return _aware(value)

    @field_validator("normalized_value")
    @classmethod
    def _bounded_value(cls, value: WatchValue) -> WatchValue:
        if isinstance(value, str):
            normalized = " ".join(value.split())
            if len(normalized) > 4_000:
                raise ValueError("watch values must be bounded")
            return normalized
        return value

    @field_validator("metadata")
    @classmethod
    def _bounded_metadata(cls, value: dict[str, WatchValue]) -> dict[str, WatchValue]:
        result: dict[str, WatchValue] = {}
        for key, item in value.items():
            normalized_key = " ".join(key.split())
            if not normalized_key or len(normalized_key) > 64:
                raise ValueError("watch metadata keys must be bounded")
            if isinstance(item, str):
                item = " ".join(item.split())
                if len(item) > 512:
                    raise ValueError("watch metadata values must be bounded")
            result[normalized_key] = item
        return result


class AlertEvent(HubContract):
    alert_id: UUID = Field(default_factory=uuid4)
    watch_id: UUID
    reason_code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_.-]+$")
    title: str = Field(min_length=1, max_length=240)
    safe_summary: str = Field(min_length=1, max_length=1_000)
    previous_value: WatchValue = None
    current_value: WatchValue = None
    observed_at: datetime
    deduplication_key: str = Field(min_length=1, max_length=256)

    _title = field_validator("title", "safe_summary")(_safe_line)

    @field_validator("observed_at")
    @classmethod
    def _timestamp(cls, value: datetime) -> datetime:
        return _aware(value)


class WatchSyncState(HubContract):
    watch_id: UUID
    state: WatchState = "stale"
    last_checked_at: datetime | None = None
    last_success_at: datetime | None = None
    failure_count: int = Field(default=0, ge=0, le=1_000_000)
    backoff_until: datetime | None = None
    safe_error: str | None = Field(default=None, max_length=512)
    baseline_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    baseline_value: WatchValue = None

    @field_validator("last_checked_at", "last_success_at", "backoff_until")
    @classmethod
    def _timestamps(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _aware(value)

    _error = field_validator("safe_error")(_safe_line)


class WatchResult(HubContract):
    watch_id: UUID
    name: str = Field(min_length=1, max_length=240)
    state: WatchState
    checked_at: datetime
    safe_message: str = Field(min_length=1, max_length=512)
    observation: WatchObservation | None = None
    alert: AlertEvent | None = None
    baseline_established: bool = False

    @field_validator("checked_at")
    @classmethod
    def _timestamp(cls, value: datetime) -> datetime:
        return _aware(value)


@dataclass(frozen=True, slots=True)
class PageResponse:
    status_code: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""
    url: str | None = None

    def __post_init__(self) -> None:
        if not 100 <= self.status_code <= 599:
            raise ValueError("page response status must be an HTTP status")
        if not isinstance(self.body, bytes):
            raise TypeError("page response body must be bytes")


__all__ = [
    "AlertEvent",
    "ConditionKind",
    "NotificationPolicy",
    "PageResponse",
    "WatchCondition",
    "WatchDefinition",
    "WatchError",
    "WatchKind",
    "WatchObservation",
    "WatchOfflineError",
    "WatchParseError",
    "WatchPermissionRequiredError",
    "WatchPolicyError",
    "WatchResult",
    "WatchState",
    "WatchSyncState",
    "WatchUnavailableError",
    "WatchValue",
]
