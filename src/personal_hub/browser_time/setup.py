"""Explicit per-user Chrome/Edge/Brave native-host registration."""

import re
import sys
from contextlib import suppress
from pathlib import Path

from personal_hub.browser_time.bridge import read_json, write_json
from personal_hub.config import HubSettings, default_personal_hub_data_dir

HOST_NAME = "com.personalhub.browser_time"
REGISTRY = {
    "chrome": r"Software\Google\Chrome\NativeMessagingHosts",
    "edge": r"Software\Microsoft\Edge\NativeMessagingHosts",
    "brave": r"Software\BraveSoftware\Brave-Browser\NativeMessagingHosts",
}


def register(browser, extension_id, host_path, data_dir):
    if sys.platform != "win32":
        raise ValueError("Browser tracking setup requires Windows")
    if browser not in REGISTRY or not re.fullmatch("[a-p]{32}", extension_id):
        raise ValueError("Use the 32-letter extension ID shown by the browser")
    host_path = Path(host_path).resolve(strict=True)
    if host_path.suffix.lower() != ".exe":
        raise ValueError("Choose PersonalHubBrowserHost.exe")
    settings = HubSettings(data_dir=data_dir)
    root = default_personal_hub_data_dir()
    try:
        config = read_json(root / "browser-native-host.json")
    except FileNotFoundError:
        config = {"origins": {}}
    if config.get("data_dir") != str(settings.data_dir):
        config = {"origins": {}}
    config["data_dir"] = str(settings.data_dir)
    origin = f"chrome-extension://{extension_id}/"
    config["origins"][origin] = sorted(set(config["origins"].get(origin, [])) | {browser})
    if len(config["origins"]) > 16:
        raise ValueError("Disconnect the helper before registering more extensions")
    manifest_path = root / "browser-native-manifest.json"
    write_json(root / "browser-native-host.json", config)
    write_json(
        manifest_path,
        {
            "name": HOST_NAME,
            "description": "Personal Hub local browser screen time",
            "path": str(host_path),
            "type": "stdio",
            "allowed_origins": list(config["origins"]),
        },
    )
    import winreg

    bases = [REGISTRY[browser]]
    if browser == "brave":
        # Brave inherits Chromium's Windows Chrome-key lookup. Keep its branded
        # key too for compatibility; this does not add origins or permissions.
        bases.append(REGISTRY["chrome"])
    for base in bases:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + "\\" + HOST_NAME) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(manifest_path))


def unregister():
    if sys.platform == "win32":
        import winreg

        for base in REGISTRY.values():
            with suppress(FileNotFoundError):
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, base + "\\" + HOST_NAME)
    root = default_personal_hub_data_dir()
    for name in ("browser-native-host.json", "browser-native-manifest.json"):
        (root / name).unlink(missing_ok=True)
