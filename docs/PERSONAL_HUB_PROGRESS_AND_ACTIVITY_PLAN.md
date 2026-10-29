# Personal Hub Tracking and Logging Proposal

- **Status:** Implemented October 28, 2026; acceptance evidence in `PERSONAL_HUB_LOGGING_IMPLEMENTATION.md`
- **Date:** October 28, 2026
- **Related specification:** `PERSONAL_HUB_IMPLEMENTATION_PLAN.md`

October 29, 2026 update: the implemented **Summary** page is now **Daily overview**. It is
the initial Logging landing page and combines one day's factual activity with an inline
Reflection editor and previous/next/date/Today navigation. Screen-time tracking is deferred.
This supersedes the Summary presentation below; record and privacy boundaries remain the
same. See `PERSONAL_HUB_LOGGING_IMPLEMENTATION.md` for current implementation details.

## Summary

Personal Hub should have two clearly distinct modes:

1. **Tracking:** the existing Home, Calendar, Tasks, Watchlists, and Inbox workflows help
   answer what needs attention and what to do next.
2. **Logging:** a new space records what happened and provides room to write and reflect on
   it.

Logging contains two related but distinct kinds of records: a factual Activity log and private,
freeform Reflections. Completing a task automatically creates an Activity entry, so the user
does not have to enter the same accomplishment twice. The user can also add progress entries
for unplanned work or work on an open task, then write a separate reflection about the day or
week.

The two modes have different jobs and visual identities, while remaining connected through
task links and date-based navigation. Tracking is for commitments and attention. Logging is
for records, context, and reflection. Neither requires the other: the user can track without
writing reflections, or use Logging without managing tasks.

## Product goal

Help the user move naturally between two questions without merging their purposes:

- **Tracking:** What should I do next, and what needs attention?
- **Logging:** What did I do, and what do I want to remember or think through?

Logging should make completed work visible, preserve useful context after a task is removed,
and support both quick capture and thoughtful end-of-day or end-of-week reflection. It should
not pressure users to log every minute or turn task counts into a score.

## Recommended information architecture

Add a persistent, accessible **Tracking / Logging** mode selector in the primary navigation.
Switching modes changes the workspace pages and visual emphasis; it does not create separate
databases or duplicate tasks. Remember the user's last page in each mode. Keep Sources and
Settings in the existing fixed Management area.

Tracking should foreground due dates, status, and next actions. Logging should use a quieter,
more spacious layout that foregrounds the activity timeline and writing surface. Keep the
current mode visible so the user always knows which side they are in.

**Tracking mode** contains the current Home, Calendar, Tasks, Watchlists, and Inbox pages. It
answers what is due, scheduled, changing, or waiting for attention.

**Logging mode** contains:

- **Activity:** the chronological, searchable record of completed tasks and manually logged
  progress.
- **Reflection:** a private writing space for freeform notes, daily or weekly reflections, and
  optional prompts.
- **Summary:** a modest view of completed tasks and logged activity for a selected period.

Activity and Reflection are separate record types and separate subviews. An Activity entry
captures a fact or work update. A Reflection captures the user's own narrative, questions, or
lessons. They can be browsed by date together in a daily view, but writing a reflection never
creates or completes a task, and task completion never requires a reflection.

Task lists, tags, due dates, and priorities remain managed in Tracking. Logging may use list
and tag context for activity filters and summaries. Opening a linked task from Logging switches
to Tracking and opens that task; returning to Logging restores the prior Log page.

Tracking Home can optionally show a compact **Recently completed** or **Logged today** widget
that links into Logging. It should not duplicate the full history or surface reflection text by
default.

## Core workflows

### Completing planned work

When a user completes a local task, Personal Hub creates one progress record automatically.
The action remains one click or keyboard action and does not open a mandatory logging dialog.
The record captures the task title as it appeared at completion, completion time, task-list and
tag context, and the stable task or recurrence-occurrence identity.

For recurring tasks, each completed occurrence gets its own record. Retrying a completion or
refreshing the task projection must not create duplicates.

If the user undoes or reopens a completion, its entry is marked **Undone** and excluded from
completion summaries. The entry remains visible when the user includes corrected history, so
the timeline does not silently rewrite what happened. Completing the task again creates a new
completion record.

Editing a task later must not rewrite the historical title or completion time. Deleting or
automatically cleaning up a completed task must not erase its progress record. The record can
retain a title snapshot and show that its original task is no longer available.

### Logging progress outside task completion

The user can choose **Log progress** from the Activity view or from a task. A short form asks
for:

- a required one-line description of the work or result;
- when it happened, defaulting to now;
- an optional link to a task, list, or tag;
- optional notes;
- an optional duration entered by the user.

Linking an entry to an open task does not complete or change that task. This supports entries
such as “outlined the proposal” while “finish proposal” remains open. Users can also log an
unplanned accomplishment without first creating a task.

Duration is always explicit user input. It is never inferred from due dates, task creation,
completion time, or time spent with the app open. The first release has no running timer.

Manual entries can be edited or deleted. Completion records can have an optional note, but
their task title, occurrence identity, and original completion timestamp remain stable. A user
can correct an accidental completion through the task's normal undo/reopen action.

## Activity page

The Activity view should provide:

- entries grouped by local date, newest first;
- a **Today**, **Last 7 days**, and custom date-range filter;
- search across entry descriptions, notes, and saved task-title snapshots;
- filters for completed tasks, manual entries, task list, and tags;
- a clear state for undone completions;
- direct navigation to the linked task when it still exists;
- an accessible **Log progress** action and keyboard navigation.

An empty state should explain that task completions appear automatically and that manual
progress can be logged when useful. Logging should remain optional for tasks that are still in
progress.

## Reflection space

The Reflection view should feel like a quiet writing surface, visually distinct from task
lists and activity rows. It should support:

- starting a dated reflection with an optional title and freeform body;
- saving drafts automatically on-device and showing a clear saved state;
- browsing entries by date, with search over titles and text;
- optional lightweight prompts such as “What moved forward?” or “What should I carry into
  tomorrow?” that can be skipped;
- an optional link to the day's Activity entries or a task, without changing either record;
- editing and deleting the user's own reflections.

Do not require a prompt, mood rating, word count, daily streak, or minimum entry length. Avoid
prescribing what the user should write. The first release can use plain text with paragraphs;
rich formatting, images, and attachments can wait until there is a demonstrated need.

A date can have multiple reflections. The default view can show a single **Write a reflection**
action for the selected day and the entries already saved there. A daily or weekly review can
show the factual Activity timeline beside the reflection editor, while keeping the two records
separate and clearly labeled.

## Summary view

The Summary view should report observable facts for a selected date range:

- number of tasks completed, counting recurring occurrences separately;
- number of manual progress entries;
- optional total duration, clearly labeled as the sum of durations the user entered;
- breakdowns by existing task list or tag;
- a day-by-day activity timeline or simple bar view.

Do not show a productivity score, inferred work hours, streaks, or completion percentage in
the first release. These metrics either imply precision the app does not have or create
pressure without helping the user decide what to do next. If there are no duration entries,
show that duration was not tracked rather than reporting zero hours worked.

## Data and behavior boundaries

### Activity record

Add a Hub-owned activity record with a stable ID, record type (`task_completion` or `manual`),
UTC occurrence timestamp, optional duration, description, optional notes, and optional links to
a task occurrence, task list, or tags. Task-completion records also store a title snapshot and
an undone/correction state. Manual records can be edited and deleted by the user.

Use the existing task/list/tag concepts rather than introducing a separate project hierarchy.
Store activity in its own repository and database table so task cleanup and task editing cannot
accidentally rewrite history. Bound text, entry size, and query results consistently with the
existing local data contracts.

### Reflection record

Store each reflection as a separate user-authored record with a stable ID, local display date,
UTC creation and update times, optional title, bounded plain-text body, and optional references
to activity entries or tasks. Reflection text must not be copied into task notes or completion
records. Keep update timestamps distinct from the date the reflection is about, so editing an
older entry does not move it to today.

### Retention and deletion

Activity and reflection history should default to **keep until the user deletes it**. Do not
silently apply completed-task cleanup to either record type. If a user enables retention later,
make it a separate, clearly named setting with a preview of how many Activity entries and
reflections will be removed.

Export, backup, restore, and explicit local data deletion should include Activity and
Reflection records. Deleting a log entry or reflection and deleting its source task are separate
actions. The existing completed-task cleanup control should explain that it removes task items
while preserving Logging history.

### Privacy and synchronization

Activity entries and reflections are user-authored local data and may contain sensitive notes.
They must follow the existing privacy mask, lock, logging, backup, export, and retention
protections. Their text must not be sent to providers or included in provider refreshes.

Include Activity records in the existing passphrase-protected shared-folder synchronization
only when that sync is enabled. Reflections should stay on the current device by default and
require a separate explicit choice to include them in encrypted shared-folder sync. Keep all
Logging content out of the provider collector relay. This keeps ordinary account connections
from publishing personal progress history or private writing.

## Scope by release

### First release

- Distinct Tracking and Logging modes with remembered page selection.
- A Logging mode with Activity, Reflection, and Summary views.
- Automatic entries when local tasks or individual recurring occurrences are completed.
- Correct handling of undo/reopen and duplicate completion attempts.
- Manual progress entries, including entries linked to open tasks or existing lists/tags.
- A private Reflection view with dated plain-text entries and optional prompts.
- Search, date filters, list/tag filters, and basic factual summaries.
- Optional user-entered duration; no timer.
- A compact optional Home widget for recent activity.
- Activity included in local backup/export/restore and explicit encrypted shared-folder sync.
- Reflections included in local backup/export/restore and excluded from shared-folder sync by
  default, with a separate opt-in.
- Independent Logging-history retention and deletion behavior.

### Later candidates

- Milestones or goal tracking based on existing task lists and activity.
- A user-controlled focus timer, if there is a clear need beyond manual duration entry.
- Weekly review prompts or configurable review templates.
- Optional local-only summaries, after non-AI behavior proves useful.

### Out of scope

- A mood journal or therapeutic journaling workflow.
- Automatic tracking of applications, websites, keystrokes, or screen activity.
- Billing, payroll, or compliance-grade timesheets.
- Mandatory time estimates or timers on tasks.
- Public leaderboards, streaks, productivity scores, or behavioral nudges.
- Sending progress records to calendar, mail, chat, or task providers.

## Delivery sequence

1. **Mode shell:** add the Tracking / Logging selector, mode-specific page navigation, saved
   page selection, and accessible transitions between the modes.
2. **Model and lifecycle:** define Activity and Reflection contracts, persistence,
   task/occurrence identity, migration, backup/export/restore, deletion, retention, and sync
   boundaries.
3. **Core capture:** create automatic completion entries, undo/reopen correction behavior,
   manual progress capture, and the Reflection writing surface.
4. **Review:** build the Activity timeline, Reflection history, filters, search, and basic
   Summary view.
5. **Home and polish:** add the optional Tracking Home widget, keyboard/accessibility
   behavior, privacy masking, and updated user guidance.

Keep provider integrations and optional AI outside these steps. The feature should work fully
offline and without an AI provider.

## Acceptance criteria

- Completing a local task creates one dated progress entry without an extra required action.
- Each recurring occurrence is represented once, even if completion handling retries.
- Undoing or reopening a task marks the original completion as undone and removes it from
  completed counts; recommitting creates a new record.
- A user can log an unplanned accomplishment or progress on an open task without changing the
  task's state.
- A user can write, edit, search, and delete a private dated reflection without creating or
  changing a task or Activity entry.
- Editing or removing a task does not silently change or remove its saved activity history.
- The user can find activity by date, text, task list, or tag and can tell manual entries from
  task completions.
- Duration totals include only explicitly entered durations and are labeled accordingly.
- Activity follows the Hub's masking, lock, backup, export, restore, and deletion controls and
  enters the existing encrypted shared-folder sync only when enabled.
- Reflection text is stored separately, follows local data protections, and is excluded from
  shared-folder sync unless the user explicitly opts in; provider relay and provider APIs never
  receive Logging content.
- No activity history disappears through task cleanup without clear user-facing retention
  behavior.

## Implementation decisions

- Tracking and Logging are first-class app modes with a persistent mode selector.
- Logging includes concise Activity records and a separate freeform Reflection space.
- Reflection uses private plain-text entries and optional prompts, not a document editor or
  mood journal.
- Duration is optional and manually entered. A timer is a later decision.
- Existing encrypted shared-folder sync is the only cross-device path. Activity entries can
  follow that explicit opt-in; reflections require a separate explicit opt-in. Provider relay
  remains excluded.
