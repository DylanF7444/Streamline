"""Validated provider snapshots shared by an opted-in collector device."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Final, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from personal_hub.calendar.contracts import CalendarSnapshot
from personal_hub.contracts import HubContract
from personal_hub.feeds.contracts import FeedEntry
from personal_hub.messages.contracts import MessageSnapshot, MessageSourceConfig
from personal_hub.watches.contracts import AlertEvent, WatchObservation

RelayCategory = Literal["calendar", "messages", "feeds", "watches"]
RELAY_CATEGORIES: Final[tuple[RelayCategory, ...]] = (
    "calendar",
    "messages",
    "feeds",
    "watches",
)
PROVIDER_RELAY_FORMAT: Final[str] = "personal-hub-provider-relay-v1"
PROVIDER_RELAY_MAX_BYTES: Final[int] = 16 * 1024 * 1024
PROVIDER_RELAY_MAX_ITEMS: Final[int] = 50_000
PROVIDER_RELAY_MAX_AGE: Final[timedelta] = timedelta(days=30)
_DEVICE_ID: Final[re.Pattern[str]] = re.compile(r"[a-f0-9]{32}\Z")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("provider relay timestamps must be timezone-aware")
    return value.astimezone(UTC)


class ProviderRelayCalendarSnapshot(HubContract):
    display_name: str = Field(min_length=1, max_length=160)
    snapshot: CalendarSnapshot

    @field_validator("display_name")
    @classmethod
    def _display_name_is_safe(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized or any(ord(character) < 32 for character in normalized):
            raise ValueError("calendar relay display name is invalid")
        return normalized


class ProviderRelayMessageSnapshot(HubContract):
    """Read-only message configuration and summaries, never message bodies."""

    config: MessageSourceConfig
    snapshot: MessageSnapshot

    @model_validator(mode="after")
    def _message_identity_matches(self) -> ProviderRelayMessageSnapshot:
        if (
            self.config.source_id != self.snapshot.source_id
            or self.config.provider != self.snapshot.provider
            or self.config.account_id != self.snapshot.account_id
            or any(
                item.source_id != self.config.source_id
                or item.provider != self.config.provider
                or item.account_id != self.config.account_id
                for item in self.snapshot.summaries
            )
            or len(self.snapshot.summaries) > self.config.query.limit
        ):
            raise ValueError("provider relay message identity is invalid")
        return self


class ProviderRelayFeedSnapshot(HubContract):
    subscription_id: UUID
    collected_at: datetime
    entries: tuple[FeedEntry, ...] = Field(default=(), max_length=1_000)

    @field_validator("collected_at")
    @classmethod
    def _collected_at_is_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def _feed_entries_match(self) -> ProviderRelayFeedSnapshot:
        if any(item.subscription_id != self.subscription_id for item in self.entries):
            raise ValueError("provider relay feed entry identity is invalid")
        return self


class ProviderRelayWatchSnapshot(HubContract):
    watch_id: UUID
    checked_at: datetime
    observations: tuple[WatchObservation, ...] = Field(default=(), max_length=200)
    alerts: tuple[AlertEvent, ...] = Field(default=(), max_length=200)

    @field_validator("checked_at")
    @classmethod
    def _checked_at_is_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def _watch_records_match(self) -> ProviderRelayWatchSnapshot:
        if any(item.watch_id != self.watch_id for item in self.observations) or any(
            item.watch_id != self.watch_id for item in self.alerts
        ):
            raise ValueError("provider relay watch identity is invalid")
        return self


class ProviderRelaySnapshot(HubContract):
    """One collector's bounded, encrypted view of currently cached provider data."""

    format: Literal["personal-hub-provider-relay-v1"] = PROVIDER_RELAY_FORMAT
    profile_id: str = Field(min_length=1, max_length=128)
    collector_device_id: str = Field(min_length=32, max_length=32)
    generated_at: datetime
    expires_at: datetime
    categories: tuple[RelayCategory, ...] = Field(default=RELAY_CATEGORIES, max_length=4)
    calendars: tuple[ProviderRelayCalendarSnapshot, ...] = Field(default=(), max_length=100)
    messages: tuple[ProviderRelayMessageSnapshot, ...] = Field(default=(), max_length=100)
    feeds: tuple[ProviderRelayFeedSnapshot, ...] = Field(default=(), max_length=1_000)
    watches: tuple[ProviderRelayWatchSnapshot, ...] = Field(default=(), max_length=1_000)

    @field_validator("profile_id")
    @classmethod
    def _profile_id_is_safe(cls, value: str) -> str:
        normalized = value.strip()
        if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}", normalized) is None:
            raise ValueError("provider relay profile identity is invalid")
        return normalized

    @field_validator("collector_device_id")
    @classmethod
    def _collector_id_is_safe(cls, value: str) -> str:
        if not _DEVICE_ID.fullmatch(value):
            raise ValueError("provider relay collector identity is invalid")
        return value

    @field_validator("generated_at", "expires_at")
    @classmethod
    def _timestamps_are_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("categories")
    @classmethod
    def _categories_are_unique(cls, value: tuple[RelayCategory, ...]) -> tuple[RelayCategory, ...]:
        if len(value) != len(set(value)):
            raise ValueError("provider relay categories must be unique")
        return tuple(category for category in RELAY_CATEGORIES if category in value)

    @model_validator(mode="after")
    def _snapshot_is_bounded(self) -> ProviderRelaySnapshot:
        if self.expires_at <= self.generated_at:
            raise ValueError("provider relay expiry must follow its generation time")
        if self.expires_at - self.generated_at > PROVIDER_RELAY_MAX_AGE:
            raise ValueError("provider relay retention exceeds the supported maximum")
        if self.calendars and "calendar" not in self.categories:
            raise ValueError("provider relay contains a disabled calendar category")
        if self.messages and "messages" not in self.categories:
            raise ValueError("provider relay contains a disabled message category")
        if self.feeds and "feeds" not in self.categories:
            raise ValueError("provider relay contains a disabled feed category")
        if self.watches and "watches" not in self.categories:
            raise ValueError("provider relay contains a disabled watch category")
        identities = [item.snapshot.source_id for item in self.calendars]
        identities.extend(item.config.source_id for item in self.messages)
        if len(identities) != len(set(identities)):
            raise ValueError("provider relay source identities must be unique")
        if len({item.subscription_id for item in self.feeds}) != len(self.feeds):
            raise ValueError("provider relay feed identities must be unique")
        if len({item.watch_id for item in self.watches}) != len(self.watches):
            raise ValueError("provider relay watch identities must be unique")
        item_count = (
            sum(len(item.snapshot.events) for item in self.calendars)
            + sum(len(item.snapshot.summaries) for item in self.messages)
            + sum(len(item.entries) for item in self.feeds)
            + sum(len(item.observations) + len(item.alerts) for item in self.watches)
        )
        if item_count > PROVIDER_RELAY_MAX_ITEMS:
            raise ValueError("provider relay contains too many records")
        encoded = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(encoded.encode("utf-8")) > PROVIDER_RELAY_MAX_BYTES:
            raise ValueError("provider relay snapshot is too large")
        return self


__all__ = [
    "PROVIDER_RELAY_FORMAT",
    "PROVIDER_RELAY_MAX_AGE",
    "PROVIDER_RELAY_MAX_BYTES",
    "PROVIDER_RELAY_MAX_ITEMS",
    "RELAY_CATEGORIES",
    "ProviderRelayCalendarSnapshot",
    "ProviderRelayFeedSnapshot",
    "ProviderRelayMessageSnapshot",
    "ProviderRelaySnapshot",
    "ProviderRelayWatchSnapshot",
    "RelayCategory",
]
