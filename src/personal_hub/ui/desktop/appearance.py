"""Appearance controls and theme styling for the Personal Hub desktop UI.

The appearance surface intentionally owns no application settings persistence.  It
edits a small, serialisable preference mapping and emits that mapping only when the
user chooses :guilabel:`Apply appearance`.  The composition root can persist the
mapping and apply :func:`theme_stylesheet` to the application afterwards.

Color overrides are optional.  An empty ``ui_*_color`` value means that the selected
``ui_theme`` supplies that color.  This mirrors :class:`personal_hub.config.HubSettings`
and keeps a preset change reversible without having to rewrite every color field.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QColor, QValidator
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

_HEX_COLOR_RE: Final[re.Pattern[str]] = re.compile(r"#[0-9a-fA-F]{6}\Z")
_THEME_NAMES: Final[tuple[str, ...]] = ("navy", "light", "sage")
_COLOR_PREFERENCE_KEYS: Final[tuple[str, ...]] = (
    "ui_accent_color",
    "ui_background_color",
    "ui_surface_color",
)


# Keep these values close to the palette used by the existing Hub shell.  The
# lighter presets are deliberately slightly tinted rather than pure gray so the
# hierarchy remains clear on a Windows desktop without looking overly stark.
THEME_PRESETS: Final[dict[str, dict[str, str]]] = {
    "navy": {
        "accent_color": "#70b5ff",
        "background_color": "#0b1421",
        "surface_color": "#101f32",
    },
    "light": {
        "accent_color": "#286a8a",
        "background_color": "#f3f6f8",
        "surface_color": "#ffffff",
    },
    "sage": {
        "accent_color": "#4f7f68",
        "background_color": "#f1f5f1",
        "surface_color": "#ffffff",
    },
}

THEME_LABELS: Final[dict[str, str]] = {
    "navy": "Navy / Midnight",
    "light": "Light / Daylight",
    "sage": "Sage / Quiet green",
}

DEFAULT_APPEARANCE_PREFERENCES: Final[dict[str, object]] = {
    "ui_theme": "navy",
    "ui_accent_color": "",
    "ui_background_color": "",
    "ui_surface_color": "",
    "ui_reduced_motion": False,
}


def _normalise_theme(value: object) -> str:
    """Return a supported theme name, falling back to the Hub default."""

    if isinstance(value, str):
        normalised = value.strip().casefold()
        if normalised in _THEME_NAMES:
            return normalised
    return "navy"


def _normalise_hex(value: object) -> str:
    """Return a canonical lower-case ``#RRGGBB`` value or an empty override."""

    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    return candidate.lower() if _HEX_COLOR_RE.fullmatch(candidate) else ""


def _coerce_bool(value: object, *, default: bool = False) -> bool:
    """Coerce settings values without treating ``"false"`` as true."""

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalised = value.strip().casefold()
        if normalised in {"1", "true", "yes", "on", "enabled"}:
            return True
        if normalised in {"0", "false", "no", "off", "disabled"}:
            return False
    return default


def _channels(value: str) -> tuple[int, int, int]:
    return (int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16))


def _hex(red: float, green: float, blue: float) -> str:
    red_value = max(0, min(255, round(red)))
    green_value = max(0, min(255, round(green)))
    blue_value = max(0, min(255, round(blue)))
    return f"#{red_value:02x}{green_value:02x}{blue_value:02x}"


def _relative_luminance(value: str) -> float:
    """Calculate sRGB relative luminance for contrast-aware foregrounds."""

    channels = [channel / 255 for channel in _channels(value)]
    linear = [
        channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast_text(background: str) -> str:
    """Choose the more readable black or white foreground for a color."""

    background = _normalise_hex(background) or "#000000"
    luminance = _relative_luminance(background)
    white_ratio = (1.0 + 0.05) / (luminance + 0.05)
    black_ratio = (luminance + 0.05) / 0.05
    return "#ffffff" if white_ratio >= black_ratio else "#101820"


def _mix(first: str, second: str, second_weight: float) -> str:
    """Blend two known colors, clamping the weight for safe stylesheet output."""

    weight = max(0.0, min(1.0, float(second_weight)))
    first_channels = _channels(first)
    second_channels = _channels(second)
    return _hex(
        first_channels[0] * (1 - weight) + second_channels[0] * weight,
        first_channels[1] * (1 - weight) + second_channels[1] * weight,
        first_channels[2] * (1 - weight) + second_channels[2] * weight,
    )


def _resolved_colors(
    *,
    theme: object,
    accent_color: object,
    background_color: object,
    surface_color: object,
) -> tuple[str, str, str]:
    """Resolve optional overrides against a named preset."""

    preset = THEME_PRESETS[_normalise_theme(theme)]
    accent = _normalise_hex(accent_color) or preset["accent_color"]
    background = _normalise_hex(background_color) or preset["background_color"]
    surface = _normalise_hex(surface_color) or preset["surface_color"]
    return accent, background, surface


def resolved_theme_colors(
    *,
    theme: str = "navy",
    accent_color: str = "",
    background_color: str = "",
    surface_color: str = "",
) -> dict[str, str]:
    """Return resolved colors for a theme and optional user overrides.

    This small helper is useful when a caller wants to paint a preview without
    applying a full stylesheet.  Empty overrides resolve to the preset values.
    """

    accent, background, surface = _resolved_colors(
        theme=theme,
        accent_color=accent_color,
        background_color=background_color,
        surface_color=surface_color,
    )
    return {
        "accent_color": accent,
        "background_color": background,
        "surface_color": surface,
    }


def theme_stylesheet(
    base: str,
    *,
    theme: str,
    accent_color: str,
    background_color: str,
    surface_color: str,
    high_contrast: bool = False,
) -> str:
    """Append a complete Hub theme override to an existing Qt stylesheet.

    ``main_window._BASE_STYLESHEET`` is intentionally accepted as ``base`` so
    existing selectors keep working.  The rules below use the same object names
    as the shell and its pages and are appended after ``base``; this lets the
    theme override the original navy values without requiring a rewrite of the
    page implementation.  High contrast is handled as a complete palette so a
    custom low-contrast color can never weaken the accessibility setting.
    """

    accent, background, surface = _resolved_colors(
        theme=theme,
        accent_color=accent_color,
        background_color=background_color,
        surface_color=surface_color,
    )

    if high_contrast:
        # Preserve the semantic accent as a bright keyboard/focus color while
        # forcing all page surfaces to black and all text to white.
        accent = "#00ffff"
        background = "#000000"
        surface = "#000000"
        page_text = surface_text = "#ffffff"
        muted_text = "#ffffff"
        border = "#ffffff"
        navigation = "#000000"
        hover = "#1c1c1c"
        selected = "#003b5c"
        accent_text = "#000000"
        input_background = "#000000"
        focus = "#ffff00"
    else:
        page_text = contrast_text(background)
        surface_text = contrast_text(surface)
        muted_text = _mix(surface_text, surface, 0.38)
        border = _mix(surface, page_text, 0.24)
        # Keep the navigation rail close to the page background.  Mixing it
        # with the text foreground makes light themes look like a gray slab;
        # a small surface blend gives the rail separation without losing the
        # quiet, polished palette.
        navigation = _mix(background, surface, 0.28)
        hover = _mix(surface, accent, 0.16)
        selected = _mix(background, accent, 0.34)
        accent_text = contrast_text(accent)
        input_background = _mix(background, surface, 0.48)
        focus = accent

    # These selectors cover the current shell and the dashboard board/cards
    # introduced by the new Today surface.  Attribute selectors are included so
    # future Hub widgets can opt into the same appearance without knowing the
    # concrete page in which they are hosted.
    # Recolor existing ID-specific rules as well as the common overrides below.
    # Otherwise a legacy input or list selector can retain pale navy-theme text
    # on a light surface because IDs outrank the generic QWidget rules.
    palette = {}
    for colors, replacement in (
        (("#08101b",), navigation),
        (("#0b1421",), background),
        (("#101f32", "#14253a", "#1c2c40"), surface),
        (("#0d1a2b",), input_background),
        (("#1a4164", "#1c3c61", "#1c3d66", "#20334d"), selected),
        (("#265a84",), hover),
        (("#22334a", "#263b55", "#294563", "#294766", "#376c98", "#385371"), border),
        (("#5f9edc", "#70b5ff"), accent),
        (("#586a80", "#687d94", "#7792b3", "#9aabc0", "#a9b9cc", "#b8c8dc", "#b9c8d9"), muted_text),
        (("#dce7f3", "#e7eef7", "#ecf4fc", "#f7fbff", "#ffffff"), surface_text),
    ):
        palette.update(dict.fromkeys(colors, replacement))
    base = re.sub(r"#[0-9a-fA-F]{6}", lambda match: palette.get(match[0].lower(), match[0]), base)
    override = f"""

/* Personal Hub appearance override: {theme} */
QMainWindow, QWidget {{
    background: {background};
    color: {page_text};
    font-family: "Segoe UI";
    font-size: 10pt;
}}

#appearanceTitle {{ font-size: 14pt; font-weight: 650; }}
#dashboardCardTitle {{ font-size: 12pt; font-weight: 600; }}

QLabel {{
    background: transparent;
}}

QToolTip {{
    background: {surface};
    color: {surface_text};
    border: 1px solid {border};
    padding: 5px;
}}

#personalHubNavigation {{
    background: {navigation};
    border-right: 1px solid {border};
}}

#brandTitle, #pageTitle, #sourceFailureTitle, #attentionNotificationTitle,
#diagnosticsTitle, #calendarPeriodLabel, #privacyOverlayTitle,
#newsEditorTitle, #newsEntryTitle, #taskRowTitle, #todaySectionTitle,
#taskEditorTitle, #calendarEventDetailsTitle {{
    color: {page_text};
}}

#pageSubtitle, #statusLabel, #settingsValue,
#navigationLabel, #workspaceNavigationLabel, #managementNavigationLabel,
#pageEyebrow, #todayDateLabel, #todayTimezoneLabel,
#todayRefreshStatus, #todaySectionBody, #todaySectionMetadata,
#calendarFreshnessLabel, #calendarStatusLabel, #calendarTimezoneLabel,
#calendarSourcesTitle, #calendarSourcesEmpty, #calendarWeekdayLabel,
#calendarMoreLabel, #calendarEmptyLabel, #taskRowDetails, #taskStatusLabel,
#taskEditorStatus, #taskFieldLabel, #sourceFailureBody, #attentionNotificationBody,
#diagnosticsBody, #privacyOverlayBody, #newsFilterLabel, #newsFilterStatusLabel,
#newsFreshnessLabel, #newsStatusLabel, #newsSubscriptionsTitle,
#newsSubscriptionsEmpty, #newsEntriesStatusLabel, #newsEntryMetadata,
#newsEntrySummary, #newsEditorLabel, #newsSubscriptionEditorStatus,
#calendarEventDetailsWhen, #calendarEventDetailsLocation,
#calendarEventDetailsSource, #calendarEventDetailsStatus,
#messageMetadataLabel, #messageStatusLabel, #settingsDescription {{
    color: {muted_text};
}}

#todayPage, #calendarPage, #tasksPage, #watchlistsPage,
#inboxPage, #sourcesPage, #settingsPage, #dashboardPage, #dashboardBoard {{
    background: {background};
    color: {page_text};
}}

#todayCard, #todaySection, #taskQuickAdd, #taskEditorPanel,
#calendarMonthCell, #calendarWeekColumn, #calendarSourcesPanel,
#calendarEventDetailsPanel, #sourceFailurePanel, #attentionNotificationPanel, #diagnosticsView,
#newsSubscriptionsPanel, #newsEntryRow, #newsFiltersPanel,
#newsSubscriptionEditor, #dashboardCard, #dashboardWidget,
#dashboardWidgetCard, #dashboardWidgetFrame, #dashboardWidgetHost,
#dashboardEmptyState,
QFrame[hubSurface="true"], QFrame[hubCard="true"], QWidget[hubWidget="true"] {{
    background: {surface};
    color: {surface_text};
    border-color: {border};
}}

#attentionNotificationPanel {{
    background: {selected};
    color: {page_text};
    border: 1px solid {accent};
    border-radius: 10px;
}}

#attentionNotificationTitle {{
    color: {page_text};
    font-weight: 650;
}}

#attentionNotificationBody {{
    color: {surface_text};
}}

#attentionNotificationOpenButton, #attentionNotificationDismissButton {{
    background: {surface};
    border-color: {border};
    color: {surface_text};
}}

#newsFiltersPanel, #newsSubscriptionsPanel, #newsSubscriptionEditor,
#calendarMonthCell, #calendarWeekColumn, #calendarAgendaView,
#calendarSettingsPanel, #inboxList, #messageSummaryList, #sourcesList,
#diagnosticsList, #messageBodyView,
#todaySectionItems, #tasksList, #newsEntriesList, #dashboardBoard {{
    background: {input_background};
}}

#pageHeader, #calendarToolbar, #taskViewBar, #messageCategoryBar,
#calendarContentSplitter, #taskContentSplitter, #messageContentSplitter {{
    background: transparent;
    border: 0;
}}

#taskRow, #inboxRow, #messageSummaryRow {{
    background: transparent;
    border: 0;
    border-bottom: 1px solid {border};
    border-radius: 0;
    color: {surface_text};
}}

#taskRow:hover, #inboxRow:hover, #messageSummaryRow:hover {{
    background: {surface};
}}

#taskQuickViewButton, #taskFiltersButton, #taskUndoButton,
#calendarSettingsButton, #taskOverflowButton, #messageCategorySelector {{
    background: {surface};
    border-color: {border};
    color: {surface_text};
}}

#taskQuickViewButton:hover, #taskQuickViewButton:focus,
#taskFiltersButton:hover, #taskFiltersButton:focus,
#taskUndoButton:hover, #taskUndoButton:focus,
#calendarSettingsButton:hover, #calendarSettingsButton:focus,
#taskOverflowButton:hover, #taskOverflowButton:focus,
#messageCategorySelector:hover, #messageCategorySelector:focus {{
    background: {hover};
    border-color: {focus};
}}

#taskCompleteButton {{
    background: {selected};
    border-color: {accent};
    color: {page_text};
}}

#taskCompleteButton:hover, #taskCompleteButton:focus {{
    background: {hover};
    border-color: {focus};
}}

#inboxModeTabs::pane {{
    border: 0;
    background: transparent;
}}

#inboxModeTabs QTabBar::tab {{
    background: transparent;
    border-color: transparent;
    color: {muted_text};
}}

#inboxModeTabs QTabBar::tab:selected {{
    background: {selected};
    border-color: {accent};
    color: {page_text};
}}

#inboxUnreadIndicator, #messageUnreadIndicator {{
    color: {accent};
}}

#messageSummaryRow[important="true"] #messageUnreadIndicator {{
    color: {accent};
}}

#dashboardWidgetCard {{
    background: {surface};
    border: 1px solid {border};
    border-radius: 14px;
    color: {surface_text};
}}

#dashboardGridPanel {{
    background: {input_background};
    border: 1px solid {border};
    border-radius: 14px;
}}

#dashboardGridPanel[dashboardEditing="true"] {{
    border: 2px dashed {accent};
}}

#dashboardResizeGrip, #dashboardResizeGripN, #dashboardResizeGripS,
#dashboardResizeGripE, #dashboardResizeGripW, #dashboardResizeGripNE,
#dashboardResizeGripNW, #dashboardResizeGripSW {{
    background: transparent;
    border: 0;
}}

#dashboardDropPreview {{
    background: transparent;
    border: 2px dashed {accent};
    border-radius: 12px;
}}

#dashboardCardHeader, #dashboardEditControls, #dashboardToolbar,
#dashboardGrid {{
    background: transparent;
}}

#dashboardBoard {{
    background: transparent;
    border: 0;
}}

#dashboardWidgetCard #todaySection {{
    background: transparent;
    border: 0;
    border-radius: 0;
}}

#newsIncludeKeywordsInput, #newsExcludeKeywordsInput,
#newsSubscriptionTitleInput, #newsSubscriptionUrlInput,
#newsSubscriptionIntervalInput, #newsSubscriptionIncludeInput,
#newsSubscriptionExcludeInput, #taskQuickAddInput, #taskSearchInput,
#taskEditorPanel QLineEdit, #taskEditorPanel QPlainTextEdit,
#taskEditorPanel QDateTimeEdit, #taskEditorPanel QComboBox,
#taskFilters QComboBox, #calendarEventDetailsDescription {{
    background: {input_background};
    border-color: {border};
    color: {surface_text};
    selection-background-color: {accent};
    selection-color: {accent_text};
}}

QPushButton[hubNavigation="true"] {{
    background: transparent;
    border: 1px solid transparent;
    color: {muted_text};
}}

QPushButton[hubNavigation="true"]:hover:enabled {{
    background: {hover};
    border-color: {accent};
    color: {page_text};
}}

QPushButton[hubNavigation="true"]:checked {{
    background: {selected};
    border-color: {accent};
    color: {page_text};
}}

QPushButton, QToolButton {{
    background: {surface};
    border: 1px solid {border};
    border-radius: 6px;
    color: {surface_text};
    padding: 5px 10px;
}}

QPushButton:hover:enabled, QToolButton:hover:enabled {{
    background: {hover};
    border-color: {accent};
}}

QPushButton:focus, QToolButton:focus, QLineEdit:focus,
QPlainTextEdit:focus, QComboBox:focus, QDateTimeEdit:focus,
QSpinBox:focus, QDoubleSpinBox:focus, QCheckBox:focus,
QListWidget:focus, QTableView:focus, QTabBar:focus {{
    border: 2px solid {focus};
}}

#dashboardCardHeader:focus {{
    border: 2px solid {focus};
    border-radius: 4px;
}}

#dashboardResizeGrip:focus, #dashboardResizeGripN:focus,
#dashboardResizeGripS:focus, #dashboardResizeGripE:focus,
#dashboardResizeGripW:focus, #dashboardResizeGripNE:focus,
#dashboardResizeGripNW:focus, #dashboardResizeGripSW:focus {{
    border: 2px solid {focus};
}}

QPushButton:pressed:enabled, QToolButton:pressed:enabled {{
    background: {selected};
}}

QPushButton#primaryButton, QPushButton#applyAppearanceButton,
QPushButton[hubAccent="true"] {{
    background: {accent};
    border-color: {accent};
    color: {accent_text};
    font-weight: 650;
}}

#newsEntryReadButton, #newsEntryOpenButton, #calendarEventButton,
#calendarEventNativeOpenButton, #calendarEventDetailsCloseButton,
#taskNativeOpenButton {{
    background: {selected};
    border-color: {accent};
    color: {page_text};
}}

#newsEntryReadButton:hover:enabled, #newsEntryReadButton:focus,
#newsEntryOpenButton:hover:enabled, #newsEntryOpenButton:focus,
#calendarEventButton:hover:enabled, #calendarEventButton:focus,
#calendarEventNativeOpenButton:hover:enabled, #calendarEventNativeOpenButton:focus,
#calendarEventDetailsCloseButton:hover:enabled, #calendarEventDetailsCloseButton:focus,
#taskNativeOpenButton:hover:enabled, #taskNativeOpenButton:focus {{
    background: {hover};
    border-color: {focus};
    color: {page_text};
}}

QPushButton#primaryButton:hover:enabled, QPushButton#applyAppearanceButton:hover:enabled,
QPushButton[hubAccent="true"]:hover:enabled {{
    background: {_mix(accent, accent_text, 0.16)};
    border-color: {accent_text};
    color: {contrast_text(_mix(accent, accent_text, 0.16))};
}}

QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QDateTimeEdit,
QSpinBox, QDoubleSpinBox {{
    background: {input_background};
    border: 1px solid {border};
    border-radius: 6px;
    color: {surface_text};
    selection-background-color: {accent};
    selection-color: {accent_text};
}}

QLineEdit:disabled, QPlainTextEdit:disabled, QComboBox:disabled,
QDateTimeEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled,
QPushButton:disabled, QToolButton:disabled {{
    background: {background};
    color: {muted_text};
    border-color: {border};
}}

QComboBox QAbstractItemView, QListWidget, QTreeWidget, QTableView,
#inboxList, #sourcesList, #diagnosticsList, #calendarAgendaView {{
    background: {input_background};
    color: {surface_text};
    border: 1px solid {border};
    selection-background-color: {selected};
    selection-color: {page_text};
}}

#todaySectionItems, #tasksList, #newsEntriesList {{
    background: transparent;
    border: 0;
    color: {surface_text};
}}

QAbstractItemView::item:selected, QListWidget::item:selected,
QTreeWidget::item:selected, QTableView::item:selected {{
    background: {selected};
    color: {page_text};
}}

#miniCalendarGrid, #miniCalendarGrid QAbstractItemView {{
    background: {surface};
    color: {surface_text};
    selection-background-color: {selected};
    selection-color: {page_text};
}}

QCheckBox, QRadioButton {{
    color: {surface_text};
    spacing: 7px;
}}

QCheckBox::indicator, QRadioButton::indicator {{
    background: {input_background};
    border: 1px solid {border};
    height: 16px;
    width: 16px;
}}

QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background: {accent};
    border-color: {accent};
}}

QProgressBar {{
    background: {input_background};
    border: 1px solid {border};
    border-radius: 5px;
    color: {surface_text};
    text-align: center;
}}

QProgressBar::chunk {{
    background: {accent};
    border-radius: 4px;
}}

QScrollBar:vertical, QScrollBar:horizontal {{
    background: {background};
    border: 0;
    margin: 0;
}}

QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
    background: {border};
    border-radius: 5px;
    min-height: 24px;
    min-width: 24px;
}}

QScrollBar::handle:hover {{
    background: {accent};
}}

QTabBar::tab {{
    background: {surface};
    color: {muted_text};
    border: 1px solid {border};
    padding: 6px 12px;
}}

QTabBar::tab:selected, QTabBar::tab:hover {{
    background: {selected};
    color: {page_text};
    border-color: {accent};
}}

#statusLabel, #settingsRow {{
    border-color: {border};
}}

#privacyOverlay {{
    background: {background};
    border-color: {accent};
}}

#appearancePanel, #appearanceHeader, #appearancePresetCard,
#appearanceColorsCard, #appearanceMotionCard {{
    background: {surface};
    color: {surface_text};
    border-color: {border};
}}

#appearancePreviewAccent {{
    background: {accent};
    color: {accent_text};
}}
"""

    if high_contrast:
        # Some existing page selectors are more specific than the broad QWidget
        # rules above.  Keep this explicit block at the end so every important
        # content surface stays black in high-contrast mode.
        override += """

/* High contrast reinforcement */
QMainWindow, QWidget, QDialog, QScrollArea, QScrollArea > QWidget > QWidget {{
    background: #000000;
    color: #ffffff;
}}

#personalHubNavigation, #todayPage, #calendarPage, #tasksPage,
#watchlistsPage, #inboxPage, #sourcesPage, #settingsPage,
#dashboardPage, #dashboardBoard, #todayCard, #todaySection, #taskQuickAdd,
#dashboardGridPanel,
#taskEditorPanel, #taskRow, #calendarMonthCell, #calendarWeekColumn,
#calendarSourcesPanel, #calendarEventDetailsPanel, #sourceFailurePanel,
#attentionNotificationPanel,
#diagnosticsView, #newsSubscriptionsPanel, #newsEntryRow, #newsFiltersPanel,
#newsSubscriptionEditor, #dashboardCard, #dashboardWidget,
#dashboardWidgetCard, #dashboardWidgetFrame, #dashboardWidgetHost,
#dashboardEmptyState,
QFrame[hubSurface="true"], QFrame[hubCard="true"], QWidget[hubWidget="true"] {{
    background: #000000;
    color: #ffffff;
    border-color: #ffffff;
}}

#dashboardCardHeader, #dashboardEditControls, #dashboardToolbar,
#dashboardGrid, #dashboardWidgetCard #todaySection {{
    background: transparent;
    border: 0;
}}

#dashboardGridPanel[dashboardEditing="true"] {{
    border: 3px dashed #ffff00;
}}

#dashboardResizeGrip, #dashboardResizeGripN, #dashboardResizeGripS,
#dashboardResizeGripE, #dashboardResizeGripW, #dashboardResizeGripNE,
#dashboardResizeGripNW, #dashboardResizeGripSW {{
    background: transparent;
    border: 0;
}}

#dashboardDropPreview {{
    background: transparent;
    border: 2px dashed #ffffff;
}}

#miniCalendarGrid, #miniCalendarGrid QAbstractItemView {{
    background: #000000;
    color: #ffffff;
    selection-background-color: #003b5c;
    selection-color: #ffffff;
}}

QLabel, QCheckBox, QRadioButton, QGroupBox, QAbstractItemView,
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QDateTimeEdit,
QSpinBox, QDoubleSpinBox {{
    color: #ffffff;
}}

QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QDateTimeEdit,
QSpinBox, QDoubleSpinBox, QListWidget, QTreeWidget, QTableView,
#inboxList, #sourcesList, #diagnosticsList, #calendarAgendaView {{
    background: #000000;
    color: #ffffff;
    border: 2px solid #ffffff;
    selection-background-color: #003b5c;
    selection-color: #ffffff;
}}

#newsIncludeKeywordsInput, #newsExcludeKeywordsInput,
#newsSubscriptionTitleInput, #newsSubscriptionUrlInput,
#newsSubscriptionIntervalInput, #newsSubscriptionIncludeInput,
#newsSubscriptionExcludeInput, #taskQuickAddInput, #taskSearchInput,
#taskEditorPanel QLineEdit, #taskEditorPanel QPlainTextEdit,
#taskEditorPanel QDateTimeEdit, #taskEditorPanel QComboBox,
#taskFilters QComboBox, #calendarEventDetailsDescription {{
    background: #000000;
    color: #ffffff;
    border-color: #ffffff;
    selection-background-color: #003b5c;
    selection-color: #ffffff;
}}

QPushButton, QToolButton {{
    background: #000000;
    color: #ffffff;
    border: 2px solid #ffffff;
}}

QPushButton:hover:enabled, QToolButton:hover:enabled,
QPushButton[hubNavigation="true"]:hover:enabled {{
    background: #003b5c;
    border-color: #00ffff;
    color: #ffffff;
}}

QPushButton[hubNavigation="true"]:checked,
QPushButton#primaryButton, QPushButton#applyAppearanceButton,
QPushButton[hubAccent="true"] {{
    background: #00ffff;
    border-color: #ffffff;
    color: #000000;
}}

#newsEntryReadButton, #newsEntryOpenButton, #calendarEventButton,
#calendarEventNativeOpenButton, #calendarEventDetailsCloseButton,
#taskNativeOpenButton {{
    background: #003b5c;
    border-color: #ffffff;
    color: #ffffff;
}}

QPushButton:focus, QToolButton:focus, QLineEdit:focus,
QPlainTextEdit:focus, QComboBox:focus, QDateTimeEdit:focus,
QSpinBox:focus, QDoubleSpinBox:focus, QCheckBox:focus,
QListWidget:focus, QTableView:focus, QTabBar:focus {{
    border: 3px solid #ffff00;
}}

#dashboardCardHeader:focus {{
    border: 3px solid #ffff00;
}}

#dashboardResizeGrip:focus, #dashboardResizeGripN:focus,
#dashboardResizeGripS:focus, #dashboardResizeGripE:focus,
#dashboardResizeGripW:focus, #dashboardResizeGripNE:focus,
#dashboardResizeGripNW:focus, #dashboardResizeGripSW:focus {{
    border: 3px solid #ffff00;
}}

QCheckBox::indicator, QRadioButton::indicator {{
    background: #000000;
    border: 2px solid #ffffff;
}}

QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background: #00ffff;
    border-color: #ffffff;
}}
"""

    return f"{base or ''}\n{override}"


class HexColorValidator(QValidator):
    """Validator for optional six-digit RGB colors.

    Empty text is accepted because it means “use the selected preset.”  Partial
    input is intermediate so users can type a color naturally into a line edit.
    """

    def validate(self, input_text: str, pos: int) -> tuple[QValidator.State, str, int]:
        del pos
        candidate = input_text.strip()
        if not candidate:
            return QValidator.State.Acceptable, input_text, 0
        if _HEX_COLOR_RE.fullmatch(candidate):
            return QValidator.State.Acceptable, input_text, len(input_text)
        if candidate == "#" or (
            candidate.startswith("#")
            and len(candidate) < 7
            and all(character in "0123456789abcdefABCDEF" for character in candidate[1:])
        ):
            return QValidator.State.Intermediate, input_text, len(input_text)
        return QValidator.State.Invalid, input_text, len(input_text)


class AppearancePanel(QWidget):
    """Native controls for Personal Hub colors, presets, and motion preference."""

    changed = Signal(object)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        high_contrast: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("appearancePanel")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._high_contrast = bool(high_contrast)
        self._updating = False
        self._preferences: dict[str, object] = dict(DEFAULT_APPEARANCE_PREFERENCES)
        self._color_inputs: dict[str, QLineEdit] = {}
        self._color_swatches: dict[str, QPushButton] = {}
        self._build_ui()
        self.set_preferences(DEFAULT_APPEARANCE_PREFERENCES)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(14)

        header = QFrame(self)
        header.setObjectName("appearanceHeader")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(3)
        title = QLabel("Appearance", header)
        title.setObjectName("appearanceTitle")
        title.setAccessibleName("Appearance heading")
        subtitle = QLabel(
            "Choose a theme preset, custom colors, and animation settings.",
            header,
        )
        subtitle.setObjectName("appearanceSubtitle")
        subtitle.setWordWrap(True)
        subtitle.setAccessibleName("Appearance description")
        header_layout.addWidget(title)
        header_layout.addWidget(subtitle)
        root.addWidget(header)

        preset_card = QFrame(self)
        preset_card.setObjectName("appearancePresetCard")
        preset_layout = QFormLayout(preset_card)
        preset_layout.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        preset_layout.setContentsMargins(14, 12, 14, 12)
        preset_layout.setHorizontalSpacing(14)
        preset_layout.setVerticalSpacing(8)
        preset_label = QLabel("Theme preset", preset_card)
        preset_label.setObjectName("appearanceFieldLabel")
        self.theme_combo = QComboBox(preset_card)
        self.theme_combo.setObjectName("appearanceThemeCombo")
        self.theme_combo.setAccessibleName("Theme preset")
        self.theme_combo.setAccessibleDescription(
            "Select navy, light, or sage colors for the Personal Hub."
        )
        for name in _THEME_NAMES:
            self.theme_combo.addItem(THEME_LABELS[name], name)
        self.preset_combo = self.theme_combo
        self.theme_selector = self.theme_combo
        preset_layout.addRow(preset_label, self.theme_combo)
        root.addWidget(preset_card)

        colors_card = QFrame(self)
        colors_card.setObjectName("appearanceColorsCard")
        colors_layout = QVBoxLayout(colors_card)
        colors_layout.setContentsMargins(14, 12, 14, 12)
        colors_layout.setSpacing(9)
        colors_title = QLabel("Custom colors", colors_card)
        colors_title.setObjectName("appearanceSectionTitle")
        colors_title.setAccessibleName("Custom colors heading")
        colors_help = QLabel(
            "Leave a field blank to use the selected preset. Enter colors as #RRGGBB.",
            colors_card,
        )
        colors_help.setObjectName("appearanceHelpLabel")
        colors_help.setWordWrap(True)
        colors_layout.addWidget(colors_title)
        colors_layout.addWidget(colors_help)

        color_form = QFormLayout()
        color_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        color_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        color_form.setContentsMargins(0, 0, 0, 0)
        color_form.setHorizontalSpacing(12)
        color_form.setVerticalSpacing(8)
        for key, label in (
            ("ui_accent_color", "Accent color"),
            ("ui_background_color", "Background color"),
            ("ui_surface_color", "Surface color"),
        ):
            field, swatch = self._build_color_field(key, label, colors_card)
            color_form.addRow(QLabel(label, colors_card), field)
            self._color_swatches[key] = swatch
        colors_layout.addLayout(color_form)
        root.addWidget(colors_card)

        motion_card = QFrame(self)
        motion_card.setObjectName("appearanceMotionCard")
        motion_layout = QVBoxLayout(motion_card)
        motion_layout.setContentsMargins(14, 12, 14, 12)
        motion_layout.setSpacing(5)
        self.animations_checkbox = QCheckBox("Enable animations", motion_card)
        self.animations_checkbox.setObjectName("appearanceAnimationsCheckBox")
        self.animations_checkbox.setAccessibleName("Enable animations")
        self.animations_checkbox.setAccessibleDescription(
            "Allow subtle page transitions and feedback animations."
        )
        self.reduced_motion_checkbox = self.animations_checkbox
        self.motion_toggle = self.animations_checkbox
        motion_help = QLabel(
            "Turn this off for reduced motion. The preference applies to page transitions and widget feedback.",
            motion_card,
        )
        motion_help.setObjectName("appearanceMotionHelp")
        motion_help.setWordWrap(True)
        motion_layout.addWidget(self.animations_checkbox)
        motion_layout.addWidget(motion_help)
        root.addWidget(motion_card)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 2, 0, 0)
        actions.setSpacing(8)
        self.reset_button = QPushButton("Reset preset", self)
        self.reset_button.setObjectName("resetAppearanceButton")
        self.reset_button.setAccessibleName("Reset preset")
        self.reset_button.setAccessibleDescription(
            "Clear custom colors and restore the selected theme preset."
        )
        self.reset_preset_button = self.reset_button
        self.apply_button = QPushButton("Apply appearance", self)
        self.apply_button.setObjectName("applyAppearanceButton")
        self.apply_button.setProperty("hubAccent", True)
        self.apply_button.setAccessibleName("Apply appearance")
        self.apply_button.setAccessibleDescription(
            "Save and apply the selected appearance settings."
        )
        self.apply_appearance_button = self.apply_button
        actions.addWidget(self.reset_button)
        actions.addStretch(1)
        actions.addWidget(self.apply_button)
        root.addLayout(actions)

        self.status_label = QLabel("Changes apply when you press Apply appearance.", self)
        self.status_label.setObjectName("appearanceStatusLabel")
        self.status_label.setWordWrap(True)
        self.status_label.setAccessibleName("Appearance status")
        root.addWidget(self.status_label)
        root.addStretch(1)

        self.theme_combo.currentIndexChanged.connect(self._preset_changed)
        self.animations_checkbox.toggled.connect(self._motion_toggled)
        self.reset_button.clicked.connect(self.reset_to_preset)
        self.apply_button.clicked.connect(self.apply_preferences)

    def _build_color_field(
        self,
        key: str,
        label: str,
        parent: QWidget,
    ) -> tuple[QLineEdit, QPushButton]:
        row = QWidget(parent)
        row.setObjectName(f"appearance{key.removeprefix('ui_').title().replace('_', '')}Row")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(7)

        input_widget = QLineEdit(row)
        input_widget.setObjectName(
            f"appearance{key.removeprefix('ui_').title().replace('_', '')}Input"
        )
        input_widget.setAccessibleName(label)
        input_widget.setAccessibleDescription(
            f"Optional {label.casefold()} in six-digit #RRGGBB notation."
        )
        input_widget.setPlaceholderText("Preset color")
        input_widget.setMaxLength(7)
        input_widget.setClearButtonEnabled(True)
        # The validator makes the native editing affordance reject malformed
        # characters, while HexColorValidator permits partial input and the
        # empty preset value.
        input_widget.setValidator(HexColorValidator(input_widget))
        input_widget.textChanged.connect(
            lambda _text, color_key=key: self._color_changed(color_key)
        )
        self._color_inputs[key] = input_widget

        swatch = QPushButton(row)
        swatch.setObjectName(f"appearance{key.removeprefix('ui_').title().replace('_', '')}Swatch")
        swatch.setAccessibleName(f"Choose {label.casefold()}")
        swatch.setAccessibleDescription(f"Open a color picker for the {label.casefold()}.")
        swatch.setToolTip(f"Choose {label.casefold()}")
        swatch.setFixedSize(34, 30)
        swatch.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        swatch.clicked.connect(lambda _checked=False, color_key=key: self._choose_color(color_key))
        layout.addWidget(input_widget, 1)
        layout.addWidget(swatch)
        # QFormLayout labels this row as a unit while the line edit remains the
        # focusable control.  Keep a direct attribute for simple integrations.
        short_name = key.removeprefix("ui_").removesuffix("_color")
        full_name = key.removeprefix("ui_")
        setattr(self, f"{short_name}_input", input_widget)
        setattr(self, f"{full_name}_input", input_widget)
        setattr(self, f"{full_name}_color_input", input_widget)
        setattr(self, f"{short_name}_line_edit", input_widget)
        setattr(self, f"{short_name}_swatch", swatch)
        setattr(self, f"{full_name}_swatch", swatch)
        setattr(self, f"{short_name}_color_button", swatch)
        setattr(self, f"{full_name}_color_button", swatch)
        return row, swatch

    def _selected_theme(self) -> str:
        return _normalise_theme(self.theme_combo.currentData())

    def _preset_color(self, key: str) -> str:
        color_name = {
            "ui_accent_color": "accent_color",
            "ui_background_color": "background_color",
            "ui_surface_color": "surface_color",
        }[key]
        return THEME_PRESETS[self._selected_theme()][color_name]

    def _resolved_panel_colors(self) -> dict[str, str]:
        values = self._collect_preferences(allow_invalid=True)
        return resolved_theme_colors(
            theme=str(values["ui_theme"]),
            accent_color=str(values["ui_accent_color"]),
            background_color=str(values["ui_background_color"]),
            surface_color=str(values["ui_surface_color"]),
        )

    def _refresh_swatches(self) -> None:
        resolved = self._resolved_panel_colors()
        for key, swatch in self._color_swatches.items():
            color_name = key.removeprefix("ui_")
            color = resolved[color_name]
            display_color = "#000000" if self._high_contrast else color
            text = "#ffffff" if self._high_contrast else contrast_text(color)
            border = "#ffffff" if self._high_contrast else text
            tooltip_background = resolved["surface_color"]
            tooltip_text = "#000000" if self._high_contrast else contrast_text(tooltip_background)
            swatch.setStyleSheet(
                f"QPushButton {{ background: {display_color}; color: {text}; "
                f"border: 1px solid {border}; border-radius: 5px; }} "
                f"QToolTip {{ background: {tooltip_background}; color: {tooltip_text}; }}"
            )
            swatch.setToolTip(f"{color.upper()} — choose {color_name.replace('_', ' ')}")

    def _update_apply_state(self) -> None:
        invalid = any(
            bool(input_widget.text().strip())
            and not _HEX_COLOR_RE.fullmatch(input_widget.text().strip())
            for input_widget in self._color_inputs.values()
        )
        self.apply_button.setEnabled(not invalid)
        if invalid:
            self.status_label.setText("Enter each color as #RRGGBB, or clear it to use the preset.")

    @Slot(int)
    def _preset_changed(self, _index: int) -> None:
        if self._updating:
            return
        # Preset selection is a deliberate base-theme change.  Clear overrides
        # so the preview and saved mapping describe the chosen preset exactly.
        self._updating = True
        try:
            for input_widget in self._color_inputs.values():
                input_widget.clear()
        finally:
            self._updating = False
        self._refresh_swatches()
        self._update_apply_state()
        self.status_label.setText(
            f"{THEME_LABELS[self._selected_theme()]} selected. Press Apply appearance to save it."
        )

    @Slot(str)
    def _color_changed(self, _key: str) -> None:
        if self._updating:
            return
        self._refresh_swatches()
        self._update_apply_state()
        if self.apply_button.isEnabled():
            self.status_label.setText("Custom colors ready. Press Apply appearance to save them.")

    @Slot(bool)
    def _motion_toggled(self, _enabled: bool) -> None:
        if not self._updating:
            self.status_label.setText(
                "Motion preference changed. Press Apply appearance to save it."
            )

    def _choose_color(self, key: str) -> None:
        current = self._color_inputs[key].text().strip() or self._preset_color(key)
        selected = QColorDialog.getColor(
            QColor(current), self, f"Choose {key.removeprefix('ui_').replace('_', ' ')}"
        )
        if selected.isValid():
            self._color_inputs[key].setText(selected.name(QColor.NameFormat.HexRgb).lower())
            self._color_inputs[key].setFocus(Qt.FocusReason.OtherFocusReason)

    def _collect_preferences(self, *, allow_invalid: bool = False) -> dict[str, object]:
        values: dict[str, object] = {"ui_theme": self._selected_theme()}
        for key, input_widget in self._color_inputs.items():
            raw = input_widget.text().strip()
            if _HEX_COLOR_RE.fullmatch(raw):
                values[key] = raw.lower()
            elif allow_invalid:
                values[key] = ""
            else:
                values[key] = raw
        values["ui_reduced_motion"] = not self.animations_checkbox.isChecked()
        return values

    def set_preferences(self, preferences: Mapping[str, object] | None) -> None:
        """Load appearance controls from a Hub-owned mapping without emitting.

        Unknown settings are ignored, allowing callers to pass a full
        ``HubSettings.model_dump()`` mapping.  Invalid color values are cleared
        to the preset value; the Hub settings model remains the persistence-layer
        validator for values that are eventually saved.
        """

        data = preferences if isinstance(preferences, Mapping) else {}
        theme = _normalise_theme(data.get("ui_theme", DEFAULT_APPEARANCE_PREFERENCES["ui_theme"]))
        reduced_motion = _coerce_bool(
            data.get(
                "ui_reduced_motion",
                data.get("reduced_motion", DEFAULT_APPEARANCE_PREFERENCES["ui_reduced_motion"]),
            )
        )
        self._updating = True
        try:
            index = self.theme_combo.findData(theme)
            self.theme_combo.setCurrentIndex(index if index >= 0 else 0)
            for key, input_widget in self._color_inputs.items():
                input_widget.setText(_normalise_hex(data.get(key, "")))
            self.animations_checkbox.setChecked(not reduced_motion)
        finally:
            self._updating = False
        self._preferences = self._collect_preferences(allow_invalid=True)
        self._refresh_swatches()
        self._update_apply_state()
        self.status_label.setText("Changes apply when you press Apply appearance.")

    def preferences(self) -> dict[str, object]:
        """Return the exact serialisable mapping edited by this panel."""

        values = self._collect_preferences(allow_invalid=True)
        return {
            "ui_theme": str(values["ui_theme"]),
            "ui_accent_color": str(values["ui_accent_color"]),
            "ui_background_color": str(values["ui_background_color"]),
            "ui_surface_color": str(values["ui_surface_color"]),
            "ui_reduced_motion": bool(values["ui_reduced_motion"]),
        }

    @Slot()
    def apply_preferences(self) -> dict[str, object] | None:
        """Emit validated preferences after the explicit Apply action."""

        values = self._collect_preferences()
        if any(
            isinstance(values.get(key), str)
            and bool(values[key])
            and not _HEX_COLOR_RE.fullmatch(str(values[key]))
            for key in _COLOR_PREFERENCE_KEYS
        ):
            self._update_apply_state()
            return None
        result = self.preferences()
        self._preferences = dict(result)
        self.status_label.setText("Appearance applied.")
        self.changed.emit(dict(result))
        return result

    # Common integration/readability aliases.
    apply = apply_preferences
    apply_appearance = apply_preferences

    @Slot()
    def reset_to_preset(self) -> None:
        """Clear color overrides while retaining the selected preset and motion choice."""

        self._updating = True
        try:
            for input_widget in self._color_inputs.values():
                input_widget.clear()
        finally:
            self._updating = False
        self._refresh_swatches()
        self._update_apply_state()
        self.status_label.setText(
            f"Reset to {THEME_LABELS[self._selected_theme()]}. Press Apply appearance to save it."
        )

    reset_preset = reset_to_preset
    reset = reset_to_preset

    def set_high_contrast(self, enabled: bool) -> None:
        """Keep the panel's preview state aligned with the application setting."""

        self._high_contrast = bool(enabled)
        self._refresh_swatches()

    @property
    def high_contrast(self) -> bool:
        return self._high_contrast

    @property
    def theme(self) -> str:
        return self._selected_theme()

    @property
    def reduced_motion(self) -> bool:
        return not self.animations_checkbox.isChecked()

    @property
    def animations_enabled(self) -> bool:
        return self.animations_checkbox.isChecked()

    def resolved_colors(self) -> dict[str, str]:
        """Return current colors after preset fallback and custom overrides."""

        return self._resolved_panel_colors()


__all__ = [
    "AppearancePanel",
    "DEFAULT_APPEARANCE_PREFERENCES",
    "HexColorValidator",
    "THEME_LABELS",
    "THEME_PRESETS",
    "contrast_text",
    "resolved_theme_colors",
    "theme_stylesheet",
]
