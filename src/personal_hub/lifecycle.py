"""Personal Hub process lifecycle and single-instance protection.

The Hub owns its instance identity.  No Local Assistant lifecycle or Windows
helper is imported here: on Windows a Hub-specific named mutex is used, while
portable test/development environments use an advisory lock on the Hub-owned
lock file.  Both backends keep an owning handle open for the lifetime of the
lock and release it idempotently.
"""

from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Callable
from contextlib import suppress
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Literal, Self, cast

from .config import HUB_MUTEX_NAME, HubSettings

_ERROR_ALREADY_EXISTS = 183


class HubAlreadyRunningError(RuntimeError):
    """Raised when another Personal Hub process owns the Hub instance lock."""

    def __init__(self, mutex_name: str = HUB_MUTEX_NAME) -> None:
        self.mutex_name = mutex_name
        super().__init__("Personal Hub is already running for this Windows user")


# Callers often use the shorter name when handling an instance collision.
AlreadyRunningError = HubAlreadyRunningError


class _LockBackend(StrEnum):
    WINDOWS_MUTEX = "windows_mutex"
    POSIX_FILE = "posix_file"
    WINDOWS_FILE = "windows_file"


class HubInstanceLock:
    """An owned Personal Hub single-instance lock.

    Instances are normally created with :func:`acquire_single_instance` or the
    :meth:`acquire` class method.  ``close``/``release`` are safe to call more
    than once, which makes cleanup reliable on both normal and exceptional
    startup paths.
    """

    def __init__(
        self,
        settings: HubSettings | Path | str | None = None,
        *,
        mutex_name: str | None = None,
        lock_path: Path | str | None = None,
    ) -> None:
        self.settings = _coerce_settings(settings)
        self.mutex_name = _validate_mutex_name(mutex_name or self.settings.mutex_name)
        if lock_path is not None:
            self.lock_path = Path(lock_path)
        elif mutex_name is not None and self.mutex_name != self.settings.mutex_name:
            # Keep explicitly named test/development identities independent on
            # the portable file-lock backend as well as on Windows mutexes.
            self.lock_path = self.settings.data_dir / f"personal_hub.{self.mutex_name}.lock"
        else:
            self.lock_path = self.settings.lock_path
        self._mutex_handle: int | None = None
        self._kernel32: object | None = None
        self._lock_file: BinaryIO | None = None
        self._backend: _LockBackend | None = None
        self._acquired = False

    @classmethod
    def acquire(
        cls,
        settings: HubSettings | Path | str | None = None,
        *,
        mutex_name: str | None = None,
        lock_path: Path | str | None = None,
    ) -> Self:
        """Acquire a lock and return its owning object."""

        lock = cls(settings, mutex_name=mutex_name, lock_path=lock_path)
        try:
            lock._acquire()
        except BaseException:
            lock.close()
            raise
        return lock

    @property
    def acquired(self) -> bool:
        return self._acquired

    @property
    def is_acquired(self) -> bool:
        return self._acquired

    def _acquire(self) -> None:
        if self._acquired:
            return

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

        # The named mutex is the Windows identity boundary.  The fallback is
        # useful for Linux/macOS development and for deterministic tests; it
        # still holds an OS-level lock rather than relying on a stale marker.
        if sys.platform == "win32" and hasattr(ctypes, "WinDLL"):
            try:
                self._acquire_windows_mutex()
            except HubAlreadyRunningError:
                raise
            except (AttributeError, OSError):
                # Some embedded Python builds expose ``sys.platform`` as win32
                # without a usable kernel32 binding.  A file lock preserves
                # correctness in that environment.
                self._acquire_file_lock()
        else:
            self._acquire_file_lock()

        self._acquired = True

    def _acquire_windows_mutex(self) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        create_mutex.restype = ctypes.c_void_p
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_bool

        ctypes.set_last_error(0)
        handle = create_mutex(None, False, f"Local\\{self.mutex_name}")
        error = ctypes.get_last_error()
        if not handle:
            raise OSError(error or 1, "Could not create the Personal Hub mutex")
        if error == _ERROR_ALREADY_EXISTS:
            close_handle(handle)
            raise HubAlreadyRunningError(self.mutex_name)

        self._kernel32 = kernel32
        self._mutex_handle = cast(int, handle)
        self._backend = _LockBackend.WINDOWS_MUTEX

    def _acquire_file_lock(self) -> None:
        handle = self.lock_path.open("a+b")
        try:
            # Ensure there is always one byte for msvcrt.locking on Windows.
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)

            if os.name == "nt":
                import msvcrt

                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as error:
                    raise HubAlreadyRunningError(self.mutex_name) from error
                self._backend = _LockBackend.WINDOWS_FILE
            else:
                import fcntl

                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise HubAlreadyRunningError(self.mutex_name) from error
                self._backend = _LockBackend.POSIX_FILE
            self._lock_file = handle
        except BaseException:
            handle.close()
            raise

    def close(self) -> None:
        """Release the instance lock.  Repeated calls are harmless."""

        lock_file, self._lock_file = self._lock_file, None
        backend, self._backend = self._backend, None
        mutex_handle, self._mutex_handle = self._mutex_handle, None
        kernel32, self._kernel32 = self._kernel32, None
        self._acquired = False

        if lock_file is not None:
            try:
                if backend == _LockBackend.WINDOWS_FILE:
                    import msvcrt

                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                elif backend == _LockBackend.POSIX_FILE:
                    import fcntl

                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            finally:
                lock_file.close()

        if mutex_handle is not None and kernel32 is not None:
            close_handle = kernel32.CloseHandle
            close_handle(mutex_handle)

    release = close

    def __enter__(self) -> Self:
        if not self._acquired:
            self._acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        self.close()
        return False

    def __del__(self) -> None:  # pragma: no cover - interpreter cleanup fallback
        with suppress(Exception):
            self.close()


def _coerce_settings(settings: HubSettings | Path | str | None) -> HubSettings:
    if settings is None:
        return HubSettings()
    if isinstance(settings, HubSettings):
        return settings
    if isinstance(settings, (Path, str)):
        return HubSettings(data_dir=Path(settings))
    raise TypeError("settings must be HubSettings, a data directory, or None")


def _validate_mutex_name(value: str) -> str:
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 127
        or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
            for character in normalized
        )
    ):
        raise ValueError("Personal Hub mutex name must be a simple local identity")
    return normalized


def acquire_single_instance(
    settings: HubSettings | Path | str | None = None,
    *,
    name: str | None = None,
    mutex_name: str | None = None,
    lock_path: Path | str | None = None,
) -> HubInstanceLock:
    """Acquire the Hub-specific process lock.

    ``name`` and ``mutex_name`` are equivalent; ``mutex_name`` is preferred for
    clarity.  They are primarily useful for isolated tests.  The default always
    uses :data:`personal_hub.config.HUB_MUTEX_NAME`.
    """

    if name is not None and mutex_name is not None and name != mutex_name:
        raise ValueError("name and mutex_name must match when both are provided")
    selected_name = mutex_name if mutex_name is not None else name
    return HubInstanceLock.acquire(settings, mutex_name=selected_name, lock_path=lock_path)


class LifecycleState(StrEnum):
    NEW = "new"
    RUNNING = "running"
    STOPPED = "stopped"


class LifecycleEvent(StrEnum):
    STARTED = "started"
    NETWORK_PAUSED = "network_paused"
    NETWORK_RESUMED = "network_resumed"
    SESSION_LOCKED = "session_locked"
    SESSION_UNLOCKED = "session_unlocked"
    SLEEPING = "sleeping"
    RESUMED = "resumed"
    STOPPING = "stopping"
    STOPPED = "stopped"


class HubLifecycle:
    """Own startup/shutdown state and the Hub single-instance lock."""

    def __init__(self, settings: HubSettings | Path | str | None = None) -> None:
        self.settings = _coerce_settings(settings)
        self._instance_lock: HubInstanceLock | None = None
        self._state = LifecycleState.NEW
        self._network_paused = False
        self._session_locked = False
        self._sleeping = False
        self._listeners: list[Callable[[LifecycleEvent], None]] = []

    @property
    def state(self) -> LifecycleState:
        return self._state

    @property
    def instance_lock(self) -> HubInstanceLock | None:
        return self._instance_lock

    @property
    def lock(self) -> HubInstanceLock | None:
        return self._instance_lock

    @property
    def network_paused(self) -> bool:
        return self._network_paused

    @property
    def session_locked(self) -> bool:
        return self._session_locked

    @property
    def sleeping(self) -> bool:
        return self._sleeping

    @property
    def network_refresh_allowed(self) -> bool:
        return (
            self._state == LifecycleState.RUNNING
            and not self._network_paused
            and not self._sleeping
            and not self._session_locked
        )

    def subscribe(self, listener: Callable[[LifecycleEvent], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def unsubscribe() -> None:
            with suppress(ValueError):
                self._listeners.remove(listener)

        return unsubscribe

    def _emit(self, event: LifecycleEvent) -> None:
        for listener in tuple(self._listeners):
            with suppress(Exception):
                listener(event)

    def start(self) -> Self:
        """Acquire the Hub lock once and transition to running."""

        if self._state == LifecycleState.RUNNING:
            return self
        if self._state == LifecycleState.STOPPED:
            raise RuntimeError("Personal Hub lifecycle cannot be started after shutdown")

        self._instance_lock = acquire_single_instance(self.settings)
        self._state = LifecycleState.RUNNING
        self._emit(LifecycleEvent.STARTED)
        return self

    acquire = start

    def set_network_paused(self, paused: bool) -> None:
        if self._state != LifecycleState.RUNNING:
            raise RuntimeError("Personal Hub lifecycle is not running")
        normalized = bool(paused)
        if normalized == self._network_paused:
            return
        self._network_paused = normalized
        self._emit(LifecycleEvent.NETWORK_PAUSED if normalized else LifecycleEvent.NETWORK_RESUMED)

    def handle_session_lock(self) -> None:
        if self._session_locked:
            return
        self._session_locked = True
        self._emit(LifecycleEvent.SESSION_LOCKED)

    def handle_session_unlock(self) -> None:
        if not self._session_locked:
            return
        self._session_locked = False
        self._emit(LifecycleEvent.SESSION_UNLOCKED)

    def prepare_for_sleep(self) -> None:
        if self._sleeping:
            return
        self._sleeping = True
        self._emit(LifecycleEvent.SLEEPING)

    def resume_from_sleep(self) -> None:
        if not self._sleeping:
            return
        self._sleeping = False
        self._emit(LifecycleEvent.RESUMED)

    def shutdown(self) -> None:
        """Release Hub resources and transition to stopped."""

        if self._state != LifecycleState.STOPPED:
            self._emit(LifecycleEvent.STOPPING)
        instance_lock, self._instance_lock = self._instance_lock, None
        if instance_lock is not None:
            instance_lock.close()
        self._state = LifecycleState.STOPPED
        self._emit(LifecycleEvent.STOPPED)
        self._listeners.clear()

    close = shutdown

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        self.shutdown()
        return False


# Explicit aliases keep the lifecycle API easy to discover without introducing
# another implementation or identity.
SingleInstanceLock = HubInstanceLock
HubApplicationLifecycle = HubLifecycle
acquire_instance_lock = acquire_single_instance
acquire_hub_lock = acquire_single_instance

__all__ = [
    "AlreadyRunningError",
    "HubAlreadyRunningError",
    "HubApplicationLifecycle",
    "HubInstanceLock",
    "HubLifecycle",
    "LifecycleEvent",
    "LifecycleState",
    "SingleInstanceLock",
    "acquire_hub_lock",
    "acquire_instance_lock",
    "acquire_single_instance",
]
