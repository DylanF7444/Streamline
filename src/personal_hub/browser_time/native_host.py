"""Native messaging stdio entry point. Never print payloads or errors."""

import json
import struct
import sys

from personal_hub.browser_time.bridge import receive
from personal_hub.config import HubSettings, default_personal_hub_data_dir


def read_message(stream):
    header = stream.read(4)
    if not header:
        return None
    if len(header) != 4:
        raise ValueError("Truncated message")
    size = struct.unpack("<I", header)[0]
    if not 0 < size <= 2048:
        raise ValueError("Invalid message size")
    data = stream.read(size)
    if len(data) != size:
        raise ValueError("Truncated message")
    return json.loads(data)


def serve(input_stream, output_stream, root, browser, *, authorized=None):
    while (message := read_message(input_stream)) is not None:
        if authorized is not None and not authorized():
            return
        try:
            if not isinstance(message, dict) or message.get("browser") not in browser:
                raise ValueError("Browser identity mismatch")
            response = receive(root, message)
        except (OSError, ValueError, KeyError, TypeError):
            response = {"recording": False}
        encoded = json.dumps(response).encode("utf-8")
        output_stream.write(struct.pack("<I", len(encoded)) + encoded)
        output_stream.flush()


def main():
    try:
        from personal_hub.browser_time.bridge import read_json

        config = read_json(default_personal_hub_data_dir() / "browser-native-host.json")
        origin = sys.argv[1] if len(sys.argv) > 1 else ""
        browser = config["origins"][origin]
        settings = HubSettings(data_dir=config["data_dir"])
        if sys.platform == "win32":
            import msvcrt
            import os

            msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
            msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)

        def authorized():
            try:
                current = read_json(default_personal_hub_data_dir() / "browser-native-host.json")
                return current["data_dir"] == config["data_dir"] and set(browser).issubset(
                    current["origins"].get(origin, [])
                )
            except (OSError, ValueError, KeyError, TypeError):
                return False

        serve(
            sys.stdin.buffer,
            sys.stdout.buffer,
            settings.data_dir / "browser-time-live",
            browser,
            authorized=authorized,
        )
    except (OSError, ValueError, KeyError, TypeError):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
