# Personal Hub release runbook

This runbook produces and verifies Personal Hub independently from Local Assistant. A local
unsigned candidate is useful implementation evidence but must not be published as a release.

## Required environment

- Windows x64 with Python 3.12.
- Inno Setup 6 for the installer.
- A code-signing certificate accessible to `signtool.exe` for public releases.
- An Ed25519 private key stored outside the repository and an associated public key/key ID in
  the update trust configuration.
- A credential-free HTTPS update origin.
- Clean Windows VM snapshots with and without Local Assistant installed.
- Project-owned Google/Microsoft/Slack OAuth registrations, a non-production Canvas iCalendar
  feed, and non-production test accounts.

Never commit private keys, certificates, tokens, local databases, generated installers, or
machine-specific paths.

## Source and regression gates

From the repository root:

```powershell
python -m pip install -e ".[desktop,build,test,dev,ui-test]"
python -m ruff format --check .
python -m ruff check .
python scripts/check_documentation_consistency.py
python scripts/check_personal_hub_imports.py --output .benchmark-data/personal-hub-imports.json
$env:QT_QPA_PLATFORM = "offscreen"
powershell -ExecutionPolicy Bypass -File scripts/test_personal_hub.ps1 -Lane fast
powershell -ExecutionPolicy Bypass -File scripts/test_personal_hub.ps1 -Lane all
git diff --check
```

The import report must contain no Local Assistant namespace and no Qt module before explicit
desktop launch. All tests are offline by default. Local Assistant regression and coexistence
checks are performed from that product's repository or on the clean coexistence VM; the Hub
repository never installs Local Assistant as a test dependency.

## Reliability gates

Run the deterministic seven-day state-growth test:

```powershell
python scripts/run_personal_hub_reliability.py --mode accelerated --output .benchmark-data/personal-hub-seven-day.json
```

The report must show 168 virtual hours, six daily restarts, SQLite integrity, no duplicate
alerts, and feed/watch/message counts within configured limits. It must also show
`qualifies_as_24_hour_wall_clock_soak: false`.

Run a separate 24-hour elapsed tray soak in a release-like environment with representative
active fake or approved test sources:

```powershell
python scripts/run_personal_hub_reliability.py --mode wall-clock --hours 24 --output .benchmark-data/personal-hub-24-hour-soak.json
```

Do not accelerate or backdate this gate. Record start/end UTC, OS/build, power and
sleep/resume events, source set, refresh/failure counts, memory/database size, database
integrity, and any UI freeze or notification duplication.

## Build and package inspection

```powershell
python -m PyInstaller --clean --noconfirm packaging/personal-hub.spec
python scripts/verify_personal_hub_package.py dist/PersonalHub.exe --analysis build/personal-hub/Analysis-00.toc --output .benchmark-data/personal-hub-package.json
```

The verifier must find `personal_hub` and `PySide6` and reject any `assistant`,
`faster_whisper`, `ctranslate2`, `sounddevice`, `onnxruntime`, `openwakeword`, `playwright`,
`qasync`, `keyboard`, `numpy`, `torch`, or `transformers` entry. Inspect PyInstaller warnings;
do not waive a missing required provider/runtime module without an executable smoke test.
The same boundary excludes test/development and unrelated vision/database-driver modules such
as Hypothesis, pytest, mypy, Ruff, Pillow, pytesseract, and psycopg2.

## Sign executable and compile installer

Supply a certificate thumbprint only through the environment or command parameter:

```powershell
$env:PERSONAL_HUB_SIGNING_THUMBPRINT = "<release certificate thumbprint>"
powershell -ExecutionPolicy Bypass -File scripts/build_personal_hub.ps1 -SigningThumbprint $env:PERSONAL_HUB_SIGNING_THUMBPRINT
```

The build script timestamps and verifies `dist\PersonalHub.exe`, compiles
`packaging\personal-hub-installer.iss`, then signs and verifies the installer when Inno and
the certificate are available. Confirm the installer uses:

- product `Personal Hub` and executable `PersonalHub.exe`;
- AppId `{A42B9C7D-8D03-4F7E-AD72-30DBE72F62A1}`;
- `%LOCALAPPDATA%\Programs\PersonalHub`;
- startup value `PersonalHub`;
- no Local Assistant path, AppId, registry value, or file operation.

## Installer lifecycle and coexistence

First run the isolated local harness:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/verify_personal_hub_installer.ps1 `
  -Installer dist/personal-hub/PersonalHub-0.1.0-Setup.exe `
  -AssistantExecutable "$env:LOCALAPPDATA\Programs\LocalAssistant\LocalAssistant.exe" `
  -AssistantDatabase "$env:LOCALAPPDATA\LocalAssistant\assistant.db" `
  -Output .benchmark-data/personal-hub-installer-lifecycle.json
```

Omit an optional Assistant path only when that artifact truly is absent. The harness performs
clean install, same-version upgrade, preserve-data uninstall, delete-data uninstall, startup
cleanup, and before/after Assistant hashes in a validated unique temporary directory.

Repeat on clean VM snapshots:

1. Install and launch Hub without Local Assistant, including an offline first launch.
2. Create tasks, a feed, and a watch; restart and verify them.
3. Back up the Hub database, upgrade, and confirm schema/data.
4. Exercise update staging/apply and rollback to the exact prior signed executable.
5. Uninstall preserving data, reinstall, and confirm recovery.
6. Uninstall deleting data and confirm binaries/startup/data removal.
7. On the coexistence snapshot, hash/export Local Assistant executable, database, credential
   namespace metadata, settings, startup value, and behavior before the Hub lifecycle.
8. Install, run, upgrade, rollback, and uninstall Hub; prove all Assistant artifacts and
   behavior are unchanged and both processes/mutexes coexist.

## SBOM, provenance, and update signatures

Keep the Ed25519 key outside the repository and use a credential-free HTTPS base URL:

```powershell
python scripts/generate_personal_hub_release.py `
  --artifact dist/PersonalHub.exe `
  --artifact dist/personal-hub/PersonalHub-0.1.0-Setup.exe `
  --output-dir dist/personal-hub/release `
  --authenticode-signed `
  --require-clean `
  --signing-key "D:\secure\personal-hub-release-key.pem" `
  --key-id "personal-hub-2026-01" `
  --artifact-url-base "https://updates.example.com/personal-hub/"
```

Verify every SHA-256 checksum, detached signature, CycloneDX component list, source commit/tree
identity, SLSA/in-toto provenance subject, product/channel, semantic version, schema minimum,
and HTTPS artifact URL. Exercise wrong-key, tamper, downgrade, truncated artifact, apply
failure, and rollback tests. An ephemeral test signature never satisfies this gate.

## Live-provider acceptance

Use synthetic accounts with no private production content:

- Google Calendar and Outlook Calendar: authorize exact read scopes, select one calendar,
  verify bounded events/Today/masking/offline/stale/disconnect, and confirm no write grant.
- Gmail and Outlook Mail: select one unread folder/query, prove summary listing makes no body
  calls, explicitly open one sanitized body, verify remote content is blocked, clear local
  summaries, and prove provider mail is unchanged.
- Slack: select only approved channels or a saved query, verify bounded results, and confirm
  no post/reaction/upload scope or operation.
- Canvas Student: paste a rotated non-production private iCalendar URL, verify every concrete
  feed item creates one due-dated task and appears in Calendar, refresh without duplicates,
  complete a task and confirm refresh does not reopen it, enable the completed-assignment
  cleanup, complete a past-due assignment, refresh twice and restart to confirm the durable
  Personal Hub deletion marker prevents recreation, then disconnect and confirm the feed URL is
  removed while other local tasks remain.
- RSS/watch: include hostile redirect/private-address/oversize/malformed cases against a
  controlled server and confirm one source failure does not stale successful sources.

Capture provider consent screens/scopes, request method/endpoints, result counts, disconnect
behavior, and redacted logs. Never record tokens or real message content.

## Accessibility review

Run automated UI tests, then manually review at 720×460, 100–175% Hub text scale, Windows
high contrast, keyboard-only operation, reduced motion, Narrator, and at least one additional
screen reader. Confirm navigation order, visible focus, names/roles/states, list/action
relationships, masked-state announcements, no color-only status, and no refresh-induced focus
loss. Record findings in `PERSONAL_HUB_ACCESSIBILITY_REVIEW.md` and block release on critical
or high-severity issues.

## Release approval checklist

A public 1.0 is approved only when all of the following are recorded against the same clean
commit and artifact hashes:

- every Phase 0–7 deterministic and regression gate passes;
- actual 24-hour tray soak and accelerated seven-day growth pass;
- clean-VM fresh install, upgrade, backup/restore, update rollback, both uninstall choices,
  and coexistence pass;
- live-provider minimum-scope workflows pass;
- executable and installer Authenticode signatures verify;
- update/release/SBOM/provenance detached signatures verify with the release public key;
- manual accessibility review has no blocking findings;
- user/privacy documentation matches the candidate.

Phase 8 AI work is optional, disabled, and not a condition of non-AI 1.0.
