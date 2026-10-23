"""Source-tree shim for running ``python -m personal_hub.app`` from the repo root."""

from __future__ import annotations

from pathlib import Path
from tomllib import load


_project_root = Path(__file__).resolve().parent.parent
_source_package = _project_root / "src" / "personal_hub"
__path__[:] = [str(_source_package)]
__file__ = str(_source_package / "__init__.py")

with (_project_root / "pyproject.toml").open("rb") as _project_file:
    __version__ = load(_project_file)["project"]["version"]

__all__ = ["__version__"]
