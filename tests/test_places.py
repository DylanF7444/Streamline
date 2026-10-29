"""Local place/visit persistence, migration, and bounded online resource policy."""

import io
import json
import sqlite3
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from personal_hub.bootstrap import build_hub_container
from personal_hub.config import HubSettings
from personal_hub.places.contracts import MapPosition, Place, Visit
from personal_hub.places.network import (
    MAX_RESOURCE_BYTES,
    STYLE_URL,
    MapNetworkError,
    MapResourceClient,
    validate_resource_url,
)
from personal_hub.storage.database import HubDatabase


def test_places_visits_restart_export_backup_restore_and_cascade(tmp_path):
    settings = HubSettings(data_dir=tmp_path / "hub", offline_mode=True)
    with build_hub_container(settings) as container:
        hub = container.controller
        place = hub.save_place(
            Place(name="Campus library", latitude=41.1, longitude=-87.2, notes="Quiet upstairs")
        )
        hub.log_visit(
            Visit(place_id=place.place_id, visited_on=date(2026, 9, 1), notes="First visit")
        )
        second = hub.log_visit(Visit(place_id=place.place_id, visited_on=date(2026, 9, 5)))
        hub.save_place(Place.model_validate({**place.model_dump(), "favorite": True}))
        hub.export_local_data(tmp_path / "export.json")
        exported = json.loads((tmp_path / "export.json").read_text())
        assert exported["places"][0]["notes"] == "Quiet upstairs"
        assert len(exported["visits"]) == 2
        snapshot = hub._build_sync_snapshot(now=datetime.now(UTC))
        assert all(record.collection not in {"places", "visits"} for record in snapshot.records)
        backup = hub.backup_database(tmp_path / "before.db")
        hub.delete_visit(second.visit_id)
        assert len(hub.list_visits(place.place_id)) == 1
        hub.delete_place(place.place_id)
        assert hub.list_places() == hub.list_visits() == ()
        hub.restore_database(backup)
        assert hub.list_places()[0].favorite
        assert len(hub.list_visits()) == 2
    with build_hub_container(settings) as container:
        assert len(container.controller.list_places()) == 1
        assert len(container.controller.list_visits()) == 2
        assert container.controller.list_visits()[0].visited_on == date(2026, 9, 5)


def test_version14_migrates_additively_and_validates_new_tables(tmp_path):
    database = HubDatabase(tmp_path / "old.db")
    database.migrate()
    with sqlite3.connect(database.path) as connection:
        connection.execute("DROP TABLE hub_visits")
        connection.execute("DROP TABLE hub_places")
        connection.execute("UPDATE hub_schema_migrations SET version=14")
    assert database._validate_database_file(database.path) == 14
    database.migrate()
    database.migrate()
    assert database._validate_database_file(database.path) == 15
    database.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"name": "   "},
        {"latitude": float("nan")},
        {"longitude": 181},
        {"latitude": -91},
        {"notes": "x" * 4001},
        {"category": ""},
        {"unexpected": "data"},
    ],
)
def test_invalid_place_records(changes):
    with pytest.raises(ValidationError):
        Place.model_validate({"name": "Test", "latitude": 1, "longitude": 2, **changes})


def test_visits_require_existing_place_and_preserve_identity(tmp_path):
    with build_hub_container(HubSettings(data_dir=tmp_path / "hub")) as container:
        hub = container.controller
        visit = Visit(place_id=uuid4(), visited_on=date(2026, 1, 1))
        with pytest.raises(ValueError):
            hub.log_visit(visit)
        place = hub.save_place(Place(name="Park", latitude=0, longitude=0))
        visit = Visit(place_id=place.place_id, visited_on=visit.visited_on)
        hub.log_visit(visit)
        with pytest.raises(ValueError):
            hub.log_visit(visit)
        assert len(hub.list_visits()) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://tiles.openfreemap.org/planet",
        "https://localhost/planet",
        "file:///etc/passwd",
        "https://tiles.openfreemap.org.evil.test/planet",
        "https://user@tiles.openfreemap.org/planet",
        "https://tiles.openfreemap.org:443/planet",
        "https://tiles.openfreemap.org/planet?secret=test",
        "https://tiles.openfreemap.org/planet/../admin",
        "https://tiles.openfreemap.org/styles/%2e%2e/admin",
        "https://tiles.openfreemap.org/styles/%0aheader",
        "https://tiles.openfreemap.org/anything",
    ],
)
def test_map_origin_and_path_policy(url):
    with pytest.raises(MapNetworkError):
        validate_resource_url(url)


class Response(io.BytesIO):
    status = 200

    def __init__(self, data, headers=None):
        super().__init__(data)
        self.headers = headers or {}


def test_network_offline_before_dns_transport_or_cache_and_cancel():
    calls = []
    online = False

    def resolve(*_):
        calls.append("dns")
        return ["1.1.1.1"]

    def open_resource(request, **_):
        calls.append(request)
        return Response(b'{"version":8}')

    client = MapResourceClient(allowed=lambda: online, resolver=resolve, opener=open_resource)
    with pytest.raises(MapNetworkError):
        client.get(STYLE_URL)
    assert not calls
    online = True
    assert client.get(STYLE_URL) == b'{"version":8}'
    assert len(calls) == 2
    assert calls[1].get_method() == "GET" and "PersonalHub" in calls[1].get_header("User-agent")
    assert client.get(STYLE_URL) == b'{"version":8}'
    assert len(calls) == 2  # Memory cache, no repeated request.
    online = False
    with pytest.raises(MapNetworkError):
        client.get(STYLE_URL)
    online = True
    with pytest.raises(MapNetworkError):
        client.get(STYLE_URL, cancelled=lambda: True)
    assert len(calls) == 2


@pytest.mark.parametrize(
    "body,headers",
    [
        (b"", {"Content-Length": str(MAX_RESOURCE_BYTES + 1)}),
        (b"x" * (MAX_RESOURCE_BYTES + 1), {}),
    ],
    ids=["oversize-header", "oversize-body"],
)
def test_map_responses_are_bounded(body, headers):
    client = MapResourceClient(
        allowed=lambda: True,
        resolver=lambda *_: ["1.1.1.1"],
        opener=lambda *_args, **_kwargs: Response(body, headers),
    )
    with pytest.raises(MapNetworkError, match="size limit"):
        client.get(STYLE_URL)


def test_map_rejects_private_dns_before_transport_and_position_bounds():
    calls = []
    client = MapResourceClient(
        allowed=lambda: True,
        resolver=lambda *_: ["127.0.0.1"],
        opener=lambda *_args, **_kwargs: calls.append(1),
    )
    with pytest.raises(MapNetworkError):
        client.get(STYLE_URL)
    assert not calls
    with pytest.raises(ValidationError):
        MapPosition(zoom=float("inf"))
