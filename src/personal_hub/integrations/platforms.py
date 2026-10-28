"""User-configurable, read-only external platform connections for Personal Hub."""

from __future__ import annotations

import asyncio
import contextlib
import html
import re
import threading
import urllib.parse
import webbrowser
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from personal_hub.calendar import GoogleCalendarAdapter, OutlookCalendarSource
from personal_hub.calendar.google_sync import CalendarSyncReport, GoogleCalendarSync
from personal_hub.calendar.native import LocalCalendarItems
from personal_hub.calendar.service import CalendarService
from personal_hub.integrations.canvas import CanvasIcalTaskImporter
from personal_hub.integrations.http import BoundedProviderTransport
from personal_hub.integrations.oauth import (
    GMAIL_READONLY_SCOPE,
    GOOGLE_CALENDAR_EVENTS_SCOPE,
    GOOGLE_CALENDAR_READ_SCOPE,
    OUTLOOK_CALENDAR_READ_SCOPE,
    OUTLOOK_MAIL_READ_SCOPE,
    OAuthConnectionError,
    OAuthConnectionService,
    OAuthProviderConfig,
    OAuthTokenStatus,
)
from personal_hub.messages import (
    GmailMessageSource,
    MessageQuery,
    MessageRepository,
    MessageService,
    MessageSourceConfig,
    OutlookMessageSource,
    SlackMessageSource,
)
from personal_hub.sources import (
    SourceDisconnectedError,
    SourcePermissionRequiredError,
    SourceRegistry,
    SourceSnapshot,
    SourceState,
    SourceStatus,
)
from personal_hub.storage import HubSecretVault, SettingsRepository
from personal_hub.tasks import TaskRepository

_SETTINGS_KEY: Final = "external_platform_connections.v1"
_CALLBACK_PATH: Final = "/personal-hub/oauth/callback"
_MICROSOFT_OFFLINE_SCOPE: Final = "offline_access"
_PLATFORM_IDS: Final = frozenset({"canvas", "google", "microsoft", "slack"})
_CAPABILITIES: Final[dict[str, frozenset[str]]] = {
    "canvas": frozenset({"tasks", "calendar"}),
    "google": frozenset({"calendar", "messages"}),
    "microsoft": frozenset({"calendar", "messages"}),
    "slack": frozenset({"messages"}),
}
_SLACK_SCOPES: Final = frozenset({"channels:history", "search:read"})


class ExternalPlatformDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    platform_id: Literal["canvas", "google", "microsoft", "slack"]
    source_id: str
    display_name: str
    description: str
    capabilities: tuple[str, ...]
    authentication: Literal["oauth_pkce", "api_token", "ical_url"]


PLATFORM_DEFINITIONS: Final = (
    ExternalPlatformDefinition(
        platform_id="canvas",
        source_id="platform-canvas",
        display_name="Canvas Student",
        description=(
            "Import every Canvas calendar item as a local to-do with a due date. "
            "Due tasks also appear in Personal Hub Calendar."
        ),
        capabilities=("tasks", "calendar"),
        authentication="ical_url",
    ),
    ExternalPlatformDefinition(
        platform_id="google",
        source_id="platform-google",
        display_name="Google",
        description="Read selected Google Calendar events and Gmail attention summaries.",
        capabilities=("calendar", "messages"),
        authentication="oauth_pkce",
    ),
    ExternalPlatformDefinition(
        platform_id="microsoft",
        source_id="platform-microsoft",
        display_name="Microsoft",
        description="Read selected Outlook Calendar events and Outlook Mail summaries.",
        capabilities=("calendar", "messages"),
        authentication="oauth_pkce",
    ),
    ExternalPlatformDefinition(
        platform_id="slack",
        source_id="platform-slack",
        display_name="Slack",
        description="Read one selected channel or saved search with a read-only token.",
        capabilities=("messages",),
        authentication="api_token",
    ),
)
_DEFINITION_BY_PLATFORM: Final = {item.platform_id: item for item in PLATFORM_DEFINITIONS}
_PLATFORM_BY_SOURCE: Final = {item.source_id: item.platform_id for item in PLATFORM_DEFINITIONS}


class ExternalPlatformConfig(BaseModel):
    """Non-secret persisted setup for one reviewed platform."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    platform_id: Literal["canvas", "google", "microsoft", "slack"]
    client_id: str | None = Field(default=None, max_length=512)
    tenant: str = Field(default="common", min_length=1, max_length=64)
    account_label: str = Field(default="Connected account", min_length=1, max_length=160)
    capabilities: tuple[str, ...] = Field(min_length=1, max_length=2)
    slack_selection_kind: Literal["channel", "query"] = "channel"
    slack_selection: str | None = Field(default=None, max_length=500)
    slack_scopes: tuple[str, ...] = Field(default=(), max_length=2)
    canvas_origin: str | None = Field(default=None, max_length=512)
    enabled: bool = True
    calendar_sync_enabled: bool = False

    @field_validator("client_id", "slack_selection", "canvas_origin")
    @classmethod
    def _bounded_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or any(character in normalized for character in "\r\n\x00"):
            return None
        return normalized

    @model_validator(mode="after")
    def _platform_fields_match(self) -> ExternalPlatformConfig:
        if self.calendar_sync_enabled and (
            self.platform_id != "google" or "calendar" not in self.capabilities
        ):
            raise ValueError("Calendar sync requires Google calendar access")
        allowed = _CAPABILITIES[self.platform_id]
        if not set(self.capabilities).issubset(allowed) or len(set(self.capabilities)) != len(
            self.capabilities
        ):
            raise ValueError("external platform capabilities are invalid")
        if self.platform_id in {"google", "microsoft"} and not self.client_id:
            raise ValueError("desktop OAuth client ID is required")
        if self.platform_id == "canvas" and not self.canvas_origin:
            raise ValueError("Canvas iCalendar feed is required")
        if self.platform_id == "canvas" and self.canvas_origin is not None:
            parsed_origin = urllib.parse.urlsplit(self.canvas_origin)
            if (
                parsed_origin.scheme.casefold() != "https"
                or not parsed_origin.hostname
                or parsed_origin.username is not None
                or parsed_origin.password is not None
                or parsed_origin.path not in {"", "/"}
                or parsed_origin.query
                or parsed_origin.fragment
            ):
                raise ValueError("Canvas origin is invalid")
        if (
            self.platform_id == "microsoft"
            and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", self.tenant) is None
        ):
            raise ValueError("Microsoft tenant is invalid")
        if self.platform_id == "slack":
            if not self.slack_selection:
                raise ValueError("Slack requires one selected channel or saved query")
            if (
                self.slack_selection_kind == "channel"
                and re.fullmatch(r"[A-Za-z0-9._-]{1,160}", self.slack_selection) is None
            ):
                raise ValueError("Slack channel ID is invalid")
            if not set(self.slack_scopes).issubset(_SLACK_SCOPES):
                raise ValueError("Slack accepts only reviewed read scopes")
            required = (
                "channels:history" if self.slack_selection_kind == "channel" else "search:read"
            )
            if required not in self.slack_scopes:
                raise ValueError("Slack token scope does not cover the selected view")
        return self


class ExternalPlatformStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    platform_id: Literal["canvas", "google", "microsoft", "slack"]
    source_id: str
    display_name: str
    state: SourceState
    safe_message: str = Field(max_length=200)
    configured: bool
    enabled: bool
    account_label: str | None = Field(default=None, max_length=160)
    capabilities: tuple[str, ...] = ()


OAuthServiceFactory = Callable[[str], OAuthConnectionService]
OAuthAuthorizer = Callable[[OAuthServiceFactory], Awaitable[OAuthTokenStatus]]


async def authorize_oauth_with_loopback(
    service_factory: OAuthServiceFactory,
    *,
    browser_open: Callable[[str], bool] = webbrowser.open,
    timeout_seconds: int = 180,
) -> OAuthTokenStatus:
    """Complete one state/PKCE-protected OAuth flow through a loopback callback."""

    values: dict[str, str] = {}
    completed = threading.Event()

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path != _CALLBACK_PATH:
                self.send_error(404)
                return
            query = urllib.parse.parse_qs(parsed.query)
            for key in ("code", "state", "error"):
                value = query.get(key, [""])[0]
                if isinstance(value, str):
                    values[key] = value[:4_096]
            heading = "Authorization received"
            message = "You can return to Personal Hub."
            document = (
                '<!doctype html><html lang="en"><head><meta charset="utf-8">'
                "<title>Personal Hub</title></head><body><main>"
                f"<h1>{html.escape(heading)}</h1><p>{html.escape(message)}</p>"
                "</main></body></html>"
            ).encode()
            with contextlib.suppress(OSError):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(document)))
                self.end_headers()
                self.wfile.write(document)
            completed.set()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), CallbackHandler)
    redirect_uri = f"http://127.0.0.1:{server.server_port}{_CALLBACK_PATH}"
    service = service_factory(redirect_uri)
    request = service.begin_authorization()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if not browser_open(request.authorization_url):
            raise OAuthConnectionError("The system browser could not be opened")
        wait_seconds = max(30, min(int(timeout_seconds), 600))
        if not await asyncio.to_thread(completed.wait, wait_seconds):
            raise OAuthConnectionError("OAuth authorization timed out")
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=1)
    remote_error = re.sub(r"[^A-Za-z0-9._ -]", "", values.get("error", ""))[:80]
    if remote_error:
        raise OAuthConnectionError(f"Authorization was declined ({remote_error})")
    code, state = values.get("code", ""), values.get("state", "")
    if not code or not state:
        raise OAuthConnectionError("OAuth provider did not return a verified authorization")
    return await service.complete_authorization(code=code, state=state)


class _OAuthTokenProvider:
    def __init__(self, manager: ExternalPlatformManager, platform_id: str) -> None:
        self.manager = manager
        self.platform_id = platform_id

    async def get_token(self, scopes: tuple[str, ...] = ()) -> str:
        service = self.manager._oauth_service(self.platform_id)
        requested = set(scopes)
        if not requested.issubset(service.config.scopes):
            raise PermissionError("Requested provider scope is not configured")
        return await service.access_token()

    async def access_token(self) -> str:
        return await self.get_token()


class _SlackTokenProvider:
    def __init__(self, manager: ExternalPlatformManager) -> None:
        self.manager = manager

    async def get_token(self, scopes: tuple[str, ...] = ()) -> str:
        config = self.manager.configuration("slack")
        token = self.manager.vault.get(self.manager._secret_name("slack", "token"))
        if config is None or not token:
            raise SourceDisconnectedError("Slack is not connected")
        if not set(scopes).issubset(config.slack_scopes):
            raise SourcePermissionRequiredError("Slack read permission is required")
        return token


class _PlatformSource:
    def __init__(self, manager: ExternalPlatformManager, definition: ExternalPlatformDefinition):
        self.manager = manager
        self.definition = definition
        self.source_id = definition.source_id
        self.provider_id = definition.platform_id
        self.display_name = definition.display_name

    async def connect(self) -> SourceStatus:
        return self.manager._source_status(await self.manager.connect(self.provider_id))

    async def status(self) -> SourceStatus:
        return self.manager._source_status(await self.manager.health(self.provider_id))

    async def refresh(self) -> SourceSnapshot | SourceStatus:
        if self.provider_id == "canvas":
            return await self.manager.refresh_canvas()
        status = await self.status()
        if status.state is not SourceState.READY:
            return status
        return SourceSnapshot(
            source_id=self.source_id,
            provider_id=self.provider_id,
            collected_at=datetime.now(UTC),
            metadata={"capabilities": ",".join(self.definition.capabilities)},
        )

    async def disconnect(self) -> None:
        await self.manager.disconnect(self.provider_id)


class ExternalPlatformManager:
    """Persist setup, own Hub credentials, and wire read-only provider adapters."""

    def __init__(
        self,
        settings_repository: SettingsRepository,
        vault: HubSecretVault,
        source_registry: SourceRegistry,
        calendar_service: CalendarService,
        message_repository: MessageRepository,
        message_service: MessageService,
        task_repository: TaskRepository,
        *,
        message_refresh_minutes: int = 10,
        message_retention_days: int = 7,
        default_timezone: str = "UTC",
        remove_completed_tasks_after_due_date: bool = False,
        canvas_remove_completed_assignments: bool | None = None,
        transport: BoundedProviderTransport | None = None,
        canvas_importer: CanvasIcalTaskImporter | None = None,
        oauth_authorizer: OAuthAuthorizer = authorize_oauth_with_loopback,
    ) -> None:
        self.settings_repository = settings_repository
        self.vault = vault
        self.source_registry = source_registry
        self.calendar_service = calendar_service
        self.message_repository = message_repository
        self.message_service = message_service
        self.task_repository = task_repository
        self.message_refresh_minutes = message_refresh_minutes
        self.message_retention_days = message_retention_days
        self.default_timezone = default_timezone
        self.remove_completed_tasks_after_due_date = bool(
            remove_completed_tasks_after_due_date
            if canvas_remove_completed_assignments is None
            else canvas_remove_completed_assignments
        )
        self.transport = transport or BoundedProviderTransport()
        self.oauth_authorizer = oauth_authorizer
        self.canvas_importer = canvas_importer or CanvasIcalTaskImporter(task_repository)
        self._configs = self._load()
        self._calendar_adapters: dict[str, object] = {}
        self._message_adapters: dict[str, object] = {}
        for definition in PLATFORM_DEFINITIONS:
            config = self._configs.get(definition.platform_id)
            source_registry.register(
                _PlatformSource(self, definition),
                source_id=definition.source_id,
                provider_id=definition.platform_id,
                display_name=definition.display_name,
                enabled=False if config is None else config.enabled,
            )
        self._sync_domain_sources()

    @staticmethod
    def platform_id(value: str) -> str:
        normalized = _PLATFORM_BY_SOURCE.get(value, value)
        if normalized not in _PLATFORM_IDS:
            raise KeyError("Unknown external platform")
        return normalized

    def definitions(self) -> tuple[ExternalPlatformDefinition, ...]:
        return PLATFORM_DEFINITIONS

    def configuration(self, platform_id: str) -> ExternalPlatformConfig | None:
        return self._configs.get(self.platform_id(platform_id))

    async def sync_calendar_items(self, local: LocalCalendarItems) -> CalendarSyncReport:
        def allowed() -> bool:
            config = self.configuration("google")
            return bool(
                config
                and config.enabled
                and config.calendar_sync_enabled
                and not self.calendar_service.offline
            )

        async def token() -> str:
            return await _OAuthTokenProvider(self, "google").get_token(
                (GOOGLE_CALENDAR_EVENTS_SCOPE,)
            )

        if allowed():
            status = self._oauth_service("google").status()
            if not status.connected or GOOGLE_CALENDAR_EVENTS_SCOPE not in status.scopes:
                return CalendarSyncReport(
                    message="Reconnect Google to grant calendar event sync access. Local items are saved."
                )
        return await GoogleCalendarSync(local, self.transport, token, allowed).sync()

    def configure(
        self,
        platform_id: str,
        values: Mapping[str, object],
    ) -> ExternalPlatformConfig:
        selected = self.platform_id(platform_id)
        current = self._configs.get(selected)
        payload = {} if current is None else current.model_dump(mode="python")
        payload.update(values)
        payload.pop("client_secret", None)
        payload.pop("token", None)
        payload.pop("feed_url", None)
        payload["platform_id"] = selected
        payload.setdefault("enabled", True if current is None else current.enabled)
        payload.setdefault("account_label", _DEFINITION_BY_PLATFORM[selected].display_name)
        payload.setdefault("tenant", "common")
        payload.setdefault("capabilities", tuple(_DEFINITION_BY_PLATFORM[selected].capabilities))
        payload.setdefault("slack_selection_kind", "channel")
        if selected == "slack":
            kind = str(payload.get("slack_selection_kind", "channel"))
            payload.setdefault(
                "slack_scopes",
                ("channels:history",) if kind == "channel" else ("search:read",),
            )
        feed_url = values.get("feed_url")
        if selected == "canvas" and isinstance(feed_url, str) and feed_url.strip():
            payload["canvas_origin"] = self.canvas_importer.validate_feed_url(feed_url.strip())
        config = ExternalPlatformConfig.model_validate(payload)
        client_secret = values.get("client_secret")
        if (
            selected in {"google", "microsoft"}
            and isinstance(client_secret, str)
            and client_secret.strip()
        ):
            self.vault.set(self._secret_name(selected, "client_secret"), client_secret.strip())
        token = values.get("token")
        if selected == "slack" and isinstance(token, str) and token.strip():
            self.vault.set(self._secret_name("slack", "token"), token.strip())
        if selected == "slack" and not self.vault.has(self._secret_name("slack", "token")):
            raise ValueError("Slack read-only API token is required")
        if selected == "canvas":
            if isinstance(feed_url, str) and feed_url.strip():
                self.vault.set(self._secret_name("canvas", "feed_url"), feed_url.strip())
                self.canvas_importer.reset()
            if not self.vault.has(self._secret_name("canvas", "feed_url")):
                raise ValueError("Canvas iCalendar feed URL is required")
        self._configs[selected] = config
        self._save()
        self.source_registry.set_enabled(
            _DEFINITION_BY_PLATFORM[selected].source_id, config.enabled
        )
        self._sync_domain_sources(force_rebuild=True)
        return config

    def set_enabled(self, platform_id: str, enabled: bool) -> None:
        selected = self.platform_id(platform_id)
        current = self._configs.get(selected)
        if current is None:
            return
        self._configs[selected] = current.model_copy(update={"enabled": bool(enabled)})
        self._save()
        self.source_registry.set_enabled(
            _DEFINITION_BY_PLATFORM[selected].source_id,
            bool(enabled),
        )
        self._sync_domain_sources(force_rebuild=True)

    async def connect(self, platform_id: str) -> ExternalPlatformStatus:
        selected = self.platform_id(platform_id)
        config = self._configs.get(selected)
        if config is None:
            raise SourceDisconnectedError("Configure this external platform before connecting")
        if not config.enabled:
            raise SourceDisconnectedError("This external platform is disabled")
        if selected == "canvas":
            snapshot = await self.refresh_canvas()
            state = SourceState.READY if snapshot.items else SourceState.EMPTY
            return self._status(config, state)
        if selected == "slack":
            return await self.health(selected)

        def service_factory(redirect_uri: str) -> OAuthConnectionService:
            return self._oauth_service(selected, redirect_uri=redirect_uri)

        try:
            await self.oauth_authorizer(service_factory)
        except OAuthConnectionError as error:
            raise SourceDisconnectedError("External account authorization failed safely") from error
        self._sync_domain_sources(force_rebuild=True)
        return await self.health(selected)

    async def health(self, platform_id: str) -> ExternalPlatformStatus:
        selected = self.platform_id(platform_id)
        definition = _DEFINITION_BY_PLATFORM[selected]
        config = self._configs.get(selected)
        if config is None:
            return ExternalPlatformStatus(
                platform_id=selected,
                source_id=definition.source_id,
                display_name=definition.display_name,
                state=SourceState.DISCONNECTED,
                safe_message="Choose Connect / Configure to set up this platform.",
                configured=False,
                enabled=False,
            )
        if not config.enabled:
            return ExternalPlatformStatus(
                platform_id=selected,
                source_id=definition.source_id,
                display_name=definition.display_name,
                state=SourceState.DISCONNECTED,
                safe_message="This platform connection is disabled.",
                configured=True,
                enabled=False,
                account_label=config.account_label,
                capabilities=config.capabilities,
            )
        try:
            if selected == "canvas":
                feed_url = self.vault.get(self._secret_name("canvas", "feed_url"))
                if not feed_url:
                    return self._status(config, SourceState.DISCONNECTED)
                self.canvas_importer.validate_feed_url(feed_url)
                state = (
                    SourceState.EMPTY
                    if self.canvas_importer.last_import_count == 0
                    else SourceState.READY
                )
                return self._status(config, state)
            if selected == "slack":
                token = await _SlackTokenProvider(self).get_token(config.slack_scopes)
                response = await self.transport.request(
                    "GET",
                    "https://slack.com/api/auth.test",
                    headers={"Authorization": f"Bearer {token}"},
                )
                if response.get("ok") is not True:
                    return self._status(config, SourceState.PERMISSION_REQUIRED)
            else:
                service = self._oauth_service(selected)
                token_status = service.status()
                if not token_status.connected:
                    return self._status(config, SourceState.DISCONNECTED)
                if not set(service.config.scopes).issubset(token_status.scopes):
                    return self._status(config, SourceState.PERMISSION_REQUIRED)
                await service.access_token()
        except (PermissionError, SourcePermissionRequiredError):
            return self._status(config, SourceState.PERMISSION_REQUIRED)
        except Exception:
            return self._status(config, SourceState.DISCONNECTED)
        return self._status(config, SourceState.READY)

    async def refresh_canvas(self, *, now: datetime | None = None) -> SourceSnapshot:
        """Fetch Canvas via its private iCalendar URL and upsert local due tasks."""

        config = self._configs.get("canvas")
        feed_url = self.vault.get(self._secret_name("canvas", "feed_url"))
        if config is None or not config.enabled or not feed_url:
            raise SourceDisconnectedError("Canvas Student is not connected")
        configured_timezone = self.settings_repository.get(
            "default_timezone", self.default_timezone
        )
        fallback_timezone = (
            configured_timezone if isinstance(configured_timezone, str) else self.default_timezone
        )
        remove_completed_tasks_after_due_date = self.settings_repository.get(
            "remove_completed_tasks_after_due_date",
            self.remove_completed_tasks_after_due_date,
        )
        tasks = await self.canvas_importer.sync(
            feed_url,
            account_label=config.account_label,
            fallback_timezone=fallback_timezone,
            remove_completed_tasks_after_due_date=(
                type(remove_completed_tasks_after_due_date) is bool
                and remove_completed_tasks_after_due_date
            ),
            now=now,
        )
        return SourceSnapshot(
            source_id=_DEFINITION_BY_PLATFORM["canvas"].source_id,
            provider_id="canvas",
            collected_at=datetime.now(UTC),
            items=tuple(str(task.task_id) for task in tasks),
            metadata={"imported_task_count": len(tasks)},
        )

    async def disconnect(self, platform_id: str) -> ExternalPlatformStatus:
        selected = self.platform_id(platform_id)
        config = self._configs.get(selected)
        if selected == "canvas":
            self.vault.delete(self._secret_name("canvas", "feed_url"))
            self.canvas_importer.reset()
        elif selected == "slack":
            self.vault.delete(self._secret_name("slack", "token"))
        elif config is not None:
            self._oauth_service(selected).disconnect()
        for source_id in self._message_source_ids(selected):
            self.message_service.clear_summaries(source_id)
            self.message_service.clear_bodies(source_id=source_id)
        self._sync_domain_sources(force_rebuild=True)
        return await self.health(selected)

    def _oauth_service(
        self,
        platform_id: str,
        *,
        redirect_uri: str = "http://127.0.0.1/personal-hub/oauth/callback",
    ) -> OAuthConnectionService:
        selected = self.platform_id(platform_id)
        config = self._configs.get(selected)
        if config is None or not config.client_id:
            raise SourceDisconnectedError("External OAuth client is not configured")
        secret = self.vault.get(self._secret_name(selected, "client_secret"))
        if selected == "google":
            scopes: list[str] = []
            if "calendar" in config.capabilities:
                scopes.append(GOOGLE_CALENDAR_READ_SCOPE)
                if config.calendar_sync_enabled:
                    scopes.append(GOOGLE_CALENDAR_EVENTS_SCOPE)
            if "messages" in config.capabilities:
                scopes.append(GMAIL_READONLY_SCOPE)
            provider = OAuthProviderConfig(
                provider_id="google",
                authorization_url="https://accounts.google.com/o/oauth2/v2/auth",
                token_url="https://oauth2.googleapis.com/token",
                client_id=config.client_id,
                client_secret=secret,
                redirect_uri=redirect_uri,
                scopes=tuple(scopes),
                allowed_scopes=(
                    GOOGLE_CALENDAR_READ_SCOPE,
                    GMAIL_READONLY_SCOPE,
                    GOOGLE_CALENDAR_EVENTS_SCOPE,
                ),
                extra_authorization_parameters={"access_type": "offline", "prompt": "consent"},
            )
        else:
            base = f"https://login.microsoftonline.com/{config.tenant}/oauth2/v2.0"
            scopes = [_MICROSOFT_OFFLINE_SCOPE]
            if "calendar" in config.capabilities:
                scopes.append(OUTLOOK_CALENDAR_READ_SCOPE)
            if "messages" in config.capabilities:
                scopes.append(OUTLOOK_MAIL_READ_SCOPE)
            provider = OAuthProviderConfig(
                provider_id="microsoft",
                authorization_url=f"{base}/authorize",
                token_url=f"{base}/token",
                client_id=config.client_id,
                client_secret=secret,
                redirect_uri=redirect_uri,
                scopes=tuple(scopes),
                allowed_scopes=(
                    _MICROSOFT_OFFLINE_SCOPE,
                    OUTLOOK_CALENDAR_READ_SCOPE,
                    OUTLOOK_MAIL_READ_SCOPE,
                ),
                extra_authorization_parameters={
                    "response_mode": "query",
                    "prompt": "select_account",
                },
            )
        return OAuthConnectionService(provider, self.vault, self.transport)

    def _sync_domain_sources(self, *, force_rebuild: bool = False) -> None:
        for platform_id in ("google", "microsoft"):
            config = self._configs.get(platform_id)
            desired = bool(config and config.enabled and "calendar" in config.capabilities)
            source_id = "google-calendar" if platform_id == "google" else "outlook-calendar"
            if source_id in self.calendar_service.source_ids and (force_rebuild or not desired):
                self.calendar_service.unregister(source_id)
                self._calendar_adapters.pop(source_id, None)
            if desired and source_id not in self.calendar_service.source_ids:
                token_provider = _OAuthTokenProvider(self, platform_id)
                adapter: object
                if platform_id == "google":
                    adapter = GoogleCalendarAdapter(
                        token_provider,
                        self.transport,
                        source_id=source_id,
                        connection_id="google",
                        discover_calendars=True,
                    )
                else:
                    adapter = OutlookCalendarSource(
                        token_provider,
                        self.transport,
                        source_id=source_id,
                        connection_id="microsoft",
                    )
                self.calendar_service.register(adapter)  # type: ignore[arg-type]
                self._calendar_adapters[source_id] = adapter

        for platform_id in ("google", "microsoft", "slack"):
            config = self._configs.get(platform_id)
            desired = bool(config and config.enabled and "messages" in config.capabilities)
            source_ids = self._message_source_ids(platform_id)
            source_id = source_ids[0]
            current = self.message_repository.get_source(source_id)
            if desired and config is not None:
                query = self._message_query(config, current)
                now = datetime.now(UTC)
                message_config = MessageSourceConfig(
                    source_id=source_id,
                    provider={"google": "gmail", "microsoft": "outlook", "slack": "slack"}[
                        platform_id
                    ],
                    account_id=config.account_label,
                    query=query,
                    refresh_minutes=self.message_refresh_minutes,
                    retention_days=self.message_retention_days,
                    enabled=True,
                    created_at=current.created_at if current is not None else now,
                    updated_at=now,
                )
                self.message_repository.add_source(message_config)
                if force_rebuild:
                    self.message_service.unregister(source_id)
                    self._message_adapters.pop(source_id, None)
                if source_id not in self.message_service.source_ids:
                    adapter = self._message_adapter(platform_id, config)
                    self.message_service.register(adapter)
                    self._message_adapters[source_id] = adapter
            elif current is not None and current.enabled:
                self.message_repository.add_source(
                    current.model_copy(update={"enabled": False, "updated_at": datetime.now(UTC)})
                )
                self.message_service.unregister(source_id)
                self._message_adapters.pop(source_id, None)

    def _message_adapter(self, platform_id: str, config: ExternalPlatformConfig) -> object:
        if platform_id == "google":
            return GmailMessageSource(
                _OAuthTokenProvider(self, "google"),
                self.transport,
                source_id="gmail",
                account_id=config.account_label,
            )
        if platform_id == "microsoft":
            return OutlookMessageSource(
                _OAuthTokenProvider(self, "microsoft"),
                self.transport,
                source_id="outlook-mail",
                account_id=config.account_label,
            )
        return SlackMessageSource(
            _SlackTokenProvider(self),
            self.transport,
            source_id="slack",
            account_id=config.account_label,
        )

    @staticmethod
    def _message_query(
        config: ExternalPlatformConfig,
        current: MessageSourceConfig | None,
    ) -> MessageQuery:
        if config.platform_id == "slack":
            if config.slack_selection_kind == "channel":
                return MessageQuery(channels=(config.slack_selection or "",), limit=50)
            return MessageQuery(query=config.slack_selection, limit=50)
        if current is not None:
            return current.query
        folder = "INBOX" if config.platform_id == "google" else "inbox"
        return MessageQuery(folders=(folder,), unread_only=True, limit=50)

    @staticmethod
    def _message_source_ids(platform_id: str) -> tuple[str, ...]:
        source_id = {
            "google": "gmail",
            "microsoft": "outlook-mail",
            "slack": "slack",
        }.get(platform_id)
        return () if source_id is None else (source_id,)

    def _status(
        self,
        config: ExternalPlatformConfig,
        state: SourceState,
    ) -> ExternalPlatformStatus:
        messages = {
            SourceState.READY: "Read-only connection is ready.",
            SourceState.EMPTY: "Read-only connection is ready but has no items.",
            SourceState.PERMISSION_REQUIRED: "Reconnect to grant the selected read access.",
            SourceState.DISCONNECTED: "External account is disconnected.",
        }
        definition = _DEFINITION_BY_PLATFORM[config.platform_id]
        return ExternalPlatformStatus(
            platform_id=config.platform_id,
            source_id=definition.source_id,
            display_name=definition.display_name,
            state=state,
            safe_message=messages.get(state, "External connection is unavailable."),
            configured=True,
            enabled=config.enabled,
            account_label=config.account_label,
            capabilities=config.capabilities,
        )

    @staticmethod
    def _source_status(status: ExternalPlatformStatus) -> SourceStatus:
        return SourceStatus(
            source_id=status.source_id,
            provider_id=status.platform_id,
            state=status.state,
            checked_at=datetime.now(UTC),
            safe_message=status.safe_message,
        )

    def _load(self) -> dict[str, ExternalPlatformConfig]:
        raw = self.settings_repository.get(_SETTINGS_KEY, {})
        if not isinstance(raw, Mapping):
            return {}
        configs: dict[str, ExternalPlatformConfig] = {}
        for platform_id, value in raw.items():
            if platform_id not in _PLATFORM_IDS or not isinstance(value, Mapping):
                continue
            try:
                config = ExternalPlatformConfig.model_validate(value)
            except (TypeError, ValueError):
                continue
            configs[config.platform_id] = config
        return configs

    def _save(self) -> None:
        self.settings_repository.set(
            _SETTINGS_KEY,
            {
                platform_id: config.model_dump(mode="json")
                for platform_id, config in sorted(self._configs.items())
            },
        )

    @staticmethod
    def _secret_name(platform_id: str, suffix: str) -> str:
        return f"platform_{platform_id}_{suffix}"


__all__ = [
    "ExternalPlatformConfig",
    "ExternalPlatformDefinition",
    "ExternalPlatformManager",
    "ExternalPlatformStatus",
    "PLATFORM_DEFINITIONS",
    "authorize_oauth_with_loopback",
]
