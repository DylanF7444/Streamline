"""Hub-owned screen-time persistence and timezone-aware interval summaries."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select

from personal_hub.screen_time.contracts import ScreenTimeSession
from personal_hub.storage.database import HubDatabase, HubScreenTimeRow


def bounds(start: date, end: date, timezone: str):
    if end < start:
        raise ValueError("End must follow start")
    zone = ZoneInfo(timezone)
    return (
        datetime.combine(start, time.min, zone).astimezone(UTC),
        datetime.combine(end + timedelta(days=1), time.min, zone).astimezone(UTC),
    )


class ScreenTimeRepository:
    def __init__(self, database: HubDatabase):
        self.database = database

    def save(self, record: ScreenTimeSession):
        record = ScreenTimeSession.model_validate(record.model_dump())
        with self.database.session() as session:
            row = session.get(HubScreenTimeRow, str(record.session_id))
            if row is None:
                row = HubScreenTimeRow(session_id=str(record.session_id))
                session.add(row)
            row.app = record.app
            row.start = record.start.astimezone(UTC).replace(tzinfo=None)
            row.end = record.end.astimezone(UTC).replace(tzinfo=None)
            row.payload_json = record.model_dump_json()

    def records(self, start=None, end=None, timezone="UTC", *, limit=200, offset=0):
        if not 1 <= limit <= 10000 or offset < 0:
            raise ValueError("Invalid query bounds")
        statement = select(HubScreenTimeRow)
        if start is not None and end is not None:
            lower, upper = bounds(start, end, timezone)
            statement = statement.where(
                HubScreenTimeRow.end > lower.replace(tzinfo=None),
                HubScreenTimeRow.start < upper.replace(tzinfo=None),
            )
        with self.database.session() as session:
            rows = session.scalars(
                statement.order_by(HubScreenTimeRow.start, HubScreenTimeRow.session_id)
                .offset(offset)
                .limit(limit)
            ).all()
            return tuple(ScreenTimeSession.model_validate_json(row.payload_json) for row in rows)

    def summary(self, start: date, end: date, timezone: str):
        lower, upper = bounds(start, end, timezone)
        # SQLite julianday arithmetic clips sessions before summing, including midnight/DST.
        seconds = (
            func.julianday(func.min(HubScreenTimeRow.end, upper.replace(tzinfo=None)))
            - func.julianday(func.max(HubScreenTimeRow.start, lower.replace(tzinfo=None)))
        ) * 86400
        with self.database.session() as session:
            rows = session.execute(
                select(HubScreenTimeRow.app, func.sum(seconds))
                .where(
                    HubScreenTimeRow.end > lower.replace(tzinfo=None),
                    HubScreenTimeRow.start < upper.replace(tzinfo=None),
                )
                .group_by(HubScreenTimeRow.app)
            ).all()
        return {app: max(0, round(total)) for app, total in rows}

    def delete_range(self, start: date, end: date, timezone: str, *, preview=False):
        lower, upper = bounds(start, end, timezone)
        # Delete whole overlapping records, with an explicit preview before applying.
        predicate = (
            HubScreenTimeRow.end > lower.replace(tzinfo=None),
            HubScreenTimeRow.start < upper.replace(tzinfo=None),
        )
        with self.database.session() as session:
            count = session.scalar(
                select(func.count()).select_from(HubScreenTimeRow).where(*predicate)
            )
            if not preview:
                session.execute(delete(HubScreenTimeRow).where(*predicate))
            return count

    def all(self):
        offset = 0
        while records := self.records(limit=1000, offset=offset):
            yield from records
            offset += len(records)
