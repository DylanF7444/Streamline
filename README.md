# Streamline (Personal Hub)

A local-first Windows desktop app for daily attention management. Streamline brings tasks, calendar, news, web watches, selected attention summaries, and reminders into one private workspace. It runs independently of Local Assistant and does not require an LLM, voice service, or cloud account for its core workflows.

## Highlights

- **Today and planning:** Tasks, recurrence, reminders, calendar views, and a durable Inbox in a customizable desktop dashboard.
- **Useful signals:** RSS/Atom feeds, price and restock alerts, webpage change watches, and selected Gmail, Outlook, and Slack summaries.
- **Privacy controls:** Offline mode, privacy masking, isolated encrypted credential storage, bounded local data, and explicit source permissions.
- **User-owned sync:** Optional, manual encrypted synchronization through a shared folder selected by the user. Provider integrations are read-only by default; Google Calendar event sync requires separate opt-in and per-item selection.

## Implementation

Python 3.12, PySide6, SQLAlchemy, SQLite, Pydantic, and cryptography. The app has its own package, data root, database, vault, process identity, and release path, separate from Local Assistant. PyInstaller and Inno Setup release tooling and a release runbook are included; final external release checks are documented there.

## Run from source

```powershell
python -m pip install -e ".[desktop,test,dev,ui-test,build]"
powershell -ExecutionPolicy Bypass -File scripts/run_personal_hub.ps1
```

Run the normal core checks with:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/test_personal_hub.ps1
```

Use -Lane all for the complete source gate. The build and packaging gate is:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build_personal_hub.ps1
```

## Project docs

- [Implementation plan](docs/PERSONAL_HUB_IMPLEMENTATION_PLAN.md)
- [Current implementation status](docs/PERSONAL_HUB_IMPLEMENTATION_STATUS.md)
- [Repository migration](docs/PERSONAL_HUB_REPOSITORY_MIGRATION.md)
- [Release runbook](docs/PERSONAL_HUB_RELEASE_RUNBOOK.md)

The app stores its own local data and credentials. Do not copy Local Assistant databases, secrets, build outputs, or machine-local configuration into this repository.