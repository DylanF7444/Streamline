"""Renderer isolation and its bounded resource bridge, without real network or WebGL."""

from pathlib import Path

import pytest
from PySide6.QtCore import QUrl

from personal_hub.places.network import STYLE_URL

pytestmark = pytest.mark.ui


def test_browser_interceptor_only_allows_bundled_files_and_inert_resources(qapp):
    from personal_hub.ui.desktop.map_browser import ASSETS, _LocalOnly

    class Request:
        def __init__(self, url):
            self.url = QUrl(url)
            self.blocked = None

        def requestUrl(self):
            return self.url

        def block(self, blocked):
            self.blocked = blocked

    interceptor = _LocalOnly(qapp)
    for url, expected in (
        (QUrl.fromLocalFile(str(ASSETS / "index.html")).toString(), False),
        (QUrl.fromLocalFile(str(ASSETS / "map.js")).toString(), False),
        ("qrc:///qtwebchannel/qwebchannel.js", False),
        ("data:image/png;base64,AAAA", False),
        (STYLE_URL, True),
        ("https://127.0.0.1/", True),
        (QUrl.fromLocalFile(str(Path.home() / "private.txt")).toString(), True),
        (QUrl.fromLocalFile(str(ASSETS / "not-bundled.js")).toString(), True),
    ):
        request = Request(url)
        interceptor.interceptRequest(request)
        assert request.blocked is expected


def test_resource_bridge_cancellation_shutdown_and_typed_events(qapp, qtbot, monkeypatch):
    import base64

    from personal_hub.ui.desktop.map_browser import _Bridge

    bridge = _Bridge(qapp, lambda: True)
    calls, results, picks, positions = [], [], [], []
    monkeypatch.setattr(bridge.client, "get", lambda url, **_: calls.append(url) or b"map bytes")
    bridge.resourceReady.connect(lambda *args: results.append(args))
    bridge.picked.connect(lambda *args: picks.append(args))
    bridge.position.connect(positions.append)
    try:
        bridge.requestResource(1, STYLE_URL)
        qtbot.waitUntil(lambda: bool(results), timeout=2000)
        assert results == [(1, base64.b64encode(b"map bytes").decode(), "")]
        bridge.requestResource(2, "https://localhost/private")
        assert len(calls) == 1 and results[-1][0] == 2 and results[-1][2]
        bridge.pick(float("nan"), 0)
        bridge.pick(91, 0)
        bridge.pick(41, -87)
        assert picks == [(41, -87)]
        bridge.moved('{"latitude":0,"longitude":0,"zoom":100}')
        bridge.moved('{"latitude":41,"longitude":-87,"zoom":12}')
        assert len(positions) == 1 and positions[0].zoom == 12
        bridge.close()
        bridge.requestResource(3, STYLE_URL)
        bridge.pick(0, 0)
        assert len(calls) == 1 and len(picks) == 1
    finally:
        bridge.close()
        bridge.deleteLater()
