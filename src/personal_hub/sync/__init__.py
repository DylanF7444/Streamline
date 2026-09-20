"""Encrypted, shared-folder synchronization for Personal Hub."""

from personal_hub.sync.contracts import (
    SyncCollection,
    SyncRecord,
    SyncResult,
    SyncSnapshot,
    SyncStatus,
    SyncTombstone,
)
from personal_hub.sync.crypto import (
    SyncBundleError,
    SyncPassphraseError,
    decrypt_snapshot,
    encrypt_snapshot,
)
from personal_hub.sync.service import (
    FileSyncEngine,
    SyncConfigurationError,
    SyncError,
    SyncService,
    merge_snapshots,
)

__all__ = [
    "FileSyncEngine",
    "SyncBundleError",
    "SyncCollection",
    "SyncConfigurationError",
    "SyncError",
    "SyncPassphraseError",
    "SyncRecord",
    "SyncResult",
    "SyncService",
    "SyncSnapshot",
    "SyncStatus",
    "SyncTombstone",
    "decrypt_snapshot",
    "encrypt_snapshot",
    "merge_snapshots",
]
