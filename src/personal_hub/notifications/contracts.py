"""Strict notification contracts owned by Personal Hub."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


class NotificationPolicy(StrEnum):
    """Delivery policy selected by a source/watch."""

    IMMEDIATE = "immediate"
    DIGEST = "digest"
    INBOX_ONLY = "inbox_only"


class NotificationSeverity(StrEnum):
    INFO = "info"
    ATTENTION = "attention"
    URGENT = "urgent"


class QuietHoursSettings(BaseModel):
    """Validated quiet-hours settings using local wall-clock HH:MM values."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    enabled: bool = False
    start: str = "22:00"
    end: str = "07:00"

    _start_format = field_validator("start", "end")(
        lambda value: _validate_clock(value, "quiet-hours time")
    )


class NotificationRequest(BaseModel):
    """Content-free notification metadata used to decide delivery."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    notification_id: str = Field(min_length=1, max_length=128)
    policy: NotificationPolicy
    severity: NotificationSeverity = NotificationSeverity.INFO
    urgent_bypass_quiet_hours: bool = False
    created_at: datetime

    _notification_id = field_validator("notification_id")(
        lambda value: _validate_id(value, "notification_id")
    )
    _created_at = field_validator("created_at")(
        lambda value: _validate_timestamp(value, "created_at")
    )


class NotificationDecision(BaseModel):
    """Deterministic Inbox/system-notification outcome."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    policy: NotificationPolicy
    enqueue_inbox: bool
    deliver_system_notification: bool
    suppressed_reason: str | None = Field(default=None, max_length=40)


class NotificationDelivery(BaseModel):
    """Safe metadata recorded after a notification decision is applied."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    delivery_id: str = Field(min_length=1, max_length=128)
    policy: NotificationPolicy
    enqueue_inbox: bool
    system_delivered: bool
    suppressed_reason: str | None = Field(default=None, max_length=40)
    recorded_at: datetime

    _delivery_id = field_validator("delivery_id")(lambda value: _validate_id(value, "delivery_id"))
    _recorded_at = field_validator("recorded_at")(
        lambda value: _validate_timestamp(value, "recorded_at")
    )


def _validate_id(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not _ID_PATTERN.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a stable identifier")
    return normalized


def _validate_clock(value: str, field_name: str) -> str:
    normalized = value.strip()
    match = re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", normalized)
    if match is None:
        raise ValueError(f"{field_name} must use HH:MM")
    return normalized


def _validate_timestamp(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


# Public aliases make the intent clear at call sites without duplicating models.
DeliveryPolicy = NotificationPolicy
DeliveryDecision = NotificationDecision

__all__ = [
    "DeliveryDecision",
    "DeliveryPolicy",
    "NotificationDecision",
    "NotificationDelivery",
    "NotificationPolicy",
    "NotificationRequest",
    "NotificationSeverity",
    "QuietHoursSettings",
]
