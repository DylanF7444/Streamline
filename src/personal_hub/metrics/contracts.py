"""Versioned, bounded wire contracts shared by file and LAN ingestion."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


Identifier = str


class MetricDefinition(Contract):
    metric_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    name: str = Field(min_length=1, max_length=120)
    unit: str = Field(min_length=1, max_length=32)
    kind: Literal["point", "interval", "session"] = "point"
    aggregation: Literal["sum", "latest", "min", "max", "mean", "duration"] = "latest"

    @model_validator(mode="after")
    def consistent(self):
        if (self.kind == "session") != (self.aggregation == "duration"):
            raise ValueError("sessions require duration aggregation")
        if self.kind == "session" and self.unit != "seconds":
            raise ValueError("session units must be seconds")
        return self


STEPS = MetricDefinition(
    metric_id="steps", name="Steps", unit="count", kind="interval", aggregation="sum"
)


class MetricSource(Contract):
    source_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    name: str = Field(min_length=1, max_length=120)
    metric_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    revoked: bool = False
    last_received_at: datetime | None = None
    reporting_timezone: str | None = Field(default=None, max_length=100)


class Measurement(Contract):
    record_id: str = Field(min_length=1, max_length=256)
    metric_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    revision: int = Field(ge=1, le=9_007_199_254_740_991)
    value: float | None = Field(default=None, ge=-1e15, le=1e15)
    unit: str = Field(min_length=1, max_length=32)
    start: datetime
    end: datetime | None = None
    timezone: str = Field(default="UTC", max_length=100)
    reporting_date: date | None = None

    @field_validator("start", "end")
    @classmethod
    def aware(cls, value):
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("timestamps require a timezone")
            try:
                return value.astimezone(UTC)
            except (OverflowError, ValueError) as error:
                raise ValueError("timestamp is outside the supported UTC range") from error
        return value

    @model_validator(mode="after")
    def consistent(self):
        ZoneInfo(self.timezone)
        if self.end is not None and self.end <= self.start:
            raise ValueError("end must follow start")
        if self.metric_id == "steps":
            if self.value is None or self.value < 0 or not self.value.is_integer():
                raise ValueError("steps require a nonnegative integer")
            if self.reporting_date is None or self.end is None:
                raise ValueError("steps require a reporting day and interval")
            zone = ZoneInfo(self.timezone)
            from datetime import time, timedelta

            expected = datetime.combine(self.reporting_date, time(), zone).astimezone(UTC)
            following = datetime.combine(
                self.reporting_date + timedelta(days=1), time(), zone
            ).astimezone(UTC)
            if self.start != expected or self.end != following:
                raise ValueError("step reports must cover their exact local day")
            if self.record_id != f"steps:{self.timezone}:{self.reporting_date}":
                raise ValueError("steps require a stable day identity")
        return self


class MeasurementBatch(Contract):
    format: Literal["personal-hub-metrics-v1"] = "personal-hub-metrics-v1"
    source_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    records: tuple[Measurement, ...] = Field(max_length=1000)


class MetricSummary(Contract):
    metric_id: str
    source_id: str | None
    value: float | None
    unit: str
    count: int


class DeviceEnrollment(Contract):
    token: str = Field(min_length=32, max_length=256)
    source_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class Snapshot(Contract):
    format: Literal["personal-hub-metrics-snapshot-v1"] = "personal-hub-metrics-snapshot-v1"
    source: MetricSource
    definitions: tuple[MetricDefinition, ...] = Field(max_length=64)
    batches: tuple[MeasurementBatch, ...] = Field(max_length=100)

    @model_validator(mode="after")
    def consistent(self):
        if any(batch.source_id != self.source.source_id for batch in self.batches):
            raise ValueError("snapshot mixes sources")
        return self
