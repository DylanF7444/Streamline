"""Phase 5 webpage-watch security, extraction, persistence, and alert tests."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from personal_hub.bootstrap import build_hub_container
from personal_hub.config import HubSettings
from personal_hub.storage.database import HubDatabase
from personal_hub.watches import (
    ChangedetectionReadAdapter,
    PageFetcher,
    PageResponse,
    WatchCondition,
    WatchDefinition,
    WatchOfflineError,
    WatchPolicyError,
    WatchUnavailableError,
    extract_observation,
)

NOW = datetime(2026, 9, 1, 14, tzinfo=UTC)
DEFAULT_WATCH_ID = UUID("00000000-0000-0000-0000-000000005001")


def _watch(
    *,
    kind: str = "page_text",
    source_url: str = "https://shop.example.test/product",
    condition: WatchCondition | None = None,
    watch_id: UUID = DEFAULT_WATCH_ID,
    allow_http: bool = False,
    approved_redirect_origins: tuple[str, ...] = (),
) -> WatchDefinition:
    from personal_hub.feeds.security import origin_for_url

    return WatchDefinition(
        watch_id=watch_id,
        name="Product watch",
        kind=kind,
        source_url=source_url,
        condition=condition or WatchCondition(kind="changed"),
        refresh_minutes=60,
        cooldown_minutes=60,
        allow_http=allow_http,
        reviewed_origin=origin_for_url(source_url),
        approved_redirect_origins=approved_redirect_origins,
        created_at=NOW,
        updated_at=NOW,
    )


class _Transport:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def request(self, method: str, url: str, *, headers, **_kwargs):
        self.calls.append((method, url, dict(headers)))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class _Fetcher:
    def __init__(self, responses: list[PageResponse | Exception]) -> None:
        self.responses = list(responses)
        self.calls = 0
        self.offline = False

    def set_offline(self, value: bool) -> None:
        self.offline = value

    async def fetch(self, _watch: WatchDefinition) -> PageResponse:
        if self.offline:
            raise WatchOfflineError("offline")
        self.calls += 1
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def _product_response(price: float, availability: str = "OutOfStock") -> PageResponse:
    return PageResponse(
        status_code=200,
        headers={"Content-Type": "text/html; charset=utf-8"},
        url="https://shop.example.test/product",
        body=f"""<html><head><script>submitForm(); price=1;</script>
        <script type="application/ld+json">{{"@type":"Product","name":"Desk lamp",
        "offers":{{"@type":"Offer","price":"{price}","priceCurrency":"USD",
        "availability":"https://schema.org/{availability}"}}}}</script></head>
        <body><form action="https://evil.invalid"><button>Buy</button></form></body></html>""".encode(),
    )


def test_watch_contract_enforces_typed_conditions_floors_and_exact_origin() -> None:
    with pytest.raises(ValidationError):
        _watch(
            kind="product_price",
            condition=WatchCondition(kind="restocked"),
        )


def test_v5_watch_schema_migrates_a_v4_database_idempotently(tmp_path: Path) -> None:
    path = tmp_path / "legacy-v4.db"
    database = HubDatabase(path)
    database.migrate()
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE hub_watch_alerts")
        connection.execute("DROP TABLE hub_watch_observations")
        connection.execute("DROP TABLE hub_watch_definitions")
        connection.execute("UPDATE hub_schema_migrations SET version = 4 WHERE id = 1")
    upgraded = HubDatabase(path)
    try:
        upgraded.migrate()
        upgraded.migrate()
        with sqlite3.connect(path) as connection:
            version = connection.execute(
                "SELECT version FROM hub_schema_migrations WHERE id = 1"
            ).fetchone()[0]
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
        assert version == HubDatabase.CURRENT_SCHEMA_VERSION
        assert {
            "hub_watch_definitions",
            "hub_watch_observations",
            "hub_watch_alerts",
        }.issubset(tables)
        assert upgraded.integrity_check()
    finally:
        upgraded.close()
    with pytest.raises(ValidationError):
        _watch(
            kind="page_selector",
            condition=WatchCondition(kind="changed", selector="div > script"),
        )
    with pytest.raises(ValidationError):
        WatchDefinition(
            name="Wrong origin",
            kind="page_text",
            source_url="https://example.test/page",
            condition=WatchCondition(),
            refresh_minutes=30,
            reviewed_origin="https://other.test",
        )
    with pytest.raises(ValidationError):
        WatchDefinition(
            name="Unreviewed HTTP",
            kind="page_text",
            source_url="http://example.test/page",
            condition=WatchCondition(),
            refresh_minutes=30,
            reviewed_origin="http://example.test",
        )


def test_inert_extraction_uses_structured_product_data_and_safe_selector() -> None:
    price_watch = _watch(
        kind="product_price",
        condition=WatchCondition(kind="price_below", threshold=90),
    )
    observation = extract_observation(price_watch, _product_response(89.5), checked_at=NOW)
    assert observation.normalized_value == 89.5
    assert observation.metadata == {
        "extraction": "structured_product",
        "currency": "USD",
        "product_name": "Desk lamp",
    }
    assert "submitForm" not in observation.model_dump_json()

    selector_watch = _watch(
        kind="page_selector",
        condition=WatchCondition(kind="changed", selector="#inventory"),
    )
    selected = extract_observation(
        selector_watch,
        PageResponse(
            200,
            {"Content-Type": "text/html"},
            b'<div id="inventory">Only <b>three</b> left</div><script>private()</script>',
        ),
        checked_at=NOW,
    )
    assert selected.normalized_value == "Only three left"
    assert "private" not in selected.model_dump_json()


@pytest.mark.asyncio
async def test_page_fetcher_blocks_ssrf_cross_origin_redirect_size_timeout_and_non_get() -> None:
    private_transport = _Transport([PageResponse(200, body=b"private")])
    fetcher = PageFetcher(
        private_transport,
        allow_http=True,
        resolve_host=False,
        max_response_bytes=1_024,
    )
    private = _watch(
        source_url="http://127.0.0.1/admin",
        allow_http=True,
    )
    with pytest.raises(WatchPolicyError):
        await fetcher.fetch(private)
    assert private_transport.calls == []

    redirect_transport = _Transport(
        [PageResponse(302, {"Location": "https://other.example.test/page"})]
    )
    with pytest.raises(WatchPolicyError):
        await PageFetcher(redirect_transport, resolve_host=False).fetch(_watch())
    assert [call[0] for call in redirect_transport.calls] == ["GET"]

    oversize = _Transport([PageResponse(200, {"Content-Length": "2048"}, b"")])
    with pytest.raises(WatchUnavailableError):
        await PageFetcher(oversize, resolve_host=False, max_response_bytes=1_024).fetch(_watch())

    timeout = _Transport([TimeoutError("secret timeout detail")])
    with pytest.raises(WatchUnavailableError, match="could not be reached") as captured:
        await PageFetcher(timeout, resolve_host=False).fetch(_watch())
    assert "secret timeout detail" not in str(captured.value)


@pytest.mark.asyncio
async def test_first_observation_baselines_then_typed_transition_alerts_once_across_restart(
    tmp_path: Path,
) -> None:
    settings = HubSettings(data_dir=tmp_path / "hub", default_timezone="UTC")
    container = build_hub_container(settings)
    watch = container.controller.create_watch(
        name="Desk lamp deal",
        kind="product_price",
        source_url="https://shop.example.test/product",
        condition=WatchCondition(kind="price_below", threshold=90),
        refresh_minutes=60,
        cooldown_minutes=60,
        now=NOW,
    )
    fetcher = _Fetcher([_product_response(100), _product_response(80), _product_response(80)])
    container.watch_service.fetcher = fetcher

    first = (await container.controller.refresh_watches((watch.watch_id,), force=True, now=NOW))[
        watch.watch_id
    ]
    assert first.baseline_established and first.alert is None
    assert container.controller.list_inbox() == []

    second_time = NOW + timedelta(minutes=61)
    second = (
        await container.controller.refresh_watches((watch.watch_id,), force=True, now=second_time)
    )[watch.watch_id]
    assert second.alert is not None
    assert second.alert.reason_code == "price_below"
    assert second.alert.previous_value == 100.0
    assert second.alert.current_value == 80.0
    assert "100" in second.alert.safe_summary and "80" in second.alert.safe_summary
    assert len(container.controller.list_inbox()) == 1
    assert container.notification_repository.was_claimed(second.alert.deduplication_key, "windows")
    assert [item.title for item in container.controller.watch_today_items(now=second_time)] == [
        "Desk lamp deal"
    ]

    third = (
        await container.controller.refresh_watches(
            (watch.watch_id,), force=True, now=second_time + timedelta(minutes=61)
        )
    )[watch.watch_id]
    assert third.alert is None
    assert container.watch_repository.counts() == (1, 3, 1)
    container.close()

    restarted = build_hub_container(settings)
    restarted.watch_service.fetcher = _Fetcher([_product_response(80)])
    repeated = (
        await restarted.controller.refresh_watches(
            (watch.watch_id,), force=True, now=second_time + timedelta(hours=3)
        )
    )[watch.watch_id]
    try:
        assert repeated.alert is None
        assert restarted.watch_repository.counts() == (1, 4, 1)
        assert len(restarted.controller.list_inbox()) == 1
        removed = restarted.watch_repository.cleanup(now=second_time + timedelta(days=365))
        assert removed == 4
        assert restarted.watch_repository.counts() == (1, 1, 0)
        assert restarted.watch_repository.latest_observation(watch.watch_id) is not None
    finally:
        restarted.close()


@pytest.mark.asyncio
async def test_restock_transition_cooldown_and_test_now_are_non_mutating(tmp_path: Path) -> None:
    container = build_hub_container(HubSettings(data_dir=tmp_path / "hub"))
    watch = container.controller.create_watch(
        name="Restock",
        kind="product_restock",
        source_url="https://shop.example.test/product",
        condition=WatchCondition(kind="restocked"),
        refresh_minutes=60,
        cooldown_minutes=120,
        now=NOW,
    )
    fetcher = _Fetcher(
        [
            _product_response(100, "OutOfStock"),
            _product_response(100, "OutOfStock"),
            _product_response(100, "InStock"),
            _product_response(100, "OutOfStock"),
            _product_response(100, "InStock"),
        ]
    )
    container.watch_service.fetcher = fetcher
    tested = await container.controller.test_watch_now(watch, now=NOW)
    assert tested.normalized_value is False
    assert container.watch_repository.counts() == (1, 0, 0)

    await container.controller.refresh_watches((watch.watch_id,), force=True, now=NOW)
    alert = (
        await container.controller.refresh_watches(
            (watch.watch_id,), force=True, now=NOW + timedelta(minutes=61)
        )
    )[watch.watch_id]
    assert alert.alert is not None and alert.alert.reason_code == "restocked"
    await container.controller.refresh_watches(
        (watch.watch_id,), force=True, now=NOW + timedelta(minutes=62)
    )
    cooled = (
        await container.controller.refresh_watches(
            (watch.watch_id,), force=True, now=NOW + timedelta(minutes=63)
        )
    )[watch.watch_id]
    assert cooled.alert is None
    assert container.watch_repository.counts()[2] == 1
    container.close()


@pytest.mark.asyncio
async def test_due_scheduler_is_independent_bounded_and_offline_before_fetch(
    tmp_path: Path,
) -> None:
    container = build_hub_container(HubSettings(data_dir=tmp_path / "hub"))
    first = container.controller.create_watch(
        name="Healthy",
        kind="page_text",
        source_url="https://one.example.test/page",
        condition=WatchCondition(),
        watch_id=UUID("00000000-0000-0000-0000-000000005011"),
        now=NOW - timedelta(hours=2),
    )
    second = container.controller.create_watch(
        name="Failed",
        kind="page_text",
        source_url="https://two.example.test/page",
        condition=WatchCondition(),
        watch_id=UUID("00000000-0000-0000-0000-000000005012"),
        now=NOW - timedelta(hours=2),
    )
    container.watch_service.fetcher = _Fetcher(
        [
            PageResponse(200, {"Content-Type": "text/plain"}, b"healthy"),
            WatchUnavailableError("unavailable"),
        ]
    )
    results = await container.controller.refresh_watches(now=NOW)
    assert results[first.watch_id].state == "ready"
    assert results[second.watch_id].state == "disconnected"
    assert container.watch_repository.latest_observation(first.watch_id) is not None
    assert container.watch_repository.latest_observation(second.watch_id) is None

    container.controller.set_offline_mode(True)
    calls = container.watch_service.fetcher.calls
    offline = await container.controller.refresh_watches(
        (first.watch_id,), force=True, now=NOW + timedelta(hours=2)
    )
    assert offline[first.watch_id].state == "offline"
    assert container.watch_service.fetcher.calls == calls
    container.close()


@pytest.mark.asyncio
async def test_changedetection_adapter_is_exact_origin_get_only_and_offline_first() -> None:
    transport = _Transport(
        [
            PageResponse(
                200,
                {"Content-Type": "application/json"},
                b'{"watch_uuid":"remote-1","current_snapshot":"changed"}',
                "https://changes.example.test/api/v1/watch/remote-1",
            )
        ]
    )
    adapter = ChangedetectionReadAdapter(
        "https://changes.example.test",
        transport,
        lambda: "vault-token",
    )
    response = await adapter.fetch_snapshot("remote-1")
    assert response.status_code == 200
    assert transport.calls[0][0] == "GET"
    assert transport.calls[0][1].startswith("https://changes.example.test/api/v1/watch/")
    assert transport.calls[0][2]["Authorization"] == "Bearer vault-token"
    assert not hasattr(adapter, "create_watch")
    assert not hasattr(adapter, "update_watch")
    assert not hasattr(adapter, "delete_watch")

    adapter.set_offline(True)
    with pytest.raises(WatchOfflineError):
        await adapter.fetch_snapshot("remote-1")
    assert len(transport.calls) == 1
