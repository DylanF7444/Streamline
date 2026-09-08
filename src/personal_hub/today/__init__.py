"""Personal Hub Today projections and deterministic local digests."""

from personal_hub.today.contracts import (
    TaskLike,
    TaskRepositoryProtocol,
    TodayItem,
    TodayProjection,
    TodaySection,
)
from personal_hub.today.digest import (
    DailyDigest,
    DailyDigestBuilder,
    DailyDigestService,
    DigestBuilder,
    DigestSection,
    DigestService,
    build_daily_digest,
)
from personal_hub.today.projection import (
    TodayProjectionService,
    TodayProjector,
    TodayService,
    build_today,
    project_today,
)

__all__ = [
    "DailyDigest",
    "DailyDigestBuilder",
    "DailyDigestService",
    "DigestBuilder",
    "DigestSection",
    "DigestService",
    "TaskLike",
    "TaskRepositoryProtocol",
    "TodayItem",
    "TodayProjection",
    "TodayProjectionService",
    "TodayProjector",
    "TodaySection",
    "TodayService",
    "build_daily_digest",
    "build_today",
    "project_today",
]
