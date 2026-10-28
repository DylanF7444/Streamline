# Personal Hub implementation status

Updated October 28, 2026. The current implementation baseline is complete for now, and
feature updates can continue without treating release evidence as a blocker. External release
checks remain pending and should be revisited when release readiness is the focus. The
authoritative product and technical specification is
`PERSONAL_HUB_IMPLEMENTATION_PLAN.md`. This record separates implemented, deterministic
acceptance evidence from release evidence that needs real provider accounts, private signing
material, assistive-technology users, a clean Windows VM, or elapsed wall-clock time.

## Tracking and Logging — October 28, 2026

Implemented the first-release scope in `PERSONAL_HUB_PROGRESS_AND_ACTIVITY_PLAN.md`.
Tracking and Logging have persistent mode navigation and separate remembered pages. Logging
provides Activity capture/history, local Reflection draft autosave and search, and factual
Summary breakdowns. Schema 14 preserves completion snapshots independently of task cleanup,
handles undo/recompletion and recurring identities, and migrates available completed tasks.
Manual progress never changes its linked task. Backup, export, restore, privacy masking,
session lock, explicit history deletion preview, and an optional count-only Home widget are
integrated. Activity follows enabled encrypted shared-folder sync; Reflection requires its
own device-local opt-in. Both stay outside provider APIs and the collector relay.

Implementation details, bounds, and acceptance coverage are recorded in
`PERSONAL_HUB_LOGGING_IMPLEMENTATION.md`. Visual review covered Activity, Reflection, Summary,
and a narrow writing view. Focused core, desktop, persistence/sync, and corrected dashboard
tests pass. Five pre-existing dashboard assertions were updated from obsolete size presets
to the current version-3 geometry, including real resize and restart-persistence checks.
Import isolation and accelerated seven-day reliability pass. The one-file executable and
Inno installer compile; package inspection finds no forbidden dependency. These are unsigned
development artifacts, with existing public-release gates still deferred.

Final validation for this extension:

| Check | Result |
| --- | --- |
| Complete collected test inventory | **339 passed**: 252 core tests and 87 UI tests, executed in 20 fresh-process batches. The Logging batch was rerun successfully after the final keyboard-focus correction. |
| Focused Logging/task/sync/desktop regression command | **36 passed**; subsequent final Logging UI rerun: **8 passed**. |
| Dashboard UI batches | **10 passed**, including geometry migration, direct resize, content refresh, and restart persistence. |
| Ruff format and lint | Passed for all 167 Python files. |
| Documentation consistency and Git whitespace | Passed. |
| Startup/import boundary | Passed: no Assistant imports and no Qt before explicit desktop startup. |
| Accelerated reliability | Passed: 168 virtual hours, six restarts, bounded state. This does not satisfy a 24-hour wall-clock soak. |
| Final executable and installer | PyInstaller build and Inno Setup 6.7.3 compilation passed; unsigned development artifacts only. |
| Packaged dependencies | Passed: 2,328 archive entries and 2,477 analysis entries; no forbidden dependencies. Logging contracts, persistence, UI, and coordinator modules are present. |

The monolithic Windows Qt runs were superseded by complete fresh-process batches because
the long-lived Qt process slows substantially. Core used `python -m pytest --strict-markers
tests --ignore=tests/ui -q`; every `tests/ui/test_*.py` file ran with the same strict-marker
options in a fresh process. The collected total was checked against 339, with no tests
omitted. Per-batch results and the corrected rerun are preserved locally in the ignored
`.benchmark-data/logging-test-batches/summary.json`. No live provider account or installed
user data was used. Signing, clean-VM lifecycle/coexistence, manual assistive-technology
review, and the actual wall-clock soak remain the existing release gates.

## Direct dashboard positioning and resizing — October 16, 2026

Home widgets now use persisted grid rectangles instead of move arrows and size presets.
In **Edit layout**, users can drag a heading to place a widget and resize from any edge or
corner while the multi-column grid is available. Keyboard users can focus a heading or resize
handle and use the arrow keys. Narrow windows stack widgets at full width, with vertical
resizing retained; the wide-window layout returns when the window grows. Saved version 1
dashboard layouts are migrated to the new geometry model on load. Layout preferences still
contain widget IDs and geometry only, not provider content.

Ruff formatting, Ruff lint for the changed Python modules, and `git diff --check` passed.
Automated tests and manual screen-reader review were not run for this change.

## Workspace navigation customization — October 16, 2026

Sources and Settings now sit in a fixed Management section. Workspace pages can be renamed,
reordered, and hidden; local task lists appear as shortcuts to filtered Tasks pages. The main
Calendar includes all available calendars by default. Users can add, edit, and remove named
calendar views that include selected calendars, and those views do not change the main calendar
selection. Tasks includes an explicit empty list creation action. Workspace navigation and
calendar view choices persist in Hub settings. Privacy masking replaces collection names in the
navigation and disables customization while content is masked or the Hub is locked. The focused
calendar and application test files completed with 23 passed; a window construction check passed
after a navigation initialization fix. A direct smoke check confirmed that a named view filters
to its chosen calendar and the main Calendar still shows all selected calendars. Ruff and syntax
checks passed; manual UI review was not run. The earlier full test lane reported 312 passed and
6 failed (five dashboard layout expectation failures and the now-fixed navigation initialization
failure); that full lane was not rerun after the fix.

| Phase | Status | Evidence |
| --- | --- | --- |
| 0. Boundary proof and application skeleton | Complete | Independent package, entry point, data root, database, process/mutex/startup identities, Qt shell, coexistence tests, and measured import boundary. |
| 1. Hub storage, shell, Inbox, and lifecycle | Complete | Schema migration, repositories, Hub-only encrypted vault, tray/lifecycle/offline/privacy behavior, Inbox/source/settings controls, diagnostics, backup/restore, and credential-free export. |
| 2. Native tasks and Today | Complete | Visual CRUD, recurrence, reminders, deterministic digest, timezone-aware Today projection, restart deduplication, and 10,000-task responsiveness coverage. |
| 3. Calendar | Complete at deterministic source gate | Read-only Google and Outlook adapters, PKCE/vault isolation, bounded multi-account refresh, cached states, Month/Day/Week/Agenda views with an hourly Week grid, and Today projection. Live-account acceptance remains a release check. |
| 4. News feeds | Complete | Secure RSS/Atom fetch, parsing, conditional requests, filters, retention, read state, deep links, digest grouping, source isolation, and UI authoring. |
| 5. Deal, restock, and webpage watches | Complete | Typed watches, secure collection, structured/text/selector extraction, baseline/change/cooldown logic, changedetection.io read mapping, Inbox/Today projection, and UI authoring/test-now. |
| 6. Attention messages | Complete at deterministic source gate | Read-only Gmail, Outlook, and Slack adapters; selected-query/folder/channel bounds; retained summaries; explicit memory-only body fetch; sanitization; masking; Inbox/Today UI. Live-account acceptance remains a release check. |
| 7. Packaging and release hardening | Implemented; release approval pending external evidence | One-file build, isolated package verifier, separate installer, install/upgrade/uninstall verifier, signed-update boundary, SBOM/provenance tooling, accessibility checks, and reliability harness are implemented. See the release-gate table below. |
| 8. Optional intelligence | Deferred | Deliberately excluded from non-AI 1.0 until stable daily-use evidence exists. |

## Calendar authoring and optional Google sync — October 9, 2026

Calendar now provides **Add item**, local editing/removal, timed and all-day scheduling,
timezone validation, location, and notes. Local items are stored with durable calendar data
in **My calendar**, remain usable offline, and participate in Home, calendar selection,
backup/restore, export, and encrypted shared-folder synchronization.

Google event syncing requires a separate account opt-in, reauthorization for
`calendar.events`, per-item selection, and an explicit **Sync Google** action. Stable event
IDs make uncertain insert retries safe. Linked Google changes update local records;
version preconditions and explicit conflict resolution prevent silent overwrites. Remote
removal retains a local copy without recreating it; local removal and stopping sync retain
the provider copy. Bindings and credentials stay device-local. Outlook and unlinked provider
events remain read-only.

The 28 core calendar-item tests and five desktop authoring tests pass with synthetic data.
They cover restart, backup/restore, export, shared-folder authorization isolation, source
selection/deduplication, validation, offline/disabled gating, new OAuth grants, bounded
write transport/redirect rejection, round trips, uncertain inserts, version races, ownership,
conflicts/resolution, and local/remote removal. The desktop tests exercise actual add/edit/remove
controls and reject ambiguous or nonexistent daylight-saving times. Import-boundary,
documentation, Ruff, and whitespace checks pass. Real Google account acceptance remains
the manual extension check in the release runbook.

The complete regression run passed 311 tests in 31 minutes 30 seconds, with one existing Qt
deprecation warning. The final focused checks passed all 28 calendar-item tests and 17
related desktop checks on the final implementation. Tests use this checkout's `src`
directory explicitly so an older editable installation cannot shadow the code under test.

## Home dashboard and mini calendar — September 30, 2026

The default Today page is now labeled **Home** in navigation, its heading, status, and tray
action. Existing Today navigation links and saved dashboard preference keys remain compatible.
Home includes a separate **Mini calendar** month grid alongside the **Agenda** event list.
It supports month browsing, returning to today, activity dots for selected-calendar events
and due tasks, and opening a selected date in Calendar's day view by mouse or keyboard.
Markers use Calendar's timezone and multi-day placement rules without displaying event titles.
Freshness and offline states are visible; widget visibility, order, and size use existing
dashboard persistence.

The five focused Home/mini-calendar tests pass with synthetic offline data. They cover legacy
links and tray navigation, timezone and multi-day markers, source selection, masking, offline
status, year boundaries, refresh stability, keyboard activation, and minimum-window layout
at 100% and 175% text scale. Visual inspection covers normal and high-contrast rendering.
Fourteen related navigation, dashboard persistence/refresh, and accessibility checks also
pass on the final source. Ruff formatting/lint, documentation consistency, and Git whitespace
checks pass. Tests explicitly used this repository's `src` directory on `PYTHONPATH`.
An additional complete-suite run was interrupted during unusually slow Windows Qt tests;
no failures were reported before interruption. This is not a completed full-suite gate.

## Encrypted cross-device synchronization — September 20, 2026

Personal Hub now supports optional manual synchronization through a user-selected shared
folder. Each device writes an independently encrypted shard under `.personal-hub-sync`, using
a passphrase-derived Scrypt key and AES-256-GCM. The passphrase is required for each explicit
sync and is never stored. A shared folder can be a local directory, removable drive, SMB share,
or user-managed cloud folder; Personal Hub does not provide a hosted sync account or inbound
network service.

The synchronized boundary is deliberately narrow: local task lists and tasks, explicitly
imported calendar items, feed subscriptions, watch definitions, and allowlisted shared
preferences/view state. Credentials and vault data, provider connections and caches, message
content, feed entries/read state, watch observations/alerts, Inbox history, and private
iCalendar URLs remain device-local. Deterministic UTC last-write-wins merging includes
tombstones for deletion propagation, conflict counting, profile pairing, bounded payloads, and
safe failure for wrong passphrases, tampering, malformed shards, and foreign profiles.

The Settings page exposes shared-folder selection, passphrase entry, manual **Sync now**, status,
and disable controls. Focused deterministic coverage verifies two-device round trips, settings
pairing, conflict tie-breaking, deletion propagation, encrypted shard contents, and rejection
of wrong passphrases or mismatched profiles. No provider or cloud account was contacted by these
tests.

## Optional provider collector relay — October 22, 2026

Settings now supports three device roles: local-only (default), provider collector, and offline
consumer. The collector publishes one separate encrypted relay shard alongside local-state sync.
The allowlist covers bounded calendar snapshots, selected message summaries, retained feed
entries, and bounded watch observations and alerts. A consumer receives those records into the
existing Hub display repositories and caches the calendar snapshot locally for up to 30 days.
Message bodies, provider credentials and connection metadata, private iCalendar URLs, and Google
write bindings are excluded. Consumer mode forces provider networking off, including explicit
Google event sync, and keeps the last received content available when the collector is offline.

The collector is not an always-on server: it must be online only when it refreshes providers and
publishes the encrypted shard. Shared-folder reachability and a cloud folder's own sync client are
required to deliver updates. Source implementation and documentation are complete. Automated
regression and two-device release acceptance remain unrun for this extension.

## Local CSV and text-list imports — September 14, 2026

Personal Hub now accepts bounded UTF-8 CSV files or pasted text lists for both local tasks
and local calendar items. The Tasks page provides **Import** beside Quick add; Calendar exposes
**Import CSV or list** under the collapsed **Calendars** panel. The dialog includes calendar
defaults for unprefixed text rows and can save the task or calendar CSV header template.

Task rows use deterministic identities so an explicit `id` updates the same local task while
preserving its completion/archive state. Calendar rows are stored as named, selectable local
calendar batches with safe HTTP(S) links, replace-on-repeat behavior, and an explicit Remove
action. They remain read-only and never write to connected provider calendars. Invalid rows
are reported without discarding valid rows; limits are 2 MB and 5,000 rows.

The database schema is now version 12 with dedicated local calendar import and event tables plus
durable provider-task deletion markers.
Focused import parser, persistence, controller, and UI coverage is included in the test suite;
the import templates and field guidance live in `docs/PERSONAL_HUB_IMPORT_TEMPLATE.md`.

## Canvas Student iCalendar source — September 9, 2026

Canvas Student is now a configurable external platform source. Its private HTTPS iCalendar
URL is stored only in the Hub vault; ordinary settings retain the non-secret origin. A bounded
refresh parses every concrete `VEVENT` once and idempotently creates or updates a local task
whose due date is the event start. Imported tasks retain completion/archive state across
refresh and appear in Calendar through the existing due-task projection. Canvas disconnect
removes the private URL and leaves the local task records intact.
Canvas iCalendar events publish a calendar URL, so assignment events are converted to their
same-origin native `/courses/{course}/assignments/{id}` URL using the assignment fragment and
course context that Canvas includes. The safe link is retained on the local task and exposed as
**Open in Canvas** in Tasks and Calendar; items without an assignment identity have no
native-open action.
The opt-in setting is now **Remove completed tasks after due date**. Local maintenance removes
completed, past-due tasks from Personal Hub at startup and during its minute schedule, including
while offline. All-day tasks are eligible after their due date passes in the task's timezone;
open, future-due, undated, and archived tasks remain. The first completed task in an active
recurring series remains as its schedule anchor. Canvas imports retain a durable local
deletion marker so later feed refreshes do not recreate removed tasks. Canvas itself is never
modified. The existing `canvas_remove_completed_assignments` preference migrates to the broader
setting.

Deterministic evidence for this slice:

- focused Canvas/platform/iCalendar and related task/calendar tests: 36 passed;
- complete UI lane: 58 passed;
- fast non-UI lane: 177 passed, 10 deselected;
- Ruff lint, changed-surface format checks, documentation consistency, and import isolation:
  passed.

The historical test counts above cover the original Canvas-specific behavior; the broadened
cleanup has not been covered by new automated tests in this update. No live Canvas feed was
contacted. A rotated non-production feed remains a release acceptance check.

## Standalone repository extraction — September 8, 2026

Personal Hub source, tests, project metadata, CI, documentation, packaging, installer, and
release tooling were moved under one repository-ready root. Local Assistant no longer
declares the Hub entry point, test markers, CI job, source package, tests, documentation, or
release files. The two production packages have no imports in either direction.

Post-extraction evidence:

- standalone editable install and bootstrap passed in a fresh virtual environment where
  `importlib.util.find_spec("assistant")` returned `None`;
- the import probe passed with no Assistant modules and no Qt modules before desktop launch;
- architecture lane: 9 passed, 232 deselected;
- release lane: 13 passed, 228 deselected;
- complete standalone Hub lane: 241 passed in 933.94 seconds;
- Ruff format and lint passed for all 131 standalone files;
- documentation/repository-path consistency passed;
- `PersonalHub.exe` rebuilt from the standalone tree, and both archive (2,300 entries) and
  analysis (2,448 entries) contained every required prefix and no forbidden entry;
- Local Assistant regression suite after removal of Hub wiring: 1,297 passed in 321.23
  seconds.

These results prove source-level repository migration. They do not replace the outstanding
standalone Inno Setup compilation (the compiler was unavailable), signed-artifact,
live-provider, clean-VM, 24-hour soak, or manual accessibility gates below.

## Desktop customization — September 7, 2026

The Hub shell now uses a shared calm theme across its native pages. Settings provides
navy, light, and sage presets, validated custom accent/background/surface colors, and an
animation toggle. High contrast overrides the custom palette. Page fades and dashboard
geometry transitions stop immediately when reduced motion is enabled.

Today is a responsive widget board with Calendar, tasks, durable Inbox, Watchlist status,
attention messages, news, and watch alerts. Add/remove, keyboard access, a visible drag
handle, direct placement, edge/corner resizing, and a theme-aware panel boundary are
implemented. Layout version 3 uses 48 horizontal units and 6-pixel vertical increments;
version 1 and 2 layouts migrate while preserving their prior geometry. Layout configuration
contains only allowlisted widget IDs, order, hidden state, and geometry. Existing section
order/visibility preferences are read when no dashboard layout has been saved.

The October 16, 2026 drag, grid-granularity, and panel-boundary follow-ups have not been run
through the Qt UI or test suite.

Verification includes offline settings reopen, real Qt mouse-event drag tests, refresh and
restart persistence, privacy masking after layout changes, reduced-motion cancellation,
minimum-size/large-text checks, and synthetic offscreen screenshot review. Windows offscreen
tests load an installed system font when Qt has no font families, so layout checks measure
actual text. This UI change does not satisfy or replace the outstanding signed-release,
live-provider, installer, or long-duration reliability evidence below.

Validation for this UI slice:

- `python -m pytest --ignore=tests/hub/ui --ignore=tests/ui -q`: 1,404 passed.
- `python -m pytest learning_tests -q`: 29 passed.
- `python -m pytest tests/hub/ui -q`: 51 passed in the broad UI run.
- Final focused run of `test_dashboard_widgets.py`, `test_dashboard_integration.py`,
  `test_phase7_accessibility.py`, `test_phase2_tasks_ui.py`, `test_window.py`, and
  `test_appearance_controls.py`: 24 passed. The six dashboard tests passed again after
  refining keyboard moves to skip hidden widgets.
- `python -m ruff check .`, changed-file formatting checks, documentation consistency,
  Hub import isolation, and `git diff --check`: passed.
- Repository-wide `ruff format --check .` still reports 20 pre-existing files outside
  this UI slice; those unrelated files were not reformatted.

Screenshot review used synthetic local content in navy/light themes, edit mode, and
720×460 at 1.75× text scale. No live account, new installer, or release artifact was used.

## UX density cleanup — September 9, 2026

The Calendar, Tasks, and Inbox surfaces now use progressive disclosure and persistent
master-detail layouts to reduce simultaneous controls without changing Hub data contracts:

- Calendar source selection and iCalendar management are collapsed behind **Calendars**;
  selected event details render in a right-side inspector beside the active view.
- Tasks expose a compact quick-view/search toolbar, disclose advanced state/due/sort filters,
  keep the editor beside the task list, and move secondary row actions into an accessible
  overflow menu.
- Inbox separates durable Hub alerts from attention messages with **Alerts** and **Messages**
  tabs. Message summaries and their explicit memory-only body use a master-detail splitter,
  while category counts and structured two-line rows improve scanning.
- Page headers and task/message/inbox rows use a lighter hierarchy with fewer heavy card borders;
  existing high-contrast, privacy, keyboard, and accessible-name paths remain covered.

Deterministic UI evidence for this slice:

- Calendar, Tasks, message, and category UI tests: 17 passed.
- Accessibility, window, appearance, Today/dashboard UI tests: 22 passed.
- Ruff checks and changed-file formatting: passed.

## Isolation contract

Personal Hub uses `personal_hub.app:main`, `%LOCALAPPDATA%\PersonalHub`,
`personal_hub.db`, `PersonalHub.exe`, `PersonalHub.Desktop`, the `PersonalHub` startup and
credential namespaces, and the `personal-hub-stable` update channel. It does not import or
package the Local Assistant router, planner, language models, voice stack, transcript state,
command surface, or Assistant UI. Local Assistant production modules and entry point were not
repurposed.

## Acceptance evidence by phase

### Phases 0–1

- Startup import measurement rejects all Assistant and pre-desktop Qt modules.
- Hub configuration rejects any data/database overlap with Local Assistant.
- The database migrates idempotently through Hub schema version 12. The migration retires and
  clears legacy calendar-event completion rows; task completion remains in the task table.
- Offline mode is checked before adapter, DNS, or token work in deterministic tests.
- Source failures become fixed safe states and durable Inbox records without blocking Qt.
- DPAPI is used for production secrets under the current Windows user and the distinct
  `PersonalHub` namespace; tests inject an offline cipher.
- SQLite online backup and integrity-checked atomic restore have regression coverage.
- The UI exposes Inbox transitions; validated settings; refresh/reconnect/enable/disable/
  disconnect for registered sources; database backup/restore; and bounded JSON export.
- Settings and Watchlists use minimum-size-aware scroll surfaces so supported text scaling does
  not compress their inputs. Global/startup/scheduled refreshes use one background event loop and
  coalesce repeat requests instead of launching five short-lived workers.
- Canvas Student, Google, Microsoft, and Slack are registered as configurable Sources. Canvas
  stores its bearer-like iCalendar URL only in the Hub vault and imports each bounded concrete
  event as a stable due-dated local task. Repeated refreshes preserve completion and avoid
  duplicates; those tasks appear through the existing Calendar projection. The Hub-owned setup
  stores provider credentials in the Hub vault and composes read-only calendar and message
  adapters with bounded provider transports. Standalone iCalendar feed imports are separate from
  platform connections and expose only read-only event data.
- Daily offline-capable maintenance applies every retention class and records metadata-only
  cleanup evidence while preserving one watch comparison baseline.

### Phase 2

- Tasks support add, edit, complete, undo, archive, filters, lists, tags, priorities,
  recurrence, and reminders without a command surface.
- Series and occurrence identities create exactly one next recurrence across repeat calls,
  concurrency, restart, and database reopen.
- Reminder claims and digest identities are durable and deterministic.
- Due task reminders and immediate watch alerts reach the tray through restart-safe delivery
  claims; privacy masking and quiet-hour suppression are enforced before native display.
- Timed and all-day Today behavior is covered across IANA timezones and DST folds.
- A projection with 10,000 completed tasks remains bounded by the open-task query.

### Phase 3

- Provider-neutral contracts distinguish ready, empty, stale, disconnected, offline,
  permission-required, and error states.
- Google Calendar requests only
  `https://www.googleapis.com/auth/calendar.readonly`; Outlook requests only
  `Calendars.Read`.
- Both adapters use injected GET-only transports, bounded windows/pages/items, selected
  calendars, cancellation, and safe error normalization.
- OAuth authorization uses PKCE and short-lived in-memory state/verifier values; stored
  tokens use the Hub vault and disconnect clears its Hub-scoped secret.
- Month, day, week, and agenda views, account-scoped selection, privacy masking, deep links, and
  Today projection have UI coverage.
- Provider events expose no completion control. Successful Google refreshes replace the
  in-memory snapshot and omit cancelled/deleted resources, while stale, offline, and
  permission/error results retain the last successful snapshot. Due-dated local to-do items
  appear in every Calendar view and can complete or reopen their underlying task.
- Day and Week provide a bounded hour-by-hour grid. Timed items are clipped across visible
  days, overlapping items receive deterministic columns, and selecting an hour opens a local
  scheduled-task editor with the due date, time, and default timezone prefilled.

### Phase 4

- RSS/Atom URLs, DNS results, redirects, response sizes, timeouts, XML, and link schemes are
  bounded or rejected.
- Stable identities prevent duplicates after restart and feed reordering.
- ETag/Last-Modified, per-origin backoff, independent failures, read state, retention, and
  per-subscription limits are durable.
- Feed authoring, filters, selection, read/unread, deep links, Watchlists, Today, and digest
  grouping have UI or controller coverage.

### Phase 5

- First observations establish baselines and do not alert.
- Identical observations do not duplicate alerts; typed state changes produce one
  explainable alert subject to cooldown and durable deduplication.
- Price threshold, percentage decrease, restock, text, selector, and structured-product
  extraction paths are covered.
- Fetchers reject private/local targets and unsafe redirects, bound bytes/time, and never
  execute scripts or submit forms.
- Watch authoring, edit/delete/enable, test-now, refresh, safe links, Today, and Inbox paths
  have regression coverage.

### Phase 6

- Gmail requests only `https://www.googleapis.com/auth/gmail.readonly`; Outlook requests
  only `Mail.Read`; Slack permits only selected combinations of `channels:history`,
  `groups:history`, and `search:read`.
- Only configured folders, channels, and saved queries are read, through GET-only injected
  transports. No send, reply, reaction, label, archive, delete, upload, or attachment API is
  exposed.
- Summary listing does not fetch message bodies. Body fetch happens only after explicit open;
  HTML becomes bounded plain text plus inert HTTP(S) links, with scripts/forms/remote content
  removed.
- Bodies are memory-only and cleared on lock, source clear, shutdown, and process exit.
- Summary retention is bounded from session-only (0 days) through 30 days and by per-source
  item count.
- Gmail-style categories use provider labels when available and conservative local heuristics
  otherwise. Primary is the default view; Promotions, Social, Updates, and Spam are local
  filters, and Spam is excluded from Today and the count-only startup attention notice.
- Unread selected messages surface through a session-only count notice at the top of the Hub
  window; message attention notices do not create durable Inbox rows.

## Phase 7 package and reliability evidence

The September 1 local Windows evidence is recorded in
`PERSONAL_HUB_RELEASE_EVIDENCE.md` and was produced from this dirty planning worktree. It is
development evidence, not a publishable signed release.

| Gate | Result |
| --- | --- |
| One-file `PersonalHub.exe` build | Passed; 80,294,662-byte executable built with PyInstaller 6.14.1. |
| Packaged dependency inspection | Passed; 2,291 archive entries and 2,440 analysis entries, with no forbidden Assistant/model/audio/browser/wake-word/test/development stack. |
| Separate Inno installer compile | Passed with Inno Setup 6.7.3. |
| Local clean install and same-version upgrade | Passed in a unique temporary per-user directory. |
| Preserve-data and delete-data uninstall choices | Passed. |
| Hub startup-entry cleanup | Passed. |
| Local Assistant executable/database/startup invariance | Passed for the locally present artifacts supplied to the verifier. |
| Signed update verification, tamper rejection, downgrade rejection, apply, and rollback | Passed with ephemeral test keys and isolated files. A real release key is still required. |
| CycloneDX SBOM, checksums, release manifest, and SLSA/in-toto provenance | Generated for the local unsigned candidate. |
| Accelerated seven-day bounded-state test | Passed for 168 virtual hours with six restarts and bounded feed/watch/message state. |
| Actual 24-hour tray soak | Not yet satisfied; accelerated time explicitly does not qualify. |
| Authenticode and detached release/update signatures | Not yet satisfied; no release certificate or private manifest key was supplied. |
| Clean Windows VM lifecycle and independent update coexistence | Local lifecycle passed; clean-VM and hosted independent-update evidence remain required. |
| Live Canvas/Google/Microsoft/Slack provider acceptance | Not yet satisfied; requires a rotated non-production Canvas feed, project-owned OAuth registrations, and test accounts. |
| Manual screen-reader/user accessibility review | Automated keyboard, minimum-size, scale, contrast, reduced-motion, and accessible-name checks pass; manual AT review remains required. |

## Validation commands

```powershell
python scripts/check_personal_hub_imports.py --output .benchmark-data/personal-hub-imports.json
powershell -ExecutionPolicy Bypass -File scripts/test_personal_hub.ps1 -Lane all
python scripts/run_personal_hub_reliability.py --mode accelerated --output .benchmark-data/personal-hub-seven-day.json
python -m PyInstaller --clean --noconfirm packaging/personal-hub.spec
python scripts/verify_personal_hub_package.py dist/PersonalHub.exe --analysis build/personal-hub/Analysis-00.toc --output .benchmark-data/personal-hub-package.json
& "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" packaging\personal-hub-installer.iss
powershell -ExecutionPolicy Bypass -File scripts/verify_personal_hub_installer.ps1 -Installer dist/personal-hub/PersonalHub-0.1.0-Setup.exe
python scripts/generate_personal_hub_release.py --artifact dist/PersonalHub.exe --artifact dist/personal-hub/PersonalHub-0.1.0-Setup.exe --output-dir dist/personal-hub/release
python scripts/check_documentation_consistency.py
python -m ruff format --check .
python -m ruff check .
git diff --check
```

Historical pre-extraction planning-branch outcomes:

```text
python -m pytest -q
1483 passed in 443.26s (0:07:23)

powershell -ExecutionPolicy Bypass -File scripts/test_personal_hub.ps1 -Lane all
189 passed in 229.93s

powershell -ExecutionPolicy Bypass -File scripts/test_personal_hub.ps1 -Lane fast
144 passed, 10 deselected in 20.56s

powershell -ExecutionPolicy Bypass -File scripts/test_personal_hub.ps1 -Lane architecture
8 passed, 181 deselected in 6.18s

powershell -ExecutionPolicy Bypass -File scripts/test_personal_hub.ps1 -Lane release
13 passed, 176 deselected in 19.48s

python -m pytest tests --ignore=tests/hub --ignore=tests/ui -q
1221 passed in 174.43s

python -m pytest tests/ui -q
73 passed in 33.46s

python -m pytest learning_tests -v
29 passed in 4.36s

python -m ruff format --check .
523 files already formatted

python -m ruff check .
All checks passed
```

Phase 8 remains intentionally deferred and must not be used to delay a non-AI 1.0 candidate
once every external release gate above is satisfied.

## Iteration test lanes

Personal Hub collection metadata is assigned by `tests/conftest.py`, so the usual
development loop does not need to execute the full UI and release suite:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/test_personal_hub.ps1                 # fast core lane
powershell -ExecutionPolicy Bypass -File scripts/test_personal_hub.ps1 -Lane phase2    # one implementation phase
powershell -ExecutionPolicy Bypass -File scripts/test_personal_hub.ps1 -Lane ui        # Qt/offscreen lane
powershell -ExecutionPolicy Bypass -File scripts/test_personal_hub.ps1 -Lane release   # packaging/release evidence tests
powershell -ExecutionPolicy Bypass -File scripts/test_personal_hub.ps1 -Lane all       # complete Hub gate
```

The fast lane ignores the Qt UI directory and excludes only the slower core checks marked
`hub_slow`; it still includes the Hub architecture, storage, provider, controller, and
security regressions. The complete `all` lane remains the required pre-release check.
