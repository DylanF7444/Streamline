"""Command-line and desktop entry point for Personal Hub."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path

from personal_hub.bootstrap import HubContainer, build_hub_container
from personal_hub.config import HUB_APPLICATION_ID, HubSettings


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Personal Hub local attention dashboard")
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="Use an isolated Personal Hub data directory for this process.",
    )
    return parser.parse_args(argv)


def _register_windows_app_identity() -> None:
    if sys.platform != "win32":
        return
    with suppress(Exception):
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(HUB_APPLICATION_ID)


def _run_desktop(container: HubContainer) -> int:
    """Load Qt only after the Hub lock, database, and services are ready."""

    from personal_hub.ui.desktop.application import run_hub_desktop

    return run_hub_desktop(container)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    settings = HubSettings(data_dir=args.data_dir) if args.data_dir is not None else HubSettings()
    container = build_hub_container(settings, acquire_instance_lock=True)
    try:
        _register_windows_app_identity()
        # Qt remains optional for non-desktop imports and is loaded only after the Hub-only
        # database and process identity have been established.
        return _run_desktop(container)
    finally:
        container.close()


if __name__ == "__main__":
    raise SystemExit(main())
