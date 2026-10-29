"""Accessible native place/visit controls surrounding a lazy online world map."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from PySide6.QtCore import QDate, QSignalBlocker, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from personal_hub.places.contracts import MapPosition, Place, Visit
from personal_hub.ui.desktop.status_label import StatusLabel


def label(text=""):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    return widget


def coordinate(name, minimum, maximum, value=0):
    widget = QDoubleSpinBox()
    widget.setRange(minimum, maximum)
    widget.setDecimals(6)
    widget.setValue(value)
    widget.setAccessibleName(name)
    return widget


class PlaceDialog(QDialog):
    def __init__(self, page, latitude, longitude, place=None):
        super().__init__(page)
        self.page = page
        self.place = place
        self.setWindowTitle("Edit place" if place else "Add place")
        self.resize(430, 470)
        layout = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        form = QFormLayout(body)
        self.name = QLineEdit(place.name if place else "")
        self.name.setMaxLength(160)
        self.name.setAccessibleName("Place name")
        form.addRow("Name", self.name)
        self.latitude = coordinate("Place latitude", -90, 90, latitude)
        self.longitude = coordinate("Place longitude", -180, 180, longitude)
        form.addRow("Latitude", self.latitude)
        form.addRow("Longitude", self.longitude)
        self.category = QComboBox()
        self.category.setEditable(True)
        self.category.addItems(["Other", "Study", "Library", "Food", "Building", "Outdoors"])
        self.category.lineEdit().setMaxLength(64)
        self.category.setCurrentText(place.category if place else "Other")
        self.category.setAccessibleName("Place category")
        form.addRow("Category", self.category)
        self.notes = QTextEdit()
        self.notes.setAcceptRichText(False)
        self.notes.setPlainText(place.notes if place else "")
        self.notes.setAccessibleName("Place notes, up to 4000 characters")
        form.addRow("Notes", self.notes)
        self.favorite = QCheckBox("Favorite")
        self.favorite.setChecked(place.favorite if place else False)
        form.addRow(self.favorite)
        scroll.setWidget(body)
        layout.addWidget(scroll)
        self.status = StatusLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def save(self):
        if self.page.private:
            return
        try:
            values = self.place.model_dump() if self.place else {}
            place = Place.model_validate(
                {
                    **values,
                    "name": self.name.text(),
                    "latitude": self.latitude.value(),
                    "longitude": self.longitude.value(),
                    "category": self.category.currentText(),
                    "notes": self.notes.toPlainText(),
                    "favorite": self.favorite.isChecked(),
                }
            )
        except ValueError:
            self.status.setText("Enter a name and category; keep notes within 4,000 characters.")
            return
        self.page.requested.emit("save_place", {"place": place, "dialog": self})


class VisitDialog(QDialog):
    def __init__(self, page, place):
        super().__init__(page)
        self.page, self.place = page, place
        self.setWindowTitle("Log visit")
        self.resize(400, 330)
        layout = QVBoxLayout(self)
        layout.addWidget(label(place.name))
        self.day = QDateEdit()
        self.day.setCalendarPopup(True)
        self.day.setDate(QDate(datetime.now(ZoneInfo(page.timezone)).date()))
        self.day.setAccessibleName("Visit date")
        layout.addWidget(self.day)
        self.notes = QTextEdit()
        self.notes.setAcceptRichText(False)
        self.notes.setAccessibleName("Visit notes, up to 2000 characters")
        self.notes.setPlaceholderText("Optional notes about this visit")
        layout.addWidget(self.notes)
        self.status = StatusLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def save(self):
        if self.page.private:
            return
        try:
            visit = Visit(
                place_id=self.place.place_id,
                visited_on=self.day.date().toPython(),
                notes=self.notes.toPlainText(),
            )
        except ValueError:
            self.status.setText("Keep visit notes within 2,000 characters.")
            return
        self.page.requested.emit("log_visit", {"visit": visit, "dialog": self})


class PlacesPage(QScrollArea):
    requested = Signal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("placesPage")
        self.setAccessibleName("Places and visit history")
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        self.private = False
        self.offline = True
        self.active = False
        self._visible = False
        self.configured = False
        self._starting_map = False
        self.timezone = "UTC"
        self.dialog = None
        self.browser = None
        self._allowed = lambda: False
        self.position = MapPosition()
        self.home_position = MapPosition()
        self.places = ()
        self.visits = ()
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(22, 18, 22, 18)
        heading = label("Places")
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)
        self.status = StatusLabel("Open Places while online to load the world map.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.content = QWidget()
        content = QVBoxLayout(self.content)
        content.setContentsMargins(0, 0, 0, 0)
        controls = self.controls_grid = QGridLayout()
        self.add_button = QPushButton("Add place")
        self.add_button.clicked.connect(
            lambda: self.add_place(self.position.latitude, self.position.longitude)
        )
        self.reload_button = QPushButton("Reload map")
        self.reload_button.clicked.connect(self.reload_map)
        self.world_button = QPushButton("World")
        self.world_button.clicked.connect(lambda: self.go_to(MapPosition()))
        self.home_button = QPushButton("Home view")
        self.home_button.clicked.connect(lambda: self.go_to(self.home_position))
        self.save_home_button = QPushButton("Set home view")
        self.save_home_button.clicked.connect(lambda: self.requested.emit("home", self.position))
        content.addLayout(controls)
        coordinates = QGridLayout()
        coordinates.addWidget(label("Latitude"), 0, 0)
        coordinates.addWidget(label("Longitude"), 0, 1)
        self.latitude = coordinate("Map latitude", -85.051129, 85.051129, 20)
        self.longitude = coordinate("Map longitude", -180, 180)
        coordinates.addWidget(self.latitude, 1, 0)
        coordinates.addWidget(self.longitude, 1, 1)
        self.go_button = QPushButton("Go")
        self.go_button.setAccessibleName("Go to latitude and longitude")
        self.go_button.clicked.connect(
            lambda: self.go_to(
                MapPosition(
                    latitude=self.latitude.value(), longitude=self.longitude.value(), zoom=15
                )
            )
        )
        coordinates.addWidget(self.go_button, 1, 2)
        content.addLayout(coordinates)
        self.map_panel = QWidget()
        self.map_panel.setMinimumHeight(230)
        self.map_layout = QVBoxLayout(self.map_panel)
        self.map_layout.setContentsMargins(0, 0, 0, 0)
        self.placeholder = QWidget()
        self.map_layout.addWidget(self.placeholder)
        content.addWidget(self.map_panel, 1)
        content.addWidget(label("OpenFreeMap · © OpenMapTiles · © OpenStreetMap contributors"))
        credits = QToolButton()
        credits.setText("Map credits")
        credits.setAccessibleName("Map credits and data licenses")
        credits.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(credits)
        for title, url in (
            ("OpenStreetMap contributors and license", "https://www.openstreetmap.org/copyright"),
            ("OpenMapTiles", "https://openmaptiles.org/"),
            ("OpenFreeMap", "https://openfreemap.org/"),
        ):
            menu.addAction(title).triggered.connect(
                lambda checked=False, target=url: QDesktopServices.openUrl(QUrl(target))
            )
        credits.setMenu(menu)
        content.addWidget(credits)
        content.addWidget(label("Green: visited · Orange: want to visit · Larger pin: favorite"))
        filters = self.filters_grid = QGridLayout()
        self.search = QLineEdit()
        self.search.setMaxLength(160)
        self.search.setPlaceholderText("Search saved places")
        self.search.setAccessibleName("Search saved places by name, category, or notes")
        self.search.textChanged.connect(self.render)
        self.category = QComboBox()
        self.category.setAccessibleName("Filter places by category")
        self.category.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.category.setMinimumContentsLength(10)
        self.category.addItem("All categories", "")
        self.category.currentIndexChanged.connect(self.render)
        self.state = QComboBox()
        self.state.setAccessibleName("Filter visited places, wanted places, or favorites")
        self.state.addItems(["All places", "Visited", "Want to visit", "Favorites"])
        self.state.currentIndexChanged.connect(self.render)
        content.addLayout(filters)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.items = QListWidget()
        self.items.setAccessibleName("Saved places")
        self.items.setMinimumHeight(170)
        self.items.currentItemChanged.connect(lambda *_: self.select_place(focus=True))
        self.splitter.addWidget(self.items)
        detail = QWidget()
        details = QVBoxLayout(detail)
        self.details = label("Save a place or select one to see its visit history.")
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        details.addWidget(self.details)
        actions = self.actions_grid = QGridLayout()
        self.visit_button = QPushButton("Log visit")
        self.visit_button.clicked.connect(self.log_visit)
        self.edit_button = QPushButton("Edit place")
        self.edit_button.clicked.connect(self.edit_place)
        self.delete_button = QPushButton("Delete place")
        self.delete_button.clicked.connect(self.delete_place)
        details.addLayout(actions)
        self.history = QListWidget()
        self.history.setAccessibleName("Visits to selected place, newest date first")
        self.history.setMinimumHeight(100)
        details.addWidget(self.history)
        self.remove_visit_button = QPushButton("Delete selected visit")
        self.remove_visit_button.clicked.connect(self.delete_visit)
        self.history.currentItemChanged.connect(
            lambda: self.remove_visit_button.setEnabled(self.history.currentItem() is not None)
        )
        details.addWidget(self.remove_visit_button)
        self.splitter.addWidget(detail)
        content.addWidget(self.splitter)
        layout.addWidget(self.content)
        self.setWidget(body)
        self.set_compact(False)
        self.render()

    def bind(self, allowed, position, home):
        self._allowed = allowed
        self.configured = True
        self.home_position = home
        self.go_to(position)

    def online_allowed(self):
        return (
            self.configured
            and self.active
            and self._visible
            and not self.private
            and not self.offline
            and self._allowed()
        )

    def set_state(self, *, active=None, offline=None, private=None):
        if active is not None:
            self.active = active
        if offline is not None:
            self.offline = offline
        if private is not None:
            self.private = private
        self.content.setVisible(not self.private)
        self.reload_button.setEnabled(not self.offline and not self.private)
        if self.private:
            if self.dialog:
                self.dialog.reject()
                self.dialog = None
            self.places, self.visits = (), ()
            with QSignalBlocker(self.category):
                self.category.clear()
                self.category.addItem("All categories", "")
            self.search.clear()
            self.render()
            self.details.clear()
            self.latitude.setValue(0)
            self.longitude.setValue(0)
            self.status.setText("Places are hidden for privacy.")
        elif self.offline:
            self.status.setText("Offline — saved places and visit history remain available.")
        if not self.online_allowed():
            self.stop_map()
        elif self.browser is None:
            self.start_map()
        if private is False and self.configured:
            self.latitude.setValue(self.position.latitude)
            self.longitude.setValue(self.position.longitude)
            self.requested.emit("refresh", None)

    def showEvent(self, event):
        self._visible = True
        self.set_state()
        super().showEvent(event)

    def hideEvent(self, event):
        self._visible = False
        self.set_state()
        super().hideEvent(event)

    def features(self):
        visited = self.visited_ids
        return [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [place.longitude, place.latitude]},
                "properties": {
                    "id": str(place.place_id),
                    "visited": place.place_id in visited,
                    "favorite": place.favorite,
                },
            }
            for place in self.filtered_places()
        ]

    def start_map(self):
        if self.browser is not None or self._starting_map or not self.online_allowed():
            return
        self.stop_map()
        self._starting_map = True
        try:
            from personal_hub.ui.desktop.map_browser import MapBrowser

            browser = MapBrowser(
                self.map_panel,
                allowed=self.online_allowed,
                position=self.position,
                features=self.features(),
            )
            if not self.online_allowed():
                browser.shutdown()
                browser.deleteLater()
                return
            self.browser = browser
        except (ImportError, RuntimeError):
            self.status.setText(
                "Map renderer unavailable. Install the desktop dependencies, then try Reload map."
            )
            return
        finally:
            self._starting_map = False
        self.placeholder.hide()
        self.map_layout.addWidget(self.browser)
        self.browser.picked.connect(self.add_place)
        self.browser.selected.connect(self.select_id)
        self.browser.position_changed.connect(self.position_changed)
        self.browser.status_changed.connect(self.status.setText)
        self.status.setText("Loading online map…")

    def stop_map(self):
        renderers = []
        if self.browser is not None:
            renderers.append(self.browser)
            self.browser = None
        for index in range(self.map_layout.count()):
            widget = self.map_layout.itemAt(index).widget()
            if widget is not None and widget is not self.placeholder and widget not in renderers:
                renderers.append(widget)
        for browser in renderers:
            self.map_layout.removeWidget(browser)
            browser.shutdown()
            browser.deleteLater()
        self.placeholder.show()

    def reload_map(self):
        self.stop_map()
        self.start_map()

    def position_changed(self, position):
        if not self.private:
            self.position = position
            self.latitude.setValue(position.latitude)
            self.longitude.setValue(position.longitude)
            self.requested.emit("position", position)

    def go_to(self, position):
        self.position_changed(position)
        if self.browser:
            self.browser.go_to(position)

    def set_records(self, places, visits, selected=None):
        self.places, self.visits = (
            (() if self.private else places),
            (() if self.private else visits),
        )
        category = self.category.currentData()
        with QSignalBlocker(self.category):
            self.category.clear()
            self.category.addItem("All categories", "")
            for name in sorted({place.category for place in self.places}):
                self.category.addItem(name, name)
            self.category.setCurrentIndex(max(0, self.category.findData(category)))
        self.render()
        if selected:
            self.select_id(str(selected))

    @property
    def visited_ids(self):
        return {visit.place_id for visit in self.visits}

    def filtered_places(self):
        term = self.search.text().casefold().strip()
        category, state, visited = (
            self.category.currentData(),
            self.state.currentIndex(),
            self.visited_ids,
        )
        return [
            place
            for place in self.places
            if (not term or term in f"{place.name} {place.category} {place.notes}".casefold())
            and (not category or category == place.category)
            and (state != 1 or place.place_id in visited)
            and (state != 2 or place.place_id not in visited)
            and (state != 3 or place.favorite)
        ]

    def render(self, *_):
        selected = self.current_place()
        visited = self.visited_ids
        with QSignalBlocker(self.items):
            self.items.clear()
            for place in self.filtered_places():
                state = "Visited" if place.place_id in visited else "Want to visit"
                item = QListWidgetItem(f"{'★ ' if place.favorite else ''}{place.name} · {state}")
                item.setData(Qt.ItemDataRole.UserRole, str(place.place_id))
                self.items.addItem(item)
                if selected and selected.place_id == place.place_id:
                    self.items.setCurrentItem(item)
        self.select_place()
        if self.browser:
            self.browser.set_features(self.features())

    def current_place(self):
        item = self.items.currentItem()
        identifier = item.data(Qt.ItemDataRole.UserRole) if item else None
        return next((place for place in self.places if str(place.place_id) == identifier), None)

    def select_id(self, identifier):
        for index in range(self.items.count()):
            item = self.items.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == identifier:
                self.items.setCurrentItem(item)
                self.items.scrollToItem(item)
                return

    def select_place(self, *, focus=False):
        place = self.current_place()
        for button in (self.visit_button, self.edit_button, self.delete_button):
            button.setEnabled(place is not None and not self.private)
        self.history.clear()
        self.remove_visit_button.setEnabled(False)
        if place is None:
            self.details.setText("Save a place or select one to see its visit history.")
            return
        visits = [visit for visit in self.visits if visit.place_id == place.place_id]
        self.details.setText(
            f"{place.name}\n{place.category} · {len(visits)} visits\n{place.latitude:.6f}, {place.longitude:.6f}\n{place.notes}"
        )
        for visit in visits:
            item = QListWidgetItem(f"{visit.visited_on.isoformat()}  {visit.notes}")
            item.setData(Qt.ItemDataRole.UserRole, str(visit.visit_id))
            self.history.addItem(item)
        if self.browser and focus:
            self.browser.go_to(
                MapPosition(
                    latitude=max(-85.051129, min(85.051129, place.latitude)),
                    longitude=place.longitude,
                    zoom=max(15, self.position.zoom),
                )
            )

    def add_place(self, latitude, longitude):
        if self.private:
            return
        self.dialog = PlaceDialog(self, latitude, longitude)
        self.dialog.open()

    def edit_place(self):
        place = self.current_place()
        if place and not self.private:
            self.dialog = PlaceDialog(self, place.latitude, place.longitude, place)
            self.dialog.open()

    def log_visit(self):
        place = self.current_place()
        if place and not self.private:
            self.dialog = VisitDialog(self, place)
            self.dialog.open()

    def delete_place(self):
        place = self.current_place()
        if (
            place
            and not self.private
            and QMessageBox.question(
                self, "Delete place", "Delete this place and all of its visits?"
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.requested.emit("delete_place", place.place_id)

    def delete_visit(self):
        item = self.history.currentItem()
        if (
            item
            and not self.private
            and QMessageBox.question(self, "Delete visit", "Delete this visit from your history?")
            == QMessageBox.StandardButton.Yes
        ):
            self.requested.emit("delete_visit", UUID(item.data(Qt.ItemDataRole.UserRole)))

    def set_compact(self, compact):
        self.splitter.setOrientation(
            Qt.Orientation.Vertical if compact else Qt.Orientation.Horizontal
        )
        controls = (
            self.add_button,
            self.reload_button,
            self.world_button,
            self.home_button,
            self.save_home_button,
        )
        for index, widget in enumerate(controls):
            self.controls_grid.removeWidget(widget)
            self.controls_grid.addWidget(
                widget, index // 2 if compact else 0, index % 2 if compact else index
            )
        for widget in (self.search, self.category, self.state):
            self.filters_grid.removeWidget(widget)
        self.filters_grid.addWidget(self.search, 0, 0, 1, 2 if compact else 1)
        self.filters_grid.addWidget(self.category, 1 if compact else 0, 0 if compact else 1)
        self.filters_grid.addWidget(self.state, 1 if compact else 0, 1 if compact else 2)
        for index, widget in enumerate((self.visit_button, self.edit_button, self.delete_button)):
            self.actions_grid.removeWidget(widget)
            self.actions_grid.addWidget(
                widget, index // 2 if compact else 0, index % 2 if compact else index
            )
