"""Shared collection metadata for the Personal Hub test lanes.

The Hub tests are intentionally grouped by filename and location so developers
can run the smallest useful lane while iterating.  The production package does
not depend on these markers; they are added during pytest collection only.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_PHASE_PATTERN = re.compile(r"test_phase(?P<phase>[1-7])(?:_|\.)")
_RELEASE_FILES = {"test_phase7_release.py", "test_phase7_accessibility.py"}
_ARCHITECTURE_FILES = {"test_boundaries.py", "test_isolation.py"}
_INTEGRATION_FILES = {
    "test_calendar_items.py",
    "test_phase3_calendar.py",
    "test_phase3_controller.py",
    "test_phase3_oauth.py",
    "test_phase3_outlook.py",
    "test_phase4_feed_fetcher.py",
    "test_phase4_ical.py",
    "test_phase4_feed_storage.py",
    "test_phase4_feed_today.py",
    "test_phase5_watches.py",
    "test_phase6_messages.py",
    "test_platform_connections.py",
}


def _add_marker(item: pytest.Item, name: str) -> None:
    item.add_marker(getattr(pytest.mark, name))


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Attach stable lane markers without duplicating decorators in every test."""

    hub_root = Path(__file__).parent
    for item in items:
        path = Path(str(item.fspath))
        try:
            relative = path.relative_to(hub_root)
        except ValueError:
            continue

        _add_marker(item, "hub")
        is_ui = "ui" in relative.parts
        _add_marker(item, "hub_ui" if is_ui else "hub_core")

        phase_match = _PHASE_PATTERN.match(path.name)
        if phase_match:
            _add_marker(item, f"hub_phase{phase_match.group('phase')}")

        if path.name in _RELEASE_FILES:
            _add_marker(item, "hub_release")
            _add_marker(item, "hub_slow")
        elif not is_ui:
            _add_marker(item, "hub_fast")
        if path.name in _ARCHITECTURE_FILES:
            _add_marker(item, "hub_architecture")
        if path.name in _INTEGRATION_FILES:
            _add_marker(item, "hub_integration")
