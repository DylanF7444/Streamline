# Personal Hub release evidence

Evidence date: September 1, 2026. Environment: Windows 11, Python 3.12.6, PyInstaller 6.14.1,
Inno Setup 6.7.3. Source: dirty `codex/personal-hub-planning` worktree. Classification:
development evidence only, not a signed public release.

## Package

```text
python -m PyInstaller --clean --noconfirm packaging/personal-hub.spec
PASS — dist/PersonalHub.exe, 80,294,662 bytes

python scripts/verify_personal_hub_package.py dist/PersonalHub.exe --analysis build/personal-hub/Analysis-00.toc --output .benchmark-data/personal-hub-package.json
PASS — archive 2,291 entries; analysis 2,440 entries; no forbidden entries; all required prefixes present
```

The package verifier reads both the actual CArchive/PYZ and PyInstaller analysis. An initial
development run exposed a verifier bug that counted the spec's explicit exclusion list as
included content; the parser now accepts only actual three-field TOC entries, and the archive
and analysis reports remain independent.

## Installer

```text
ISCC.exe packaging\personal-hub-installer.iss
PASS — dist/personal-hub/PersonalHub-0.1.0-Setup.exe, 81,122,892 bytes

scripts/verify_personal_hub_installer.ps1
PASS — clean install (final verification at 2026-09-02T03:50:29Z)
PASS — same-version upgrade
PASS — preserve-data uninstall
PASS — delete-data uninstall
PASS — startup registration cleanup
PASS — Local Assistant executable/database/startup invariance for supplied local artifacts
```

The verifier uses a unique validated system-temporary root. Its first development execution
also found that quoted Inno path-valued arguments were required on user profiles containing
spaces; that defect was corrected before the passing evidence above.

## Reliability

```text
python scripts/run_personal_hub_reliability.py --mode accelerated --output .benchmark-data/personal-hub-seven-day.json
PASS — 168 virtual hours; six restarts; SQLite integrity; no alerts; max/final feed entries 20, watch observations 20, message summaries 20
```

The report correctly states that accelerated time does not qualify as the required 24-hour
wall-clock tray soak.

## Release metadata

```text
python scripts/generate_personal_hub_release.py --artifact dist/PersonalHub.exe --artifact dist/personal-hub/PersonalHub-0.1.0-Setup.exe --output-dir dist/personal-hub/release
PASS — SHA256SUMS, CycloneDX SBOM, release manifest, SLSA/in-toto provenance
```

The candidate reports `NotSigned` for both executable and installer. No Authenticode
thumbprint, Ed25519 private release key, key ID, or hosted update URL was supplied. The
deterministic test suite does prove signature verification/tamper/downgrade/apply/rollback
behavior using ephemeral keys, but that is not release signing evidence.

## Pre-extraction source and coexistence regressions

The commands and counts in this section are historical evidence from the former combined
repository. Current standalone commands and migration results are recorded in
`PERSONAL_HUB_REPOSITORY_MIGRATION.md` and `PERSONAL_HUB_IMPLEMENTATION_STATUS.md`.

```text
python -m pytest tests/hub -q
PASS — 182 tests in 218.17s

python -m pytest tests --ignore=tests/hub --ignore=tests/ui -q
PASS — 1,221 Local Assistant non-UI tests in 183.59s

python -m pytest tests/ui -q
PASS — 73 Local Assistant UI tests in 30.64s

python -m pytest learning_tests -v
PASS — 29 tests in 3.69s

python -m ruff format --check .
PASS — 517 files already formatted

python -m ruff check .
PASS

python scripts/check_personal_hub_imports.py
PASS — no Assistant or pre-desktop Qt modules

python scripts/check_documentation_consistency.py
PASS

git diff --check
PASS (line-ending notices only)
```

The actual packaged executable also smoke-launched with `QT_QPA_PLATFORM=offscreen` and an
isolated `--data-dir`, created a 303,104-byte migrated `personal_hub.db`, and produced the two
expected PyInstaller one-file processes. The harness then stopped every process whose image
path exactly matched the workspace artifact and verified none remained.

## Pending evidence

- actual 24-hour wall-clock active-source tray soak;
- clean-VM install/upgrade/backup/restore/update rollback/uninstall/coexistence;
- Authenticode and detached release/update signatures from protected release keys;
- live Google, Microsoft, and Slack minimum-scope workflows;
- manual Windows high-contrast, scaling, keyboard, screen-reader, and user review.

Artifact outputs under `dist/` and `.benchmark-data/` are local build evidence and are not
source files to commit.
