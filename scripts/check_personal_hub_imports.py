"""Measure and enforce the Personal Hub Phase 0 runtime import boundary."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
FORBIDDEN_PREFIXES = (
    "assistant.audio",
    "assistant.language",
    "assistant.models",
    "assistant.orchestration",
    "assistant.orchestrator",
    "assistant.router",
    "assistant.stt",
    "assistant.tts",
    "assistant.ui",
)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check the Personal Hub startup dependency boundary"
    )
    parser.add_argument("--output", type=Path, help="Optional JSON report path")
    return parser.parse_args(argv)


def _matches_prefix(module_name: str, prefix: str) -> bool:
    return module_name == prefix or module_name.startswith(prefix + ".")


def build_report() -> dict[str, object]:
    if str(SOURCE_ROOT) not in sys.path:
        sys.path.insert(0, str(SOURCE_ROOT))

    from personal_hub import app, bootstrap
    from personal_hub.config import HubSettings

    with tempfile.TemporaryDirectory(prefix="personal-hub-import-check-") as directory:
        container = bootstrap.build_hub_container(HubSettings(data_dir=Path(directory)))
        container.close()

    loaded = tuple(sorted(sys.modules))
    assistant_modules = tuple(name for name in loaded if _matches_prefix(name, "assistant"))
    forbidden_modules = tuple(
        name
        for name in loaded
        if any(_matches_prefix(name, prefix) for prefix in FORBIDDEN_PREFIXES)
    )
    qt_modules = tuple(name for name in loaded if _matches_prefix(name, "PySide6"))
    return {
        "application_entry_point": f"{app.__name__}:main",
        "composition_root": bootstrap.__name__,
        "assistant_modules": assistant_modules,
        "forbidden_assistant_modules": forbidden_modules,
        "qt_modules_before_desktop_launch": qt_modules,
        "forbidden_prefixes": FORBIDDEN_PREFIXES,
        "passes": not assistant_modules and not forbidden_modules and not qt_modules,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    report = build_report()
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["passes"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
