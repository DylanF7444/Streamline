from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
HUB_ROOT = SOURCE_ROOT / "personal_hub"

# These modules are deliberately listed by architectural responsibility.  The
# Hub may eventually use a reviewed, Hub-owned adapter around a neutral helper,
# but its Phase 0 composition root must not import any of these assistant-only
# runtime boundaries.
FORBIDDEN_ASSISTANT_RUNTIME_PREFIXES = ("assistant",)


def _imports_from(path: Path) -> set[str]:
    """Return direct and literal dynamic imports from one Python module."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
            imports.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # ``from . import personal_hub`` has no module on the AST node,
            # but the imported alias still identifies the forbidden package.
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Call):
            # Catch the common lazy-import form without trying to evaluate
            # arbitrary source code.  Non-literal module names are outside
            # the scope of this static boundary check.
            function = node.func
            is_import_module = (
                isinstance(function, ast.Name) and function.id in {"import_module", "__import__"}
            ) or (
                isinstance(function, ast.Attribute)
                and function.attr == "import_module"
                and isinstance(function.value, ast.Name)
                and function.value.id == "importlib"
            )
            if is_import_module and node.args:
                module_name = node.args[0]
                if isinstance(module_name, ast.Constant) and isinstance(module_name.value, str):
                    imports.add(module_name.value)
    return imports


def _assert_source_tree_exists(path: Path, description: str) -> None:
    assert path.is_dir(), f"{description} is missing: {path}"


def _run_probe(script: str, *arguments: str) -> dict[str, object]:
    """Run an import probe in a clean interpreter and decode its result."""

    environment = os.environ.copy()
    source_path = str(SOURCE_ROOT)
    existing_python_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_path if not existing_python_path else source_path + os.pathsep + existing_python_path
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, *arguments],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, (
        f"clean Hub probe failed\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    result_lines = [line for line in completed.stdout.splitlines() if line.startswith("RESULT=")]
    assert result_lines, f"Hub probe did not emit a result: {completed.stdout!r}"
    return json.loads(result_lines[-1][len("RESULT=") :])


def test_hub_source_never_imports_local_assistant():
    _assert_source_tree_exists(HUB_ROOT, "Personal Hub source package")
    violations: list[str] = []
    for path in HUB_ROOT.rglob("*.py"):
        for imported in _imports_from(path):
            if imported == "assistant" or imported.startswith("assistant."):
                violations.append(f"{path.relative_to(HUB_ROOT)} -> {imported}")

    assert violations == []


def test_standalone_project_metadata_exposes_only_personal_hub():
    project = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    assert project["name"] == "personal-hub"
    assert project["scripts"] == {"personal-hub": "personal_hub.app:main"}
    dependency_text = "\n".join(project["dependencies"]).casefold()
    assert "local-assistant" not in dependency_text
    assert "assistant" not in dependency_text
    assert not (SOURCE_ROOT / "assistant").exists()


def test_hub_ui_never_imports_local_assistant():
    _assert_source_tree_exists(HUB_ROOT, "Personal Hub source package")
    ui_files = [
        path
        for path in HUB_ROOT.rglob("*.py")
        if "ui" in path.relative_to(HUB_ROOT).parts or path.name in {"ui.py", "desktop.py"}
    ]
    assert ui_files, f"Personal Hub UI package is missing under {HUB_ROOT}"

    violations: list[str] = []
    for path in ui_files:
        for imported in _imports_from(path):
            if imported == "assistant" or imported.startswith("assistant."):
                violations.append(f"{path.relative_to(HUB_ROOT)} -> {imported}")

    assert violations == []


def test_importing_and_bootstrapping_hub_does_not_load_assistant_runtime():
    probe = r"""
import json
import sys
import tempfile
from pathlib import Path

from personal_hub import app as hub_app
from personal_hub import bootstrap as hub_bootstrap
from personal_hub.config import HubSettings

forbidden = ("assistant",)

def loaded_forbidden():
    return sorted(
        name
        for name in sys.modules
        if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
    )

after_import = loaded_forbidden()
with tempfile.TemporaryDirectory(prefix="personal-hub-probe-") as directory:
    settings = HubSettings(data_dir=Path(directory))
    container = hub_bootstrap.build_hub_container(settings)
    after_bootstrap = loaded_forbidden()
    close = getattr(container, "close", None)
    if callable(close):
        close()

print("RESULT=" + json.dumps({
    "app_imported": hub_app.__name__,
    "bootstrap_imported": hub_bootstrap.__name__,
    "has_main": callable(getattr(hub_app, "main", None)),
    "forbidden_after_import": after_import,
    "forbidden_after_bootstrap": after_bootstrap,
}))
"""
    result = _run_probe(probe)

    assert result["app_imported"] == "personal_hub.app"
    assert result["bootstrap_imported"] == "personal_hub.bootstrap"
    assert result["has_main"] is True
    assert result["forbidden_after_import"] == []
    assert result["forbidden_after_bootstrap"] == []


def test_hub_import_probe_is_independent_of_optional_qt():
    """The non-UI composition root is importable without a Qt installation."""

    probe = r"""
import json
from personal_hub import bootstrap
from personal_hub.config import HubSettings

print("RESULT=" + json.dumps({
    "settings": HubSettings.__name__,
    "bootstrap": bootstrap.__name__,
}))
"""
    result = _run_probe(probe)
    assert result == {"settings": "HubSettings", "bootstrap": "personal_hub.bootstrap"}
