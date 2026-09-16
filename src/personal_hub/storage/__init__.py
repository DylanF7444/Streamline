"""Personal Hub-owned persistence infrastructure."""

from personal_hub.storage.database import (
    HubCalendarImportRow,
    HubDatabase,
    HubImportedCalendarEventRow,
    HubTaskDeletionMarkerRow,
)
from personal_hub.storage.repositories import (
    AuditEventRepository,
    AuditRepository,
    InboxRepository,
    NotificationDeliveryRepository,
    NotificationRepository,
    SettingsRepository,
    SourceHealthRepository,
    SourceRepository,
)
from personal_hub.storage.secrets import (
    HUB_SECRET_ENTROPY,
    DeterministicTestCipher,
    DPAPICipher,
    HubSecretVault,
    PersonalHubSecretVault,
    SecretCipher,
    SecretVault,
    SecretVaultError,
)


def __getattr__(name: str) -> object:
    """Load task repositories lazily to keep ``personal_hub.tasks`` acyclic."""

    if name in {"HubTaskRepository", "TaskListRepository", "TaskRepository", "TasksRepository"}:
        from personal_hub.tasks.repository import (
            HubTaskRepository,
            TaskListRepository,
            TaskRepository,
            TasksRepository,
        )

        return {
            "HubTaskRepository": HubTaskRepository,
            "TaskListRepository": TaskListRepository,
            "TaskRepository": TaskRepository,
            "TasksRepository": TasksRepository,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AuditEventRepository",
    "AuditRepository",
    "DPAPICipher",
    "DeterministicTestCipher",
    "HubDatabase",
    "HubCalendarImportRow",
    "HUB_SECRET_ENTROPY",
    "HubSecretVault",
    "HubImportedCalendarEventRow",
    "HubTaskDeletionMarkerRow",
    "HubTaskRepository",
    "InboxRepository",
    "NotificationDeliveryRepository",
    "NotificationRepository",
    "PersonalHubSecretVault",
    "SecretCipher",
    "SecretVault",
    "SecretVaultError",
    "SettingsRepository",
    "SourceHealthRepository",
    "SourceRepository",
    "TaskListRepository",
    "TaskRepository",
    "TasksRepository",
]
