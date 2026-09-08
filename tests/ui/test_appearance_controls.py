"""Appearance editing validates drafts and emits only explicit applied preferences."""

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from personal_hub.ui.desktop.appearance import AppearancePanel, theme_stylesheet


def test_custom_colors_motion_and_reset_require_apply(qtbot):
    panel = AppearancePanel()
    qtbot.addWidget(panel)
    applied = []
    panel.changed.connect(applied.append)
    panel.accent_input.setText("#abcdef")
    panel.animations_checkbox.setChecked(False)
    assert not applied
    panel.apply_button.click()
    assert applied[-1]["ui_accent_color"] == "#abcdef"
    assert applied[-1]["ui_reduced_motion"] is True
    panel.reset_button.click()
    assert len(applied) == 1
    panel.apply_button.click()
    assert applied[-1]["ui_accent_color"] == ""


def test_partial_hex_draft_cannot_be_applied(qtbot):
    panel = AppearancePanel()
    qtbot.addWidget(panel)
    applied = []
    panel.changed.connect(applied.append)
    panel.accent_input.setText("#abc")
    panel.apply_preferences()
    assert applied == []


def test_light_theme_recolors_legacy_id_specific_inputs():
    rendered = theme_stylesheet(
        "#newsSubscriptionTitleInput {color: #e7eef7; background: #101f32;}",
        theme="light",
        accent_color="",
        background_color="",
        surface_color="",
    )
    first_rule = rendered.split("}", 1)[0]
    assert "#e7eef7" not in first_rule
    assert "#101f32" not in first_rule
    assert "#ffffff" in first_rule
