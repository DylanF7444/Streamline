# Repository Guidelines

## Structure

Personal Hub is a Python 3.12 project using a `src/` layout. Production code lives in
`src/personal_hub/`; regression and UI tests live in `tests/`; release tooling is in
`scripts/` and `packaging/`; product and release documentation is in `docs/`.

## Commands

- `python -m pip install -e ".[desktop,test,dev,ui-test,build]"`
- `python -m personal_hub.app`
- `powershell -ExecutionPolicy Bypass -File scripts/test_personal_hub.ps1 -Lane all`
- `python -m ruff format --check .`
- `python -m ruff check .`
- `powershell -ExecutionPolicy Bypass -File scripts/build_personal_hub.ps1`

## Architecture and security

Read `docs/PERSONAL_HUB_IMPLEMENTATION_PLAN.md` and
`docs/PERSONAL_HUB_AGENT_HANDOFF.md` before product changes. Personal Hub must remain useful
without AI and must not import, package, or require Local Assistant. Preserve its separate
application/process identity, data directory, database, credential namespace, settings,
logs, installer, startup entry, and update channel. External content is untrusted; retain
schema validation, minimum read scopes, bounded responses, SSRF defenses, sanitization,
privacy masking, retention controls, and offline-before-network behavior.

Use pytest for tests, 4-space Python indentation, explicit imports, `snake_case` names for
modules/functions/variables, and `PascalCase` for classes and Pydantic models. Keep tests
offline and deterministic with fake providers and temporary data roots. Do not commit
secrets, databases, generated artifacts, signing material, or machine-specific settings.
