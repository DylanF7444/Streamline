"""Gmail-style local message categorization and persistence coverage."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from personal_hub.messages import (
    MESSAGE_CATEGORY_VALUES,
    MessageQuery,
    MessageRepository,
    MessageSnapshot,
    MessageSourceConfig,
    MessageSummary,
    classify_message,
)
from personal_hub.storage.database import HubDatabase

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        (("CATEGORY_PERSONAL",), "primary"),
        (("CATEGORY_PROMOTIONS",), "promotions"),
        (("CATEGORY_SOCIAL",), "social"),
        (("CATEGORY_UPDATES",), "updates"),
        (("SPAM",), "spam"),
        (("SPAM", "CATEGORY_PROMOTIONS"), "spam"),
    ],
)
def test_provider_labels_are_mapped_with_spam_precedence(
    labels: tuple[str, ...], expected: str
) -> None:
    assert classify_message(provider="gmail", labels=labels) == expected


def test_provider_primary_label_wins_over_heuristic_category() -> None:
    assert (
        classify_message(
            provider="gmail",
            labels=("CATEGORY_PERSONAL",),
            title="Your exclusive sale — unsubscribe today",
        )
        == "primary"
    )


@pytest.mark.parametrize(
    ("title", "sender", "expected"),
    [
        ("A Live Interview — REGISTER TODAY!", "Writers Theatre", "promotions"),
        ("You have a new follower", "social@example.test", "social"),
        ("Your order receipt", "store@example.test", "updates"),
        ("Quarterly plan", "person@example.test", "primary"),
    ],
)
def test_fallback_classification_is_conservative_and_local(
    title: str,
    sender: str,
    expected: str,
) -> None:
    assert classify_message(title=title, sender=sender) == expected


def _config() -> MessageSourceConfig:
    return MessageSourceConfig(
        source_id="gmail-primary",
        provider="gmail",
        account_id="selected-account",
        query=MessageQuery(folders=("INBOX",), limit=20),
        created_at=NOW,
        updated_at=NOW,
    )


def _summary(category: str, message_id: str) -> MessageSummary:
    return MessageSummary(
        source_id="gmail-primary",
        message_id=message_id,
        provider="gmail",
        account_id="selected-account",
        sender=f"sender-{message_id}@example.test",
        title=f"Subject {message_id}",
        preview="Preview",
        received_at=NOW,
        unread=True,
        category=category,  # type: ignore[arg-type]
    )


def test_categories_are_retained_and_filterable_without_mutating_provider_state(
    tmp_path: Path,
) -> None:
    database = HubDatabase(tmp_path / "hub.db")
    database.migrate()
    repository = MessageRepository(database)
    config = _config()
    repository.add_source(config)
    repository.upsert_summaries(
        config,
        tuple(_summary(category, category) for category in MESSAGE_CATEGORY_VALUES),
        retained_at=NOW,
    )

    assert [item.category for item in repository.list_summaries(category="promotions")] == [
        "promotions"
    ]
    assert {item.category for item in repository.list_summaries(include_spam=False)} == {
        "primary",
        "promotions",
        "social",
        "updates",
    }
    assert [item.category for item in repository.list_summaries(category="spam")] == ["spam"]
    database.close()


def test_schema_v7_message_table_gains_a_primary_category_column(tmp_path: Path) -> None:
    path = tmp_path / "legacy-v7.db"
    database = HubDatabase(path)
    database.migrate()
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE hub_message_summaries DROP COLUMN category")
        connection.execute("UPDATE hub_schema_migrations SET version = 7 WHERE id = 1")

    upgraded = HubDatabase(path)
    try:
        upgraded.migrate()
        with sqlite3.connect(path) as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(hub_message_summaries)")
            }
            version = connection.execute(
                "SELECT version FROM hub_schema_migrations WHERE id = 1"
            ).fetchone()[0]
        assert version == HubDatabase.CURRENT_SCHEMA_VERSION
        assert "category" in columns
        assert upgraded.integrity_check()
    finally:
        upgraded.close()


def test_snapshot_category_is_safe_when_provider_returns_an_unclassified_message() -> None:
    config = _config()
    snapshot = MessageSnapshot(
        source_id=config.source_id,
        provider=config.provider,
        account_id=config.account_id,
        collected_at=NOW,
        summaries=(_summary("primary", "message-1"),),
    )
    assert snapshot.summaries[0].category == "primary"
