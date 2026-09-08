"""Hub-owned source contracts, registry, and offline-aware service."""

from personal_hub.sources.contracts import (
    HubSource,
    SourceDefinition,
    SourceDisconnectedError,
    SourceError,
    SourceHealth,
    SourcePermissionRequiredError,
    SourceProtocol,
    SourceResult,
    SourceSnapshot,
    SourceSnapshotResult,
    SourceState,
    SourceStatus,
    SourceUnavailableError,
    safe_state_message,
)
from personal_hub.sources.registry import HubSourceRegistry, RegisteredSource, SourceRegistry
from personal_hub.sources.service import (
    HubSourceService,
    SourceManager,
    SourceService,
    normalize_source_error,
)

__all__ = [
    "HubSource",
    "HubSourceRegistry",
    "HubSourceService",
    "RegisteredSource",
    "SourceDefinition",
    "SourceDisconnectedError",
    "SourceError",
    "SourceHealth",
    "SourceManager",
    "SourcePermissionRequiredError",
    "SourceProtocol",
    "SourceRegistry",
    "SourceResult",
    "SourceService",
    "SourceSnapshot",
    "SourceSnapshotResult",
    "SourceState",
    "SourceStatus",
    "SourceUnavailableError",
    "normalize_source_error",
    "safe_state_message",
]
