"""Connect the Places surface to typed local application operations."""

from sqlalchemy.exc import SQLAlchemyError

from personal_hub.places.contracts import MapPosition


class PlacesCoordinator:
    def __init__(self, controller, window):
        self.controller, self.window, self.view = controller, window, window.places_page
        self.view.requested.connect(self.handle)
        self.view.bind(
            lambda: not controller.is_closed and not controller.offline,
            self.position("places.position"),
            self.position("places.home"),
        )
        window.destination_changed.connect(
            lambda destination: self.refresh() if destination == "Places" else None
        )
        self.refresh()

    def position(self, key):
        try:
            return MapPosition.model_validate(self.controller.settings_repository.get(key, {}))
        except ValueError:
            return MapPosition()

    def refresh(self, selected=None):
        if self.controller.is_closed:
            return
        self.view.timezone = self.controller.settings.default_timezone
        self.view.home_position = self.position("places.home")
        self.view.set_records(
            self.controller.list_places(), self.controller.list_visits(), selected
        )

    def handle(self, action, payload):
        if self.view.private or self.controller.is_closed:
            return
        try:
            if action == "refresh":
                self.refresh()
            elif action == "save_place":
                place = self.controller.save_place(payload["place"])
                payload["dialog"].accept()
                self.refresh(place.place_id)
                self.view.status.setText(
                    "Place saved on this device. Use Log visit to record a visit."
                )
            elif action == "log_visit":
                visit = self.controller.log_visit(payload["visit"])
                payload["dialog"].accept()
                self.refresh(visit.place_id)
                self.view.status.setText("Visit saved on this device.")
            elif action == "delete_place":
                self.controller.delete_place(payload)
                self.refresh()
                self.view.status.setText("Place and its visits deleted.")
            elif action == "delete_visit":
                self.controller.delete_visit(payload)
                self.refresh()
                self.view.status.setText("Visit deleted.")
            elif action in {"position", "home"}:
                position = MapPosition.model_validate(payload.model_dump())
                self.controller.settings_repository.set(f"places.{action}", position.model_dump())
                if action == "home":
                    self.view.home_position = position
                    self.view.status.setText("Home view saved. Use Home view to return here.")
        except (ValueError, KeyError, SQLAlchemyError, OSError, RuntimeError):
            message = "Could not save this change. Check the fields and try again."
            if isinstance(payload, dict) and "dialog" in payload:
                payload["dialog"].status.setText(message)
            else:
                self.view.status.setText(message)

    def close(self):
        self.view.active = False
        self.view.stop_map()
