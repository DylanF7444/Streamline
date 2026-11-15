# Weekly review

Implemented November 13, 2026. Open **Logging > Weekly review**. Daily overview remains the
initial Logging page; the existing per-mode remembered destination supports Weekly review.

## Dates and factual summaries

A review covers Monday through Sunday in Settings' IANA timezone. The picker accepts any
date and selects its Monday. Previous/next move one calendar week, including month/year and
DST boundaries. This week follows the local week on refresh; choosing a historical week
disables that behavior. The visible page refreshes local records once a minute and offers
an explicit Refresh review action. Hidden/private pages do not poll. Draft failures block
week changes, navigation and quitting; refresh preserves pending text and cursor position.

Activity totals stream all matching records independently of detail pagination. Corrected
completions are excluded. Completion snapshots preserve titles, tags and lists after task
editing/deletion. Seven daily rows show completed tasks, manual progress, entered duration,
timer duration saved to Activity, app usage and site usage. Activate a day or use Open selected
day's overview to open Daily overview without changing the selected week. Accomplishments
include local date/time and existing list/tag context.

Details start at 200 entries, expand by 200, and stop at 10,000; the load control identifies
the displayed count and full total. At the cap, Activity filters can narrow the history.
The combined breakdown shows the ten largest lists, apps and sites per group, with stable
name tie-breaking. Totals always include all groups. Reflection choices contain at most
10,000 entries from the week; Reflection supports narrower date/text filters for larger histories.

## Distinct time measurements

Entered durations and explicitly saved task-timer durations are separate Activity facts.
Running/unfinished focus sessions and unrecorded timer time are not inferred as work.
App/site intervals use existing repository clipping to exact local-day/week boundaries,
including 23/25-hour days. Totals are this PC's retained observations, including already
checkpointed intervals; gaps, idle periods and disabled collection remain unrecorded.
Missing history is labeled No recorded usage or Not tracked. App, website and logged work
durations overlap and are never summed into a combined hours-worked number. Recording
consent, exclusions, pause, retention and deletion remain controlled by Screen time.

## Metrics

Choose a definition and toggle Include this metric in Weekly review. Up to 64 IDs are stored
under the device-local `logging.weekly_metrics` setting. Unknown/deleted definitions are
ignored safely. Definitions and primary sources remain managed in Metrics.

Each summary uses exactly its configured primary source and aggregation over retained
records; sources are not added together. Sum/latest/min/max/mean/duration keep the existing
metric semantics. A zero reading stays zero; missing readings stay unavailable. Labels show
units, aggregation, source, record count and the source reporting timezone when available.
Reporting dates remain those recorded by the source; values are not redistributed into the
Hub display timezone. Missing days are not manufactured and partial weeks are not extrapolated.

## Writing and lifecycle

Weekly writing uses the existing ReflectionEntry and repository, with no schema migration
or parallel journal store. New entries are dated the week's Monday and initially titled
Week of [date]. Multiple entries are allowed. The chooser lists existing reflections from
all seven dates, with their date/title; it starts on New reflection when a week is opened,
so daily writing is not automatically edited. Select an existing entry explicitly to continue it.
These records are searchable/editable/deletable in Reflection and visible in their dated
Daily overview. Refresh reloads external edits/deletions when the editor is clean.

The existing 400ms local autosave and 20,000-character body limit apply. Failed saves retain
writing and show fixed safe messages; privacy masking/lock clear all visible review content
and temporarily retain unsaved text only in memory for recovery. Backup, restore, JSON
export, Logging deletion and separately opted-in reflection sync use existing record paths.
Metric choices stay local; usage/metrics are still excluded from shared-state sync and relay.
Review performs no network work, enables no collector and requires no AI or new dependency.

## Verification

Offline tests are in `tests/test_weekly_review.py` and `tests/ui/test_weekly_review_ui.py`.
They cover boundary weeks, bounded details with full totals, undo, distinct durations,
timezone/DST interval clipping, source/aggregation semantics, missing versus zero values,
reflection lifecycle, navigation, autosave/recovery, lock/mask, date rollover and 175% text
at 720×460. Run these alongside existing Logging, window, accessibility, metrics, screen-time,
browser-time, sync, storage and isolation regressions. Test results are recorded in
`PERSONAL_HUB_IMPLEMENTATION_STATUS.md`. Offscreen evidence does not establish native DPI
or screen-reader acceptance; existing manual release checks remain separate.
