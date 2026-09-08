from __future__ import annotations

import importlib
import inspect
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from personal_hub.config import default_local_assistant_data_dir


def _path_value(value: Any) -> Path | None:
    """Normalize common Hub/SQLAlchemy database path representations."""

    if value is None:
        return None
    if isinstance(value, Path):
        return value
    database = getattr(value, "database", None)
    if database:
        return Path(str(database))
    text = str(value)
    if text.startswith("sqlite:///"):
        return Path(text.removeprefix("sqlite:///"))
    if not text or text in {"None", ":memory:"}:
        return None
    return Path(text)


def _first_path(owner: Any, names: tuple[str, ...]) -> Path:
    for name in names:
        if not hasattr(owner, name):
            continue
        value = getattr(owner, name)
        if callable(value):
            continue
        path = _path_value(value)
        if path is not None:
            return path

    model_dump = getattr(owner, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        for name in names:
            path = _path_value(dumped.get(name))
            if path is not None:
                return path
    raise AssertionError(f"{type(owner).__name__} does not expose one of {names}")


def _data_path(settings: Any) -> Path:
    return _first_path(settings, ("data_dir", "data_path", "root_dir", "storage_dir"))


def _database_path(owner: Any) -> Path:
    direct = (
        "database_path",
        "db_path",
        "database_file",
        "sqlite_path",
        "path",
        "url",
    )
    try:
        return _first_path(owner, direct)
    except AssertionError:
        data = _data_path(owner)
        database_name = getattr(owner, "database_name", None)
        if database_name is None:
            model_dump = getattr(owner, "model_dump", None)
            if callable(model_dump):
                database_name = model_dump().get("database_name")
        assert database_name, f"{type(owner).__name__} does not expose a database name"
        return data / str(database_name)


def _same_path(left: Path, right: Path) -> bool:
    return (
        left.resolve(strict=False).as_posix().casefold()
        == right.resolve(strict=False).as_posix().casefold()
    )


def _close(value: Any) -> None:
    for method_name in ("close", "release", "shutdown"):
        method = getattr(value, method_name, None)
        if callable(method):
            method()
            return


def _hub_settings_type():
    module = importlib.import_module("personal_hub.config")
    settings_type = getattr(module, "HubSettings", None)
    assert settings_type is not None, "Phase 0 must expose personal_hub.config.HubSettings"
    return settings_type


def test_default_and_overridden_hub_paths_are_distinct_from_local_assistant(tmp_path: Path):
    hub_settings_type = _hub_settings_type()
    hub_defaults = hub_settings_type()

    assistant_data = default_local_assistant_data_dir()
    hub_data = _data_path(hub_defaults)
    assistant_database = assistant_data / "assistant.db"
    hub_database = _database_path(hub_defaults)

    assert not _same_path(hub_data, assistant_data)
    assert not _same_path(hub_database, assistant_database)
    assert hub_database.name.casefold() != "assistant.db"
    assert _same_path(hub_database.parent, hub_data)

    hub_override_dir = tmp_path / "hub-data"
    assistant_override_dir = tmp_path / "assistant-data"
    hub_override = hub_settings_type(
        data_dir=hub_override_dir,
        database_name="phase0-hub.sqlite",
    )
    assistant_override_database = assistant_override_dir / "phase0-assistant.sqlite"

    assert not _same_path(_data_path(hub_override), assistant_override_dir)
    assert not _same_path(_database_path(hub_override), assistant_override_database)
    assert _same_path(_database_path(hub_override).parent, hub_override_dir)


def test_hub_rejects_or_rewrites_explicit_local_assistant_path_collisions(tmp_path: Path):
    """A user override must never make the Hub point at Assistant storage."""

    hub_settings_type = _hub_settings_type()
    assistant_data = default_local_assistant_data_dir()
    assistant_database = assistant_data / "assistant.db"

    for override in (
        {"data_dir": assistant_data, "database_name": "personal_hub.db"},
        {"data_dir": tmp_path / "hub-data", "database_name": "assistant.db"},
    ):
        try:
            hub_settings = hub_settings_type(**override)
        except (TypeError, ValueError):
            # Rejecting an explicit collision is the safest valid behavior.
            continue
        assert not _same_path(_data_path(hub_settings), assistant_data)
        assert not _same_path(_database_path(hub_settings), assistant_database)
        assert _database_path(hub_settings).name.casefold() != "assistant.db"


def test_hub_container_starts_with_an_independent_empty_sqlite_database(tmp_path: Path):
    hub_settings_type = _hub_settings_type()
    bootstrap = importlib.import_module("personal_hub.bootstrap")
    build_hub_container = getattr(bootstrap, "build_hub_container", None)
    assert callable(build_hub_container), "Phase 0 must expose build_hub_container"

    assistant_database = tmp_path / "assistant" / "assistant.db"
    assistant_database.parent.mkdir(parents=True)
    with sqlite3.connect(assistant_database) as connection:
        connection.execute("CREATE TABLE assistant_only_marker (value TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO assistant_only_marker(value) VALUES (?)",
            ("must-not-migrate",),
        )

    hub_data = tmp_path / "hub"
    container = build_hub_container(
        hub_settings_type(data_dir=hub_data, database_name="personal_hub.db")
    )
    try:
        settings = getattr(container, "settings", None)
        assert settings is not None
        assert _same_path(_data_path(settings), hub_data)

        database = getattr(container, "database", None)
        assert database is not None
        database_path = _database_path(database)
        assert database_path.exists()
        assert _same_path(database_path.parent, hub_data)
        assert database_path.name.casefold() == "personal_hub.db"

        with sqlite3.connect(database_path) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            table_names = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            assert "assistant_only_marker" not in table_names
    finally:
        _close(container)


def _identity_values(settings: Any) -> dict[str, str]:
    aliases = {
        "application_name": (
            "application_name",
            "app_name",
            "product_name",
            "window_title",
        ),
        "application_id": (
            "application_id",
            "app_id",
            "app_user_model_id",
            "windows_app_id",
        ),
        "process_name": (
            "process_name",
            "executable_name",
            "executable",
        ),
        "mutex_name": (
            "mutex_name",
            "instance_name",
            "single_instance_name",
            "lock_name",
        ),
    }
    values: dict[str, str] = {}
    dumped = settings.model_dump() if callable(getattr(settings, "model_dump", None)) else {}
    for key, names in aliases.items():
        for name in names:
            value = getattr(settings, name, dumped.get(name, None))
            if value is not None and str(value).strip():
                values[key] = str(value)
                break
    return values


def _hub_lock_factory():
    candidates = (
        ("personal_hub.lifecycle", "acquire_single_instance"),
        ("personal_hub.lifecycle", "acquire_instance_lock"),
        ("personal_hub.lifecycle", "acquire_hub_lock"),
        ("personal_hub.bootstrap", "acquire_single_instance"),
        ("personal_hub.app", "acquire_single_instance"),
    )
    for module_name, attribute_name in candidates:
        module = importlib.import_module(module_name)
        factory = getattr(module, attribute_name, None)
        if callable(factory):
            return factory
    raise AssertionError(
        "Phase 0 must expose a callable Hub single-instance lock factory "
        "from personal_hub.lifecycle"
    )


def _acquire_with_unique_name(
    factory: Any,
    name: str,
    *,
    settings: Any | None = None,
    lock_path: Path | None = None,
):
    try:
        parameters = inspect.signature(factory).parameters
    except (TypeError, ValueError):
        parameters = {}
    kwargs: dict[str, Any] = {}
    if settings is not None and "settings" in parameters:
        kwargs["settings"] = settings
    if lock_path is not None and "lock_path" in parameters:
        kwargs["lock_path"] = lock_path
    for parameter_name in ("name", "mutex_name", "lock_name", "instance_name"):
        if parameter_name in parameters:
            kwargs[parameter_name] = name
            return factory(**kwargs)
    if not parameters:
        return factory()
    raise AssertionError(
        f"Cannot supply a unique test mutex name to {factory!r}; parameters are {tuple(parameters)}"
    )


def test_personal_hub_and_local_assistant_have_distinct_identities_and_locks(tmp_path: Path):
    hub_settings_type = _hub_settings_type()
    hub_settings = hub_settings_type(data_dir=tmp_path / "hub")
    hub_identity = _identity_values(hub_settings)
    assert {"application_name", "application_id", "process_name", "mutex_name"}.issubset(
        hub_identity
    )
    assert hub_identity["application_name"].casefold() != "local assistant"
    assert hub_identity["application_id"].casefold() not in {
        "localassistant",
        "localassistant.desktop",
    }
    assert hub_identity["process_name"].casefold() != "localassistant.exe"
    assert "assistant" not in hub_identity["mutex_name"].casefold()

    assert not _same_path(_data_path(hub_settings), default_local_assistant_data_dir())

    suffix = uuid.uuid4().hex
    hub_factory = _hub_lock_factory()
    assistant_lock = _acquire_with_unique_name(
        hub_factory,
        f"LocalAssistant.Phase0.{suffix}",
        settings=hub_settings,
        lock_path=tmp_path / "assistant" / "local_assistant.lock",
    )
    hub_lock = _acquire_with_unique_name(
        hub_factory,
        f"PersonalHub.Phase0.{suffix}",
        settings=hub_settings,
        lock_path=tmp_path / "hub" / "personal_hub.lock",
    )
    try:
        assert assistant_lock is not None
        assert hub_lock is not None
        assert assistant_lock is not hub_lock
    finally:
        _close(hub_lock)
        _close(assistant_lock)
