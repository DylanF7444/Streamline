"""Offline deterministic Places workflows; fake renderer for online transitions."""

import sys
from datetime import date

import pytest
from PySide6.QtCore import QDate, Signal
from PySide6.QtWidgets import QWidget

from personal_hub.bootstrap import build_hub_container
from personal_hub.config import HubSettings
from personal_hub.places.contracts import MapPosition, Place
from personal_hub.ui.desktop.application import HubDesktopCoordinator
from personal_hub.ui.desktop.main_window import create_main_window

pytestmark = pytest.mark.ui


@pytest.fixture
def ui(qtbot, qapp, tmp_path):
    container = build_hub_container(HubSettings(data_dir=tmp_path / "hub", offline_mode=True))
    container.lifecycle.start()
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    coordinator = HubDesktopCoordinator(container, window, qapp)
    yield container.controller, window, coordinator
    coordinator.shutdown()
    container.close()


def test_local_place_and_repeat_visits_edit_filter_and_delete(ui):
    hub, window, coordinator = ui
    window.show_destination("Places")
    view = window.places_page
    assert window.workspace_mode == "Logging" and view.browser is None
    assert "Offline" in view.status.text()
    view.add_button.click()
    view.dialog.name.setText("Library")
    view.dialog.latitude.setValue(41)
    view.dialog.longitude.setValue(-87)
    view.dialog.category.setCurrentText("Library")
    view.dialog.save()
    (place,) = hub.list_places()
    assert view.current_place().place_id == place.place_id
    for day in (date(2025, 1, 2), date(2025, 1, 5)):
        view.visit_button.click()
        view.dialog.day.setDate(QDate(day))
        view.dialog.notes.setPlainText("Read a book")
        view.dialog.save()
    assert len(hub.list_visits()) == view.history.count() == 2
    assert "2 visits" in view.details.text()
    view.edit_button.click()
    view.dialog.name.setText("Main library")
    view.dialog.favorite.setChecked(True)
    view.dialog.save()
    view.state.setCurrentIndex(3)
    assert view.items.count() == 1
    view.search.setText("missing")
    assert view.items.count() == 0
    view.search.clear()
    view.state.setCurrentIndex(2)
    assert view.items.count() == 0
    view.state.setCurrentIndex(1)
    assert view.items.count() == 1
    view.items.setCurrentRow(0)
    visit = hub.list_visits()[0]
    coordinator.places_coordinator.handle("delete_visit", visit.visit_id)
    assert len(hub.list_visits()) == 1
    coordinator.places_coordinator.handle("delete_place", place.place_id)
    assert not hub.list_places() and not hub.list_visits()


def test_mask_lock_clears_private_controls_rejects_dialog_and_blocks_mutations(ui):
    hub, window, coordinator = ui
    hub.save_place(
        Place(
            name="Private library",
            category="Private category",
            notes="Private note",
            latitude=1,
            longitude=2,
        )
    )
    coordinator.places_coordinator.refresh()
    window.show_destination("Places")
    view = window.places_page
    view.items.setCurrentRow(0)
    view.log_visit()
    dialog = view.dialog
    dialog.notes.setPlainText("Unsaved writing")
    window.set_privacy_masked(True)
    assert view.private and view.content.isHidden()
    assert view.items.count() == view.history.count() == 0 and not view.details.text()
    assert not dialog.isVisible()
    dialog.save()
    assert not hub.list_visits()
    window.set_privacy_masked(False)
    assert view.items.count() == 1
    window.set_locked(True)
    assert view.private
    window.set_locked(False)
    assert not view.private and view.items.count() == 1


class FakeBrowser(QWidget):
    picked = Signal(float, float)
    selected = Signal(str)
    position_changed = Signal(object)
    status_changed = Signal(str)

    def __init__(self, parent, **options):
        super().__init__(parent)
        self.options = options
        self.stopped = False

    def shutdown(self):
        self.stopped = True

    def set_features(self, features):
        self.features = features

    def go_to(self, position):
        self.position = position


def test_renderer_lazy_online_navigation_pause_privacy_and_view_persistence(ui, monkeypatch):
    from types import SimpleNamespace

    hub, window, coordinator = ui
    monkeypatch.setitem(
        sys.modules, "personal_hub.ui.desktop.map_browser", SimpleNamespace(MapBrowser=FakeBrowser)
    )
    coordinator.set_offline_mode(False)
    view = window.places_page
    assert view.browser is None
    window.show_destination("Places")
    assert view.browser is None  # A restored page must not load maps while hidden in tray.
    window.show()
    browser = view.browser
    assert browser is not None and browser.options["allowed"]()
    position = MapPosition(latitude=42, longitude=-88, zoom=14)
    browser.position_changed.emit(position)
    view.save_home_button.click()
    assert hub.settings_repository.get("places.home") == position.model_dump()
    window.show_destination("Tasks")
    assert browser.stopped and view.browser is None
    window.set_workspace_mode("Logging")
    assert window.active_destination == "Places" and view.browser is not None
    window.set_network_refresh_paused(True)
    assert view.browser is None
    window.set_network_refresh_paused(False)
    assert view.browser is not None
    browser = view.browser
    window.hide()
    assert browser.stopped and view.browser is None
    window.show()
    assert view.browser is not None
    window.set_privacy_masked(True)
    assert view.browser is None
    window.set_privacy_masked(False)
    assert view.browser is not None
    coordinator.set_offline_mode(True)
    assert view.browser is None
    view.home_button.click()
    assert view.position == position


def test_coordinate_validation_and_failed_save_preserves_editor(ui, monkeypatch):
    hub, window, coordinator = ui
    view = window.places_page
    view.add_place(0, 0)
    view.dialog.name.setText("   ")
    view.dialog.save()
    assert not hub.list_places() and "Enter a name" in view.dialog.status.text()
    view.dialog.name.setText("Keep this writing")

    def failure(_):
        raise OSError("private failure details")

    monkeypatch.setattr(hub, "save_place", failure)
    view.dialog.save()
    assert view.dialog.name.text() == "Keep this writing"
    assert "Could not save" in view.dialog.status.text()
