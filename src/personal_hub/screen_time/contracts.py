"""Bounded screen-time data contracts; no content or window titles."""

from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AppRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="", max_length=160)
    category: str = Field(default="Uncategorized", min_length=1, max_length=64)


class ScreenTimeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_on_open: bool = False
    overview_card: bool = False
    idle_seconds: int = Field(default=180, ge=30, le=1800)
    excluded_apps: tuple[str, ...] = Field(default=(), max_length=256)
    rules: dict[str, AppRule] = Field(default_factory=dict, max_length=512)

    @field_validator("excluded_apps")
    @classmethod
    def app_names(cls, values):
        if any(not value or len(value) > 160 or "/" in value or "\\" in value for value in values):
            raise ValueError("Use executable names without paths")
        return tuple(dict.fromkeys(value.casefold() for value in values))

    @field_validator("rules")
    @classmethod
    def rule_names(cls, values):
        if any(not key or len(key) > 160 or "/" in key or "\\" in key for key in values):
            raise ValueError("Use executable names without paths")
        return {key.casefold(): value for key, value in values.items()}


class ScreenTimeSession(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    session_id: UUID = Field(default_factory=uuid4)
    device: Literal["This PC"] = "This PC"
    source: Literal["windows-foreground"] = "windows-foreground"
    app: str = Field(min_length=1, max_length=160)
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def interval(self):
        if self.start.utcoffset() is None or self.end.utcoffset() is None:
            raise ValueError("Timestamps require a timezone")
        if self.end <= self.start or (self.end - self.start).total_seconds() > 86400:
            raise ValueError("Invalid session interval")
        if "/" in self.app or "\\" in self.app or any(ord(c) < 32 for c in self.app):
            raise ValueError("Invalid executable name")
        return self
