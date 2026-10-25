"""Phase 7 package, installer, update, provenance, and reliability gates."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from personal_hub.storage.database import HubDatabase
from personal_hub.updates import (
    HubUpdateArtifact,
    HubUpdateError,
    HubUpdateManifest,
    apply_staged_update,
    parse_and_verify_manifest,
    rollback_update,
    stage_verified_artifact,
)
from scripts.generate_personal_hub_release import generate_release
from scripts.run_personal_hub_reliability import run_accelerated
from scripts.verify_personal_hub_package import analysis_names, verify_names

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 1, 16, tzinfo=UTC)


def _signed_manifest(payload: bytes) -> tuple[HubUpdateManifest, bytes]:
    private = Ed25519PrivateKey.generate()
    artifact = HubUpdateArtifact(
        url="https://updates.example.test/personal-hub/PersonalHub.exe",
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    placeholder = base64.b64encode(bytes(64)).decode()
    manifest = HubUpdateManifest(
        version="0.2.0",
        published_at=NOW,
        minimum_schema_version=HubDatabase.CURRENT_SCHEMA_VERSION,
        artifact=artifact,
        key_id="release-2026",
        signature=placeholder,
    )
    manifest = manifest.model_copy(
        update={"signature": base64.b64encode(private.sign(manifest.signed_payload())).decode()}
    )
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return manifest, public


def test_personal_hub_spec_is_one_product_and_explicitly_excludes_assistant_stacks() -> None:
    text = (ROOT / "packaging" / "personal-hub.spec").read_text(encoding="utf-8")
    assert "../src/personal_hub/app.py" in text
    assert 'name="PersonalHub"' in text
    assert 'collect_submodules("personal_hub")' in text
    assert 'collect_submodules("assistant")' not in text
    for forbidden in (
        "assistant",
        "faster_whisper",
        "ctranslate2",
        "sounddevice",
        "onnxruntime",
        "openwakeword",
        "playwright",
        "qasync",
        "hypothesis",
        "pytest",
        "PIL",
        "psycopg2",
    ):
        assert f'"{forbidden}"' in text


def test_installer_uses_separate_identity_startup_and_explicit_data_choice() -> None:
    hub = (ROOT / "packaging" / "personal-hub-installer.iss").read_text(encoding="utf-8")
    assert "A42B9C7D-8D03-4F7E-AD72-30DBE72F62A1" in hub
    assert "8468D5DE-ED95-4D85-9103-E4D25A03131C" not in hub
    assert "Programs\\PersonalHub" in hub
    assert 'ValueName: "PersonalHub"' in hub
    assert "uninsdeletevalue" in hub
    assert 'Name: "startup"' in hub and "Flags: unchecked" in hub
    assert "DeleteUserDataRequested" in hub
    assert "DELETEUSERDATA" in hub and "HUBDATADIR" in hub
    assert "SafeToDelete" in hub
    assert "PersonalHubInstallerVerification-" in hub
    assert hub.index("if SafeToDelete then") < hub.index("DelTree(NormalizedDataDir")
    assert "Choose No to preserve all user data" in hub
    assert "LocalAssistant" not in hub


def test_signed_update_manifest_rejects_wrong_key_tamper_and_downgrade() -> None:
    payload = b"new-personal-hub-executable"
    manifest, public = _signed_manifest(payload)
    verified = parse_and_verify_manifest(
        manifest.model_dump_json(),
        {"release-2026": public},
        current_version="0.1.0",
    )
    assert verified.artifact.sha256 == hashlib.sha256(payload).hexdigest()
    with pytest.raises(HubUpdateError, match="signing key"):
        parse_and_verify_manifest(
            manifest.model_dump_json(),
            {},
            current_version="0.1.0",
        )
    tampered = json.loads(manifest.model_dump_json())
    tampered["artifact"]["sha256"] = "0" * 64
    with pytest.raises(HubUpdateError, match="signature"):
        parse_and_verify_manifest(
            json.dumps(tampered),
            {"release-2026": public},
            current_version="0.1.0",
        )
    with pytest.raises(HubUpdateError, match="not newer"):
        parse_and_verify_manifest(
            manifest.model_dump_json(),
            {"release-2026": public},
            current_version="0.2.0",
        )


def test_update_stage_apply_and_rollback_touch_only_personal_hub_executable(
    tmp_path: Path,
) -> None:
    new_payload = b"new-personal-hub-executable"
    manifest, _public = _signed_manifest(new_payload)
    downloaded = tmp_path / "download" / "PersonalHub.exe"
    downloaded.parent.mkdir()
    downloaded.write_bytes(new_payload)
    staged = stage_verified_artifact(downloaded, manifest, tmp_path / "staging")
    installed = tmp_path / "install" / "PersonalHub.exe"
    installed.parent.mkdir()
    installed.write_bytes(b"previous-personal-hub-executable")
    assistant = tmp_path / "assistant" / "LocalAssistant.exe"
    assistant.parent.mkdir()
    assistant.write_bytes(b"assistant-must-not-change")

    backup = apply_staged_update(staged, installed, tmp_path / "backups")
    assert installed.read_bytes() == new_payload
    assert backup.read_bytes() == b"previous-personal-hub-executable"
    assert assistant.read_bytes() == b"assistant-must-not-change"
    rollback_update(backup, installed)
    assert installed.read_bytes() == b"previous-personal-hub-executable"
    assert assistant.read_bytes() == b"assistant-must-not-change"


def test_release_generator_outputs_signed_sbom_manifest_update_and_provenance(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "PersonalHub.exe"
    artifact.write_bytes(b"release-candidate")
    private = Ed25519PrivateKey.generate()
    key_path = tmp_path / "manifest-key.pem"
    key_path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    outputs = generate_release(
        (artifact,),
        tmp_path / "release",
        artifact_url_base="https://updates.example.test/personal-hub/",
        signing_key=key_path,
        key_id="test-release-key",
        authenticode_signed=True,
    )
    assert {
        "sbom",
        "release_manifest",
        "update_manifest",
        "provenance",
        "checksums",
        "sbom_signature",
        "release_manifest_signature",
        "provenance_signature",
    }.issubset(outputs)
    sbom = json.loads(outputs["sbom"].read_text(encoding="utf-8"))
    assert sbom["bomFormat"] == "CycloneDX"
    components = {item["name"].casefold() for item in sbom["components"]}
    assert "pyside6" in components and "sqlalchemy" in components
    assert not components.intersection(
        {"faster-whisper", "ctranslate2", "onnxruntime", "openwakeword"}
    )
    release = json.loads(outputs["release_manifest"].read_text(encoding="utf-8"))
    assert release["release"]["product"] == "personal-hub"
    assert release["release"]["channel"] == "personal-hub-stable"
    assert release["gates"]["authenticode"] == "passed"
    update = parse_and_verify_manifest(
        outputs["update_manifest"].read_bytes(),
        {
            "test-release-key": private.public_key().public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
        },
        current_version="0.0.0",
    )
    assert update.artifact.sha256 == hashlib.sha256(b"release-candidate").hexdigest()
    provenance = json.loads(outputs["provenance"].read_text(encoding="utf-8"))
    assert provenance["predicateType"] == "https://slsa.dev/provenance/v1"


def test_package_analysis_verifier_rejects_forbidden_modules(tmp_path: Path) -> None:
    safe = tmp_path / "safe.toc"
    safe.write_text(
        repr(
            (
                [("personal_hub.app", "src/personal_hub/app.py", "PYMODULE")],
                [("PySide6.QtCore", "site-packages/PySide6/QtCore.pyd", "BINARY")],
            )
        ),
        encoding="utf-8",
    )
    safe_report = verify_names(analysis_names(safe))
    assert safe_report["passes"]
    unsafe = set(analysis_names(safe)) | {"assistant.router", "faster_whisper"}
    unsafe_report = verify_names(unsafe)
    assert not unsafe_report["passes"]
    assert set(unsafe_report["forbidden_entries"]) == {
        "assistant.router",
        "faster_whisper",
    }


def test_package_analysis_does_not_count_pyinstaller_exclusion_list_as_content(
    tmp_path: Path,
) -> None:
    analysis = tmp_path / "Analysis-00.toc"
    analysis.write_text(
        repr(
            (
                ["personal_hub", "PySide6.QtCore"],
                ["assistant", "assistant.router", "faster_whisper"],
                [
                    ("personal_hub.app", "src/personal_hub/app.py", "PYMODULE"),
                    ("PySide6.QtCore", "site-packages/PySide6/QtCore.pyd", "BINARY"),
                ],
            )
        ),
        encoding="utf-8",
    )
    names = analysis_names(analysis)
    assert names == {"personal_hub.app", "PySide6.QtCore"}
    assert verify_names(names)["passes"]


def test_accelerated_seven_day_state_growth_is_bounded_across_daily_restarts(
    tmp_path: Path,
) -> None:
    report = run_accelerated(tmp_path / "reliability")
    assert report["passes"]
    assert report["qualifies_as_seven_day_bounded_growth"]
    assert not report["qualifies_as_24_hour_wall_clock_soak"]
    assert report["restart_count"] == 6
    assert report["maximum_counts"] == {
        "feed_entries": 20,
        "watch_observations": 20,
        "watch_alerts": 0,
        "message_summaries": 20,
    }


def test_release_build_script_never_installs_assistant_heavy_extras() -> None:
    text = (ROOT / "scripts" / "build_personal_hub.ps1").read_text(encoding="utf-8")
    assert ".[desktop,build,test,dev,ui-test]" in text
    assert "packaging/personal-hub.spec" in text
    assert "verify_personal_hub_package.py" in text
    assert "generate_personal_hub_release.py" in text
    assert "build/personal-hub/Analysis-00.toc" in text
    install_line = next(line for line in text.splitlines() if "pip install" in line)
    assert not any(
        extra in install_line
        for extra in ("audio", "stt", "wakeword", "browser", "integrations", "vision")
    )


def test_release_documentation_exists_and_preserves_evidence_boundaries() -> None:
    required = {
        "PERSONAL_HUB_USER_GUIDE.md": (
            "memory-only bodies",
            "https://www.googleapis.com/auth/gmail.readonly",
            "live-provider release acceptance",
        ),
        "PERSONAL_HUB_PRIVACY.md": (
            "Provider-canonical data",
            "personal-hub-stable",
            "24-hour elapsed tray soak",
        ),
        "PERSONAL_HUB_RELEASE_RUNBOOK.md": (
            "qualifies_as_24_hour_wall_clock_soak: false",
            "code-signing certificate",
            "Live-provider acceptance",
        ),
        "PERSONAL_HUB_ACCESSIBILITY_REVIEW.md": (
            "Manual review still required before release",
            "screen reader",
        ),
        "PERSONAL_HUB_RELEASE_EVIDENCE.md": (
            "development evidence only",
            "NotSigned",
        ),
    }
    for name, phrases in required.items():
        text = (ROOT / "docs" / name).read_text(encoding="utf-8")
        for phrase in phrases:
            assert phrase.casefold() in text.casefold(), f"{name} is missing {phrase!r}"
