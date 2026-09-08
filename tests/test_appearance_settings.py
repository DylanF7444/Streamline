"""Appearance settings remain validated and isolated across database reopen."""

import pytest
from pydantic import ValidationError

from personal_hub.config import HubSettings
from personal_hub.storage.database import HubDatabase
from personal_hub.storage.repositories import SettingsRepository


@pytest.mark.parametrize("value", ["red", "#fff", "url(secret)", "#123456; color: red", "#gggggg"])
def test_custom_colors_reject_stylesheet_injection(value, tmp_path):
    with pytest.raises(ValidationError):
        HubSettings(data_dir=tmp_path, ui_accent_color=value)


def test_colors_and_motion_round_trip_without_affecting_other_settings(tmp_path):
    defaults = HubSettings(data_dir=tmp_path, offline_mode=True)
    database = HubDatabase(defaults.database_path)
    database.migrate()
    repository = SettingsRepository(database)
    repository.save_settings(
        HubSettings(
            **{
                **defaults.model_dump(),
                "ui_theme": "sage",
                "ui_accent_color": "#AABBCC",
                "ui_background_color": "#101010",
                "ui_surface_color": "#232323",
                "ui_reduced_motion": True,
            }
        )
    )
    database.close()
    reopened = HubDatabase(defaults.database_path)
    try:
        settings = SettingsRepository(reopened).load_settings(defaults)
        assert settings.ui_theme == "sage"
        assert settings.ui_accent_color == "#aabbcc"
        assert settings.ui_background_color == "#101010"
        assert settings.ui_surface_color == "#232323"
        assert settings.ui_reduced_motion
        assert settings.offline_mode
        assert settings.data_dir == defaults.data_dir
    finally:
        reopened.close()
