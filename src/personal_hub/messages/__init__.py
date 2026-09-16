"""Personal Hub attention-message domain and read-only providers."""

from personal_hub.messages.categories import (
    MESSAGE_CATEGORY_LABELS,
    MESSAGE_CATEGORY_VALUES,
    MessageCategory,
    categorize_message,
    classify_message,
    is_message_category,
    message_category_label,
)
from personal_hub.messages.contracts import (
    MessageAttentionNotice,
    MessageBody,
    MessageDisconnectedError,
    MessageError,
    MessageInvalidResponseError,
    MessageOfflineError,
    MessagePermissionRequiredError,
    MessageQuery,
    MessageResult,
    MessageSnapshot,
    MessageSource,
    MessageSourceConfig,
    MessageSummary,
    MessageSyncState,
    MessageUnavailableError,
)
from personal_hub.messages.gmail import GMAIL_READONLY_SCOPE, GmailMessageSource
from personal_hub.messages.outlook import OUTLOOK_MAIL_READ_SCOPE, OutlookMessageSource
from personal_hub.messages.repository import MessageRepository
from personal_hub.messages.sanitizer import sanitize_message_body
from personal_hub.messages.service import MessageService
from personal_hub.messages.slack import (
    SLACK_CHANNEL_HISTORY_SCOPE,
    SLACK_SEARCH_SCOPE,
    SlackMessageSource,
)

__all__ = [
    "GMAIL_READONLY_SCOPE",
    "MESSAGE_CATEGORY_LABELS",
    "MESSAGE_CATEGORY_VALUES",
    "MessageCategory",
    "OUTLOOK_MAIL_READ_SCOPE",
    "SLACK_CHANNEL_HISTORY_SCOPE",
    "SLACK_SEARCH_SCOPE",
    "GmailMessageSource",
    "MessageAttentionNotice",
    "MessageBody",
    "MessageDisconnectedError",
    "MessageError",
    "MessageInvalidResponseError",
    "MessageOfflineError",
    "MessagePermissionRequiredError",
    "MessageQuery",
    "MessageRepository",
    "MessageResult",
    "MessageService",
    "MessageSnapshot",
    "MessageSource",
    "MessageSourceConfig",
    "MessageSummary",
    "MessageSyncState",
    "MessageUnavailableError",
    "OutlookMessageSource",
    "SlackMessageSource",
    "categorize_message",
    "classify_message",
    "is_message_category",
    "message_category_label",
    "sanitize_message_body",
]
