# -*- mode: python ; coding: utf-8 -*-
"""Personal Hub one-file target with an explicit Assistant-stack exclusion boundary."""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules
from PyInstaller.utils.hooks.qt import pyside6_library_info

hub_source = Path(SPECPATH).parent / "src"
sys.path.insert(0, str(hub_source))
hub_hiddenimports = collect_submodules("personal_hub")
qt_plugin_binaries = []
for plugin_type in ("imageformats", "platforms", "styles", "tls"):
    qt_plugin_binaries += pyside6_library_info.collect_plugins(plugin_type)

hub_map_assets = hub_source / "personal_hub" / "ui" / "desktop" / "map_assets"
# Editable-install package finders can misreport a src-layout data directory.
# Collect these pinned runtime assets from the spec's own source tree instead.
hub_datas = collect_data_files("certifi") + collect_data_files("tzdata") + [
    (str(path), "personal_hub/ui/desktop/map_assets")
    for path in hub_map_assets.iterdir() if path.is_file()
]

a = Analysis(
    [str(hub_source / "personal_hub" / "app.py")],
    pathex=[str(hub_source)],
    binaries=qt_plugin_binaries + pyside6_library_info.collect_extra_binaries(),
    datas=hub_datas,
    hiddenimports=hub_hiddenimports + ["PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets"],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "assistant",
        "assistant.audio",
        "assistant.language",
        "assistant.models",
        "assistant.orchestration",
        "assistant.router",
        "assistant.stt",
        "assistant.tts",
        "assistant.ui",
        # This app uses QWidget WebEngine, not QML/QtQuick. Retain linked Qt
        # DLLs through binary dependency analysis without collecting QML plugins.
        "PySide6.QtQml",
        "PySide6.QtQuick",
        "PySide6.QtQuickWidgets",
        "PySide6.QtWebEngineQuick",
        "ctranslate2",
        "faster_whisper",
        "keyboard",
        "hypothesis",
        "mypy",
        "numpy",
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
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="PersonalHub",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
