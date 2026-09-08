"""Validate the standalone Personal Hub documentation set and repository paths."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_DOCUMENTS = (
    "PERSONAL_HUB_IMPLEMENTATION_PLAN.md",
    "PERSONAL_HUB_IMPLEMENTATION_STATUS.md",
    "PERSONAL_HUB_USER_GUIDE.md",
    "PERSONAL_HUB_PRIVACY.md",
    "PERSONAL_HUB_RELEASE_RUNBOOK.md",
    "PERSONAL_HUB_ACCESSIBILITY_REVIEW.md",
    "PERSONAL_HUB_RELEASE_EVIDENCE.md",
    "PERSONAL_HUB_REPOSITORY_MIGRATION.md",
)


def main() -> int:
    missing = [name for name in REQUIRED_DOCUMENTS if not (ROOT / "docs" / name).is_file()]
    if missing:
        raise SystemExit("Missing required Personal Hub documentation: " + ", ".join(missing))
    required_paths = (
        ROOT / "src" / "personal_hub" / "app.py",
        ROOT / "tests" / "test_boundaries.py",
        ROOT / "packaging" / "personal-hub.spec",
        ROOT / "scripts" / "test_personal_hub.ps1",
    )
    absent = [str(path.relative_to(ROOT)) for path in required_paths if not path.is_file()]
    if absent:
        raise SystemExit("Standalone repository paths are missing: " + ", ".join(absent))
    print("Personal Hub documentation and repository paths are consistent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
