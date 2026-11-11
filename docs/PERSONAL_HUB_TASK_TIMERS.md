# Personal Hub task timers

## Timed task goals — November 9, 2026

Tasks can optionally have a **Time goal** or **Effort estimate**, with a target from 1 to
1,440 minutes. Edit a task to configure its timer, then choose **Start / resume** on its row.
The Tasks page shows a filling progress bar, remaining time, elapsed minutes, and controls.
A Time goal automatically completes the task when its target is reached. An Effort estimate
pauses at its target and offers completion, Keep working, or Stop session. Completing early
uses the normal completion and Undo behavior. Recurring occurrences inherit the timer settings
and begin with zero progress.

Pause retains progress. Starting another task pauses the previous timer; select its row's
Start / resume to return. Stop session clears progress and saves optional logged time.
Discard session clears progress without creating a duration record. Changing the timer type
or target resets its progress; ordinary title/notes edits retain it. Archiving discards its
session, deletion removes it, and reopening a completed task requires a fresh session.

Timers use active elapsed time, independent of timezone and wall-clock changes. They continue
while Hub is minimized or another page is open. Exit, lock, restart, and gaps over ten seconds
(sleep or a blocked app) pause counting; Resume is explicit. Recovery retains the last saved
whole second. The app must be running to complete a timed task; closed/suspended time is not
credited. This is an explicit focus timer and does not monitor whether you are studying.

**Save measured minutes to Activity** is optional and off by default for each task. Finishing
or stopping a session writes one linked Activity progress entry with its measured whole minutes
(partial minutes are omitted; sessions under a minute create no duration entry). Task completion
still creates its usual independent completion fact. Duration entries are labeled timed; manually
changing their duration changes the label to entered. Undo corrects the completion fact and retains
the measured focus record. Daily overview totals include both entered and timed durations.

Task timer settings follow existing task backup/export and enabled encrypted task sync. Running
sessions and unfinished seconds remain device-local in `hub_focus_sessions` (schema 17), are
included in database backup and explicit JSON export, and never sync or enter provider APIs.
Restoring progress pauses it. Saved duration Activity entries follow existing Activity retention,
backup/export and explicitly enabled encrypted sync; task cleanup preserves them. Use updated
builds on all sync devices because older strict contracts may reject timer settings or duration
source metadata. Privacy masking hides timer content immediately; lock also pauses counting.

