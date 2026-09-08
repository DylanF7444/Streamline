"""Bounded notification-delivery persistence facade."""

from personal_hub.storage.repositories import (
    NotificationDeliveryRepository,
    NotificationRepository,
)

__all__ = ["NotificationDeliveryRepository", "NotificationRepository"]
