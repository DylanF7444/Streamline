"""Convenience functions for Hub database backup and restore."""

from pathlib import Path

from personal_hub.storage.database import HubDatabase


def backup_database(database: HubDatabase, destination: Path | str) -> Path:
    return database.backup(destination)


def restore_database(
    database: HubDatabase,
    source: Path | str,
    *,
    destination: Path | str | None = None,
) -> Path:
    return database.restore(source, destination=destination)


__all__ = ["backup_database", "restore_database"]
