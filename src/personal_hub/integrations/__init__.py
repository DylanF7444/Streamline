"""Personal Hub-owned external connection boundaries."""

from personal_hub.integrations.canvas import CANVAS_TASK_NAMESPACE, CanvasIcalTaskImporter
from personal_hub.integrations.http import BoundedProviderTransport, ProviderHttpError
from personal_hub.integrations.oauth import (
    GMAIL_READONLY_SCOPE,
    GOOGLE_CALENDAR_READ_SCOPE,
    OUTLOOK_CALENDAR_READ_SCOPE,
    OUTLOOK_MAIL_READ_SCOPE,
    SLACK_MESSAGE_READ_SCOPES,
    OAuthAuthorizationRequest,
    OAuthConnectionError,
    OAuthConnectionService,
    OAuthProviderConfig,
    OAuthTokenStatus,
    OAuthTransport,
    gmail_oauth_config,
    google_calendar_oauth_config,
    outlook_calendar_oauth_config,
    outlook_mail_oauth_config,
    slack_messages_oauth_config,
)
from personal_hub.integrations.platforms import (
    PLATFORM_DEFINITIONS,
    ExternalPlatformConfig,
    ExternalPlatformDefinition,
    ExternalPlatformManager,
    ExternalPlatformStatus,
    authorize_oauth_with_loopback,
)

__all__ = [
    "BoundedProviderTransport",
    "CANVAS_TASK_NAMESPACE",
    "CanvasIcalTaskImporter",
    "ExternalPlatformConfig",
    "ExternalPlatformDefinition",
    "ExternalPlatformManager",
    "ExternalPlatformStatus",
    "GOOGLE_CALENDAR_READ_SCOPE",
    "GMAIL_READONLY_SCOPE",
    "OUTLOOK_CALENDAR_READ_SCOPE",
    "OUTLOOK_MAIL_READ_SCOPE",
    "SLACK_MESSAGE_READ_SCOPES",
    "OAuthAuthorizationRequest",
    "OAuthConnectionError",
    "OAuthConnectionService",
    "OAuthProviderConfig",
    "OAuthTokenStatus",
    "OAuthTransport",
    "PLATFORM_DEFINITIONS",
    "ProviderHttpError",
    "authorize_oauth_with_loopback",
    "google_calendar_oauth_config",
    "gmail_oauth_config",
    "outlook_calendar_oauth_config",
    "outlook_mail_oauth_config",
    "slack_messages_oauth_config",
]
