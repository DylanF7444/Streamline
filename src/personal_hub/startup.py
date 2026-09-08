"""Personal Hub-owned per-user Windows startup registration."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Protocol

from personal_hub.config import HUB_STARTUP_ENTRY_NAME


class StartupRegistration(Protocol):
    def enabled(self) -> bool: ...

    def set_enabled(self, enabled: bool) -> None: ...


class WindowsStartupRegistration:
    """Manage only Personal Hub's exact HKCU Run value."""

    _RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

    def __init__(
        self,
        executable: Path | None = None,
        *,
        arguments: tuple[str, ...] | None = None,
        value_name: str = HUB_STARTUP_ENTRY_NAME,
    ) -> None:
        selected_executable = executable or Path(sys.executable)
        self.executable = selected_executable.resolve()
        default_arguments = (
            ()
            if executable is not None or getattr(sys, "frozen", False)
            else (
                "-m",
                "personal_hub.app",
            )
        )
        self.arguments = default_arguments if arguments is None else tuple(arguments)
        if any(
            not value or any(character in value for character in '"\r\n')
            for value in self.arguments
        ):
            raise ValueError("Startup arguments must be bounded plain values")
        self.value_name = value_name

    @property
    def command(self) -> str:
        suffix = "" if not self.arguments else " " + " ".join(self.arguments)
        return f'"{self.executable}"{suffix}'

    def enabled(self) -> bool:
        if sys.platform != "win32":
            return False
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._RUN_KEY) as key:
                value, _ = winreg.QueryValueEx(key, self.value_name)
        except FileNotFoundError:
            return False
        return str(value) == self.command

    def set_enabled(self, enabled: bool) -> None:
        if sys.platform != "win32":
            if enabled:
                raise RuntimeError("Start-at-login is available only on Windows")
            return
        import winreg

        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            self._RUN_KEY,
            access=winreg.KEY_READ | winreg.KEY_WRITE,
        ) as key:
            if enabled:
                winreg.SetValueEx(key, self.value_name, 0, winreg.REG_SZ, self.command)
                return
            try:
                winreg.DeleteValue(key, self.value_name)
            except FileNotFoundError:
                return


__all__ = ["StartupRegistration", "WindowsStartupRegistration"]
