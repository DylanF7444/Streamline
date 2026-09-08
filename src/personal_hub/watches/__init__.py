"""Personal Hub webpage watch domain."""

from personal_hub.watches.changedetection import ChangedetectionReadAdapter
from personal_hub.watches.contracts import (
    AlertEvent,
    PageResponse,
    WatchCondition,
    WatchDefinition,
    WatchError,
    WatchObservation,
    WatchOfflineError,
    WatchParseError,
    WatchPermissionRequiredError,
    WatchPolicyError,
    WatchResult,
    WatchSyncState,
    WatchUnavailableError,
)
from personal_hub.watches.extractor import extract_observation
from personal_hub.watches.fetcher import PageFetcher
from personal_hub.watches.repository import WatchRepository
from personal_hub.watches.service import WatchService

__all__ = [
    "AlertEvent",
    "ChangedetectionReadAdapter",
    "PageFetcher",
    "PageResponse",
    "WatchCondition",
    "WatchDefinition",
    "WatchError",
    "WatchObservation",
    "WatchOfflineError",
    "WatchParseError",
    "WatchPermissionRequiredError",
    "WatchPolicyError",
    "WatchRepository",
    "WatchResult",
    "WatchService",
    "WatchSyncState",
    "WatchUnavailableError",
    "extract_observation",
]
