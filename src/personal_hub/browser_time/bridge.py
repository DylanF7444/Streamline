"""Bounded latest-sample mailbox; a live Hub lease is required for writes."""

import json
import os
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from personal_hub.browser_time.contracts import BrowserSample, BrowserTimeSession
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
        return {"recording": False}
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
    return {"recording": True}


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

    def pause(self):
        self.recorder.pause()
        self.generation = uuid4().hex
        self.seen.clear()
        for name in ("lease.json", "chrome.json", "edge.json"):
            with suppress(OSError):
                (self.root / name).unlink(missing_ok=True)

    def tick(self, observation, *, locked=False, now=None, monotonic=None):
        if not self.running:
            return
        if locked:
            self.pause()
            return
        now = time.time() if now is None else now
        collect = (
            observation.app in {"chrome.exe", "msedge.exe"}
            and observation.idle_seconds < self.settings.idle_seconds
        )
        write_json(
            self.root / "lease.json",
            {
                "id": self.generation,
                "expires": now + 12,
                "collect": collect,
                "excluded_domains": self.settings.excluded_domains,
            },
        )
        self.recorder.settings = ScreenTimeSettings(idle_seconds=self.settings.idle_seconds)
        browser = {"chrome.exe": "chrome", "msedge.exe": "edge"}.get(observation.app)
        identity = None
        if browser:
            try:
                value = read_json(self.root / f"{browser}.json")
                sample = BrowserSample.model_validate(value["sample"])
                domain = sample.domain
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
