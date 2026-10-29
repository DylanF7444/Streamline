"""Transactional local places and repeat visits with explicit storage bounds."""

from uuid import UUID

from sqlalchemy import func, select

from personal_hub.places.contracts import MAX_PLACES, MAX_VISITS, Place, Visit
from personal_hub.storage.database import HubDatabase, HubPlaceRow, HubVisitRow


class PlacesRepository:
    def __init__(self, database: HubDatabase) -> None:
        self.database = database

    def places(self) -> tuple[Place, ...]:
        with self.database.session() as session:
            rows = session.scalars(select(HubPlaceRow).order_by(HubPlaceRow.name).limit(MAX_PLACES))
            return tuple(Place.model_validate_json(row.payload_json) for row in rows)

    def get(self, place_id: UUID | str) -> Place | None:
        with self.database.session() as session:
            row = session.get(HubPlaceRow, str(UUID(str(place_id))))
            return Place.model_validate_json(row.payload_json) if row else None

    def save(self, place: Place) -> Place:
        place = Place.model_validate(place.model_dump())
        with self.database.session() as session:
            row = session.get(HubPlaceRow, str(place.place_id))
            if row is None:
                if session.scalar(select(func.count()).select_from(HubPlaceRow)) >= MAX_PLACES:
                    raise ValueError("The saved-place limit has been reached")
                row = HubPlaceRow(place_id=str(place.place_id))
                session.add(row)
            row.name = place.name
            row.payload_json = place.model_dump_json()
        return place

    def delete(self, place_id: UUID | str) -> None:
        with self.database.session() as session:
            row = session.get(HubPlaceRow, str(UUID(str(place_id))))
            if row is not None:
                session.delete(row)

    def add_visit(self, visit: Visit) -> Visit:
        visit = Visit.model_validate(visit.model_dump())
        with self.database.session() as session:
            if session.get(HubPlaceRow, str(visit.place_id)) is None:
                raise ValueError("Save a place before logging a visit")
            if session.get(HubVisitRow, str(visit.visit_id)) is not None:
                raise ValueError("This visit is already saved")
            if session.scalar(select(func.count()).select_from(HubVisitRow)) >= MAX_VISITS:
                raise ValueError("The visit limit has been reached")
            session.add(
                HubVisitRow(
                    visit_id=str(visit.visit_id),
                    place_id=str(visit.place_id),
                    visited_on=visit.visited_on.isoformat(),
                    payload_json=visit.model_dump_json(),
                )
            )
        return visit

    def visits(self, place_id: UUID | str | None = None) -> tuple[Visit, ...]:
        with self.database.session() as session:
            query = select(HubVisitRow)
            if place_id is not None:
                query = query.where(HubVisitRow.place_id == str(UUID(str(place_id))))
            rows = session.scalars(
                query.order_by(HubVisitRow.visited_on.desc(), HubVisitRow.visit_id).limit(
                    MAX_VISITS
                )
            )
            return tuple(Visit.model_validate_json(row.payload_json) for row in rows)

    def delete_visit(self, visit_id: UUID | str) -> None:
        with self.database.session() as session:
            row = session.get(HubVisitRow, str(UUID(str(visit_id))))
            if row is not None:
                session.delete(row)
