"""Strict hostname-only browser usage contracts."""

import ipaddress
import re
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from personal_hub.screen_time.contracts import AppRule

BrowserName = Literal["chrome", "edge", "brave"]
BROWSER_EXECUTABLES = {"chrome.exe": "chrome", "msedge.exe": "edge", "brave.exe": "brave"}


def hostname(value: str) -> str:
    value = value.lower().rstrip(".")
    if len(value) > 253 or "." not in value:
        raise ValueError("Use a public hostname")
    if not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", p) for p in value.split(".")):
        raise ValueError("Use a hostname without a URL or path")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        raise ValueError("IP addresses are not recorded")
    if value.endswith((".local", ".localhost", ".internal", ".test", ".invalid", ".example")):
        raise ValueError("Local and reserved names are not recorded")
    return value


class BrowserTimeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idle_seconds: int = Field(default=180, ge=30, le=1800)
    excluded_domains: tuple[str, ...] = Field(default=(), max_length=256)
    rules: dict[str, AppRule] = Field(default_factory=dict, max_length=512)

    @field_validator("excluded_domains")
    @classmethod
    def domains(cls, values):
        return tuple(dict.fromkeys(hostname(v) for v in values))

    @field_validator("rules")
    @classmethod
    def rule_domains(cls, values):
        return {hostname(k): v for k, v in values.items()}


class BrowserSample(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    browser: BrowserName
    domain: str | None = Field(default=None, max_length=253)

    @field_validator("domain")
    @classmethod
    def domain_only(cls, value):
        return hostname(value) if value is not None else None


class BrowserTimeSession(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    session_id: UUID = Field(default_factory=uuid4)
    source: Literal["browser-extension"] = "browser-extension"
    device: Literal["This PC"] = "This PC"
    browser: BrowserName
    domain: str = Field(max_length=253)
    start: datetime
    end: datetime

    @field_validator("domain")
    @classmethod
    def domain_only(cls, value):
        return hostname(value)

    @model_validator(mode="after")
    def interval(self):
        if self.start.utcoffset() is None or self.end.utcoffset() is None:
            raise ValueError("Timestamps require a timezone")
        if not 0 < (self.end - self.start).total_seconds() <= 86400:
            raise ValueError("Invalid interval")
        return self
