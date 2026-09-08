"""Deterministic quiet-hours and notification-policy helpers."""

from __future__ import annotations

import re
from datetime import datetime, time

from personal_hub.notifications.contracts import (
    NotificationDecision,
    NotificationPolicy,
    NotificationSeverity,
    QuietHoursSettings,
)


def is_quiet_hours(
    now: datetime,
    *,
    settings: QuietHoursSettings | None = None,
    enabled: bool | None = None,
    start: str | None = None,
    end: str | None = None,
) -> bool:
    """Return whether ``now`` falls within configured local quiet hours.

    A range crossing midnight, such as 22:00–07:00, is treated as one interval.  The
    start is inclusive and the end is exclusive.  ``now`` must be timezone-aware so the
    caller makes the local-time conversion explicitly.
    """

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("quiet-hours evaluation needs a timezone-aware datetime")
    selected = settings or QuietHoursSettings(
        enabled=bool(enabled),
        start=start or "22:00",
        end=end or "07:00",
    )
    if not selected.enabled:
        return False
    start_time = _parse_clock(selected.start)
    end_time = _parse_clock(selected.end)
    current = now.timetz().replace(tzinfo=None)
    if start_time == end_time:
        return False
    if start_time < end_time:
        return start_time <= current < end_time
    return current >= start_time or current < end_time


def decide_delivery(
    policy: NotificationPolicy | str,
    *,
    now: datetime,
    quiet_hours: QuietHoursSettings | None = None,
    severity: NotificationSeverity | str = NotificationSeverity.INFO,
    urgent_bypass_quiet_hours: bool = False,
) -> NotificationDecision:
    """Decide Inbox and operating-system delivery without inspecting content."""

    selected_policy = NotificationPolicy(policy)
    selected_severity = NotificationSeverity(severity)
    quiet = is_quiet_hours(now, settings=quiet_hours or QuietHoursSettings())
    enqueue_inbox = True
    if selected_policy is not NotificationPolicy.IMMEDIATE:
        return NotificationDecision(
            policy=selected_policy,
            enqueue_inbox=enqueue_inbox,
            deliver_system_notification=False,
            suppressed_reason="policy",
        )
    bypass = selected_severity is NotificationSeverity.URGENT and urgent_bypass_quiet_hours
    if quiet and not bypass:
        return NotificationDecision(
            policy=selected_policy,
            enqueue_inbox=enqueue_inbox,
            deliver_system_notification=False,
            suppressed_reason="quiet_hours",
        )
    return NotificationDecision(
        policy=selected_policy,
        enqueue_inbox=enqueue_inbox,
        deliver_system_notification=True,
        suppressed_reason=None,
    )


def should_deliver_system_notification(
    policy: NotificationPolicy | str,
    *,
    now: datetime,
    quiet_hours: QuietHoursSettings | None = None,
    severity: NotificationSeverity | str = NotificationSeverity.INFO,
    urgent_bypass_quiet_hours: bool = False,
) -> bool:
    """Convenience predicate for tray/Windows notification adapters."""

    return decide_delivery(
        policy,
        now=now,
        quiet_hours=quiet_hours,
        severity=severity,
        urgent_bypass_quiet_hours=urgent_bypass_quiet_hours,
    ).deliver_system_notification


def _parse_clock(value: str) -> time:
    if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) is None:
        raise ValueError("quiet-hours time must use HH:MM")
    hour, minute = (int(part) for part in value.split(":", 1))
    return time(hour, minute)


# Alternate names are kept as aliases because source adapters often call the operation
# a routing/evaluation step rather than a policy decision.
evaluate_notification = decide_delivery
notification_decision = decide_delivery

__all__ = [
    "decide_delivery",
    "evaluate_notification",
    "is_quiet_hours",
    "notification_decision",
    "should_deliver_system_notification",
]
