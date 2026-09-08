"""Hub-owned OAuth 2.0 PKCE connection boundary for read-only providers.

Authorization state and verifier material live only in memory. Tokens are written only to
the Personal Hub vault and transport responses are never included in errors or audit data.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlencode, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from personal_hub.storage import HubSecretVault

GOOGLE_CALENDAR_READ_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
OUTLOOK_CALENDAR_READ_SCOPE = "Calendars.Read"
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
OUTLOOK_MAIL_READ_SCOPE = "Mail.Read"
SLACK_MESSAGE_READ_SCOPES = frozenset({"channels:history", "groups:history", "search:read"})
_PROVIDER_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")


class OAuthConnectionError(RuntimeError):
    """Safe OAuth connection failure without provider response content."""


class OAuthProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    provider_id: str = Field(min_length=1, max_length=32)
    authorization_url: str = Field(min_length=1, max_length=512)
    token_url: str = Field(min_length=1, max_length=512)
    client_id: str = Field(min_length=1, max_length=512)
    client_secret: str | None = Field(default=None, min_length=1, max_length=4_096)
    redirect_uri: str = Field(min_length=1, max_length=512)
    scopes: tuple[str, ...] = Field(min_length=1, max_length=16)
    allowed_scopes: tuple[str, ...] = Field(min_length=1, max_length=16)
    extra_authorization_parameters: dict[str, str] = Field(default_factory=dict)

    @field_validator("provider_id")
    @classmethod
    def _provider_id_is_safe(cls, value: str) -> str:
        if _PROVIDER_PATTERN.fullmatch(value) is None:
            raise ValueError("OAuth provider_id must be a simple Hub identifier")
        return value

    @field_validator("authorization_url", "token_url")
    @classmethod
    def _provider_endpoints_are_https(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("OAuth provider endpoints must be credential-free HTTPS URLs")
        return value

    @field_validator("redirect_uri")
    @classmethod
    def _redirect_is_loopback_or_https(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme == "https" and parsed.hostname:
            return value
        if parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
            return value
        raise ValueError("OAuth redirect must be HTTPS or an exact loopback URI")

    @model_validator(mode="after")
    def _requested_scopes_are_explicitly_allowed(self) -> OAuthProviderConfig:
        if len(set(self.scopes)) != len(self.scopes):
            raise ValueError("OAuth scopes must be unique")
        if not set(self.scopes).issubset(self.allowed_scopes):
            raise ValueError("OAuth requested scope is not approved for this Hub connection")
        return self


class OAuthAuthorizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_id: str
    authorization_url: str
    state: str
    expires_at: datetime


class OAuthTokenStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_id: str
    connected: bool
    scopes: tuple[str, ...] = ()
    expires_at: datetime | None = None


@runtime_checkable
class OAuthTransport(Protocol):
    async def post_form(
        self,
        url: str,
        data: dict[str, str],
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> dict[str, Any]: ...


class OAuthConnectionService:
    """Perform one provider's PKCE exchange through an injected bounded transport."""

    def __init__(
        self,
        config: OAuthProviderConfig,
        vault: HubSecretVault,
        transport: OAuthTransport,
        *,
        timeout_seconds: float = 20.0,
        authorization_ttl: timedelta = timedelta(minutes=10),
        now: Any = None,
    ) -> None:
        if not 1 <= timeout_seconds <= 60:
            raise ValueError("OAuth timeout must be between 1 and 60 seconds")
        if not timedelta(minutes=1) <= authorization_ttl <= timedelta(minutes=30):
            raise ValueError("OAuth authorization TTL must be between 1 and 30 minutes")
        self.config = config
        self.vault = vault
        self.transport = transport
        self.timeout_seconds = float(timeout_seconds)
        self.authorization_ttl = authorization_ttl
        self._now = now or (lambda: datetime.now(UTC))
        self._pending: dict[str, tuple[str, datetime]] = {}

    def begin_authorization(self) -> OAuthAuthorizationRequest:
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
            .decode("ascii")
            .rstrip("=")
        )
        expires_at = self._now() + self.authorization_ttl
        self._pending[state] = (verifier, expires_at)
        parameters = {
            "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.config.scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            **self.config.extra_authorization_parameters,
        }
        return OAuthAuthorizationRequest(
            provider_id=self.config.provider_id,
            authorization_url=f"{self.config.authorization_url}?{urlencode(parameters)}",
            state=state,
            expires_at=expires_at,
        )

    async def complete_authorization(self, *, code: str, state: str) -> OAuthTokenStatus:
        pending = self._pending.pop(state, None)
        if pending is None:
            raise OAuthConnectionError("OAuth authorization state is invalid or expired")
        verifier, expires_at = pending
        if self._now() >= expires_at:
            raise OAuthConnectionError("OAuth authorization state is invalid or expired")
        if not code or len(code) > 4_096:
            raise OAuthConnectionError("OAuth authorization code is invalid")
        response = await self._token_request(
            {
                "grant_type": "authorization_code",
                "client_id": self.config.client_id,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": self.config.redirect_uri,
            }
        )
        return self._store_token_response(response)

    async def access_token(self) -> str:
        token = self.vault.get(self._name("access"))
        expiry_text = self.vault.get(self._name("expires"))
        if token and expiry_text:
            try:
                expiry = datetime.fromisoformat(expiry_text)
            except ValueError:
                expiry = datetime.min.replace(tzinfo=UTC)
            if expiry.tzinfo is not None and expiry > self._now() + timedelta(seconds=30):
                return token
        refresh_token = self.vault.get(self._name("refresh"))
        if not refresh_token:
            raise OAuthConnectionError("Calendar connection is not available")
        response = await self._token_request(
            {
                "grant_type": "refresh_token",
                "client_id": self.config.client_id,
                "refresh_token": refresh_token,
                "scope": " ".join(self.config.scopes),
            }
        )
        self._store_token_response(response, existing_refresh=refresh_token)
        token = self.vault.get(self._name("access"))
        if not token:  # pragma: no cover - guarded by response validation
            raise OAuthConnectionError("Calendar connection is not available")
        return token

    def status(self) -> OAuthTokenStatus:
        scopes = self._stored_scopes()
        expiry_text = self.vault.get(self._name("expires"))
        expiry: datetime | None = None
        if expiry_text:
            try:
                expiry = datetime.fromisoformat(expiry_text)
            except ValueError:
                expiry = None
        return OAuthTokenStatus(
            provider_id=self.config.provider_id,
            connected=self.vault.has(self._name("refresh")),
            scopes=scopes,
            expires_at=expiry,
        )

    def disconnect(self) -> None:
        for suffix in ("access", "refresh", "expires", "scopes"):
            self.vault.delete(self._name(suffix))
        self._pending.clear()

    async def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        if self.config.client_secret:
            data = {**data, "client_secret": self.config.client_secret}
        try:
            response = await self.transport.post_form(
                self.config.token_url,
                data,
                timeout_seconds=self.timeout_seconds,
                max_response_bytes=64 * 1024,
            )
        except OAuthConnectionError:
            raise
        except Exception as error:
            raise OAuthConnectionError("OAuth provider request failed safely") from error
        if not isinstance(response, dict):
            raise OAuthConnectionError("OAuth provider returned an invalid response")
        return response

    def _store_token_response(
        self,
        response: dict[str, Any],
        *,
        existing_refresh: str | None = None,
    ) -> OAuthTokenStatus:
        access_token = response.get("access_token")
        refresh_token = response.get("refresh_token", existing_refresh)
        expires_in = response.get("expires_in", 3_600)
        if not isinstance(access_token, str) or not 1 <= len(access_token) <= 32_768:
            raise OAuthConnectionError("OAuth provider returned an invalid token response")
        if not isinstance(refresh_token, str) or not 1 <= len(refresh_token) <= 32_768:
            raise OAuthConnectionError("OAuth provider did not provide a durable connection")
        if not isinstance(expires_in, (int, float)) or not 1 <= float(expires_in) <= 86_400:
            raise OAuthConnectionError("OAuth provider returned an invalid token lifetime")
        granted = _parse_scopes(response.get("scope"), fallback=self.config.scopes)
        if not set(self.config.scopes).issubset(granted):
            raise OAuthConnectionError("OAuth provider did not grant the required read scope")
        expiry = self._now() + timedelta(seconds=float(expires_in))
        self.vault.set(self._name("access"), access_token)
        self.vault.set(self._name("refresh"), refresh_token)
        self.vault.set(self._name("expires"), expiry.isoformat())
        self.vault.set(self._name("scopes"), json.dumps(sorted(granted), separators=(",", ":")))
        return OAuthTokenStatus(
            provider_id=self.config.provider_id,
            connected=True,
            scopes=tuple(sorted(granted)),
            expires_at=expiry,
        )

    def _stored_scopes(self) -> tuple[str, ...]:
        encoded = self.vault.get(self._name("scopes"))
        if not encoded:
            return ()
        try:
            value = json.loads(encoded)
        except json.JSONDecodeError:
            return ()
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            return ()
        return tuple(value)

    def _name(self, suffix: str) -> str:
        return f"oauth_{self.config.provider_id}_{suffix}"


def google_calendar_oauth_config(
    *,
    client_id: str,
    redirect_uri: str,
) -> OAuthProviderConfig:
    return OAuthProviderConfig(
        provider_id="google_calendar",
        authorization_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        client_id=client_id,
        redirect_uri=redirect_uri,
        scopes=(GOOGLE_CALENDAR_READ_SCOPE,),
        allowed_scopes=(GOOGLE_CALENDAR_READ_SCOPE,),
        extra_authorization_parameters={"access_type": "offline", "prompt": "consent"},
    )


def outlook_calendar_oauth_config(
    *,
    client_id: str,
    redirect_uri: str,
    tenant: str = "common",
) -> OAuthProviderConfig:
    normalized_tenant = tenant.strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", normalized_tenant):
        raise ValueError("Microsoft OAuth tenant is invalid")
    base = f"https://login.microsoftonline.com/{normalized_tenant}/oauth2/v2.0"
    return OAuthProviderConfig(
        provider_id="outlook_calendar",
        authorization_url=f"{base}/authorize",
        token_url=f"{base}/token",
        client_id=client_id,
        redirect_uri=redirect_uri,
        scopes=(OUTLOOK_CALENDAR_READ_SCOPE,),
        allowed_scopes=(OUTLOOK_CALENDAR_READ_SCOPE,),
    )


def gmail_oauth_config(*, client_id: str, redirect_uri: str) -> OAuthProviderConfig:
    return OAuthProviderConfig(
        provider_id="gmail",
        authorization_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        client_id=client_id,
        redirect_uri=redirect_uri,
        scopes=(GMAIL_READONLY_SCOPE,),
        allowed_scopes=(GMAIL_READONLY_SCOPE,),
        extra_authorization_parameters={"access_type": "offline", "prompt": "consent"},
    )


def outlook_mail_oauth_config(
    *, client_id: str, redirect_uri: str, tenant: str = "common"
) -> OAuthProviderConfig:
    normalized_tenant = tenant.strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", normalized_tenant):
        raise ValueError("Microsoft OAuth tenant is invalid")
    base = f"https://login.microsoftonline.com/{normalized_tenant}/oauth2/v2.0"
    return OAuthProviderConfig(
        provider_id="outlook_mail",
        authorization_url=f"{base}/authorize",
        token_url=f"{base}/token",
        client_id=client_id,
        redirect_uri=redirect_uri,
        scopes=(OUTLOOK_MAIL_READ_SCOPE,),
        allowed_scopes=(OUTLOOK_MAIL_READ_SCOPE,),
    )


def slack_messages_oauth_config(
    *, client_id: str, redirect_uri: str, scopes: tuple[str, ...]
) -> OAuthProviderConfig:
    if not scopes or not set(scopes).issubset(SLACK_MESSAGE_READ_SCOPES):
        raise ValueError("Slack message OAuth accepts only explicitly selected read scopes")
    return OAuthProviderConfig(
        provider_id="slack_messages",
        authorization_url="https://slack.com/oauth/v2/authorize",
        token_url="https://slack.com/api/oauth.v2.access",
        client_id=client_id,
        redirect_uri=redirect_uri,
        scopes=scopes,
        allowed_scopes=tuple(sorted(SLACK_MESSAGE_READ_SCOPES)),
    )


def _parse_scopes(value: object, *, fallback: tuple[str, ...]) -> frozenset[str]:
    if value is None:
        return frozenset(fallback)
    if isinstance(value, str):
        scopes = tuple(value.split())
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        scopes = tuple(value)
    else:
        raise OAuthConnectionError("OAuth provider returned invalid scope metadata")
    return frozenset(scopes)


__all__ = [
    "GOOGLE_CALENDAR_READ_SCOPE",
    "OUTLOOK_CALENDAR_READ_SCOPE",
    "GMAIL_READONLY_SCOPE",
    "OUTLOOK_MAIL_READ_SCOPE",
    "SLACK_MESSAGE_READ_SCOPES",
    "OAuthAuthorizationRequest",
    "OAuthConnectionError",
    "OAuthConnectionService",
    "OAuthProviderConfig",
    "OAuthTokenStatus",
    "OAuthTransport",
    "google_calendar_oauth_config",
    "outlook_calendar_oauth_config",
    "gmail_oauth_config",
    "outlook_mail_oauth_config",
    "slack_messages_oauth_config",
]
