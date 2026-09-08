# Personal Hub

Personal Hub is a standalone, local-first Windows desktop application for daily attention
management. It provides Today, Tasks, Calendar, Watchlists, Inbox, Sources, and Settings
without loading or depending on Local Assistant, language models, voice services, or a
general command surface.

## Development

Python 3.12 is required.

```powershell
python -m pip install -e ".[desktop,test,dev,ui-test,build]"
python -m personal_hub.app
powershell -ExecutionPolicy Bypass -File scripts/test_personal_hub.ps1
```

Use `-Lane all` for the complete source gate. The build and packaging gate is:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build_personal_hub.ps1
```

The source, tests, release tooling, installer, and documentation in this directory are
self-contained and can be initialized as their own Git repository. Do not copy Local
Assistant databases, credentials, build outputs, or machine-local configuration into it.

See [the implementation plan](docs/PERSONAL_HUB_IMPLEMENTATION_PLAN.md), [current status](docs/PERSONAL_HUB_IMPLEMENTATION_STATUS.md), [migration status](docs/PERSONAL_HUB_REPOSITORY_MIGRATION.md), and [release runbook](docs/PERSONAL_HUB_RELEASE_RUNBOOK.md).
