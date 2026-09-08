"""Phase 5 visual watch authoring, test-now, alert, and privacy behavior."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from personal_hub.ui.desktop.main_window import (
    WatchAlertView,
    WebWatchesView,
    WebWatchView,
    create_main_window,
)


def _window(qtbot):
    window = create_main_window(close_to_tray=False)
    qtbot.addWidget(window)
    window.show_destination("Watchlists")
    return window


@pytest.mark.ui
def test_web_watch_authoring_validates_typed_conditions_and_emits_local_requests(qtbot):
    window = _window(qtbot)
    created: list[dict[str, object]] = []
    tested: list[object] = []
    window.watch_create_requested.connect(created.append)
    window.watch_test_requested.connect(tested.append)

    invalid = window.request_create_watch(
        {
            "name": "Deal",
            "kind": "product_price",
            "source_url": "https://shop.example.test/product",
            "condition_kind": "price_below",
            "threshold": "not-a-number",
            "refresh_minutes": 60,
            "cooldown_minutes": 60,
        }
    )
    assert invalid is None
    assert "numeric" in window.watch_editor_status.text().casefold()

    payload = window.request_create_watch(
        {
            "name": "Deal",
            "kind": "product_price",
            "source_url": "https://shop.example.test/product",
            "condition_kind": "price_below",
            "threshold": 90,
            "refresh_minutes": 60,
            "cooldown_minutes": 120,
            "notification_policy": "immediate",
        }
    )
    assert payload is not None
    assert payload["condition"] == {
        "kind": "price_below",
        "threshold": 90.0,
        "percentage": None,
        "term": None,
        "selector": None,
        "alert_on_current_match": False,
    }
    assert created[-1] == payload

    draft = window.request_test_watch(
        {
            "name": "Stock",
            "kind": "product_restock",
            "source_url": "https://shop.example.test/product",
            "condition_kind": "restocked",
            "refresh_minutes": 60,
            "cooldown_minutes": 60,
        }
    )
    assert draft is not None and tested[-1] == draft
    assert "without saving a baseline" in window.watch_editor_status.text()


@pytest.mark.ui
def test_web_watch_render_edit_test_result_alerts_and_privacy(qtbot):
    window = _window(qtbot)
    watch = WebWatchView(
        watch_id="watch-1",
        name="Private product",
        kind="product_price",
        source_url="https://shop.example.test/product",
        condition_kind="price_below",
        threshold=90,
        status="ready",
        last_value="89.50",
    )
    alert = WatchAlertView(
        alert_id="alert-1",
        watch_id="watch-1",
        title="Private product",
        summary="Price moved below the threshold.",
        reason_code="price_below",
        observed_at=datetime(2026, 9, 1, 14, tzinfo=UTC),
    )
    window.render_web_watches(WebWatchesView(watches=(watch,), alerts=(alert,)))
    assert window.watch_list.count() == 1
    assert "Private product" in window.watch_list.item(0).text()
    assert "89.50" in window.watch_list.item(0).text()
    assert "Price moved" in window.watch_alerts_label.text()

    assert window.edit_watch("watch-1") == watch
    assert window.watch_save_button.text() == "Save watch"
    tested: list[object] = []
    window.watch_test_requested.connect(tested.append)
    window.request_test_watch()
    assert tested[-1] == {"watch_id": "watch-1"}
    window.render_watch_test_result({"success": True, "value": "89.5"})
    assert "Extraction succeeded" in window.watch_editor_status.text()

    window.set_privacy_masked(True)
    assert "Private product" not in window.watch_list.item(0).text()
    assert "89.50" not in window.watch_list.item(0).text()
    assert "Hidden by privacy settings" in window.watch_list.item(0).text()
    assert "Price moved" not in window.watch_alerts_label.text()


@pytest.mark.ui
def test_web_watch_editor_rejects_http_without_dual_opt_in_and_executable_selectors(qtbot):
    window = _window(qtbot)
    base = {
        "name": "Unsafe",
        "kind": "page_selector",
        "source_url": "http://example.test/page",
        "condition_kind": "changed",
        "selector": "div > script",
        "refresh_minutes": 30,
        "cooldown_minutes": 0,
    }
    assert window.request_create_watch(base) is None
    assert "selectors" in window.watch_editor_status.text().casefold()
    base["selector"] = "#inventory"
    assert window.request_create_watch(base) is None
    assert "http requires explicit approval" in window.watch_editor_status.text().casefold()
    base["allow_http"] = True
    assert window.request_create_watch(base) is not None
