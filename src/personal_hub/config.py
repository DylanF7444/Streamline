"""Configuration and platform paths owned by Personal Hub.

This module deliberately has no dependency on :mod:`assistant`.  Personal Hub is a
separate application target, so its storage roots and application identities are
defined here instead of being inherited from the Local Assistant settings model.

The settings model only describes configuration.  In particular, validating a
``HubSettings`` instance never creates a directory, database, lock file, or any
other machine state.  Code which starts the application is responsible for
creating the required directories after it has acquired the instance lock.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import ClassVar, Final, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from platformdirs import user_data_path
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

HUB_PRODUCT_NAME: Final[str] = "Personal Hub"
HUB_APPLICATION_ID: Final[str] = "PersonalHub.Desktop"
HUB_PROCESS_NAME: Final[str] = "PersonalHub.exe"
HUB_MUTEX_NAME: Final[str] = "PersonalHub.Desktop"
HUB_STARTUP_ENTRY_NAME: Final[str] = "PersonalHub"
HUB_UPDATE_CHANNEL: Final[str] = "personal-hub-stable"
HUB_SECRET_NAMESPACE: Final[str] = "PersonalHub"

_DATABASE_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_IDENTITY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def default_personal_hub_data_dir() -> Path:
    """Return the platform default Hub data directory without creating it.

    ``platformdirs`` maps this to ``%LOCALAPPDATA%\\PersonalHub`` on Windows and
    to the normal per-user data directory on other platforms.  Passing
    ``appauthor=False`` intentionally avoids the platformdirs default of adding
    a second ``PersonalHub`` component.  Keeping the
    platform lookup in a function means tests can safely override the platform
    environment before constructing settings.
    """

    return Path(user_data_path("PersonalHub", appauthor=False))


def default_hub_data_dir() -> Path:
    """Backward-compatible short name for :func:`default_personal_hub_data_dir`."""

    return default_personal_hub_data_dir()


def default_local_assistant_data_dir() -> Path:
    """Return the Local Assistant data root for isolation checks only.

    This is intentionally a direct platform-path calculation rather than an
    import from ``assistant.config``.  Importing the assistant package from Hub
    startup would violate the application boundary this module is meant to
    enforce.
    """

    return Path(user_data_path("LocalAssistant", "LocalAssistant"))


def _resolved_path(value: Path | str) -> Path:
    """Resolve a path lexically and through existing symlinks, without requiring it."""

    candidate = Path(value).expanduser()
    try:
        # ``strict=False`` is important: settings validation must not create or
        # require the data root to exist yet.
        return candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        # A malformed symlink chain should not make the error path itself
        # surprising.  ``absolute`` remains side-effect free and is sufficient
        # for the normal isolation comparison.
        return Path(os.path.abspath(os.fspath(candidate)))


def _normalised_for_comparison(value: Path | str) -> str:
    """Return a case-normalized absolute path suitable for ancestry checks."""

    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(value))))


def _paths_overlap(first: Path, second: Path) -> bool:
    """Return whether either path is the same as, or contains, the other."""

    first_value = _normalised_for_comparison(first)
    second_value = _normalised_for_comparison(second)
    try:
        common = os.path.normcase(os.path.normpath(os.path.commonpath([first_value, second_value])))
    except ValueError:
        # Different Windows drives (or otherwise incomparable path flavours)
        # cannot overlap.
        return False
    return common in (first_value, second_value)


def _validate_identity(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized or not _IDENTITY_PATTERN.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a simple application identity")
    return normalized


class HubSettings(BaseModel):
    """Strict, Hub-only application settings.

    The application identity values are class constants rather than user
    settings.  This prevents a configuration file from accidentally changing
    the process/mutex/credential namespace that establishes coexistence with
    Local Assistant.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        validate_default=True,
        str_strip_whitespace=True,
    )

    profile: Literal["development", "testing", "production"] = "development"
    data_dir: Path = Field(default_factory=default_personal_hub_data_dir)
    database_name: str = "personal_hub.db"

    # These are intentionally small local-first controls.  They do not include
    # assistant, model, voice, planner, router, or transcript settings.
    default_timezone: str = "UTC"
    offline_mode: bool = False
    start_in_tray: bool = False
    close_to_tray: bool = True
    start_at_login: bool = False
    privacy_masking: bool = False
    pause_network_while_locked: bool = True
    notifications_enabled: bool = True
    notification_quiet_hours_enabled: bool = False
    notification_quiet_hours_start: str = "22:00"
    notification_quiet_hours_end: str = "07:00"
    inbox_retention_days: int = Field(default=90, ge=1, le=365)
    audit_retention_days: int = Field(default=30, ge=1, le=365)
    feed_retention_days: int = Field(default=30, ge=1, le=365)
    feed_max_entries_per_subscription: int = Field(default=500, ge=10, le=10_000)
    feed_refresh_minutes: int = Field(default=30, ge=15, le=1_440)
    calendar_refresh_minutes: int = Field(default=15, ge=5, le=1_440)
    canvas_remove_completed_assignments: bool = False
    allow_insecure_feed_http: bool = False
    watch_observation_retention_days: int = Field(default=90, ge=1, le=365)
    watch_max_observations_per_watch: int = Field(default=500, ge=10, le=10_000)
    watch_refresh_minutes: int = Field(default=60, ge=30, le=1_440)
    allow_insecure_watch_http: bool = False
    message_summary_retention_days: int = Field(default=7, ge=0, le=30)
    message_max_summaries_per_source: int = Field(default=500, ge=10, le=10_000)
    message_refresh_minutes: int = Field(default=10, ge=5, le=1_440)
    digest_enabled: bool = True
    digest_time: str = "08:00"
    ui_text_scale: float = Field(default=1.0, ge=1.0, le=1.75)
    ui_high_contrast: bool = False
    ui_reduced_motion: bool = False
    ui_theme: Literal["navy", "light", "sage"] = "navy"
    ui_accent_color: str = ""
    ui_background_color: str = ""
    ui_surface_color: str = ""

    @field_validator("ui_accent_color", "ui_background_color", "ui_surface_color")
    @classmethod
    def _ui_color_is_hex(cls, value: str) -> str:
        if value and not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            raise ValueError("Hub colors must be empty or use #RRGGBB notation")
        return value.lower()

    # Public constants are available from an instance (``settings.mutex_name``)
    # while remaining impossible to override through configuration input.
    product_name: ClassVar[str] = HUB_PRODUCT_NAME
    application_name: ClassVar[str] = HUB_PRODUCT_NAME
    application_id: ClassVar[str] = HUB_APPLICATION_ID
    app_id: ClassVar[str] = HUB_APPLICATION_ID
    app_user_model_id: ClassVar[str] = HUB_APPLICATION_ID
    process_name: ClassVar[str] = HUB_PROCESS_NAME
    executable_name: ClassVar[str] = HUB_PROCESS_NAME
    mutex_name: ClassVar[str] = HUB_MUTEX_NAME
    startup_entry_name: ClassVar[str] = HUB_STARTUP_ENTRY_NAME
    startup_registry_key: ClassVar[str] = HUB_STARTUP_ENTRY_NAME
    update_channel: ClassVar[str] = HUB_UPDATE_CHANNEL
    update_channel_id: ClassVar[str] = HUB_UPDATE_CHANNEL
    secret_namespace: ClassVar[str] = HUB_SECRET_NAMESPACE
    credential_namespace: ClassVar[str] = HUB_SECRET_NAMESPACE

    @field_validator("data_dir", mode="before")
    @classmethod
    def _data_dir_is_hub_owned(cls, value: object) -> Path:
        if value is None:
            raise ValueError("data_dir is required")
        try:
            # Keep the caller's absolute spelling (rather than silently
            # replacing it with a symlink- or 8.3-resolved spelling), while
            # using a resolved copy below for the isolation check.
            candidate = Path(os.path.abspath(os.fspath(Path(value).expanduser())))
        except (TypeError, ValueError, OSError) as error:
            raise ValueError("data_dir must be a valid filesystem path") from error

        assistant_dir = _resolved_path(default_local_assistant_data_dir())
        if _paths_overlap(_resolved_path(candidate), assistant_dir):
            raise ValueError(
                "Personal Hub data_dir cannot overlap the Local Assistant data directory"
            )
        return candidate

    @field_validator("database_name")
    @classmethod
    def _database_name_is_safe(cls, value: str) -> str:
        normalized = value.strip()
        if not _DATABASE_NAME_PATTERN.fullmatch(normalized):
            raise ValueError("database_name must be a simple filename without path separators")
        if normalized.casefold() == "assistant.db":
            raise ValueError("Personal Hub cannot use the Local Assistant database name")
        return normalized

    @field_validator("default_timezone")
    @classmethod
    def _timezone_exists(cls, value: str) -> str:
        normalized = value.strip()
        try:
            ZoneInfo(normalized)
        except ZoneInfoNotFoundError as error:
            raise ValueError(f"Unknown IANA time zone: {normalized}") from error
        return normalized

    @field_validator(
        "notification_quiet_hours_start",
        "notification_quiet_hours_end",
        "digest_time",
    )
    @classmethod
    def _quiet_hours_use_24_hour_time(cls, value: str) -> str:
        normalized = value.strip()
        try:
            parsed = datetime.strptime(normalized, "%H:%M")
        except ValueError as error:
            raise ValueError("quiet-hours time must use 24-hour HH:MM format") from error
        return parsed.strftime("%H:%M")

    @model_validator(mode="after")
    def _database_remains_isolated(self) -> HubSettings:
        assistant_database = _resolved_path(default_local_assistant_data_dir()) / "assistant.db"
        if _paths_overlap(_resolved_path(self.database_path), assistant_database):
            raise ValueError("Personal Hub database cannot overlap the Local Assistant database")
        return self

    @property
    def database_path(self) -> Path:
        """Absolute Hub database path; this property does not create the file."""

        return self.data_dir / self.database_name

    @property
    def database_url(self) -> str:
        """SQLAlchemy URL for the Hub-owned SQLite database."""

        return f"sqlite:///{self.database_path.as_posix()}"

    @property
    def secrets_dir(self) -> Path:
        return self.data_dir / "secrets"

    @property
    def secret_dir(self) -> Path:
        """Singular alias used by a few composition-root integrations."""

        return self.secrets_dir

    @property
    def secret_vault_dir(self) -> Path:
        return self.secrets_dir

    @property
    def vault_dir(self) -> Path:
        return self.secrets_dir

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def log_dir(self) -> Path:
        return self.logs_dir

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def backup_dir(self) -> Path:
        return self.backups_dir

    @property
    def settings_path(self) -> Path:
        return self.data_dir / "settings.json"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "personal_hub.lock"

    @property
    def lock_file_path(self) -> Path:
        return self.lock_path

    @property
    def data_path(self) -> Path:
        return self.data_dir

    @property
    def db_path(self) -> Path:
        return self.database_path

    @property
    def is_default_data_dir(self) -> bool:
        return _normalised_for_comparison(self.data_dir) == _normalised_for_comparison(
            default_personal_hub_data_dir()
        )


# A concise alias is useful to callers that refer to the model as configuration,
# while HubSettings remains the canonical public name.
Settings = HubSettings

__all__ = [
    "HUB_APPLICATION_ID",
    "HUB_MUTEX_NAME",
    "HUB_PROCESS_NAME",
    "HUB_PRODUCT_NAME",
    "HUB_SECRET_NAMESPACE",
    "HUB_STARTUP_ENTRY_NAME",
    "HUB_UPDATE_CHANNEL",
    "HubSettings",
    "Settings",
    "default_hub_data_dir",
    "default_local_assistant_data_dir",
    "default_personal_hub_data_dir",
]
