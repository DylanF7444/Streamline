# -*- mode: python ; coding: utf-8 -*-
"""Personal Hub one-file target with an explicit Assistant-stack exclusion boundary."""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules
from PyInstaller.utils.hooks.qt import pyside6_library_info

hub_hiddenimports = collect_submodules("personal_hub")
qt_plugin_binaries = []
for plugin_type in ("imageformats", "platforms", "styles", "tls"):
    qt_plugin_binaries += pyside6_library_info.collect_plugins(plugin_type)

hub_datas = collect_data_files("certifi") + collect_data_files("tzdata")

a = Analysis(
    ["../src/personal_hub/app.py"],
    pathex=["../src"],
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
