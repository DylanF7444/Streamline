"""Lazy, isolated MapLibre renderer. Chromium has no direct network access."""

from __future__ import annotations

import base64
import json
import math
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from threading import Event

from PySide6.QtCore import QObject, Qt, QUrl, Signal, Slot
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import (
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineSettings,
    QWebEngineUrlRequestInterceptor,
)
from PySide6.QtWebEngineWidgets import QWebEngineView

from personal_hub.places.contracts import MapPosition
from personal_hub.places.network import MapResourceClient, validate_resource_url

ASSETS = Path(__file__).with_name("map_assets").resolve()


class _LocalOnly(QWebEngineUrlRequestInterceptor):
    def interceptRequest(self, info) -> None:
        url = info.requestUrl()
        allowed = (
            url.scheme() in {"data", "blob"}
            or url.toString() == "qrc:///qtwebchannel/qwebchannel.js"
        )
        if url.isLocalFile():
            path = Path(url.toLocalFile()).resolve()
            allowed = path.parent == ASSETS and path.name in {
                "index.html",
                "map.js",
                "maplibre-gl.js",
                "maplibre-gl.css",
            }
        info.block(not allowed)


class _Page(QWebEnginePage):
    def acceptNavigationRequest(self, url, navigation_type, is_main_frame):
        return is_main_frame and url == QUrl.fromLocalFile(str(ASSETS / "index.html"))

    def javaScriptConsoleMessage(self, level, message, line, source):
        # Provider/style content and saved coordinates never enter logs.
        pass


class _Bridge(QObject):
    resourceReady = Signal(int, str, str)
    completed = Signal(int, str, str)
    picked = Signal(float, float)
    selected = Signal(str)
    position = Signal(object)
    ready = Signal()
    status = Signal(str)

    def __init__(self, parent, allowed):
        super().__init__(parent)
        self.stopped = Event()
        self.client = MapResourceClient(allowed=lambda: not self.stopped.is_set() and allowed())
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="hub-map")
        self.pending = {}
        self.completed.connect(self._complete)

    @Slot(int, str)
    def requestResource(self, request_id: int, url: str) -> None:
        if self.stopped.is_set() or request_id < 0 or request_id in self.pending:
            return
        if len(self.pending) >= 64:
            self.resourceReady.emit(request_id, "", "Map is busy. Try Reload map.")
            return
        try:
            validate_resource_url(url)
        except ValueError:
            self.resourceReady.emit(request_id, "", "Map resource address is not allowed.")
            return
        except RuntimeError:
            self.resourceReady.emit(request_id, "", "Map resource address is not allowed.")
            return

        def fetch():
            try:
                body = self.client.get(url, cancelled=lambda: request_id not in self.pending)
                data, error = base64.b64encode(body).decode("ascii"), ""
            except Exception:
                data, error = "", "Map provider is unavailable. Try Reload map."
            with suppress(RuntimeError):
                self.completed.emit(request_id, data, error)

        # Register before submitting so a quick cache hit cannot race cancellation.
        self.pending[request_id] = None
        self.pending[request_id] = self.pool.submit(fetch)

    @Slot(int)
    def cancelResource(self, request_id: int) -> None:
        future = self.pending.pop(request_id, None)
        if future is not None:
            future.cancel()

    @Slot(int, str, str)
    def _complete(self, request_id, data, error):
        if request_id in self.pending:
            self.pending.pop(request_id)
            if not self.stopped.is_set():
                self.resourceReady.emit(request_id, data, error)

    @Slot(float, float)
    def pick(self, latitude, longitude):
        if (
            not self.stopped.is_set()
            and math.isfinite(latitude)
            and math.isfinite(longitude)
            and -90 <= latitude <= 90
            and -180 <= longitude <= 180
        ):
            self.picked.emit(latitude, longitude)

    @Slot(str)
    def select(self, identifier):
        if not self.stopped.is_set() and len(identifier) == 36:
            self.selected.emit(identifier)

    @Slot(str)
    def moved(self, value):
        if self.stopped.is_set() or len(value) > 250:
            return
        with suppress(ValueError):
            self.position.emit(MapPosition.model_validate_json(value))

    @Slot()
    def loaded(self):
        if not self.stopped.is_set():
            self.ready.emit()

    @Slot(bool)
    def mapStatus(self, ready):
        if not self.stopped.is_set():
            self.status.emit(
                "Online map ready." if ready else "Map could not finish loading. Try Reload map."
            )

    def close(self):
        self.stopped.set()
        for future in tuple(self.pending.values()):
            if future is not None:
                future.cancel()
        self.pending.clear()
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.client.clear()


class MapBrowser(QWebEngineView):
    picked = Signal(float, float)
    selected = Signal(str)
    position_changed = Signal(object)
    status_changed = Signal(str)

    def __init__(self, parent, *, allowed, position, features):
        super().__init__(parent)
        self.setAccessibleName("World map. Pan and zoom; click to add a place.")
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.profile = QWebEngineProfile(self)  # Off-the-record; no disk history or cookies.
        self.interceptor = _LocalOnly(self.profile)
        self.profile.setUrlRequestInterceptor(self.interceptor)
        self.map_page = _Page(self.profile, self)
        self.setPage(self.map_page)
        settings = self.settings()
        for attribute in (
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls,
            QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows,
            QWebEngineSettings.WebAttribute.LocalStorageEnabled,
            QWebEngineSettings.WebAttribute.HyperlinkAuditingEnabled,
        ):
            settings.setAttribute(attribute, False)
        self.bridge = _Bridge(self, allowed)
        self.channel = QWebChannel(self.map_page)
        self.channel.registerObject("hub", self.bridge)
        self.map_page.setWebChannel(self.channel)
        self.bridge.picked.connect(self.picked)
        self.bridge.selected.connect(self.selected)
        self.bridge.position.connect(self.position_changed)
        self.bridge.status.connect(self.status_changed)
        self.bridge.ready.connect(self._ready)
        self.loadFinished.connect(self._load_finished)
        self.renderProcessTerminated.connect(lambda *_: self.bridge.close())
        self._position = position
        self._features = features
        self._loaded = False
        self.load(QUrl.fromLocalFile(str(ASSETS / "index.html")))

    def _load_finished(self, ok):
        if not ok and not self.bridge.stopped.is_set():
            self.status_changed.emit("Map renderer files could not be loaded. Try Reload map.")

    def _ready(self):
        if self._loaded:
            return
        self._loaded = True
        self.map_page.runJavaScript(
            "window.startMap(" + json.dumps(self._position.model_dump()) + ");"
        )
        self.set_features(self._features)

    def set_features(self, features):
        self._features = features
        if self._loaded:
            self.map_page.runJavaScript("window.setPlaces(" + json.dumps(features) + ");")

    def go_to(self, position):
        self._position = position
        if self._loaded:
            self.map_page.runJavaScript("window.goTo(" + json.dumps(position.model_dump()) + ");")

    def shutdown(self):
        self.bridge.close()
        self.stop()
        self.hide()
        # The page must be destroyed before its off-the-record profile.
        self.map_page.deleteLater()
