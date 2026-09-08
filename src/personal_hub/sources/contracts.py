"""Strict, Hub-owned contracts for local and remote attention sources.

The source boundary intentionally does not expose Assistant skills or result models.  A
provider adapter implements :class:`SourceProtocol` and returns only the bounded,
validated values represented here.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


class SourceState(StrEnum):
    """User-visible source health/freshness states."""

    READY = "ready"
    EMPTY = "empty"
    STALE = "stale"
    OFFLINE = "offline"
    PERMISSION_REQUIRED = "permission_required"
    DISCONNECTED = "disconnected"
    ERROR = "error"


_STATE_MESSAGES: dict[SourceState, str] = {
    SourceState.READY: "Source is ready.",
    SourceState.EMPTY: "Source is ready but has no items.",
    SourceState.STALE: "Showing the last successful source snapshot.",
    SourceState.OFFLINE: "Source refresh is paused by offline mode.",
    SourceState.PERMISSION_REQUIRED: "Permission is required to access this source.",
    SourceState.DISCONNECTED: "Source is disconnected.",
    SourceState.ERROR: "Source refresh failed safely.",
}


def safe_state_message(state: SourceState) -> str:
    """Return a fixed, non-content-bearing message for a source state."""

    return _STATE_MESSAGES[state]


def _validate_identifier(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not _IDENTIFIER_PATTERN.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a stable identifier")
    return normalized


def _validate_timestamp(value: datetime | None, field_name: str) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


class SourceDefinition(BaseModel):
    """Stable, non-secret identity and display metadata for one source."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    source_id: str = Field(min_length=1, max_length=128)
    provider_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=200)
    enabled: bool = True

    _source_id = field_validator("source_id")(
        lambda value: _validate_identifier(value, "source_id")
    )
    _provider_id = field_validator("provider_id")(
        lambda value: _validate_identifier(value, "provider_id")
    )


class SourceStatus(BaseModel):
    """Bounded status returned by a provider adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    source_id: str = Field(min_length=1, max_length=128)
    provider_id: str = Field(min_length=1, max_length=128)
    state: SourceState
    checked_at: datetime | None = None
    item_count: int = Field(default=0, ge=0, le=100_000)
    safe_message: str | None = Field(default=None, max_length=160)

    _source_id = field_validator("source_id")(
        lambda value: _validate_identifier(value, "source_id")
    )
    _provider_id = field_validator("provider_id")(
        lambda value: _validate_identifier(value, "provider_id")
    )
    _checked_at = field_validator("checked_at")(
        lambda value: _validate_timestamp(value, "checked_at")
    )


class SourceSnapshot(BaseModel):
    """A bounded last-good snapshot retained in memory by the source service."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    source_id: str = Field(min_length=1, max_length=128)
    provider_id: str = Field(min_length=1, max_length=128)
    collected_at: datetime
    items: tuple[Any, ...] = Field(default_factory=tuple, max_length=5_000)
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    _source_id = field_validator("source_id")(
        lambda value: _validate_identifier(value, "source_id")
    )
    _provider_id = field_validator("provider_id")(
        lambda value: _validate_identifier(value, "provider_id")
    )
    _collected_at = field_validator("collected_at")(
        lambda value: _validate_timestamp(value, "collected_at")
    )


class SourceResult(BaseModel):
    """Safe result envelope returned by Hub source operations."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    source_id: str = Field(min_length=1, max_length=128)
    provider_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=200)
    state: SourceState
    checked_at: datetime
    snapshot: SourceSnapshot | None = None
    last_success_at: datetime | None = None
    item_count: int = Field(default=0, ge=0, le=100_000)
    duration_ms: float = Field(default=0.0, ge=0, le=86_400_000)
    safe_message: str = Field(max_length=160)

    _source_id = field_validator("source_id")(
        lambda value: _validate_identifier(value, "source_id")
    )
    _provider_id = field_validator("provider_id")(
        lambda value: _validate_identifier(value, "provider_id")
    )
    _checked_at = field_validator("checked_at")(
        lambda value: _validate_timestamp(value, "checked_at")
    )
    _last_success_at = field_validator("last_success_at")(
        lambda value: _validate_timestamp(value, "last_success_at")
    )


class SourceError(RuntimeError):
    """Base class for adapter failures with a known safe classification."""


class SourcePermissionRequiredError(SourceError):
    """The user must grant or renew source permission."""


class SourceDisconnectedError(SourceError):
    """The source account or connection is not available."""


class SourceUnavailableError(SourceError):
    """The source could not be reached or is temporarily unavailable."""


@runtime_checkable
class SourceProtocol(Protocol):
    """Async provider boundary owned by Personal Hub.

    ``connect`` is optional at runtime for local sources, but adapters that expose it
    must keep it asynchronous.  SourceService checks for each method before invoking it.
    """

    async def connect(self) -> None | SourceStatus: ...

    async def status(self) -> SourceStatus: ...

    async def refresh(self) -> SourceSnapshot | SourceStatus: ...

    async def disconnect(self) -> None: ...


# Friendly aliases used by provider-facing code and tests.
HubSource = SourceProtocol
SourceHealth = SourceStatus
SourceSnapshotResult = SourceSnapshot


__all__ = [
    "HubSource",
    "SourceDefinition",
    "SourceDisconnectedError",
    "SourceError",
    "SourceHealth",
    "SourcePermissionRequiredError",
    "SourceProtocol",
    "SourceResult",
    "SourceSnapshot",
    "SourceSnapshotResult",
    "SourceState",
    "SourceStatus",
    "SourceUnavailableError",
    "safe_state_message",
]
