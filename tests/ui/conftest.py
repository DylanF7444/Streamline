from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

# Match the existing tests/ui setup while keeping Hub UI tests independent of
# the Local Assistant UI fixtures and its singleton monkeypatch.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def hub_offscreen_fonts(qapp):
    """Windows' offscreen plugin needs a font to measure real text, not tofu boxes."""
    from PySide6.QtGui import QFont, QFontDatabase

    font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "segoeui.ttf"
    if not QFontDatabase.families() and font_path.is_file():
        QFontDatabase.addApplicationFont(str(font_path))
        qapp.setFont(QFont("Segoe UI", 9))


@pytest.fixture(autouse=True)
def isolated_hub_instance_lock(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    """Give each UI test a private mutex identity.

    The shipped application must keep one fixed per-user mutex so two Hub
    processes cannot run together.  Tests use temporary data directories but
    still call the real lifecycle, and therefore need a test-only identity to
    coexist with a developer's running Hub and with one another.
    """

    from personal_hub import lifecycle as lifecycle_module

    digest = hashlib.sha256(request.node.nodeid.encode("utf-8")).hexdigest()[:24]
    test_mutex_name = f"PersonalHub.Test.{digest}"
    acquire = lifecycle_module.acquire_single_instance

    def acquire_isolated(
        settings=None,
        *,
        name=None,
        mutex_name=None,
        lock_path=None,
    ):
        if name is None and mutex_name is None:
            mutex_name = test_mutex_name
        return acquire(
            settings,
            name=name,
            mutex_name=mutex_name,
            lock_path=lock_path,
        )

    monkeypatch.setattr(lifecycle_module, "acquire_single_instance", acquire_isolated)
