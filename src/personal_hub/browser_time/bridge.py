"""Bounded latest-sample mailbox; a live Hub lease is required for writes."""

import hashlib
import json
import os
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from personal_hub.browser_time.contracts import (
    BROWSER_EXECUTABLES,
    BrowserSample,
    BrowserTimeSession,
)
from personal_hub.screen_time.collector import Observation, ScreenTimeRecorder
from personal_hub.screen_time.contracts import ScreenTimeSettings


def read_json(path: Path):
    with path.open("rb") as stream:
        data = stream.read(131073)
    if len(data) > 131072:
        raise ValueError("Message too large")
    return json.loads(data)


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def receive(root: Path, payload, *, now=None):
    """Native host accepts metadata only, never an interval or arbitrary file path."""
    sample = BrowserSample.model_validate(payload)
    now = time.time() if now is None else now
    lease = read_json(root / "lease.json")
    if not 0 <= lease["expires"] - now <= 20:
        return {"recording": False}
    if not lease.get("collect", False):
        return {"recording": False, "state": lease.get("state", "waiting")}
    if sample.domain and any(
        sample.domain == d or sample.domain.endswith("." + d)
        for d in lease.get("excluded_domains", [])
    ):
        sample = sample.model_copy(update={"domain": None})
    write_json(
        root / f"{sample.browser}.json",
        {
            "lease": lease["id"],
            "received": now,
            "id": uuid4().hex,
            "sample": sample.model_dump(),
        },
    )
    response = {"recording": True}
    usage = lease.get("usage")
    if usage and sample.domain:
        response["usage"] = {**usage, "current": None}
        if (usage.get("current") or {}).get("domain") == sample.domain:
            response["usage"]["current"] = usage["current"]
    if "state" in lease:
        confirmed = (
            sample.domain
            and lease.get("state") == "recording"
            and lease.get("confirmed")
            == hashlib.sha256(f"{sample.browser}|{sample.domain}".encode()).hexdigest()
            and 0 <= now - lease.get("saved_at", 0) <= 10
        )
        response["state"] = (
            "recording" if confirmed else ("waiting" if sample.domain else "ineligible")
        )
    return response


class _BrowserRecorder(ScreenTimeRecorder):
    def save_checkpoint(self, now):
        browser, domain = self.app.split("|", 1)
        self.repository.save(
            BrowserTimeSession(
                session_id=self.session_id,
                browser=browser,
                domain=domain,
                start=self.start,
                end=now,
            )
        )


class BrowserTimeRuntime:
    def __init__(self, data_dir, repository, settings):
        self.root = Path(data_dir) / "browser-time-live"
        self.settings = settings
        self.timezone = "UTC"
        self.private = False
        self.recorder = _BrowserRecorder(repository)
        self.seen = {}
        self.generation = uuid4().hex
        self.pause()

    @property
    def running(self):
        return self.recorder.running

    def start(self):
        self.pause()
        self.recorder.running = True
        self.recorder.state = "waiting"
        self.recorder.status = "Waiting for browser extension"

    def pause(self):
        self.recorder.pause()
        self.generation = uuid4().hex
        self.seen.clear()
        for name in (
            "lease.json",
            *(f"{browser}.json" for browser in BROWSER_EXECUTABLES.values()),
        ):
            with suppress(OSError):
                (self.root / name).unlink(missing_ok=True)

    def set_private(self, private):
        self.private = private
        if private:
            with suppress(OSError, ValueError):
                lease = read_json(self.root / "lease.json")
                lease.pop("usage", None)
                write_json(self.root / "lease.json", lease)

    def daily_usage(self, now, identity):
        day = datetime.fromtimestamp(now, ZoneInfo(self.timezone)).date()
        totals = self.recorder.repository.summary(day, day, self.timezone)
        sites = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
        domain = identity.split("|", 1)[1] if identity else None
        return {
            "date": day.isoformat(),
            "timezone": self.timezone,
            "total_seconds": sum(totals.values()),
            "sites": [{"domain": host, "seconds": seconds} for host, seconds in sites[:8]],
            "other_seconds": sum(seconds for _, seconds in sites[8:]),
            "current": {"domain": domain, "seconds": totals.get(domain, 0)},
        }

    def tick(self, observation, *, locked=False, now=None, monotonic=None):
        if not self.running:
            return
        if locked:
            self.pause()
            return
        now = time.time() if now is None else now
        collect = (
            observation.app in BROWSER_EXECUTABLES
            and observation.idle_seconds < self.settings.idle_seconds
        )
        self.recorder.settings = ScreenTimeSettings(idle_seconds=self.settings.idle_seconds)
        browser = BROWSER_EXECUTABLES.get(observation.app)
        identity = None
        reason = "Recording enabled — focus Chrome, Edge or Brave"
        if observation.idle_seconds >= self.settings.idle_seconds:
            reason = "Paused — no recent input"
        elif browser:
            reason = "Waiting for browser extension — check its connection status"
        if browser:
            try:
                value = read_json(self.root / f"{browser}.json")
                sample = BrowserSample.model_validate(value["sample"])
                domain = sample.domain
                if value["lease"] == self.generation and 0 <= now - value["received"] <= 10:
                    reason = (
                        "Waiting for a fresh browser sample"
                        if domain
                        else "No eligible site — private, excluded or internal page"
                    )
                if (
                    sample.browser == browser
                    and value["lease"] == self.generation
                    and 0 <= now - value["received"] <= 10
                    and value["id"] != self.seen.get(browser)
                    and domain
                    and not any(
                        domain == d or domain.endswith("." + d)
                        for d in self.settings.excluded_domains
                    )
                ):
                    identity = f"{browser}|{domain}"
                self.seen[browser] = value["id"]
            except (OSError, ValueError, KeyError, TypeError):
                pass
        self.recorder.observe(
            Observation(identity, observation.idle_seconds),
            now=datetime.fromtimestamp(now, UTC),
            monotonic=monotonic,
        )
        if observation.idle_seconds >= self.settings.idle_seconds:
            self.recorder.status = "Paused — no recent input"
        elif identity is None:
            self.recorder.status = reason
        # Publish only after a successful checkpoint. A helper acknowledgement of
        # mailbox delivery alone must never turn the toolbar badge green.
        saved = self.recorder.last_saved_at
        state = self.recorder.state
        if not collect:
            state = "idle" if observation.idle_seconds >= self.settings.idle_seconds else "focus"
        elif identity is None:
            state = "waiting"
        write_json(
            self.root / "lease.json",
            {
                "id": self.generation,
                "expires": now + 12,
                "collect": collect,
                "excluded_domains": self.settings.excluded_domains,
                "state": state,
                "confirmed": hashlib.sha256(identity.encode()).hexdigest()
                if identity and state == "recording"
                else None,
                "saved_at": saved.timestamp() if saved else 0,
                **(
                    {"usage": self.daily_usage(now, identity)}
                    if not self.private and collect
                    else {}
                ),
            },
        )
