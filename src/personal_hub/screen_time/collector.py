"""Sample only foreground executable identity and OS idle state on Windows."""

import ctypes
import ntpath
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from personal_hub.screen_time.contracts import ScreenTimeSession, ScreenTimeSettings


@dataclass(frozen=True)
class Observation:
    app: str | None
    idle_seconds: float = 0
    executable_path: str | None = field(default=None, repr=False, compare=False)


class WindowsObserver:
    supported = sys.platform == "win32"

    def __init__(self):
        if not self.supported:
            return
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user.GetForegroundWindow.restype = wintypes.HWND
        self.user.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.user.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.user.OpenInputDesktop.restype = wintypes.HANDLE
        self.user.SwitchDesktop.argtypes = [wintypes.HANDLE]
        self.user.CloseDesktop.argtypes = [wintypes.HANDLE]
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]

    def sample(self):
        if not self.supported:
            return Observation(None)
        desktop = self.user.OpenInputDesktop(0, False, 0x0100)
        if not desktop:
            return Observation(None)
        try:
            if not self.user.SwitchDesktop(desktop):
                return Observation(None)
        finally:
            self.user.CloseDesktop(desktop)

        class LastInput(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        last = LastInput()
        last.cbSize = ctypes.sizeof(last)
        if not self.user.GetLastInputInfo(ctypes.byref(last)):
            return Observation(None)
        idle = ((self.kernel.GetTickCount() - last.dwTime) & 0xFFFFFFFF) / 1000
        pid = wintypes.DWORD()
        hwnd = self.user.GetForegroundWindow()
        if not hwnd:
            return Observation(None, idle)
        self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = self.kernel.OpenProcess(0x1000, False, pid.value)
        if not handle:
            return Observation(None, idle)
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buffer))
            if not self.kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return Observation(None, idle)
            executable_path = buffer.value
            return Observation(
                ntpath.basename(executable_path).casefold()[:160], idle, executable_path
            )
        finally:
            self.kernel.CloseHandle(handle)


class ScreenTimeRecorder:
    """Checkpoint one session at each sample; interruptions never extend history."""

    def __init__(self, repository, settings=None):
        self.repository = repository
        self.settings = settings or ScreenTimeSettings()
        self.running = False
        self.status = "Paused"
        self.state = "paused"
        self.last_saved_at = None
        self.reset()

    def reset(self):
        self.state = "waiting" if self.running else "paused"
        self.status = "Waiting for next sample" if self.running else "Paused"
        self.previous = None
        self.tick = None
        self.session_id = uuid4()
        self.start = None
        self.app = None

    def pause(self):
        self.running = False
        self.status = "Paused"
        self.reset()

    def observe(self, observation, *, now=None, monotonic=None, locked=False):
        now = now or datetime.now(UTC)
        monotonic = time.monotonic() if monotonic is None else monotonic
        if not self.running or locked:
            self.reset()
            return
        app = observation.app
        if (
            not app
            or app in self.settings.excluded_apps
            or observation.idle_seconds >= self.settings.idle_seconds
        ):
            self.reset()
            self.status = "Idle, locked, excluded, or unavailable"
            return
        elapsed = monotonic - self.tick if self.tick is not None else 0
        wall = (now - self.previous).total_seconds() if self.previous is not None else 0
        if (
            self.app != app
            or not 0 < elapsed <= 15
            or abs(wall - elapsed) > 2
            or (now - self.start).total_seconds() >= 86400
        ):
            self.reset()
            self.app, self.start = app, now
        else:
            self.save_checkpoint(now)
            self.last_saved_at = now
            self.state = "recording"
        self.previous, self.tick = now, monotonic
        self.status = (
            "Recording · This PC" if self.state == "recording" else "Waiting for next sample"
        )

    def save_checkpoint(self, now):
        self.repository.save(
            ScreenTimeSession(session_id=self.session_id, app=self.app, start=self.start, end=now)
        )
