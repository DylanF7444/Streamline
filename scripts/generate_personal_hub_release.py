"""Generate Personal Hub SBOM, provenance, hashes, and optionally signed manifests."""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.metadata as metadata
import json
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from uuid import NAMESPACE_URL, uuid5

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from packaging.utils import canonicalize_name

from personal_hub import __version__
from personal_hub.config import HUB_UPDATE_CHANNEL
from personal_hub.storage.database import HubDatabase
from personal_hub.updates import HubUpdateArtifact, HubUpdateManifest

ROOT = Path(__file__).resolve().parents[1]
PRODUCT_ID = "personal-hub"
RUNTIME_DISTRIBUTIONS = (
    "annotated-types",
    "certifi",
    "cffi",
    "cryptography",
    "greenlet",
    "platformdirs",
    "pycparser",
    "pydantic",
    "pydantic-core",
    "PySide6",
    "PySide6-Addons",
    "PySide6-Essentials",
    "shiboken6",
    "SQLAlchemy",
    "typing-extensions",
    "typing-inspection",
    "tzdata",
)


def _git(*arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _source_identity() -> dict[str, object]:
    paths = _git("ls-files", "--cached", "--others", "--exclude-standard", "-z").split("\0")
    digest = hashlib.sha256()
    count = 0
    for relative in sorted(item for item in paths if item):
        path = (ROOT / relative).resolve()
        if not path.is_file() or ROOT.resolve() not in path.parents:
            continue
        payload = path.read_bytes()
        name = relative.replace("\\", "/").encode()
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
        count += 1
    return {
        "git_commit": _git("rev-parse", "HEAD"),
        "git_dirty": bool(_git("status", "--porcelain=v1", "--untracked-files=all")),
        "source_tree_sha256": digest.hexdigest(),
        "source_file_count": count,
    }


def _artifact(path: Path) -> dict[str, object]:
    payload = path.read_bytes()
    return {
        "filename": path.name,
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _license(distribution: metadata.Distribution) -> str:
    value = distribution.metadata.get("License-Expression") or distribution.metadata.get("License")
    return value.strip()[:500] if value and value.strip() else "NOASSERTION"


def build_sbom(*, generated_at: datetime, source: dict[str, object]) -> dict[str, object]:
    components: list[dict[str, object]] = []
    for requested in RUNTIME_DISTRIBUTIONS:
        try:
            distribution = metadata.distribution(requested)
        except metadata.PackageNotFoundError:
            continue
        name = str(distribution.metadata["Name"])
        canonical = canonicalize_name(name)
        reference = f"pkg:pypi/{canonical}@{distribution.version}"
        component: dict[str, object] = {
            "type": "library",
            "bom-ref": reference,
            "name": name,
            "version": distribution.version,
            "purl": reference,
            "licenses": [{"license": {"name": _license(distribution)}}],
        }
        raw_metadata = distribution.read_text("METADATA")
        if raw_metadata:
            component["hashes"] = [
                {
                    "alg": "SHA-256",
                    "content": hashlib.sha256(raw_metadata.encode()).hexdigest(),
                }
            ]
        components.append(component)
    root_reference = f"pkg:generic/{PRODUCT_ID}@{__version__}"
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid5(NAMESPACE_URL, str(source['source_tree_sha256']))}",
        "version": 1,
        "metadata": {
            "timestamp": generated_at.isoformat(),
            "tools": [{"vendor": "Personal Hub", "name": Path(__file__).name}],
            "component": {
                "type": "application",
                "bom-ref": root_reference,
                "name": PRODUCT_ID,
                "version": __version__,
            },
            "properties": [
                {"name": "personal-hub:git-commit", "value": source["git_commit"]},
                {"name": "personal-hub:update-channel", "value": HUB_UPDATE_CHANNEL},
            ],
        },
        "components": sorted(components, key=lambda item: str(item["name"]).casefold()),
        "dependencies": [
            {
                "ref": root_reference,
                "dependsOn": sorted(str(item["bom-ref"]) for item in components),
            }
        ],
    }


def _load_private_key(path: Path | None) -> Ed25519PrivateKey | None:
    if path is None:
        return None
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise TypeError("Personal Hub manifest signing requires an Ed25519 private key")
    return key


def _signed_update_manifest(
    artifact: dict[str, object],
    *,
    artifact_url: str,
    generated_at: datetime,
    key_id: str,
    private_key: Ed25519PrivateKey,
) -> HubUpdateManifest:
    parsed = urlsplit(artifact_url)
    if parsed.scheme.casefold() != "https" or not parsed.hostname:
        raise ValueError("Personal Hub artifact URL must use HTTPS")
    placeholder = base64.b64encode(bytes(64)).decode()
    manifest = HubUpdateManifest(
        version=__version__,
        published_at=generated_at,
        minimum_schema_version=HubDatabase.CURRENT_SCHEMA_VERSION,
        artifact=HubUpdateArtifact(
            url=artifact_url,
            size_bytes=int(artifact["size_bytes"]),
            sha256=str(artifact["sha256"]),
        ),
        key_id=key_id,
        signature=placeholder,
    )
    signature = base64.b64encode(private_key.sign(manifest.signed_payload())).decode()
    return manifest.model_copy(update={"signature": signature})


def generate_release(
    artifacts: tuple[Path, ...],
    output_directory: Path,
    *,
    artifact_url_base: str | None = None,
    signing_key: Path | None = None,
    key_id: str | None = None,
    authenticode_signed: bool = False,
    require_clean: bool = False,
) -> dict[str, Path]:
    selected = tuple(path.resolve(strict=True) for path in artifacts)
    records = tuple(_artifact(path) for path in selected)
    source = _source_identity()
    if require_clean and source["git_dirty"]:
        raise RuntimeError("Personal Hub releases require a clean source tree")
    generated_at = datetime.now(UTC)
    private_key = _load_private_key(signing_key)
    if (private_key is None) != (key_id is None):
        raise ValueError("manifest signing key and key ID must be supplied together")
    output_directory.mkdir(parents=True, exist_ok=True)
    outputs: dict[str, Path] = {}

    sbom = build_sbom(generated_at=generated_at, source=source)
    outputs["sbom"] = output_directory / "personal-hub-sbom.cdx.json"
    outputs["sbom"].write_text(json.dumps(sbom, indent=2) + "\n", encoding="utf-8")

    update_path: Path | None = None
    executable = next(
        (record for record in records if record["filename"] == "PersonalHub.exe"), None
    )
    if executable is not None and artifact_url_base and private_key is not None and key_id:
        base = artifact_url_base.rstrip("/") + "/"
        update = _signed_update_manifest(
            executable,
            artifact_url=urljoin(base, "PersonalHub.exe"),
            generated_at=generated_at,
            key_id=key_id,
            private_key=private_key,
        )
        update_path = output_directory / "personal-hub-update-manifest.json"
        update_path.write_text(update.model_dump_json(indent=2) + "\n", encoding="utf-8")
        outputs["update_manifest"] = update_path

    gates = {
        "hub_tests": "verified_by_build_script",
        "standalone_repository_boundary": "verified_by_build_script",
        "local_assistant_coexistence": "requires_clean_windows_vm",
        "packaged_dependency_boundary": "verified_by_build_script",
        "authenticode": "passed" if authenticode_signed else "requires_release_certificate",
        "signed_update_manifest": "passed" if update_path else "requires_release_key_and_url",
        "wall_clock_24_hour_soak": "requires_recorded_release_environment",
        "clean_install_upgrade_rollback_uninstall": "requires_recorded_windows_vm",
    }
    manifest = {
        "schema_version": "1.0",
        "generated_at": generated_at.isoformat(),
        "release": {
            "product": PRODUCT_ID,
            "display_name": "Personal Hub",
            "version": __version__,
            "channel": HUB_UPDATE_CHANNEL,
            "schema_version": HubDatabase.CURRENT_SCHEMA_VERSION,
            **source,
        },
        "artifacts": records,
        "gates": gates,
    }
    outputs["release_manifest"] = output_directory / "personal-hub-release-manifest.json"
    outputs["release_manifest"].write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    checksums = "".join(f"{item['sha256']}  {item['filename']}\n" for item in records)
    outputs["checksums"] = output_directory / "SHA256SUMS"
    outputs["checksums"].write_text(checksums, encoding="utf-8")

    provenance = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [
            {"name": item["filename"], "digest": {"sha256": item["sha256"]}} for item in records
        ],
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildDefinition": {
                "buildType": "https://personal-hub.invalid/build/PyInstaller-InnoSetup/v1",
                "externalParameters": {
                    "spec": "packaging/personal-hub.spec",
                    "installer": "packaging/personal-hub-installer.iss",
                    "channel": HUB_UPDATE_CHANNEL,
                },
                "resolvedDependencies": [
                    {
                        "uri": "git+local://personal-hub",
                        "digest": {"sha256": source["source_tree_sha256"]},
                    }
                ],
            },
            "runDetails": {
                "builder": {"id": "scripts/build_personal_hub.ps1"},
                "metadata": {
                    "invocationId": hashlib.sha256(
                        f"{source['source_tree_sha256']}:{generated_at.isoformat()}".encode()
                    ).hexdigest(),
                    "startedOn": generated_at.isoformat(),
                    "finishedOn": generated_at.isoformat(),
                },
                "byproducts": [
                    {"name": "python", "value": sys.version.split()[0]},
                    {"name": "platform", "value": platform.platform()},
                ],
            },
        },
    }
    outputs["provenance"] = output_directory / "personal-hub-provenance.intoto.jsonl"
    outputs["provenance"].write_text(json.dumps(provenance) + "\n", encoding="utf-8")

    if private_key is not None:
        for name in ("release_manifest", "sbom", "provenance"):
            path = outputs[name]
            signature_path = path.with_suffix(path.suffix + ".sig")
            signature_path.write_text(
                base64.b64encode(private_key.sign(path.read_bytes())).decode() + "\n",
                encoding="ascii",
            )
            outputs[name + "_signature"] = signature_path
    return outputs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", action="append", type=Path, default=[])
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "dist" / "personal-hub" / "release",
    )
    parser.add_argument("--artifact-url-base")
    parser.add_argument("--signing-key", type=Path)
    parser.add_argument("--key-id")
    parser.add_argument("--authenticode-signed", action="store_true")
    parser.add_argument("--require-clean", action="store_true")
    args = parser.parse_args()
    outputs = generate_release(
        tuple(args.artifact),
        args.output_dir,
        artifact_url_base=args.artifact_url_base,
        signing_key=args.signing_key,
        key_id=args.key_id,
        authenticode_signed=args.authenticode_signed,
        require_clean=args.require_clean,
    )
    print("Generated Personal Hub release evidence: " + ", ".join(sorted(outputs)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
