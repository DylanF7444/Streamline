from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from personal_hub.config import HubSettings
from personal_hub.integrations import (
    GOOGLE_CALENDAR_READ_SCOPE,
    OAuthConnectionError,
    OAuthConnectionService,
    OAuthProviderConfig,
    google_calendar_oauth_config,
    outlook_calendar_oauth_config,
)
from personal_hub.storage import DeterministicTestCipher, HubSecretVault


class _Transport:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, str], float, int]] = []

    async def post_form(
        self,
        url: str,
        data: dict[str, str],
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ):
        self.calls.append((url, data, timeout_seconds, max_response_bytes))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _vault(tmp_path: Path) -> HubSecretVault:
    settings = HubSettings(data_dir=tmp_path / "hub")
    return HubSecretVault(settings, DeterministicTestCipher())


@pytest.mark.asyncio
async def test_google_pkce_connection_stores_only_hub_vault_tokens_and_refreshes(tmp_path: Path):
    clock = [datetime(2026, 8, 31, 12, tzinfo=UTC)]
    transport = _Transport(
        {
            "access_token": "first-access",
            "refresh_token": "durable-refresh",
            "expires_in": 60,
            "scope": GOOGLE_CALENDAR_READ_SCOPE,
        },
        {
            "access_token": "second-access",
            "expires_in": 3_600,
            "scope": GOOGLE_CALENDAR_READ_SCOPE,
        },
    )
    service = OAuthConnectionService(
        google_calendar_oauth_config(
            client_id="hub-client",
            redirect_uri="http://127.0.0.1:8765/oauth/callback",
        ),
        _vault(tmp_path),
        transport,
        now=lambda: clock[0],
    )

    request = service.begin_authorization()
    query = parse_qs(urlsplit(request.authorization_url).query)
    assert query["scope"] == [GOOGLE_CALENDAR_READ_SCOPE]
    assert query["code_challenge_method"] == ["S256"]
    assert "code_challenge" in query
    assert "code_verifier" not in query

    status = await service.complete_authorization(code="provider-code", state=request.state)
    assert status.connected
    assert service.status().scopes == (GOOGLE_CALENDAR_READ_SCOPE,)
    assert "provider-code" not in repr(service.status())

    clock[0] += timedelta(minutes=2)
    assert await service.access_token() == "second-access"
    assert transport.calls[1][1]["grant_type"] == "refresh_token"
    assert transport.calls[1][1]["refresh_token"] == "durable-refresh"
    assert transport.calls[1][3] == 64 * 1024

    service.disconnect()
    assert not service.status().connected


@pytest.mark.asyncio
async def test_microsoft_offline_access_is_satisfied_by_refresh_token(tmp_path: Path):
    transport = _Transport(
        {
            "access_token": "microsoft-access",
            "refresh_token": "microsoft-refresh",
            "expires_in": 1,
            "scope": "Calendars.Read",
        },
        {
            "access_token": "microsoft-access-renewed",
            "expires_in": 3_600,
            "scope": "Calendars.Read",
        },
    )
    service = OAuthConnectionService(
        OAuthProviderConfig(
            provider_id="microsoft",
            authorization_url="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
            token_url="https://login.microsoftonline.com/common/oauth2/v2.0/token",
            client_id="hub-client",
            redirect_uri="http://127.0.0.1:8765/oauth/callback",
            scopes=("offline_access", "Calendars.Read"),
            allowed_scopes=("offline_access", "Calendars.Read"),
        ),
        _vault(tmp_path),
        transport,
    )

    request = service.begin_authorization()
    status = await service.complete_authorization(code="provider-code", state=request.state)

    assert status.connected
    assert status.scopes == ("Calendars.Read", "offline_access")
    assert await service.access_token() == "microsoft-access-renewed"


@pytest.mark.asyncio
async def test_oauth_rejects_state_scope_and_provider_content_safely(tmp_path: Path):
    config = google_calendar_oauth_config(
        client_id="hub-client",
        redirect_uri="http://localhost:8765/callback",
    )
    service = OAuthConnectionService(config, _vault(tmp_path), _Transport({}))
    with pytest.raises(OAuthConnectionError, match="invalid or expired"):
        await service.complete_authorization(code="code", state="attacker-state")

    transport = _Transport(RuntimeError("secret provider response"))
    service = OAuthConnectionService(config, _vault(tmp_path / "other"), transport)
    request = service.begin_authorization()
    with pytest.raises(OAuthConnectionError) as captured:
        await service.complete_authorization(code="code", state=request.state)
    assert "secret provider response" not in str(captured.value)

    with pytest.raises(ValueError, match="not approved"):
        OAuthProviderConfig(
            provider_id="unsafe",
            authorization_url="https://example.test/authorize",
            token_url="https://example.test/token",
            client_id="client",
            redirect_uri="https://app.example.test/callback",
            scopes=("calendar.write",),
            allowed_scopes=("calendar.read",),
        )


def test_calendar_provider_configs_request_only_read_scope():
    google = google_calendar_oauth_config(
        client_id="client",
        redirect_uri="http://127.0.0.1:8765/callback",
    )
    outlook = outlook_calendar_oauth_config(
        client_id="client",
        redirect_uri="http://127.0.0.1:8765/callback",
    )

    assert google.scopes == ("https://www.googleapis.com/auth/calendar.readonly",)
    assert outlook.scopes == ("Calendars.Read",)
    assert all("write" not in scope.casefold() for scope in google.scopes + outlook.scopes)
