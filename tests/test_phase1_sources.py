from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from personal_hub.diagnostics import DiagnosticsAggregator, aggregate_diagnostics
from personal_hub.notifications import (
    NotificationDelivery,
    NotificationPolicy,
    NotificationSeverity,
    QuietHoursSettings,
    decide_delivery,
    is_quiet_hours,
)
from personal_hub.sources import (
    SourceDefinition,
    SourceDisconnectedError,
    SourcePermissionRequiredError,
    SourceRegistry,
    SourceResult,
    SourceService,
    SourceSnapshot,
    SourceState,
    SourceStatus,
    SourceUnavailableError,
)

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def _snapshot(source_id: str, provider_id: str, *items: Any) -> SourceSnapshot:
    return SourceSnapshot(
        source_id=source_id,
        provider_id=provider_id,
        collected_at=NOW,
        items=items,
    )


class FakeSource:
    def __init__(
        self,
        source_id: str,
        provider_id: str = "fake",
        *,
        refresh_values: list[SourceSnapshot | BaseException] | None = None,
        delay: float = 0.0,
        status_value: SourceStatus | None = None,
        concurrency: dict[str, int] | None = None,
    ) -> None:
        self.source_id = source_id
        self.provider_id = provider_id
        self.display_name = f"Fake {source_id}"
        self.refresh_values = list(refresh_values or [])
        self.delay = delay
        self.status_value = status_value
        self.calls: list[str] = []
        self.active = 0
        self.max_active = 0
        self.cancelled = 0
        self.started = asyncio.Event()
        self.concurrency = concurrency

    async def connect(self) -> SourceStatus | None:
        self.calls.append("connect")
        return None

    async def status(self) -> SourceStatus:
        self.calls.append("status")
        return self.status_value or SourceStatus(
            source_id=self.source_id,
            provider_id=self.provider_id,
            state=SourceState.READY,
            checked_at=NOW,
        )

    async def refresh(self) -> SourceSnapshot:
        self.calls.append("refresh")
        self.started.set()
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self.concurrency is not None:
            self.concurrency["active"] += 1
            self.concurrency["max"] = max(self.concurrency["max"], self.concurrency["active"])
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            value: SourceSnapshot | BaseException = (
                self.refresh_values.pop(0)
                if self.refresh_values
                else _snapshot(self.source_id, self.provider_id, self.source_id)
            )
            if isinstance(value, BaseException):
                raise value
            return value
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.active -= 1
            if self.concurrency is not None:
                self.concurrency["active"] -= 1

    async def disconnect(self) -> None:
        self.calls.append("disconnect")


def _registry(*sources: FakeSource) -> SourceRegistry:
    registry = SourceRegistry()
    for source in sources:
        registry.register(
            source,
            source_id=source.source_id,
            provider_id=source.provider_id,
            display_name=source.display_name,
        )
    return registry


@pytest.mark.asyncio
async def test_offline_gate_blocks_connect_status_and_refresh_before_provider_calls():
    source = FakeSource("offline-source")
    service = SourceService(_registry(source), offline=True)

    connect_result = await service.connect(source.source_id)
    status_result = await service.status(source.source_id)
    refresh_result = await service.refresh(source.source_id)

    assert connect_result.state is SourceState.OFFLINE
    assert status_result.state is SourceState.OFFLINE
    assert refresh_result.state is SourceState.OFFLINE
    assert source.calls == []


@pytest.mark.asyncio
async def test_offline_getter_is_checked_before_queued_provider_invocation():
    source = FakeSource("queued-source", delay=0.01)
    offline = False
    service = SourceService(_registry(source), max_concurrency=1, offline_getter=lambda: offline)

    first = asyncio.create_task(service.refresh(source.source_id))
    await source.started.wait()
    offline = True
    second = asyncio.create_task(service.refresh(source.source_id))

    first_result = await first
    second_result = await second

    assert first_result.state is SourceState.READY
    assert second_result.state is SourceState.OFFLINE
    assert source.calls == ["refresh"]


@pytest.mark.asyncio
async def test_slow_or_failing_source_does_not_block_independent_sources():
    slow = FakeSource("slow", delay=0.2)
    fast = FakeSource("fast", refresh_values=[_snapshot("fast", "fake", "ok")])
    failing = FakeSource(
        "failing",
        refresh_values=[RuntimeError("token=private-value https://private.example/path?x=1")],
    )
    service = SourceService(_registry(slow, fast, failing), timeout_seconds=0.03)

    results = await service.refresh_all()

    assert results["slow"].state is SourceState.ERROR
    assert results["fast"].state is SourceState.READY
    assert results["fast"].snapshot is not None
    assert results["failing"].state is SourceState.ERROR
    assert "private-value" not in results["failing"].safe_message
    assert "private.example" not in results["failing"].safe_message


@pytest.mark.asyncio
async def test_last_good_snapshot_is_stale_on_failure_and_preserved_offline():
    source = FakeSource(
        "stale-source",
        refresh_values=[
            _snapshot("stale-source", "fake", {"title": "private title"}),
            SourceUnavailableError("account=private"),
        ],
    )
    service = SourceService(_registry(source))

    ready = await service.refresh(source.source_id)
    failed = await service.refresh(source.source_id)
    service.set_offline(True)
    offline = await service.refresh(source.source_id)

    assert ready.state is SourceState.READY
    assert failed.state is SourceState.STALE
    assert failed.snapshot == ready.snapshot
    assert offline.state is SourceState.OFFLINE
    assert offline.snapshot == ready.snapshot
    assert source.calls == ["refresh", "refresh"]


@pytest.mark.asyncio
async def test_refresh_all_never_exceeds_two_remote_calls():
    concurrency = {"active": 0, "max": 0}
    sources = tuple(
        FakeSource(f"source-{index}", delay=0.01, concurrency=concurrency) for index in range(7)
    )
    service = SourceService(_registry(*sources), max_concurrency=2)

    results = await service.refresh_all()

    assert len(results) == len(sources)
    assert all(result.state is SourceState.READY for result in results.values())
    assert concurrency["max"] <= 2


@pytest.mark.asyncio
async def test_disabled_sources_are_skipped_and_disconnect_clears_last_good_state():
    enabled = FakeSource("enabled")
    disabled = FakeSource("disabled")
    registry = _registry(enabled, disabled)
    registry.set_enabled(disabled.source_id, False)
    service = SourceService(registry)

    results = await service.refresh_all()
    assert tuple(results) == (enabled.source_id,)
    assert disabled.calls == []
    assert service.last_good_snapshot(enabled.source_id) is not None

    disconnected = await service.disconnect(enabled.source_id)
    assert disconnected.state is SourceState.DISCONNECTED
    assert enabled.calls[-1] == "disconnect"
    assert service.last_good_snapshot(enabled.source_id) is None

    service.set_offline(True)
    before = tuple(disabled.calls)
    assert (await service.disconnect(disabled.source_id)).state is SourceState.OFFLINE
    assert tuple(disabled.calls) == before


@pytest.mark.asyncio
async def test_cancellation_propagates_and_releases_source_capacity():
    source = FakeSource("cancel-source", delay=10)
    service = SourceService(_registry(source), max_concurrency=1)

    task = asyncio.create_task(service.refresh(source.source_id))
    await source.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert source.cancelled == 1
    replacement = FakeSource("replacement")
    service.registry.register(replacement, source_id="replacement", provider_id="fake")
    result = await service.refresh("replacement")
    assert result.state is SourceState.READY


@pytest.mark.asyncio
async def test_provider_statuses_remain_distinct_and_invalid_failures_are_safe():
    statuses = (
        SourceState.READY,
        SourceState.EMPTY,
        SourceState.STALE,
        SourceState.PERMISSION_REQUIRED,
        SourceState.DISCONNECTED,
        SourceState.ERROR,
    )
    sources = tuple(
        FakeSource(
            state.value,
            status_value=SourceStatus(
                source_id=state.value,
                provider_id="fake",
                state=state,
                checked_at=NOW,
            ),
        )
        for state in statuses
    )
    service = SourceService(_registry(*sources))

    results = {source.source_id: await service.status(source.source_id) for source in sources}

    assert {result.state for result in results.values()} == {
        SourceState.READY,
        SourceState.EMPTY,
        SourceState.ERROR,
        SourceState.PERMISSION_REQUIRED,
        SourceState.DISCONNECTED,
    }
    # STALE without a last-good snapshot is safely downgraded to ERROR.
    assert results[SourceState.STALE.value].state is SourceState.ERROR


@pytest.mark.asyncio
async def test_timeout_is_per_call_and_connection_permission_errors_are_classified():
    timeout_source = FakeSource("timeout", delay=0.2)
    permission_source = FakeSource(
        "permission",
        refresh_values=[SourcePermissionRequiredError("OAuth scope=private")],
    )
    disconnected_source = FakeSource(
        "disconnected",
        refresh_values=[SourceDisconnectedError("account private")],
    )
    service = SourceService(_registry(timeout_source, permission_source, disconnected_source))

    timeout_result, permission_result, disconnected_result = await asyncio.gather(
        service.refresh("timeout", timeout_seconds=0.01),
        service.refresh("permission"),
        service.refresh("disconnected"),
    )

    assert timeout_result.state is SourceState.ERROR
    assert permission_result.state is SourceState.PERMISSION_REQUIRED
    assert disconnected_result.state is SourceState.DISCONNECTED
    assert "private" not in permission_result.safe_message
    assert "private" not in disconnected_result.safe_message


def test_source_registry_enforces_stable_unique_ids_and_strict_contracts():
    source = FakeSource("stable")
    registry = _registry(source)
    with pytest.raises(ValueError):
        registry.register(FakeSource("other"), source_id="stable", provider_id="fake")
    with pytest.raises(ValueError):
        SourceDefinition(
            source_id="bad/id",
            provider_id="fake",
            display_name="Bad",
        )
    with pytest.raises(ValueError):
        SourceStatus(
            source_id="stable",
            provider_id="fake",
            state=SourceState.READY,
            unexpected="not allowed",
        )


def test_quiet_hours_and_delivery_policies_are_deterministic():
    quiet_hours = QuietHoursSettings(enabled=True, start="22:00", end="07:00")
    assert is_quiet_hours(datetime(2026, 8, 31, 23, 30, tzinfo=UTC), settings=quiet_hours)
    assert is_quiet_hours(datetime(2026, 9, 1, 6, 59, tzinfo=UTC), settings=quiet_hours)
    assert not is_quiet_hours(datetime(2026, 8, 31, 12, 0, tzinfo=UTC), settings=quiet_hours)

    ordinary = decide_delivery(
        NotificationPolicy.IMMEDIATE,
        now=datetime(2026, 8, 31, 23, 30, tzinfo=UTC),
        quiet_hours=quiet_hours,
    )
    urgent_without_opt_in = decide_delivery(
        NotificationPolicy.IMMEDIATE,
        now=datetime(2026, 8, 31, 23, 30, tzinfo=UTC),
        quiet_hours=quiet_hours,
        severity=NotificationSeverity.URGENT,
    )
    urgent_with_opt_in = decide_delivery(
        NotificationPolicy.IMMEDIATE,
        now=datetime(2026, 8, 31, 23, 30, tzinfo=UTC),
        quiet_hours=quiet_hours,
        severity=NotificationSeverity.URGENT,
        urgent_bypass_quiet_hours=True,
    )
    digest = decide_delivery(
        NotificationPolicy.DIGEST,
        now=NOW,
        quiet_hours=quiet_hours,
    )

    assert ordinary.enqueue_inbox is True
    assert ordinary.deliver_system_notification is False
    assert ordinary.suppressed_reason == "quiet_hours"
    assert urgent_without_opt_in.deliver_system_notification is False
    assert urgent_with_opt_in.deliver_system_notification is True
    assert digest.enqueue_inbox is True
    assert digest.deliver_system_notification is False
    assert digest.suppressed_reason == "policy"


def test_diagnostics_are_bounded_metadata_and_redact_content_and_urls():
    result = SourceResult(
        source_id="source-1",
        provider_id="fake",
        display_name="Private account display name",
        state=SourceState.STALE,
        checked_at=NOW,
        last_success_at=NOW,
        item_count=1,
        duration_ms=12.5,
        safe_message="body=private title=https://private.example/path?token=secret",
        snapshot=_snapshot(
            "source-1",
            "fake",
            {
                "title": "private article title",
                "body": "private message body",
                "url": "https://private.example/item?token=secret",
            },
        ),
    )
    delivery = NotificationDelivery(
        delivery_id="delivery-1",
        policy=NotificationPolicy.IMMEDIATE,
        enqueue_inbox=True,
        system_delivered=False,
        suppressed_reason="private recipient and body",
        recorded_at=NOW,
    )
    aggregator = DiagnosticsAggregator(application_version="1.0.0", schema_version=1)
    aggregator.record_source(result, response_size_bytes=70_000)
    aggregator.record_notification(delivery)
    payload = aggregator.to_json(generated_at=NOW)
    decoded = json.loads(payload)

    assert decoded["sources"][0]["state"] == "stale"
    assert decoded["sources"][0]["item_count"] == 1
    assert decoded["sources"][0]["response_size_bucket"] == "medium"
    assert decoded["notifications"][0]["suppressed_reason"] == "policy"
    for private_value in (
        "private article title",
        "private message body",
        "private.example",
        "token=secret",
        "Private account display name",
    ):
        assert private_value not in payload

    report = aggregate_diagnostics([result], [delivery], generated_at=NOW)
    assert report.generated_at == NOW
    assert len(report.sources) == 1
    assert len(report.notifications) == 1
