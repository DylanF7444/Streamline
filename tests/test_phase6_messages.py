"""Phase 6 read-only message summaries, body safety, and retention gates."""

from __future__ import annotations

import base64
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from personal_hub.bootstrap import build_hub_container
from personal_hub.config import HubSettings
from personal_hub.integrations import (
    GMAIL_READONLY_SCOPE,
    OUTLOOK_MAIL_READ_SCOPE,
    gmail_oauth_config,
    outlook_mail_oauth_config,
    slack_messages_oauth_config,
)
from personal_hub.messages import (
    GmailMessageSource,
    MessageBody,
    MessageOfflineError,
    MessageQuery,
    MessageRepository,
    MessageService,
    MessageSnapshot,
    MessageSourceConfig,
    MessageSummary,
    OutlookMessageSource,
    SlackMessageSource,
    sanitize_message_body,
)
from personal_hub.storage.database import HubDatabase

NOW = datetime(2026, 9, 1, 14, 30, tzinfo=UTC)


class _Token:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def get_token(self, *, scopes: tuple[str, ...]) -> str:
        self.calls.append(scopes)
        return "read-only-token"


class _NoArgumentToken:
    def __init__(self) -> None:
        self.calls = 0

    async def access_token(self) -> str:
        self.calls += 1
        return "oauth-service-token"


class _Transport:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    async def request(self, method: str, url: str, **kwargs: object) -> object:
        self.calls.append((method, url, kwargs))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def _summary(
    source_id: str = "gmail-primary",
    *,
    provider: str = "gmail",
    message_id: str = "message-1",
    title: str = "Quarterly plan",
) -> MessageSummary:
    return MessageSummary(
        source_id=source_id,
        message_id=message_id,
        provider=provider,
        account_id="selected-account",
        sender="Sensitive Sender <sender@example.test>",
        title=title,
        preview="Sensitive preview",
        received_at=NOW,
        unread=True,
        importance="important",
        deep_link="https://example.test/message/1",
    )


def _config(
    source_id: str = "gmail-primary",
    *,
    provider: str = "gmail",
    retention_days: int = 7,
) -> MessageSourceConfig:
    query = (
        MessageQuery(folders=("INBOX",), limit=20)
        if provider == "gmail"
        else MessageQuery(folders=("inbox",), limit=20)
        if provider == "outlook"
        else MessageQuery(channels=("C123",), limit=20)
    )
    return MessageSourceConfig(
        source_id=source_id,
        provider=provider,
        account_id="selected-account",
        query=query,
        retention_days=retention_days,
        created_at=NOW,
        updated_at=NOW,
    )


class _Source:
    def __init__(
        self,
        config: MessageSourceConfig,
        *,
        failure: Exception | None = None,
        body_text: str = "BODY SECRET 73821",
    ) -> None:
        self.source_id = config.source_id
        self.provider_id = config.provider
        self.config = config
        self.failure = failure
        self.body_text = body_text
        self.list_calls = 0
        self.body_calls = 0
        self.offline = False

    def set_offline(self, value: bool) -> None:
        self.offline = value

    async def list_summaries(self, query: MessageQuery) -> MessageSnapshot:
        self.list_calls += 1
        if self.failure is not None:
            raise self.failure
        return MessageSnapshot(
            source_id=self.source_id,
            provider=self.config.provider,
            account_id=self.config.account_id,
            collected_at=NOW,
            summaries=(
                _summary(
                    self.source_id,
                    provider=self.config.provider,
                    message_id=f"{self.source_id}-message",
                ),
            ),
        )

    async def get_body(self, message_id: str) -> MessageBody:
        self.body_calls += 1
        return sanitize_message_body(
            self.source_id,
            message_id,
            self.body_text,
            fetched_at=NOW,
        )


def test_v6_schema_migrates_v5_without_creating_a_body_table(tmp_path: Path) -> None:
    path = tmp_path / "legacy-v5.db"
    database = HubDatabase(path)
    database.migrate()
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE hub_message_summaries")
        connection.execute("DROP TABLE hub_message_sources")
        connection.execute("UPDATE hub_schema_migrations SET version = 5 WHERE id = 1")
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
        assert {"hub_message_sources", "hub_message_summaries"}.issubset(tables)
        assert not any("body" in table for table in tables)
        assert upgraded.integrity_check()
    finally:
        upgraded.close()


def test_message_contracts_require_explicit_bounded_provider_selections() -> None:
    for provider in ("gmail", "outlook", "slack"):
        with pytest.raises(ValidationError):
            MessageSourceConfig(
                source_id=f"{provider}-empty",
                provider=provider,
                account_id="account",
                query=MessageQuery(),
            )
    with pytest.raises(ValidationError):
        MessageSourceConfig(
            source_id="gmail-injected-folder",
            provider="gmail",
            account_id="account",
            query=MessageQuery(folders=("INBOX OR from:attacker",)),
        )
    with pytest.raises(ValidationError):
        MessageSummary(
            **{
                **_summary().model_dump(exclude={"deep_link"}),
                "deep_link": "mailto:sender@example.test",
            }
        )
    with pytest.raises(ValidationError):
        MessageSourceConfig(
            source_id="too-long-retention",
            provider="gmail",
            account_id="account",
            query=MessageQuery(folders=("INBOX",)),
            retention_days=31,
        )


def test_message_sanitizer_blocks_active_remote_and_custom_content() -> None:
    body = sanitize_message_body(
        "gmail-primary",
        "message-1",
        """
        <h1>Visible heading</h1>
        <script>BODY SHOULD NOT RUN</script>
        <style>SECRET STYLE</style>
        <form><input value="secret">FORM SECRET</form>
        <iframe src="https://tracker.invalid">FRAME SECRET</iframe>
        <img src="https://tracker.invalid/pixel?secret=1">
        <a href="mailto:secret@example.test">email</a>
        <a href="hub://commands/run">custom</a>
        <a href="https://safe.example.test/path">safe link</a>
        <p>Visible body</p>
        """,
        content_type="text/html",
        fetched_at=NOW,
    )
    assert body.plain_text == "Visible heading\nemail\ncustom\nsafe link\nVisible body"
    assert body.links == ("https://safe.example.test/path",)
    assert "tracker" not in body.model_dump_json()
    assert "SECRET" not in body.plain_text


@pytest.mark.asyncio
async def test_gmail_lists_metadata_only_then_fetches_an_inline_body_explicitly() -> None:
    encoded = base64.urlsafe_b64encode(
        b'<p>Visible</p><img src="https://tracker.invalid/x"><script>secret()</script>'
    ).decode()
    token = _Token()
    transport = _Transport(
        [
            {"messages": [{"id": "m1"}]},
            {
                "id": "m1",
                "threadId": "t1",
                "internalDate": str(int(NOW.timestamp() * 1_000)),
                "labelIds": ["UNREAD", "IMPORTANT"],
                "snippet": "Selected preview",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "Sender"},
                        {"name": "Subject", "value": "Subject"},
                    ]
                },
            },
            {
                "payload": {
                    "mimeType": "multipart/mixed",
                    "parts": [
                        {
                            "mimeType": "application/pdf",
                            "body": {"attachmentId": "must-not-be-read"},
                        },
                        {"mimeType": "text/html", "body": {"data": encoded}},
                    ],
                }
            },
        ]
    )
    source = GmailMessageSource(
        token,
        transport,
        source_id="gmail-primary",
        account_id="selected-account",
    )
    snapshot = await source.list_summaries(
        MessageQuery(folders=("INBOX",), query="label:work", important_only=True, limit=10)
    )
    assert token.calls == [(GMAIL_READONLY_SCOPE,)]
    assert len(snapshot.summaries) == 1
    assert len(transport.calls) == 2
    assert transport.calls[0][0] == "GET"
    assert transport.calls[0][2]["params"] == {
        "q": "in:INBOX (label:work) is:unread is:important",
        "maxResults": 10,
    }
    assert transport.calls[1][2]["params"]["format"] == "metadata"
    body = await source.get_body("m1")
    assert token.calls == [(GMAIL_READONLY_SCOPE,), (GMAIL_READONLY_SCOPE,)]
    assert body.plain_text == "Visible"
    assert "tracker" not in body.model_dump_json()
    assert all("attachments" not in url for _method, url, _kwargs in transport.calls)


@pytest.mark.asyncio
async def test_outlook_reads_only_selected_folders_and_fetches_body_on_open() -> None:
    token = _NoArgumentToken()
    transport = _Transport(
        [
            {
                "value": [
                    {
                        "id": "m1",
                        "conversationId": "c1",
                        "from": {
                            "emailAddress": {
                                "name": "Sender",
                                "address": "sender@example.test",
                            }
                        },
                        "subject": "Subject",
                        "bodyPreview": "Preview",
                        "receivedDateTime": NOW.isoformat(),
                        "isRead": False,
                        "importance": "high",
                        "webLink": "https://outlook.office.com/mail/inbox/id/m1",
                    }
                ]
            },
            {"id": "m1", "body": {"contentType": "html", "content": "<p>Body</p>"}},
        ]
    )
    source = OutlookMessageSource(
        token,
        transport,
        source_id="outlook-primary",
        account_id="selected-account",
    )
    snapshot = await source.list_summaries(
        MessageQuery(folders=("inbox",), unread_only=True, important_only=True)
    )
    assert token.calls == 1
    assert len(transport.calls) == 1
    assert "/mailFolders/inbox/messages" in transport.calls[0][1]
    assert transport.calls[0][2]["params"]["$filter"] == (
        "isRead eq false and importance eq 'high'"
    )
    assert snapshot.summaries[0].importance == "important"
    body = await source.get_body("m1")
    assert token.calls == 2
    assert body.plain_text == "Body"
    assert transport.calls[-1][2]["params"] == {"$select": "id,body"}


@pytest.mark.asyncio
async def test_slack_uses_only_selected_channels_or_saved_query_and_read_scopes() -> None:
    channel_token = _Token()
    channel_transport = _Transport(
        [
            {
                "ok": True,
                "messages": [{"ts": "1788273000.000001", "user": "U1", "text": "Channel body"}],
            },
            {
                "ok": True,
                "messages": [{"ts": "1788273000.000001", "user": "U1", "text": "Full body"}],
            },
        ]
    )
    channel_source = SlackMessageSource(
        channel_token,
        channel_transport,
        source_id="slack-channel",
        account_id="T123",
    )
    snapshot = await channel_source.list_summaries(MessageQuery(channels=("C123",), limit=10))
    assert channel_token.calls == [("channels:history",)]
    assert channel_transport.calls[0][2]["params"]["channel"] == "C123"
    assert len(snapshot.summaries) == 1
    await channel_source.get_body(snapshot.summaries[0].message_id)
    assert channel_transport.calls[-1][1].endswith("/conversations.replies")

    query_token = _Token()
    query_transport = _Transport(
        [
            {
                "ok": True,
                "messages": {
                    "matches": [
                        {
                            "ts": "1788273001.000001",
                            "user": "U2",
                            "text": "Search result",
                            "channel": {"id": "C456"},
                        }
                    ]
                },
            }
        ]
    )
    query_source = SlackMessageSource(
        query_token,
        query_transport,
        source_id="slack-query",
        account_id="T123",
    )
    await query_source.list_summaries(MessageQuery(query="project alpha", limit=10))
    assert query_token.calls == [("search:read",)]
    assert query_transport.calls[0][2]["params"]["query"] == "project alpha"


@pytest.mark.asyncio
async def test_offline_mode_prevents_token_and_transport_access() -> None:
    token = _Token()
    transport = _Transport([])
    source = GmailMessageSource(token, transport, offline=True)
    with pytest.raises(MessageOfflineError):
        await source.list_summaries(MessageQuery(folders=("INBOX",)))
    assert token.calls == []
    assert transport.calls == []


@pytest.mark.asyncio
async def test_service_retention_session_only_restart_and_body_memory_boundary(
    tmp_path: Path,
) -> None:
    database = HubDatabase(tmp_path / "hub.db")
    database.migrate()
    repository = MessageRepository(database)
    session_config = _config(retention_days=0)
    repository.add_source(session_config)
    service = MessageService(repository)
    source = _Source(session_config)
    service.register(source)
    result = await service.refresh(session_config.source_id, force=True, now=NOW)
    assert result.state == "ready"
    assert repository.counts() == (1, 0)
    assert len(service.summaries()) == 1
    assert MessageService(repository).summaries() == ()

    retained_config = session_config.model_copy(
        update={"retention_days": 7, "updated_at": NOW + timedelta(minutes=1)}
    )
    repository.add_source(retained_config)
    await service.refresh(
        session_config.source_id,
        force=True,
        now=NOW + timedelta(minutes=1),
    )
    assert repository.counts() == (1, 1)
    assert len(MessageService(repository).summaries()) == 1

    message_id = service.summaries()[0].message_id
    body = await service.get_body(session_config.source_id, message_id)
    assert body.plain_text == "BODY SECRET 73821"
    assert service.cached_body(session_config.source_id, message_id) is body
    raw_database = (tmp_path / "hub.db").read_bytes()
    assert b"BODY SECRET 73821" not in raw_database
    service.clear_bodies()
    assert service.cached_body(session_config.source_id, message_id) is None
    assert repository.cleanup(now=NOW + timedelta(days=8)) == 1
    assert repository.counts() == (1, 0)
    database.close()


@pytest.mark.asyncio
async def test_message_sources_fail_independently_and_keep_last_good_summaries(
    tmp_path: Path,
) -> None:
    database = HubDatabase(tmp_path / "hub.db")
    database.migrate()
    repository = MessageRepository(database)
    first = _config("gmail-good")
    second = _config("gmail-failing")
    repository.add_source(first)
    repository.add_source(second)
    service = MessageService(repository)
    good = _Source(first)
    failing = _Source(second, failure=RuntimeError("provider secret must not escape"))
    service.register(good)
    service.register(failing)
    results = await service.refresh_all(force=True, now=NOW)
    assert results[first.source_id].state == "ready"
    assert results[second.source_id].state == "error"
    assert "secret" not in results[second.source_id].safe_message
    assert len(service.summaries(source_id=first.source_id)) == 1

    good.failure = RuntimeError("later failure")
    stale = await service.refresh(first.source_id, force=True, now=NOW + timedelta(hours=1))
    assert stale.state == "error"
    assert len(service.summaries(source_id=first.source_id)) == 1
    service.set_offline(True)
    calls = good.list_calls
    offline = await service.refresh(
        first.source_id,
        force=True,
        now=NOW + timedelta(hours=2),
    )
    assert offline.state == "offline"
    assert good.list_calls == calls
    assert offline.snapshot is not None and len(offline.snapshot.summaries) == 1
    database.close()


@pytest.mark.asyncio
async def test_controller_projects_sensitive_today_items_and_count_only_digest(
    tmp_path: Path,
) -> None:
    settings = HubSettings(data_dir=tmp_path / "hub", default_timezone="UTC")
    with build_hub_container(settings) as container:
        config = container.controller.create_message_source(
            source_id="gmail-primary",
            provider="gmail",
            account_id="selected-account",
            query=MessageQuery(folders=("INBOX",)),
            now=NOW,
        )
        source = _Source(config)
        container.controller.register_message_adapter(source)
        await container.controller.refresh_messages(force=True, now=NOW)
        items = container.controller.message_today_items()
        assert len(items) == 1 and items[0].sensitive
        digest = container.controller.reconcile_message_digest(now=NOW)
        assert digest is not None
        assert digest.safe_summary == "1 unread message(s) from 1 selected source(s)."
        assert "Sensitive Sender" not in digest.model_dump_json()
        assert "Quarterly plan" not in digest.model_dump_json()
        assert container.controller.reconcile_message_digest(now=NOW) is None
        audit = container.audit_repository.list(limit=10)
        assert all("Sensitive Sender" not in event.model_dump_json() for event in audit)


def test_message_oauth_factories_request_only_read_scopes() -> None:
    gmail = gmail_oauth_config(
        client_id="client",
        redirect_uri="http://127.0.0.1:8765/callback",
    )
    outlook = outlook_mail_oauth_config(
        client_id="client",
        redirect_uri="http://127.0.0.1:8765/callback",
    )
    slack = slack_messages_oauth_config(
        client_id="client",
        redirect_uri="http://127.0.0.1:8765/callback",
        scopes=("channels:history", "search:read"),
    )
    assert gmail.scopes == (GMAIL_READONLY_SCOPE,)
    assert outlook.scopes == (OUTLOOK_MAIL_READ_SCOPE,)
    assert set(slack.scopes) == {"channels:history", "search:read"}
    assert not any(
        scope.casefold().endswith(("send", "write"))
        for config in (gmail, outlook, slack)
        for scope in config.scopes
    )
    with pytest.raises(ValueError):
        slack_messages_oauth_config(
            client_id="client",
            redirect_uri="http://127.0.0.1:8765/callback",
            scopes=("chat:write",),
        )


def test_message_adapters_expose_no_mutation_surface() -> None:
    forbidden = {
        "send",
        "reply",
        "delete",
        "archive",
        "mark_read",
        "react",
        "upload",
        "move",
    }
    for adapter in (GmailMessageSource, OutlookMessageSource, SlackMessageSource):
        assert forbidden.isdisjoint(set(dir(adapter)))
