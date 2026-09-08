"""Metadata-only diagnostics for Personal Hub.

Diagnostics are intentionally assembled from typed source and notification envelopes.  The
aggregator never serializes source snapshots, item payloads, URLs, titles, or exception
text, so a copyable report remains useful without becoming a second content store.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from personal_hub.notifications.contracts import NotificationDelivery, NotificationPolicy
from personal_hub.sources.contracts import SourceResult, SourceState, safe_state_message

_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_VERSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+\-]{0,63}\Z")
_SAFE_SUPPRESSION_REASONS = frozenset({"quiet_hours", "policy", "offline", "disabled"})


class DiagnosticSourceRecord(BaseModel):
    """Safe operational metadata for one source; no source content is retained."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    source_id: str = Field(min_length=1, max_length=128)
    provider_id: str = Field(min_length=1, max_length=128)
    state: SourceState
    checked_at: datetime
    last_success_at: datetime | None = None
    duration_ms: float = Field(default=0.0, ge=0, le=86_400_000)
    item_count: int = Field(default=0, ge=0, le=100_000)
    response_size_bucket: str = Field(default="unknown", max_length=32)
    backoff_seconds: float = Field(default=0.0, ge=0, le=86_400)
    safe_message: str = Field(max_length=160)

    _source_id = field_validator("source_id")(
        lambda value: _validate_identifier(value, "source_id")
    )
    _provider_id = field_validator("provider_id")(
        lambda value: _validate_identifier(value, "provider_id")
    )
    _checked_at = field_validator("checked_at", "last_success_at")(
        lambda value: _validate_timestamp(value)
    )


class DiagnosticNotificationRecord(BaseModel):
    """Safe delivery metadata without notification text or recipient details."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    delivery_id: str = Field(min_length=1, max_length=128)
    policy: NotificationPolicy
    inbox_enqueued: bool
    system_delivered: bool
    suppressed_reason: str | None = Field(default=None, max_length=40)
    recorded_at: datetime

    _delivery_id = field_validator("delivery_id")(
        lambda value: _validate_identifier(value, "delivery_id")
    )
    _recorded_at = field_validator("recorded_at")(lambda value: _validate_timestamp(value))


class DiagnosticsReport(BaseModel):
    """Copyable redacted report containing only bounded operational metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    generated_at: datetime
    application_version: str = Field(min_length=1, max_length=64)
    schema_version: int = Field(ge=0, le=1_000_000)
    sources: tuple[DiagnosticSourceRecord, ...] = Field(max_length=512)
    notifications: tuple[DiagnosticNotificationRecord, ...] = Field(max_length=512)
    safe_summary: str = Field(max_length=160)

    _generated_at = field_validator("generated_at")(lambda value: _validate_timestamp(value))
    _application_version = field_validator("application_version")(
        lambda value: _validate_version(value)
    )

    def to_json(self) -> str:
        """Serialize only the report schema in deterministic key order."""

        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


class DiagnosticsAggregator:
    """Collect bounded source/delivery metadata and produce a redacted report."""

    def __init__(
        self,
        *,
        application_version: str = "unknown",
        schema_version: int = 1,
        max_sources: int = 512,
        max_notifications: int = 512,
    ) -> None:
        if max_sources < 1 or max_sources > 512:
            raise ValueError("max_sources must be between 1 and 512")
        if max_notifications < 1 or max_notifications > 512:
            raise ValueError("max_notifications must be between 1 and 512")
        self.application_version = _validate_version(application_version)
        if schema_version < 0 or schema_version > 1_000_000:
            raise ValueError("schema_version is outside the supported range")
        self.schema_version = schema_version
        self.max_sources = max_sources
        self.max_notifications = max_notifications
        self._sources: dict[str, DiagnosticSourceRecord] = {}
        self._notifications: dict[str, DiagnosticNotificationRecord] = {}

    def record_source(
        self,
        result: SourceResult,
        *,
        response_size_bytes: int | None = None,
        backoff_seconds: float = 0.0,
    ) -> DiagnosticSourceRecord:
        """Record one result while intentionally ignoring its snapshot contents."""

        if response_size_bytes is not None and response_size_bytes < 0:
            raise ValueError("response_size_bytes cannot be negative")
        if backoff_seconds < 0 or backoff_seconds > 86_400:
            raise ValueError("backoff_seconds is outside the supported range")
        record = DiagnosticSourceRecord(
            source_id=result.source_id,
            provider_id=result.provider_id,
            state=result.state,
            checked_at=result.checked_at,
            last_success_at=result.last_success_at,
            duration_ms=result.duration_ms,
            item_count=result.item_count,
            response_size_bucket=_response_size_bucket(response_size_bytes),
            backoff_seconds=backoff_seconds,
            # Replace any adapter-provided text with a fixed state message.  The source
            # service already emits safe messages, but this is a second boundary for
            # callers constructing SourceResult values directly.
            safe_message=safe_state_message(result.state),
        )
        self._sources[result.source_id] = record
        self._trim(self._sources, self.max_sources)
        return record

    def record_notification(
        self,
        delivery: NotificationDelivery,
    ) -> DiagnosticNotificationRecord:
        """Record only delivery booleans and a bounded policy reason."""

        reason = delivery.suppressed_reason
        if reason not in _SAFE_SUPPRESSION_REASONS:
            reason = "policy" if not delivery.system_delivered else None
        record = DiagnosticNotificationRecord(
            delivery_id=delivery.delivery_id,
            policy=delivery.policy,
            inbox_enqueued=delivery.enqueue_inbox,
            system_delivered=delivery.system_delivered,
            suppressed_reason=reason,
            recorded_at=delivery.recorded_at,
        )
        self._notifications[delivery.delivery_id] = record
        self._trim(self._notifications, self.max_notifications)
        return record

    def report(self, *, generated_at: datetime | None = None) -> DiagnosticsReport:
        when = generated_at or datetime.now(UTC)
        if when.tzinfo is None or when.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        sources = tuple(self._sources[source_id] for source_id in sorted(self._sources))
        notifications = tuple(
            self._notifications[delivery_id] for delivery_id in sorted(self._notifications)
        )
        return DiagnosticsReport(
            generated_at=when,
            application_version=self.application_version,
            schema_version=self.schema_version,
            sources=sources,
            notifications=notifications,
            safe_summary=(
                f"{len(sources)} source(s); {len(notifications)} notification record(s)."
            ),
        )

    def to_json(self, *, generated_at: datetime | None = None) -> str:
        return self.report(generated_at=generated_at).to_json()

    @staticmethod
    def _trim(records: dict[str, Any], limit: int) -> None:
        while len(records) > limit:
            oldest = next(iter(records))
            records.pop(oldest, None)


def aggregate_diagnostics(
    source_results: Iterable[SourceResult] = (),
    notification_deliveries: Iterable[NotificationDelivery] = (),
    *,
    application_version: str = "unknown",
    schema_version: int = 1,
    generated_at: datetime | None = None,
) -> DiagnosticsReport:
    """Build one deterministic metadata-only report from typed operation envelopes."""

    aggregator = DiagnosticsAggregator(
        application_version=application_version,
        schema_version=schema_version,
    )
    for result in source_results:
        aggregator.record_source(result)
    for delivery in notification_deliveries:
        aggregator.record_notification(delivery)
    return aggregator.report(generated_at=generated_at)


def _validate_identifier(value: str, field_name: str) -> str:
    normalized = value.strip()
    if _IDENTIFIER_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"{field_name} must be a stable identifier")
    return normalized


def _validate_version(value: str) -> str:
    normalized = value.strip()
    if _VERSION_PATTERN.fullmatch(normalized) is None:
        return "unknown"
    return normalized


def _validate_timestamp(value: datetime | None) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("diagnostic timestamps must be timezone-aware")
    return value


def _response_size_bucket(size_bytes: int | None) -> str:
    if size_bytes is None:
        return "unknown"
    if size_bytes <= 64 * 1024:
        return "small"
    if size_bytes <= 1024 * 1024:
        return "medium"
    if size_bytes <= 5 * 1024 * 1024:
        return "large"
    return "oversize"


# Concise aliases for callers that use "report" or "metadata" terminology.
DiagnosticReport = DiagnosticsReport
DiagnosticService = DiagnosticsAggregator
build_diagnostics_report = aggregate_diagnostics

__all__ = [
    "DiagnosticNotificationRecord",
    "DiagnosticReport",
    "DiagnosticService",
    "DiagnosticSourceRecord",
    "DiagnosticsAggregator",
    "DiagnosticsReport",
    "aggregate_diagnostics",
    "build_diagnostics_report",
]
