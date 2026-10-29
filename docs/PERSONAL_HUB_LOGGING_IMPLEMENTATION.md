# Tracking and Logging implementation

Implemented October 28, 2026 from `PERSONAL_HUB_PROGRESS_AND_ACTIVITY_PLAN.md`.

## Daily overview extension — October 29, 2026

Summary is now labeled **Daily overview** and is the initial Logging landing page. The
internal Summary route remains compatible with saved per-mode preferences. The overview
shows one selected local day: complete task/manual counts, manually entered duration,
completion title/list/tag snapshots and manual progress, plus an inline reflection editor.
Previous/next/date/Today navigation supports past-day review. Today follows the configured
timezone's date on refresh; choosing another date pins it. No screen-time tracking, new
provider calls, evening notification, schema change, or new dependency is added.

Activity details load 200 entries initially, with Load more up to 10,000 and explicit partial
list labeling. Counts and duration cover the full day, independently of the displayed list.
Undone completions are excluded. Local midnight boundaries use the existing timezone-aware
repository queries, including 23- and 25-hour daylight saving dates.

The inline editor reuses Reflection records and preserves task/Activity links and identity.
The user can choose among the day's reflections or start another. Opening the overview
does not create blank entries. Autosave, validation bounds, navigation/close/backup flush,
failed-save recovery, masking/lock, history deletion, and existing backup/export/sync policy
apply to daily drafts. Refresh preserves pending writing and the cursor. The full Reflection
page remains available for search, links, and deletion.

Regression coverage is in `tests/ui/test_logging_ui.py`: local-day/DST boundaries, complete
totals beyond the display limit, immutable completion snapshots, undo, date browsing,
existing/multiple reflections, cross-page edits, autosave, privacy, failed-save recovery,
and text bounds. Validation outcomes are recorded in `PERSONAL_HUB_IMPLEMENTATION_STATUS.md`.

## Delivered behavior

1. The persistent Tracking / Logging selector changes primary workspace pages. Tracking
   retains Home, Calendar, Tasks, Watchlists, Inbox, and collection shortcuts. Logging has
   Activity, Reflection, and Daily overview. Each mode remembers its page across restart; Management
   remains fixed. Linked tasks open the Tracking task editor.
2. Schema 14 adds independent Activity and Reflection tables without cascading task foreign
   keys. Migration snapshots existing completed tasks with available timestamps once.
   Previously deleted tasks cannot be reconstructed. Completion and its history write share
   one transaction. Repeated and concurrent completion calls produce one active record;
   recurrence occurrences have stable identities. Undo/reopen preserves an Undone record,
   and completing again creates another record, even at the same supplied timestamp.
3. Activity offers manual capture, linked-task capture from the task actions menu, edit/delete,
   optional notes and entered duration. Completion facts are immutable; their note can be
   edited. Deleting an Activity entry is independent of its task. Task edits, deletion,
   completed-task cleanup, and provider refresh do not erase or rewrite history.
4. Activity groups by the configured local timezone and supports Today, Last 7 days, custom
   range, all dates, literal text search, type, list, tag, and corrected-history filters.
   Queries show 200 entries initially with bounded Load more controls. Daily overview aggregates
   every matching record, independently of the display limit, and excludes undone facts.
   Duration is explicitly the sum of user-entered minutes, or shown as not tracked.
5. Reflection provides multiple entries per date, an optional title, plain-text paragraphs,
   optional skippable prompts, task and Activity references, search, edit, and delete. Drafts
   autosave after a short typing pause and flush before navigation, lock/mask, backup,
   export, sync, window close, and shutdown. A failed save keeps the draft and displays a
   safe unsaved state; navigation/close cannot silently discard it. The about-date remains
   separate from creation and modification timestamps. Writing never changes tasks.
6. Masking and locking hide Logging content and close capture dialogs. The Home **Logged
   today** widget is optional and count-only. All operations work offline without AI.
7. SQLite backups/restores include both tables. Explicit JSON exports include all Activity
   and Reflection records. History stays until explicit deletion; Settings offers a separate
   age-based preview and apply control. This is an explicit deletion action, not an automatic
   retention schedule. Completed-task cleanup preserves both record types.
8. Enabled encrypted shared-folder sync includes Activity. Reflection sync is off by default
   and requires a separate choice on each device; that permission never syncs as a preference.
   Opted-out devices neither receive nor republish reflections nor infer their deletion.
   Existing shared copies remain until removed on participating devices. Deletions propagate
   through tombstones; duplicate offline completions reconcile to one active fact while
   retaining corrections. Correction state survives stale offline note edits. Recurrence
   metadata travels with tasks so subsequent completion/undo keeps its original identity.
   The provider relay and provider request paths have no Logging collection.

## Bounds and boundaries

Descriptions and titles are at most 240 characters; notes and reflection bodies at most
20,000 characters. Tags use existing simple task-tag names, up to 32 per entry. Activity
references in a reflection are bounded to 100. Duration is optional integer minutes. All
stored timestamps are UTC; reflection display dates and Activity grouping retain their
separate local-date semantics. Search is parameterized, literal, and result-bounded.
Encrypted snapshots retain the existing 16 MiB bound; an oversized sync fails safely rather
than truncating or deleting history. Local backup and explicit export preserve the full log.
Use a Logging-capable build on every participating sync device; older builds reject the new
collections rather than silently discarding them.

No new runtime dependency, provider permission, executable identity, data root, credential
namespace, hosted service, or Local Assistant dependency is introduced. Logging content is
absent from audit metadata, diagnostics, notifications, and the collector relay. Ordinary
database backups and JSON exports contain private writing and should be protected accordingly.

## Validation

Focused core and desktop regressions live in `tests/test_logging.py` and
`tests/ui/test_logging_ui.py`. The core suite covers concurrency/retries, immutable snapshots,
recurrence, correction/recompletion, cleanup independence, manual capture, local dates,
literal search, complete summaries, reflection separation, bounds, schema migration,
backup/export/restore, retention preview/apply, encrypted opt-in sync, tombstones, and relay
exclusion. Desktop tests exercise actual controls, autosave, saved-state failures, mode
memory, linked-task navigation, privacy, corrected history, and narrow-window keyboard use.

Final validation covers **339 passing tests** across the complete collected inventory:
252 core tests and 87 UI tests in fresh-process batches, including the successful Logging
rerun after the final keyboard traversal fix. The focused regression command passed 36 tests;
the final Logging UI file passed all eight. Ruff, documentation, whitespace, import isolation,
accelerated reliability, executable build, installer compilation, and dependency inspection
pass. Detailed results are recorded in `PERSONAL_HUB_IMPLEMENTATION_STATUS.md`.
Live provider credentials are unnecessary for Logging. Existing
manual screen-reader, clean-VM, signing, and wall-clock release gates remain external release
work and are not claimed by deterministic tests.

Commands used for this implementation:

```powershell
python -m pytest tests/test_logging.py tests/test_phase2_tasks.py tests/test_sync.py tests/ui/test_logging_ui.py -q
powershell -ExecutionPolicy Bypass -File scripts/test_personal_hub.ps1 -Lane all
python -m ruff format --check .
python -m ruff check .
python scripts/check_documentation_consistency.py
python scripts/check_personal_hub_imports.py --output .benchmark-data/personal-hub-logging-imports.json
python scripts/run_personal_hub_reliability.py --mode accelerated --output .benchmark-data/personal-hub-logging-reliability.json
python -m PyInstaller --noconfirm packaging/personal-hub.spec
python scripts/verify_personal_hub_package.py dist/PersonalHub.exe --analysis build/personal-hub/Analysis-00.toc --output .benchmark-data/personal-hub-logging-package.json
git diff --check
```

Inno Setup 6.7.3 also compiled `packaging/personal-hub-installer.iss`. No installer was
installed or uninstalled over the user's current app. Generated artifacts, test logs, and
visual review captures remain ignored under `dist/`, `build/`, and `.benchmark-data/`.
