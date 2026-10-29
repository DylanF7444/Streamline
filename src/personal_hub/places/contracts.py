"""Bounded geographic records."""

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from pydantic import Field, field_validator

from personal_hub.contracts import HubContract

MAX_PLACES = 5_000
MAX_VISITS = 20_000


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("A timezone is required")
    return value.astimezone(UTC)


class Place(HubContract):
    place_id: UUID = Field(default_factory=uuid4)
    name: str = Field(min_length=1, max_length=160)
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    category: str = Field(default="Other", min_length=1, max_length=64)
    notes: str = Field(default="", max_length=4_000)
    favorite: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("name", "category")
    @classmethod
    def nonblank(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Enter a name and category")
        return value

    _utc = field_validator("created_at")(utc)


class Visit(HubContract):
    visit_id: UUID = Field(default_factory=uuid4)
    place_id: UUID
    visited_on: date
    notes: str = Field(default="", max_length=2_000)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    _utc = field_validator("created_at")(utc)


class MapPosition(HubContract):
    latitude: float = Field(default=20, ge=-85.051129, le=85.051129, allow_inf_nan=False)
    longitude: float = Field(default=0, ge=-180, le=180, allow_inf_nan=False)
    zoom: float = Field(default=2, ge=0, le=19, allow_inf_nan=False)
