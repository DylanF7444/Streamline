"""Phase 1 tests for the Personal Hub-owned secret vault."""

from __future__ import annotations

import inspect
import os
import stat
from pathlib import Path

import pytest

import personal_hub.storage.secrets as secrets_module
from personal_hub.config import HUB_SECRET_NAMESPACE, HubSettings
from personal_hub.storage.secrets import (
    HUB_SECRET_ENTROPY,
    DeterministicTestCipher,
    DPAPICipher,
    HubSecretVault,
    SecretVaultError,
)


def _settings(tmp_path: Path, name: str = "PersonalHub") -> HubSettings:
    return HubSettings(data_dir=tmp_path / name)


def test_vault_is_rooted_at_hub_settings_and_does_not_create_on_validation(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    assert not settings.secrets_dir.exists()
    vault = HubSecretVault(settings, DeterministicTestCipher())
    assert vault.root == settings.secrets_dir
    assert not settings.secrets_dir.exists()
    with pytest.raises(TypeError, match="HubSettings"):
        HubSecretVault(settings.secrets_dir, DeterministicTestCipher())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "name",
    [
        "",
        ".",
        "..",
        "../outside",
        r"..\outside",
        "nested/name",
        r"nested\name",
        "/absolute",
        "token.bin",
        " leading",
        "trailing ",
        "ümlaut",
        None,
    ],
)
def test_secret_name_validation_rejects_traversal_and_path_shapes(
    tmp_path: Path,
    name: str | None,
) -> None:
    vault = HubSecretVault(_settings(tmp_path), DeterministicTestCipher())
    with pytest.raises(SecretVaultError):
        vault.set(name, "value")  # type: ignore[arg-type]
    with pytest.raises(SecretVaultError):
        vault.get(name)  # type: ignore[arg-type]


def test_set_get_list_delete_keep_values_out_of_records_and_listing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    vault = HubSecretVault(settings, DeterministicTestCipher())
    private_value = "hub-only-token-value"

    vault.set("calendar_token", private_value)
    record_path = settings.secrets_dir / "calendar_token.bin"
    record = record_path.read_bytes()

    assert vault.get("calendar_token") == private_value
    assert vault.list() == ["calendar_token"]
    assert vault.names() == ["calendar_token"]
    assert private_value.encode() not in record
    assert private_value not in repr(vault)

    vault.delete("calendar_token")
    assert vault.get("calendar_token") is None
    assert vault.list() == []
    vault.delete("calendar_token")


def test_hub_namespace_and_roots_are_distinct_from_local_assistant(tmp_path: Path) -> None:
    first = HubSecretVault(_settings(tmp_path, "hub-one"), DeterministicTestCipher())
    second = HubSecretVault(_settings(tmp_path, "hub-two"), DeterministicTestCipher())

    assert first.root != second.root
    assert first.namespace == HUB_SECRET_NAMESPACE == "PersonalHub"
    assert first.namespace != "LocalAssistant"
    assert DPAPICipher.namespace == HUB_SECRET_NAMESPACE
    assert DPAPICipher.entropy == HUB_SECRET_ENTROPY
    assert b"LocalAssistant" not in HUB_SECRET_ENTROPY


def test_default_cipher_is_dpapi_and_test_cipher_is_explicit(tmp_path: Path) -> None:
    vault = HubSecretVault(_settings(tmp_path))
    assert isinstance(vault.cipher, DPAPICipher)
    assert isinstance(DeterministicTestCipher(), DeterministicTestCipher)


def test_atomic_replace_preserves_previous_record_on_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    vault = HubSecretVault(settings, DeterministicTestCipher())
    vault.set("token", "old-value")
    record_path = settings.secrets_dir / "token.bin"
    previous = record_path.read_bytes()

    def fail_replace(*_args: object) -> None:
        raise OSError("simulated atomic replace failure")

    monkeypatch.setattr(secrets_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="atomic replace"):
        vault.set("token", "new-value")

    assert record_path.read_bytes() == previous
    assert vault.get("token") == "old-value"
    assert list(settings.secrets_dir.glob("*.tmp")) == []


def test_secret_files_use_restrictive_modes_where_supported(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    vault = HubSecretVault(settings, DeterministicTestCipher())
    vault.set("token", "value")

    if os.name != "nt":
        assert stat.S_IMODE(settings.secrets_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE((settings.secrets_dir / "token.bin").stat().st_mode) == 0o600


def test_listing_does_not_decrypt_values(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    writer = HubSecretVault(settings, DeterministicTestCipher())
    writer.set("token", "value-that-stays-encrypted")

    class NoDecryptCipher(DeterministicTestCipher):
        def decrypt(self, _ciphertext: bytes) -> str:
            raise AssertionError("list must not decrypt secret values")

    reader = HubSecretVault(settings, NoDecryptCipher())
    assert reader.list() == ["token"]


def test_assistant_vault_file_is_never_discovered_or_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assistant_root = tmp_path / "LocalAssistant" / "secrets"
    assistant_root.mkdir(parents=True)
    assistant_file = assistant_root / "token.bin"
    assistant_file.write_bytes(b"legacy-assistant-ciphertext")
    settings = _settings(tmp_path, "PersonalHub")
    vault = HubSecretVault(settings, DeterministicTestCipher())

    original_read_bytes = Path.read_bytes

    def reject_assistant_read(path: Path) -> bytes:
        if path == assistant_file:
            raise AssertionError("Personal Hub read the Assistant vault")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", reject_assistant_read)
    assert vault.list() == []
    assert vault.get("token") is None
    assert "assistant.storage.secrets" not in inspect.getsource(secrets_module)
