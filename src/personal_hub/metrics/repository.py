"""Transactional metrics ingestion, provenance, summaries and replay suppression."""

from __future__ import annotations

from datetime import UTC, date, datetime
from threading import RLock
from zoneinfo import ZoneInfo

from sqlalchemy import select

from personal_hub.metrics.contracts import (
    STEPS,
    Measurement,
    MeasurementBatch,
    MetricDefinition,
    MetricSource,
    MetricSummary,
)
from personal_hub.storage.database import (
    HubMeasurementRow,
    HubMetricDefinitionRow,
    HubMetricSourceRow,
)


class MetricsRepository:
    def __init__(self, database):
        self.database = database
        self.lock = RLock()
        self.define(STEPS)

    def define(self, definition: MetricDefinition):
        with self.lock, self.database.session() as session:
            row = session.get(HubMetricDefinitionRow, definition.metric_id)
            if row and MetricDefinition.model_validate_json(row.payload) != definition:
                raise ValueError("existing metric definitions cannot change")
            if not row:
                session.add(
                    HubMetricDefinitionRow(
                        metric_id=definition.metric_id, payload=definition.model_dump_json()
                    )
                )

    def definitions(self):
        with self.database.session() as session:
            return [
                MetricDefinition.model_validate_json(row.payload)
                for row in session.scalars(
                    select(HubMetricDefinitionRow).order_by(HubMetricDefinitionRow.metric_id)
                )
            ]

    def sources(self):
        with self.database.session() as session:
            return [
                MetricSource.model_validate_json(row.payload)
                for row in session.scalars(
                    select(HubMetricSourceRow).order_by(HubMetricSourceRow.source_id)
                )
            ]

    def source(self, source_id):
        return next((s for s in self.sources() if s.source_id == source_id), None)

    def enroll(self, source: MetricSource):
        with self.lock, self.database.session() as session:
            if session.get(HubMetricSourceRow, source.source_id):
                raise ValueError("source already exists; use its existing identity")
            for metric_id in set(source.metric_ids):
                metric = session.get(HubMetricDefinitionRow, metric_id)
                if metric is None:
                    raise ValueError("define each allowed metric first")
                if metric.primary_source is None:
                    metric.primary_source = source.source_id
            session.add(
                HubMetricSourceRow(
                    source_id=source.source_id,
                    payload=source.model_copy(
                        update={"revoked": False, "last_received_at": None}
                    ).model_dump_json(),
                )
            )

    def revoke(self, source_id):
        with self.lock, self.database.session() as session:
            row = session.get(HubMetricSourceRow, source_id)
            if row:
                source = MetricSource.model_validate_json(row.payload)
                row.payload = source.model_copy(update={"revoked": True}).model_dump_json()

    def update_permissions(self, source_id, metric_ids):
        with self.lock, self.database.session() as session:
            row = session.get(HubMetricSourceRow, source_id)
            if row is None:
                raise ValueError("unknown source")
            source = MetricSource.model_validate_json(row.payload)
            updated = MetricSource.model_validate(source.model_dump() | {"metric_ids": metric_ids})
            for metric_id in updated.metric_ids:
                definition = session.get(HubMetricDefinitionRow, metric_id)
                if definition is None:
                    raise ValueError("define each metric before granting permission")
                if definition.primary_source is None:
                    definition.primary_source = source_id
            row.payload = updated.model_dump_json()

    def set_primary(self, metric_id, source_id):
        source = self.source(source_id)
        if source is None or source.revoked or metric_id not in source.metric_ids:
            raise ValueError("select an active source allowed to report this metric")
        with self.lock, self.database.session() as session:
            session.get(HubMetricDefinitionRow, metric_id).primary_source = source_id

    def primary(self, metric_id):
        with self.database.session() as session:
            row = session.get(HubMetricDefinitionRow, metric_id)
            return row.primary_source if row else None

    def ingest(self, batch: MeasurementBatch, *, allowed=lambda: True):
        return self.ingest_many([batch], allowed=allowed)

    def ingest_many(self, batches, *, allowed=lambda: True):
        """All-or-nothing ingestion, including snapshots spanning several batches."""
        accepted = unchanged = suppressed = 0
        with self.lock, self.database.session() as session:
            if not allowed():
                raise ValueError("ingestion is paused")
            for batch in batches:
                source_row = session.get(HubMetricSourceRow, batch.source_id)
                if source_row is None:
                    raise ValueError("source requires explicit approval")
                source = MetricSource.model_validate_json(source_row.payload)
                if source.revoked:
                    raise ValueError("source is revoked")
                for record in batch.records:
                    if not allowed():
                        raise ValueError("ingestion is paused")
                    if record.metric_id == "steps":
                        if (
                            source.reporting_timezone is not None
                            and source.reporting_timezone != record.timezone
                        ):
                            raise ValueError(
                                "a step source must keep its original reporting timezone"
                            )
                        source = source.model_copy(update={"reporting_timezone": record.timezone})
                    if record.metric_id not in source.metric_ids:
                        raise ValueError("source cannot report this metric")
                    definition = MetricDefinition.model_validate_json(
                        session.get(HubMetricDefinitionRow, record.metric_id).payload
                    )
                    if record.unit != definition.unit:
                        raise ValueError("measurement units must match the definition")
                    if definition.kind == "point" and (
                        record.value is None or record.end is not None
                    ):
                        raise ValueError("points require a value and a single timestamp")
                    if definition.kind == "interval" and (
                        record.value is None or record.end is None
                    ):
                        raise ValueError("interval totals require a value and end")
                    if definition.kind == "session" and (
                        record.end is None or record.value is not None
                    ):
                        raise ValueError("sessions require bounds and no numeric value")
                    identity = (batch.source_id, record.record_id)
                    row = session.get(HubMeasurementRow, identity)
                    if row and row.suppressed:
                        suppressed += 1
                        continue
                    if row:
                        previous = Measurement.model_validate_json(row.payload)
                        if previous.metric_id != record.metric_id:
                            raise ValueError("record identity cannot change metric")
                        if record.revision < previous.revision:
                            unchanged += 1
                            continue
                        if record.revision == previous.revision:
                            if previous != record:
                                raise ValueError("conflicting record revision")
                            unchanged += 1
                            continue
                    else:
                        row = HubMeasurementRow(
                            source_id=batch.source_id, record_id=record.record_id
                        )
                        session.add(row)
                    row.metric_id = record.metric_id
                    row.day = str(
                        record.reporting_date
                        or record.start.astimezone(ZoneInfo(record.timezone)).date()
                    )
                    row.payload = record.model_dump_json()
                    row.suppressed = False
                    session.flush()
                    accepted += 1
                source_row.payload = source.model_copy(
                    update={"last_received_at": datetime.now(UTC)}
                ).model_dump_json()
            if not allowed():
                raise ValueError("ingestion is paused")
        return {"accepted": accepted, "unchanged": unchanged, "suppressed": suppressed}

    def records(
        self, metric_id=None, source_id=None, start=None, end=None, *, limit=10000, offset=0
    ):
        query = select(HubMeasurementRow).where(HubMeasurementRow.suppressed.is_(False))
        if metric_id:
            query = query.where(HubMeasurementRow.metric_id == metric_id)
        if source_id:
            query = query.where(HubMeasurementRow.source_id == source_id)
        if start:
            query = query.where(HubMeasurementRow.day >= str(start))
        if end:
            query = query.where(HubMeasurementRow.day <= str(end))
        query = (
            query.order_by(HubMeasurementRow.day, HubMeasurementRow.record_id)
            .offset(offset)
            .limit(limit)
        )
        with self.database.session() as session:
            return [
                (row.source_id, Measurement.model_validate_json(row.payload))
                for row in session.scalars(query)
            ]

    def summary(self, metric_id, start: date, end: date, source_id=None):
        if end < start:
            raise ValueError("end must follow start")
        definition = next(d for d in self.definitions() if d.metric_id == metric_id)
        chosen = source_id or self.primary(metric_id)
        count = 0
        total = 0.0
        value = None
        latest = None
        if chosen:
            offset = 0
            while True:
                page = self.records(metric_id, chosen, start, end, limit=1000, offset=offset)
                for _, record in page:
                    sample = (
                        (record.end - record.start).total_seconds()
                        if definition.kind == "session"
                        else record.value
                    )
                    count += 1
                    total += sample
                    if value is None or definition.aggregation in {"sum", "mean", "duration"}:
                        value = sample
                    elif definition.aggregation == "min":
                        value = min(value, sample)
                    elif definition.aggregation == "max":
                        value = max(value, sample)
                    if definition.aggregation == "latest" and (
                        latest is None or (record.start, record.record_id) > latest
                    ):
                        value = sample
                        latest = (record.start, record.record_id)
                if len(page) < 1000:
                    break
                offset += len(page)
        if count and definition.aggregation in {"sum", "duration", "mean"}:
            value = total / count if definition.aggregation == "mean" else total
        return MetricSummary(
            metric_id=metric_id, source_id=chosen, value=value, unit=definition.unit, count=count
        )

    def delete_history(self, *, source_id=None, metric_id=None, start=None, end=None, apply=False):
        with self.lock, self.database.session() as session:
            query = select(HubMeasurementRow).where(HubMeasurementRow.suppressed.is_(False))
            for column, value in (
                (HubMeasurementRow.source_id, source_id),
                (HubMeasurementRow.metric_id, metric_id),
            ):
                if value is not None:
                    query = query.where(column == value)
            if start:
                query = query.where(HubMeasurementRow.day >= str(start))
            if end:
                query = query.where(HubMeasurementRow.day <= str(end))
            rows = list(session.scalars(query))
            if apply:
                for row in rows:
                    row.suppressed = True
                    row.payload = (
                        "{}"  # retain only replay-suppression identity, never deleted readings
                    )
            return len(rows)

    def restore_identity(self, source_id, record_id):
        """Explicitly permit this deleted identity to be imported again."""
        with self.lock, self.database.session() as session:
            row = session.get(HubMeasurementRow, (source_id, record_id))
            if row and row.suppressed:
                session.delete(row)
