"""Hub settings persistence facade."""

from personal_hub.storage.repositories import SettingsRepository

SettingsStore = SettingsRepository

__all__ = ["SettingsRepository", "SettingsStore"]
