"""Prove the built Personal Hub archive excludes Assistant/model/audio stacks."""

from __future__ import annotations

import argparse
import ast
import json
import re
from collections.abc import Iterable
from pathlib import Path

from PyInstaller.archive.readers import CArchiveReader

FORBIDDEN_PREFIXES = (
    "assistant",
    "ctranslate2",
    "faster_whisper",
    "hypothesis",
    "keyboard",
    "mypy",
    "onnxruntime",
    "openwakeword",
    "PIL",
    "playwright",
    "pre_commit",
    "psycopg2",
    "pytest",
    "pytest_asyncio",
    "pytesseract",
    "qasync",
    "ruff",
    "sounddevice",
    "torch",
    "transformers",
)
REQUIRED_PREFIXES = ("personal_hub", "PySide6")


def _entry_names(value: object) -> set[str]:
    names: set[str] = set()
    if isinstance(value, (tuple, list)):
        # PyInstaller TOC entries are three-tuples of logical name, source path, and
        # type code.  Analysis-00.toc also contains lists of hidden imports,
        # excluded imports, and search paths; treating arbitrary two-string lists as
        # entries makes an explicitly excluded module look packaged.
        if (
            isinstance(value, tuple)
            and len(value) >= 3
            and isinstance(value[0], str)
            and isinstance(value[1], str)
            and isinstance(value[2], str)
        ):
            names.add(value[0])
        else:
            for item in value:
                names.update(_entry_names(item))
    elif isinstance(value, dict):
        names.update(str(item) for item in value)
        for item in value.values():
            names.update(_entry_names(item))
    return names


def analysis_names(path: Path) -> set[str]:
    try:
        value = ast.literal_eval(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError) as error:
        raise RuntimeError("Personal Hub PyInstaller analysis could not be parsed") from error
    return _entry_names(value)


def archive_names(path: Path) -> set[str]:
    archive = CArchiveReader(str(path.resolve(strict=True)))
    names = set(archive.toc)
    for name, entry in archive.toc.items():
        if entry[-1] != "z":
            continue
        names.update(archive.open_embedded_archive(name).toc)
    return names


def _matches(name: str, prefix: str) -> bool:
    normalized = name.replace("\\", "/").casefold()
    selected = prefix.casefold()
    module = re.split(r"[/]", normalized)[-1]
    return (
        normalized == selected
        or normalized.startswith(selected + ".")
        or module == selected
        or module.startswith(selected + ".")
        or module.startswith(selected + "-")
    )


def verify_names(names: Iterable[str]) -> dict[str, object]:
    selected = tuple(sorted(set(names)))
    forbidden = tuple(
        name for name in selected if any(_matches(name, prefix) for prefix in FORBIDDEN_PREFIXES)
    )
    missing = tuple(
        prefix
        for prefix in REQUIRED_PREFIXES
        if not any(_matches(name, prefix) for name in selected)
    )
    return {
        "entry_count": len(selected),
        "forbidden_entries": forbidden,
        "missing_required_prefixes": missing,
        "passes": not forbidden and not missing,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("--analysis", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    names = archive_names(args.executable)
    analysis_report: dict[str, object] | None = None
    if args.analysis is not None:
        analysis_report = verify_names(analysis_names(args.analysis))
    report = {
        "executable": args.executable.name,
        "archive": verify_names(names),
        "analysis": analysis_report,
    }
    report["passes"] = bool(report["archive"]["passes"]) and (
        analysis_report is None or bool(analysis_report["passes"])
    )
    rendered = json.dumps(report, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["passes"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
