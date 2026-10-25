"""Local cache for validated encrypted provider-relay snapshots."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, select

from personal_hub.storage.database import HubDatabase, HubProviderRelayCacheRow
from personal_hub.sync.relay_contracts import ProviderRelaySnapshot


class ProviderRelayRepository:
    """Persist only a schema-validated snapshot received from the collector shard."""

    _CACHE_ID = "active"

    def __init__(self, database: HubDatabase) -> None:
        self.database = database

    def get(self) -> ProviderRelaySnapshot | None:
        with self.database.session() as session:
            row = session.get(HubProviderRelayCacheRow, self._CACHE_ID)
            if row is None:
                return None
            payload = row.payload_json
            profile_id = row.profile_id
            collector_device_id = row.collector_device_id
        try:
            snapshot = ProviderRelaySnapshot.model_validate_json(payload)
        except (TypeError, ValueError):
            self.clear()
            return None
        if snapshot.profile_id != profile_id or (
            snapshot.collector_device_id != collector_device_id
        ):
            self.clear()
            return None
        return snapshot

    def save(self, snapshot: ProviderRelaySnapshot) -> ProviderRelaySnapshot:
        if not isinstance(snapshot, ProviderRelaySnapshot):
            raise TypeError("provider relay cache expects a ProviderRelaySnapshot")
        encoded = snapshot.model_dump_json()
        with self.database.session() as session:
            row = session.get(HubProviderRelayCacheRow, self._CACHE_ID)
            if row is None:
                row = HubProviderRelayCacheRow(cache_id=self._CACHE_ID)
                session.add(row)
            row.profile_id = snapshot.profile_id
            row.collector_device_id = snapshot.collector_device_id
            row.generated_at = snapshot.generated_at.astimezone(UTC).replace(tzinfo=None)
            row.expires_at = snapshot.expires_at.astimezone(UTC).replace(tzinfo=None)
            row.payload_json = encoded
            row.updated_at = datetime.now(UTC).replace(tzinfo=None)
        return snapshot

    def clear(self) -> bool:
        with self.database.session() as session:
            result = session.execute(
                delete(HubProviderRelayCacheRow).where(
                    HubProviderRelayCacheRow.cache_id == self._CACHE_ID
                )
            )
            return bool(result.rowcount)

    def expire(self, *, now: datetime | None = None) -> bool:
        timestamp = now or datetime.now(UTC)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("provider relay cache clock must be timezone-aware")
        with self.database.session() as session:
            row = session.scalar(
                select(HubProviderRelayCacheRow).where(
                    HubProviderRelayCacheRow.cache_id == self._CACHE_ID
                )
            )
            if row is None or row.expires_at.replace(tzinfo=UTC) > timestamp.astimezone(UTC):
                return False
            session.delete(row)
            return True


__all__ = ["ProviderRelayRepository"]
