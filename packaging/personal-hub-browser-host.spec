# -*- mode: python ; coding: utf-8 -*-
"""Small console executable required for native messaging's binary stdio."""
from pathlib import Path

hub_source = Path(SPECPATH).parent / "src"
a = Analysis(
    [str(hub_source / "personal_hub" / "browser_time" / "native_host.py")],
    pathex=[str(hub_source)], binaries=[], datas=[], hiddenimports=[],
    hookspath=[], runtime_hooks=[],
    excludes=["assistant", "PySide6", "pytest", "numpy", "torch"], noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="PersonalHubBrowserHost",
          debug=False, strip=False, upx=False, console=True)
