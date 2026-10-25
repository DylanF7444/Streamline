"""Encrypted, shared-folder synchronization for Personal Hub."""

from personal_hub.sync.contracts import (
    SyncCollection,
    SyncRecord,
    SyncRelayCategory,
    SyncResult,
    SyncRole,
    SyncSnapshot,
    SyncStatus,
    SyncTombstone,
)
from personal_hub.sync.crypto import (
    PROVIDER_RELAY_BUNDLE_FORMAT,
    SyncBundleError,
    SyncPassphraseError,
    decrypt_provider_relay,
    decrypt_snapshot,
    encrypt_provider_relay,
    encrypt_snapshot,
)
from personal_hub.sync.relay_contracts import (
    PROVIDER_RELAY_FORMAT,
    RELAY_CATEGORIES,
    ProviderRelayCalendarSnapshot,
    ProviderRelayFeedSnapshot,
    ProviderRelayMessageSnapshot,
    ProviderRelaySnapshot,
    ProviderRelayWatchSnapshot,
    RelayCategory,
)
from personal_hub.sync.repository import ProviderRelayRepository
from personal_hub.sync.service import (
    FileSyncEngine,
    SyncConfigurationError,
    SyncError,
    SyncService,
    merge_snapshots,
)

__all__ = [
    "FileSyncEngine",
    "PROVIDER_RELAY_BUNDLE_FORMAT",
    "PROVIDER_RELAY_FORMAT",
    "RELAY_CATEGORIES",
    "SyncBundleError",
    "SyncCollection",
    "SyncConfigurationError",
    "SyncError",
    "SyncPassphraseError",
    "SyncRecord",
    "SyncResult",
    "SyncRole",
    "SyncRelayCategory",
    "SyncService",
    "SyncSnapshot",
    "SyncStatus",
    "SyncTombstone",
    "ProviderRelayCalendarSnapshot",
    "ProviderRelayFeedSnapshot",
    "ProviderRelayMessageSnapshot",
    "ProviderRelayRepository",
    "ProviderRelaySnapshot",
    "ProviderRelayWatchSnapshot",
    "RelayCategory",
    "decrypt_snapshot",
    "decrypt_provider_relay",
    "encrypt_snapshot",
    "encrypt_provider_relay",
    "merge_snapshots",
]
