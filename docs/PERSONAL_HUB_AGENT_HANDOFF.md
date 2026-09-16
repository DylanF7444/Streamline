# Personal Hub Implementation Agent Handoff

- **Status:** Ready for implementation
- **Date:** August 31, 2026
- **Authoritative specification:** `docs/PERSONAL_HUB_IMPLEMENTATION_PLAN.md`

## Purpose

This document is the portable handoff for starting Personal Hub implementation in a fresh
Codex task or on another development machine. It records the recommended models, repository
setup, authoritative documents, execution expectations, master implementation prompt, and
completion evidence required for a trustworthy handoff.

Local Assistant remains an existing, independent product in another repository. Personal Hub
is a standalone application and must not require a Local Assistant checkout or package.

## Recommended Codex Configuration

### Primary agent

- **Model:** `gpt-5.6-sol`
- **Reasoning:** `xhigh`
- **Role:** Own the full implementation goal, architecture, security decisions, integration,
  delegated-work review, test strategy, packaging, and completion judgment.

OpenAI describes GPT-5.6 Sol as its flagship model for complex professional work and
recommends it as the starting point for complex reasoning and coding. The model supports
`xhigh` and `max` reasoning. See the
[official GPT-5.6 Sol documentation](https://developers.openai.com/api/docs/models/gpt-5.6-sol).

Use `xhigh` for the main task because it matches this repository's required agent role and is
appropriate for a long, security-sensitive, cross-cutting implementation. If no delegation
is available, keep the entire implementation in this Sol task.

### Delegated implementation agents

- **Model:** `gpt-5.6-luna`
- **Reasoning:** `max`
- **Role:** Complete bounded, non-overlapping implementation units assigned by the Sol agent.

Good delegated units include an isolated repository, provider adapter, UI surface, test
suite, documentation set, or packaging component. The Sol agent must review, integrate, and
test every delegated result. Child agents must receive explicit model and reasoning overrides
because they may otherwise inherit the parent configuration.

### Cost-balanced fallback

If GPT-5.6 Sol is unavailable, use `gpt-5.6-terra` with `xhigh` reasoning. Terra is the
documented balance between intelligence and cost, but Sol remains the recommendation for
this implementation's architecture and security burden. Do not substitute a cheaper model
without retaining the same acceptance gates and verification requirements.

## Authoritative Repository Documents

Read these completely before editing:

1. `AGENTS.md`
2. `docs/PERSONAL_HUB_IMPLEMENTATION_PLAN.md`
3. `docs/PERSONAL_HUB_AGENT_HANDOFF.md`
4. `docs/PERSONAL_HUB_IMPLEMENTATION_STATUS.md`
5. `docs/PERSONAL_HUB_REPOSITORY_MIGRATION.md`
6. `docs/PERSONAL_HUB_PRIVACY.md`
7. `docs/PERSONAL_HUB_RELEASE_RUNBOOK.md`
8. `pyproject.toml`

Inspect related source and tests progressively as each phase begins. The Personal Hub plan is
authoritative when it intentionally chooses a narrower boundary than the existing assistant.

## Starting on Another Device

After the documentation changes are committed and pushed, prepare the new machine:

```powershell
git clone <repository-url>
Set-Location -LiteralPath '<cloned-repository-path>'
git pull
python -m pip install -e ".[test,dev,ui-test,desktop,build]"
python scripts/check_documentation_consistency.py
git status --short
```

Before implementation:

- Confirm the expected branch or worktree is checked out.
- Confirm `git status --short` contains no unexplained changes.
- Do not copy Local Assistant databases, tokens, local models, installers, or machine-specific
  settings to the new device.
- Provider credentials are not required for the initial phases; use deterministic fakes.
- When live-provider acceptance is eventually needed, configure credentials locally and keep
  them outside version control.

Recommended task configuration in Codex:

```text
Model: gpt-5.6-sol
Reasoning: xhigh
Project: this repository
Environment: a dedicated worktree when available
```

## Master Implementation Prompt

Paste the following prompt into a new Codex task rooted at this repository:

```text
Comprehensively implement the Personal Hub application described in
docs/PERSONAL_HUB_IMPLEMENTATION_PLAN.md.

This is an end-to-end implementation objective, not a request to produce another
plan or stop after scaffolding. Continue working through the implementation phases,
tests, documentation, packaging, and verification until every required completion
criterion in the plan is genuinely satisfied or a concrete external blocker remains.

First, read these files completely:

- AGENTS.md
- docs/PERSONAL_HUB_IMPLEMENTATION_PLAN.md
- docs/PERSONAL_HUB_AGENT_HANDOFF.md
- pyproject.toml
- the existing architecture, security, permissions, packaging, calendar, dashboard,
  integrations, scheduling, notifications, and storage documentation relevant to
  the implementation

Inspect the repository and current git status before editing. Preserve all existing
user changes. Treat the Personal Hub plan as the authoritative product and technical
specification. Do not silently reduce its scope.

Primary objective:

Build Personal Hub as a separately packaged Windows desktop application in its standalone
repository. It must provide the Today, Tasks, Calendar, Watchlists, Inbox, Sources,
and Settings experiences specified by the plan. It must support the required local
task management, calendar connections, RSS/news feeds, deal/restock/page watches,
bounded message summaries, in-app attention notices, notifications, digests, source health, privacy controls,
offline behavior, backups, diagnostics, packaging, and release documentation.

Non-negotiable boundaries:

1. Leave the existing Local Assistant behavior and user experience unchanged.
2. Keep assistant.app:main as the existing assistant entry point.
3. Keep Personal Hub in its own personal_hub package, composition root,
   entry point, executable, installer identity, application ID, data directory,
   database, secret-vault namespace, logs, cache, settings, mutex, startup entry,
   and update channel.
4. Personal Hub must not read, migrate, or modify Local Assistant data automatically.
5. Both applications must be able to run and be installed simultaneously.
6. Personal Hub source must never import the assistant package.
7. Personal Hub UI must use only Personal Hub controllers and contracts.
8. Do not load or package the assistant router, planner, model, STT, TTS, voice,
   transcript, orchestration, or general computer-control stack in Personal Hub.
9. Personal Hub must have no conversational prompt box, command surface, microphone,
   or model requirement.
10. External integrations begin with the exact read-only boundaries specified by
    the plan. Do not add sending, replying, posting, purchasing, mailbox mutation,
    calendar writes, scripts, arbitrary webhooks, or general external execution.
11. External content is untrusted. Preserve schema validation, minimum scopes,
    sanitization, SSRF protection, bounded responses, safe logging, retention,
    privacy masking, and explicit source identity.
12. Core functionality must remain useful with no AI provider configured.

Execution process:

- Establish a detailed working plan mapped directly to implementation Phases 0-7
  and their acceptance gates.
- If persistent goal tracking is available, create a goal for completing this
  objective.
- Work phase by phase, but continue automatically after each completed phase.
- At each phase boundary:
  - implement the complete vertical slice;
  - add or update production regression tests;
  - run focused tests;
  - fix failures before proceeding;
  - verify the phase's acceptance gate with concrete evidence;
  - update implementation status in the plan or a dedicated status document.
- Do not mark placeholders, disabled controls, stubs, TODOs, mocked production
  behavior, or documentation-only claims as completed features.
- Make reasonable decisions using the recommended defaults in the plan.
- Ask for user direction only when a choice would materially change the product,
  new external authority is required, or progress depends on credentials or
  infrastructure that cannot be replaced with deterministic fakes.
- Missing live provider credentials are not a reason to stop other work. Complete
  provider contracts, fake-server integration tests, OAuth boundaries, UI states,
  and documented manual acceptance procedures.
- Diagnose and fix test failures rather than merely reporting them.
- Preserve narrow module boundaries and typed interfaces. Keep provider clients behind
  Personal Hub-owned adapters and declare every runtime dependency in this repository.
- If shared code is ever required, publish a neutral, independently versioned package;
  neither application may import the other.
- Avoid broad unrelated refactors.

Delegation:

When multi-agent implementation tools are available, use gpt-5.6-luna with max
reasoning for bounded, non-overlapping implementation tasks, as required by
AGENTS.md. Suitable delegated tasks include isolated repositories, provider
adapters, UI components, test suites, documentation, or packaging work.

The main gpt-5.6-sol agent remains responsible for:

- interpreting the authoritative plan;
- architecture and security decisions;
- assigning non-overlapping work;
- reviewing every delegated change;
- resolving integration conflicts;
- running cross-component and full-suite verification;
- ensuring the existing assistant remains unchanged;
- determining whether the overall objective is actually complete.

Required verification:

Run focused tests while iterating with the supported Personal Hub lanes. The default lane
is intentionally limited to non-UI, non-release-slow Hub tests; select a phase or focused
lane when the change crosses that boundary:

- `powershell -ExecutionPolicy Bypass -File scripts/test_personal_hub.ps1`
- `powershell -ExecutionPolicy Bypass -File scripts/test_personal_hub.ps1 -Lane phaseN`
- `powershell -ExecutionPolicy Bypass -File scripts/test_personal_hub.ps1 -Lane ui`
- `powershell -ExecutionPolicy Bypass -File scripts/test_personal_hub.ps1 -Lane integration`
- `powershell -ExecutionPolicy Bypass -File scripts/test_personal_hub.ps1 -Lane architecture`

Before completion, run the complete Hub lane and all other applicable checks, including:

- python -m pytest
- python -m ruff format --check .
- python -m ruff check .
- python scripts/check_documentation_consistency.py
- architecture-boundary tests
- Personal Hub unit, contract, integration, UI, security, and reliability tests
- standalone editable-install and entry-point smoke tests
- database migration, backup, restore, and isolation tests
- Personal Hub and Local Assistant coexistence tests
- packaged dependency/import checks proving assistant-only stacks are excluded
- installer/build verification when the required local build tools are available
- git diff --check

Exercise the acceptance workflows in the implementation plan. Where a workflow
requires an unavailable live account, execute the deterministic equivalent and
record the exact remaining manual verification.

Phase 8:

Complete every required item through Phase 7. Treat Phase 8 according to the plan:
optional intelligence must not begin until its prerequisites are satisfied and the
non-AI product is stable. Do not add AI merely to claim the phase is complete. If
the prerequisites cannot genuinely be established in this implementation run,
leave Phase 8 explicitly deferred with its reason and acceptance prerequisites.
This is an intentional product decision, not incomplete 1.0 core work.

Documentation and release evidence:

- Keep docs/PERSONAL_HUB_IMPLEMENTATION_PLAN.md authoritative.
- Record phase status and acceptance evidence.
- Create or complete the Personal Hub user guide, privacy documentation, and release
  runbook required by the plan.
- Document setup for each supported provider without exposing credentials.
- Ensure feature catalogs describe only implemented behavior.
- Document retention, deletion, offline, notification, and security behavior.
- Record commands run, test outcomes, packaging results, remaining manual checks,
  and any intentionally deferred optional work.

Completion standard:

Do not stop because the code compiles, the initial window opens, one phase passes,
or the remaining work is extensive. The objective is complete only when:

- all required Phases 0-7 and their acceptance gates are implemented and evidenced;
- Personal Hub is a useful standalone application rather than a shell;
- the Today, Tasks, Calendar, Watchlists, Inbox, Sources, and Settings workflows are
  functional;
- the required connectors and deterministic test substitutes are complete;
- privacy, security, offline, retention, reliability, and failure states work;
- Personal Hub has an independent executable and packaging path;
- Local Assistant remains behaviorally unchanged and its regression suite passes;
- both products coexist without data, credential, process, installer, or update
  collisions;
- documentation matches actual behavior;
- no required safe in-scope work remains.

At final handoff, provide:

1. A concise implementation summary organized by completed phase.
2. Links to the primary application, architecture, test, packaging, and documentation
   files.
3. Exact validation commands and results.
4. Packaging and coexistence results.
5. Any manual provider checks that still require user credentials.
6. Any optional Phase 8 work intentionally deferred and why.
7. A clear statement of whether every required release criterion is satisfied.

Begin by inspecting the current repository state and reading the authoritative files.
Then implement Phase 0 and continue through the remaining required phases without
waiting for another prompt.
```

## Required Implementation Discipline

The prompt is intentionally persistent, but it does not broaden authority beyond the
repository and implementation plan.

- Preserve unrelated user changes.
- Do not use destructive Git commands.
- Do not commit secrets, provider fixtures copied from real accounts, local databases,
  model files, installers, or machine-specific configuration.
- Do not claim live-provider verification when only fake-provider tests ran.
- Do not weaken permissions or network validation to make a test pass.
- Do not convert optional Phase 8 work into a 1.0 dependency.
- Do not mark the goal complete merely because a turn or context window is ending.

## Expected Progress Artifacts

The implementation agent should add or maintain:

- a phase status and acceptance-evidence record;
- focused regression tests with each feature;
- `docs/PERSONAL_HUB_USER_GUIDE.md`;
- `docs/PERSONAL_HUB_PRIVACY.md`;
- `docs/PERSONAL_HUB_RELEASE_RUNBOOK.md`;
- build and packaging configuration;
- manual provider acceptance steps where real credentials are required;
- a final list of validation commands and outcomes.

The agent should keep future-feature prose out of implemented feature catalogs until the
corresponding code and tests are complete.

## Handoff Completion Checklist

Before moving implementation to another task or device, confirm:

- [ ] All changes are committed on the intended branch.
- [ ] The branch is pushed to the expected remote.
- [ ] No secrets, databases, caches, build outputs, or machine paths are committed.
- [ ] `git status --short` is clean or every remaining item is documented.
- [ ] The latest phase and acceptance evidence are recorded.
- [ ] Full and focused test commands and outcomes are recorded.
- [ ] Known live-provider checks are separated from automated test results.
- [ ] The next task can identify the exact first incomplete acceptance gate.
- [ ] Local Assistant regressions remain green.
- [ ] Personal Hub and Local Assistant isolation remains intact.

## Initial Implementation Boundary

The first implementation task should complete Phase 0 only before broad feature work:

1. Keep the `src/personal_hub/` package self-contained.
2. Keep Hub-only configuration, bootstrap, entry point, data path, and database.
3. Maintain architecture and identity/coexistence contract tests without importing Local
   Assistant.
4. Prove that no `assistant` module loads or is packaged.
5. Run clean-VM coexistence verification as an external release gate.

After Phase 0 passes its acceptance gate, the same primary task should continue through the
remaining required phases without waiting for another prompt.
