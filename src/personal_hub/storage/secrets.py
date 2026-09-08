"""Personal Hub-owned encrypted credential storage.

The vault in this module intentionally owns its root, file format, and DPAPI
entropy.  It does not discover, import, or migrate credentials from another
application.  Production Windows storage uses the current-user DPAPI scope;
tests can inject a deterministic cipher without weakening that production
default.
"""

from __future__ import annotations

import ctypes
import hashlib
import hmac
import os
import re
import tempfile
from contextlib import suppress
from ctypes import wintypes
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from personal_hub.config import HUB_SECRET_NAMESPACE, HubSettings

HUB_SECRET_ENTROPY: Final[bytes] = b"PersonalHub.SecretVault.v1"
_SECRET_RECORD_MAGIC: Final[bytes] = b"PersonalHub.SecretVault.Record.v1\x00"
_SECRET_RECORD_SUFFIX: Final[str] = ".bin"
_SECRET_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z",
    re.ASCII,
)
_MAX_SECRET_VALUE_BYTES: Final[int] = 64 * 1024
_MAX_SECRET_RECORD_BYTES: Final[int] = _MAX_SECRET_VALUE_BYTES + 64 * 1024
_CRYPTPROTECT_UI_FORBIDDEN: Final[int] = 0x1


class SecretVaultError(ValueError):
    """Raised when a Hub secret name, path, or encrypted record is invalid."""


@runtime_checkable
class SecretCipher(Protocol):
    """Small injection boundary for encryption implementations.

    The injected object must not retain plaintext after ``encrypt`` returns.
    The production implementation is :class:`DPAPICipher`; deterministic
    test implementations can satisfy this protocol without importing any
    platform or Assistant credential code.
    """

    def encrypt(self, plaintext: str) -> bytes:
        """Encrypt one UTF-8 secret value."""

    def decrypt(self, ciphertext: bytes) -> str:
        """Decrypt one UTF-8 secret value."""


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _data_blob(data: bytes) -> tuple[_DataBlob, object]:
    buffer = ctypes.create_string_buffer(data)
    blob = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    return blob, buffer


def _dpapi_functions() -> tuple[object, object]:
    """Return the Windows DPAPI and allocation APIs without importing pywin32."""

    windll = getattr(ctypes, "windll", None)
    if windll is None:
        raise RuntimeError("Personal Hub DPAPI storage is available only on Windows")
    try:
        return windll.crypt32, windll.kernel32
    except AttributeError as error:
        raise RuntimeError("Personal Hub DPAPI APIs are unavailable") from error


class DPAPICipher:
    """Encrypt Hub credentials with current-user Windows DPAPI.

    The explicit entropy and description are Hub-specific.  A ciphertext
    protected with another product's entropy cannot be unprotected by this
    cipher, even if both products happen to use the same filename.
    """

    namespace: str = HUB_SECRET_NAMESPACE
    entropy: bytes = HUB_SECRET_ENTROPY

    def encrypt(self, plaintext: str) -> bytes:
        if not isinstance(plaintext, str) or not plaintext:
            raise ValueError("Secret plaintext must be a non-empty string")
        crypt32, kernel32 = _dpapi_functions()
        source, source_buffer = _data_blob(plaintext.encode("utf-8"))
        entropy, entropy_buffer = _data_blob(self.entropy)
        output = _DataBlob()
        if not crypt32.CryptProtectData(
            ctypes.byref(source),
            self.namespace,
            ctypes.byref(entropy),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output),
        ):
            raise ctypes.WinError()
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            kernel32.LocalFree(output.pbData)
            del source_buffer, entropy_buffer

    def decrypt(self, ciphertext: bytes) -> str:
        if not isinstance(ciphertext, bytes) or not ciphertext:
            raise ValueError("Secret ciphertext must be non-empty bytes")
        crypt32, kernel32 = _dpapi_functions()
        source, source_buffer = _data_blob(ciphertext)
        entropy, entropy_buffer = _data_blob(self.entropy)
        output = _DataBlob()
        if not crypt32.CryptUnprotectData(
            ctypes.byref(source),
            None,
            ctypes.byref(entropy),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output),
        ):
            raise ctypes.WinError()
        try:
            return ctypes.string_at(output.pbData, output.cbData).decode("utf-8")
        finally:
            kernel32.LocalFree(output.pbData)
            del source_buffer, entropy_buffer


class DeterministicTestCipher:
    """Deterministic, authenticated cipher intended solely for offline tests.

    This is deliberately not selected by default.  It provides a stable
    injection path on non-Windows test hosts while ensuring test fixtures do
    not write plaintext into the vault.  Production callers must use the
    default :class:`DPAPICipher`.
    """

    namespace: str = HUB_SECRET_NAMESPACE

    def __init__(self, namespace: str = HUB_SECRET_NAMESPACE) -> None:
        self.namespace = namespace
        self._key = hashlib.sha256(
            b"PersonalHub deterministic test cipher\x00" + namespace.encode("utf-8")
        ).digest()

    def encrypt(self, plaintext: str) -> bytes:
        if not isinstance(plaintext, str) or not plaintext:
            raise ValueError("Secret plaintext must be a non-empty string")
        payload = plaintext.encode("utf-8")
        ciphertext = self._xor(payload)
        tag = hmac.new(self._key, payload, hashlib.sha256).digest()[:16]
        return b"PHTEST1\x00" + tag + ciphertext

    def decrypt(self, ciphertext: bytes) -> str:
        if not isinstance(ciphertext, bytes) or len(ciphertext) < 24:
            raise ValueError("Invalid deterministic Hub ciphertext")
        if not ciphertext.startswith(b"PHTEST1\x00"):
            raise ValueError("Invalid deterministic Hub ciphertext")
        tag = ciphertext[8:24]
        payload = self._xor(ciphertext[24:])
        expected = hmac.new(self._key, payload, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(tag, expected):
            raise ValueError("Invalid deterministic Hub ciphertext")
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("Invalid deterministic Hub plaintext") from error

    def _xor(self, payload: bytes) -> bytes:
        output = bytearray(len(payload))
        offset = 0
        counter = 0
        while offset < len(payload):
            stream = hashlib.sha256(self._key + counter.to_bytes(8, "big")).digest()
            chunk = payload[offset : offset + len(stream)]
            for index, value in enumerate(chunk):
                output[offset + index] = value ^ stream[index]
            offset += len(chunk)
            counter += 1
        return bytes(output)


class HubSecretVault:
    """Persist named encrypted secrets under ``HubSettings.secrets_dir`` only."""

    namespace: str = HUB_SECRET_NAMESPACE

    def __init__(
        self,
        settings: HubSettings,
        cipher: SecretCipher | None = None,
    ) -> None:
        if not isinstance(settings, HubSettings):
            raise TypeError("HubSecretVault requires HubSettings")
        self.settings = settings
        self.root = Path(settings.secrets_dir)
        self._validate_root()
        self.cipher: SecretCipher = cipher if cipher is not None else DPAPICipher()
        if not callable(getattr(self.cipher, "encrypt", None)) or not callable(
            getattr(self.cipher, "decrypt", None)
        ):
            raise TypeError("cipher must provide encrypt and decrypt methods")

    def __repr__(self) -> str:
        return f"HubSecretVault(root={self.root!s}, namespace={self.namespace!r})"

    def get(self, name: str) -> str | None:
        """Return one decrypted value, or ``None`` when the name is absent."""

        path = self._path(name)
        if not path.exists():
            return None
        if not path.is_file():
            raise SecretVaultError("Secret record path is not a regular file")
        try:
            if path.stat().st_size > _MAX_SECRET_RECORD_BYTES:
                raise SecretVaultError("Secret record exceeds the size limit")
            record = path.read_bytes()
        except SecretVaultError:
            raise
        except OSError as error:
            raise SecretVaultError("Unable to read Hub secret record") from error
        if not record.startswith(_SECRET_RECORD_MAGIC):
            raise SecretVaultError("Secret record is not a Personal Hub record")
        encrypted = record[len(_SECRET_RECORD_MAGIC) :]
        if not encrypted:
            raise SecretVaultError("Secret record has no encrypted payload")
        try:
            value = self.cipher.decrypt(encrypted)
        except Exception as error:
            raise SecretVaultError("Unable to decrypt Hub secret record") from error
        return _validate_secret_value(value)

    def set(self, name: str, value: str) -> None:
        """Encrypt and atomically replace one secret without exposing its value."""

        validated_name = _validate_secret_name(name)
        validated_value = _validate_secret_value(value)
        try:
            encrypted = self.cipher.encrypt(validated_value)
        except Exception:
            # Preserve the cipher exception type while keeping this layer from
            # adding the plaintext to an error message or log record.
            raise
        if not isinstance(encrypted, (bytes, bytearray, memoryview)):
            raise TypeError("cipher.encrypt must return bytes")
        encrypted_bytes = bytes(encrypted)
        record = _SECRET_RECORD_MAGIC + encrypted_bytes
        if len(record) > _MAX_SECRET_RECORD_BYTES:
            raise SecretVaultError("Encrypted secret record exceeds the size limit")

        self._ensure_root()
        path = self._path(validated_name)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".personal-hub-secret-",
            suffix=".tmp",
            dir=os.fspath(self.root),
        )
        descriptor_open = True
        try:
            _restrictive_mode(Path(temporary_name), 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor_open = False
                stream.write(record)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, path)
            _restrictive_mode(path, 0o600)
            _sync_directory(self.root)
        except BaseException:
            if descriptor_open:
                with suppress(OSError):
                    os.close(descriptor)
            with suppress(OSError):
                Path(temporary_name).unlink(missing_ok=True)
            raise

    def delete(self, name: str) -> None:
        """Delete one Hub secret, if present, without returning its value."""

        path = self._path(name)
        try:
            path.unlink()
        except FileNotFoundError:
            return
        _sync_directory(self.root)

    def list(self) -> list[str]:
        """Return sorted secret names only; encrypted values are never loaded."""

        self._validate_root()
        if not self.root.exists():
            return []
        if not self.root.is_dir():
            raise SecretVaultError("Hub secret root is not a directory")
        names: list[str] = []
        try:
            entries = tuple(self.root.iterdir())
        except OSError as error:
            raise SecretVaultError("Unable to enumerate Hub secret names") from error
        for entry in entries:
            if entry.is_symlink() or not entry.is_file():
                continue
            if entry.suffix.casefold() != _SECRET_RECORD_SUFFIX:
                continue
            try:
                names.append(_validate_secret_name(entry.stem))
            except (TypeError, ValueError):
                continue
        return sorted(names)

    def has(self, name: str) -> bool:
        """Return whether an encrypted record exists without decrypting it."""

        return self._path(name).is_file()

    names = list
    list_names = list

    def _validate_root(self) -> None:
        expected_root = Path(self.settings.data_dir) / "secrets"
        if os.path.normcase(os.path.normpath(os.fspath(self.root))) != os.path.normcase(
            os.path.normpath(os.fspath(expected_root))
        ):
            raise SecretVaultError("Hub secret root must be HubSettings.secrets_dir")
        if self.root.is_symlink():
            raise SecretVaultError("Hub secret root must not be a symlink")
        if self.root.exists() and not self.root.is_dir():
            raise SecretVaultError("Hub secret root is not a directory")

    def _ensure_root(self) -> None:
        self._validate_root()
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as error:
            raise SecretVaultError("Unable to create Hub secret root") from error
        self._validate_root()
        _restrictive_mode(self.root, 0o700)

    def _path(self, name: str) -> Path:
        validated_name = _validate_secret_name(name)
        self._validate_root()
        path = self.root / f"{validated_name}{_SECRET_RECORD_SUFFIX}"
        if path.parent != self.root:
            raise SecretVaultError("Secret path escaped the Hub secret root")
        if path.is_symlink():
            raise SecretVaultError("Secret record must not be a symlink")
        return path


def _validate_secret_name(name: str) -> str:
    if type(name) is not str or _SECRET_NAME_PATTERN.fullmatch(name) is None:
        raise SecretVaultError("Invalid Personal Hub secret name")
    return name


def _validate_secret_value(value: str) -> str:
    if type(value) is not str or not value:
        raise SecretVaultError("Secret value must be a non-empty string")
    if len(value.encode("utf-8")) > _MAX_SECRET_VALUE_BYTES:
        raise SecretVaultError("Secret value exceeds the size limit")
    return value


def _restrictive_mode(path: Path, mode: int) -> None:
    with suppress(OSError):
        os.chmod(path, mode)


def _sync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(os.fspath(path), flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


SecretVault = HubSecretVault
PersonalHubSecretVault = HubSecretVault


__all__ = [
    "DPAPICipher",
    "DeterministicTestCipher",
    "HUB_SECRET_ENTROPY",
    "HubSecretVault",
    "PersonalHubSecretVault",
    "SecretCipher",
    "SecretVault",
    "SecretVaultError",
]
