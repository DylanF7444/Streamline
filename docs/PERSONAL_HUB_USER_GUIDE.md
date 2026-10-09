# Personal Hub user guide

Personal Hub is a separate Windows desktop application for tasks, calendar attention, news,
web watches, messages, and durable alerts. It is not a conversational assistant: it has no
prompt, microphone, transcript, command executor, or model requirement.

## Install and start

For development:

```powershell
python -m pip install -e ".[desktop,test]"
powershell -ExecutionPolicy Bypass -File scripts/run_personal_hub.ps1
```

Release candidates install per user to `%LOCALAPPDATA%\Programs\PersonalHub` and keep user
data under `%LOCALAPPDATA%\PersonalHub`. Personal Hub can be installed and run beside Local
Assistant. The optional installer task registers only the `PersonalHub` sign-in entry.

The development launcher and test runner prefer this checkout's source when an older
editable Hub checkout is also registered in the Python environment.

The current planning-branch installer is an unsigned development candidate. Do not treat it
as a public release until the signing and release-evidence gates in the runbook pass.

## Main screens

### Home

Home (previously Today) is the default view. It offers calendar events, overdue/due/upcoming/unscheduled tasks,
attention messages, unread news, and watch alerts. One refresh request runs the provider passes
through one background worker and shows one stable refresh state; repeat clicks are coalesced.
The main panel is a widget dashboard, including a mini calendar, Agenda, task sections, Inbox, Watchlist,
attention messages, news, and watch alerts. Each widget opens its corresponding full page.
Mini calendar, Agenda, due and overdue tasks, Inbox, and Watchlist start on the board; the other widgets
are available from **Add widget**.

The **Mini calendar** shows a compact month grid with an outline around today and dots on
dates with loaded events or due tasks from selected calendars. Use the arrows to browse months
or **Today** to return to the current date. Select a date (or press Enter on a selected date)
to open its full Calendar day view. The widget shows calendar freshness and offline status;
markers reflect available data. The separate **Agenda** widget retains the event list.

Use **Edit layout** to arrange your space. Drag a widget by its heading to move it, or use
its earlier/later controls with the keyboard. Drag the bottom-right resize grip or choose
a size preset to change its width and height. **Remove** hides a widget; **Add widget**
brings it back without changing any underlying data. **Reset layout** restores the initial
arrangement. Layout changes are saved locally as you make them. Narrow windows use one
column and retain your wide-screen size preferences.

In **Settings → Appearance**, choose a theme preset or customize the accent, background,
and surface colors with a color picker or `#RRGGBB` value. Choose **Apply appearance** to
save. The animation toggle controls page fades and widget movement. **Reduced motion**
in accessibility settings also disables these animations, including any already running.
High contrast takes precedence over custom colors. Colors, motion preferences, widget order,
visibility, and sizes survive restart and remain separate from Local Assistant settings.

### Tasks

Use Quick add for a title-only task, or open the editor for notes, due date/time, all-day
status, IANA timezone, priority, list, tags, recurrence, and reminder offsets. Tasks can be
filtered and sorted, completed, archived, edited, and undone. Completing a recurring task
creates exactly one next occurrence.

The Tasks toolbar keeps the common path compact: choose a quick view such as **Due today**,
**Upcoming**, **Overdue**, or **No date**, search by title or metadata, and open **Filters**
only when state or sort controls are needed. Select **Edit** from a task's actions menu to
open the editor beside the list; the list remains visible while editing.

Use **Import** beside Quick add to add many local tasks at once. Choose **CSV file** to browse
for a UTF-8 CSV or paste CSV text, or choose **Text list** to enter one task title per line.
The dialog can save the [task CSV template](PERSONAL_HUB_TASK_IMPORT_TEMPLATE.csv); `title`
is required, while due dates, times, lists, tags, priorities, and reminders are optional.
Rows with an `id` can be safely imported again to update the same task. Invalid rows are
reported without stopping valid rows, and imported tasks remain local to Personal Hub.

Canvas Student can also create tasks from a private iCalendar feed. Open **Sources**, select
**Canvas Student**, and choose **Connect / Configure**. Paste the calendar feed URL from Canvas.
Every concrete calendar item is imported as a local task using the event start as its due date;
the existing due-task projection then places it on Calendar. Refreshes update the same tasks
without duplicating them or reopening completed/archived tasks.
Canvas's iCalendar feed identifies assignment events in its calendar URL; Personal Hub converts
those same-origin links to the native assignment page. The task row provides **Open in Canvas**
for those assignments; items without a safe assignment identity do not show a native-open action.
To remove completed assignment copies after they are no longer due, enable **Remove completed
Canvas assignments after due date** in Settings. On a successful Canvas refresh, this removes
only completed, past-due assignment copies from the local task list and records a durable
Personal Hub deletion marker. Later refreshes and restarts honor that marker instead of
recreating the assignment as an open task. Open, future, and generic Canvas calendar items
remain, and Canvas itself is never modified.

### Calendar

Choose **Add item** to create a calendar item directly. Enter its title, start and end,
timezone, and optional location and notes. All-day items use an exclusive end date (a
one-day item ends the following day). Items save in the local **My calendar** calendar,
work offline, survive restart, and appear in Home and every Calendar view. Select an item
to **Edit item** or **Remove locally**. Local removal retains any Google copy.

Google sync is optional. In **Sources → Google → Connect / Configure**, enable
**Allow syncing selected local events to Google**, then sign in again to grant event
access. In an item's editor, select a Google calendar under **Optional sync**; other items
stay local. Choose **Sync Google** to send local changes and receive changes to those linked
items. This is an explicit action, not a background write on ordinary refresh. An uncertain
write can be retried without creating another copy. If both versions changed, sync reports
a conflict; edit the local item, choose **Use Google changes** or **Send these local changes**,
save, and sync again. If Google removes an item, its local copy is retained and will not be
recreated remotely. Choose **Keep on this device** in the editor to stop syncing an item.
Outlook and other calendar feeds continue to support incoming read-only display.

Calendar supports Month, Day, Week, and Agenda views, selected calendars/accounts, timed and
all-day events, freshness state, privacy masking, and provider deep links. Day and Week show
an hour-by-hour schedule. Select an hourly slot to start a new local scheduled task with its
due date, time, and default timezone prefilled; save it from the task editor and it appears
in that slot. Canvas assignment
tasks with a safe URL provide **Open in Canvas** from their Calendar details card. Provider calendar
events are read-only and have no create, edit, response, completion, or delete action. A
successful Google refresh removes events that were deleted or cancelled at Google; stale,
offline, and permission/error results keep the last successful in-memory snapshot because they
cannot prove a deletion. Local to-do items with due dates also appear on Calendar and include a
checkbox that completes or reopens the underlying task. Use Import iCal feed to add a direct
HTTPS iCalendar URL for read-only display; feed definitions and validators are stored locally,
while event snapshots remain in memory.

To add local calendar items from a file or list, open **Calendars** and choose **Import CSV or
list** under **Local calendar imports**. The [calendar CSV template](PERSONAL_HUB_CALENDAR_IMPORT_TEMPLATE.csv)
includes the date, time, timezone, location, description, and safe-link columns. A text list
can use `YYYY-MM-DD HH:MM | title` or `YYYY-MM-DD | title`; unprefixed rows use the defaults
shown in the dialog. Each named import appears as a selectable calendar and can be removed
from the same panel. These items are read-only local records and never change a connected
calendar.

Use **Calendars** in the toolbar when you need to change selected calendars or manage imported
iCalendar feeds; those controls stay collapsed during normal browsing. Selecting an event opens
its details in the inspector beside the calendar grid, so the current period remains visible
while reviewing the event.

### Watchlists and news

Add an RSS/Atom URL and optional include/exclude terms, refresh interval, digest-only mode,
and HTTPS policy. Entries can be selected by subscription, filtered, marked read/unread, and
opened at their validated provider link.

Web watches support exact public URLs and typed conditions for text, selector text,
structured product price, percentage decrease, and restock. Test now establishes or previews
the extraction before saving. The first observation is a baseline; only a later qualifying
transition alerts. The collector does not run page scripts, submit forms, automate logins, or
make purchases.

### Inbox and messages

Inbox contains safe, durable task reminders, news digests, watch alerts, and source failures.
The page separates that workflow from **Messages** with top-level **Alerts** and **Messages**
tabs. Select an Alerts row to mark it read or unread, dismiss it, or archive it. These actions
change only the Hub attention record; they never mutate the originating provider item.

When unread selected messages are available, Hub shows a count-only attention notice at the top
of the window when it opens or completes a message refresh. **Review messages** opens the
Messages tab; **Dismiss** hides the notice for the current Hub session. The notice is transient
and never creates an Inbox row.

Messages use a master-detail layout: summaries remain on the left while the explicitly opened
memory-only body and its safe links appear on the right. Listing summaries never fetches all
bodies. The displayed body is sanitized plain text with inert, separately listed HTTP(S) links.
**Clear summaries** deletes the Hub's local summaries and memory-only bodies, not provider mail
or Slack content. The message section starts in **Primary** and provides local **Promotions**,
**Social**, **Updates**, and **Spam** filters with item counts. Gmail labels and Outlook
categories are used when present; otherwise conservative local sender/subject rules classify the
summary. Selecting a filter only changes the Hub view—nothing is moved, deleted, labeled, or
marked in the provider mailbox. Spam is hidden from the default Primary view, Today, and the
count-only attention notice, but remains available in its own filter for review.

### Sources and Settings

Sources displays Canvas Student, Google, Microsoft, and Slack connections and their safe
health/freshness information. Select one of those rows and choose **Connect / Configure**.
Canvas accepts its private HTTPS iCalendar feed URL and imports due-dated local tasks. Google
and Microsoft accept a desktop/public OAuth client ID (and an optional client secret), then open
the system browser once for PKCE sign-in. Slack accepts one read-only API token plus one channel
ID or saved search. Calendar also supports standalone read-only iCalendar feed imports, which
are managed separately from platform connections.
Secret fields—including the full Canvas feed URL—go directly to the Hub vault and are not saved
in settings. You can then refresh,
reconnect, enable/disable, or disconnect the source. Disabled sources are excluded from scheduled
refresh across restart; disconnect clears provider tokens and connector-owned local message state
without changing provider data.

Settings provides editable Hub-owned offline, privacy, tray/startup, notification, quiet-hour,
digest, refresh, retention, Canvas cleanup, and accessibility controls plus redacted diagnostics.
Offline mode can also be toggled from the tray menu and is persisted before later refreshes.
Database backup,
integrity-checked restore, and credential-free JSON export are user-facing actions. Restore is
refused while refresh or maintenance work is active.

The connection composition and setup UI are present. Publishing a release still requires a
rotated non-production Canvas feed, project-owned Google and Microsoft OAuth registrations, and
live Canvas, Google, Microsoft, and Slack acceptance connections; deterministic tests are not
represented as release evidence.

## Provider permissions

| Provider | Purpose | Approved scope(s) | Not available |
| --- | --- | --- | --- |
| Canvas Student | import calendar items as local due tasks | private iCalendar feed URL; no API scope | Canvas writes, submissions, grade/course API access |
| Google Calendar | selected calendar display; opt-in sync for local items | `https://www.googleapis.com/auth/calendar.readonly`; optional `https://www.googleapis.com/auth/calendar.events` after reauthorization | Responses, invitations, remote deletion, and changes to unlinked events |
| Outlook Calendar | selected read-only calendars | `Calendars.Read` | Event writes or responses |
| Gmail | selected unread folder/query summaries and explicit body | `https://www.googleapis.com/auth/gmail.readonly` | Send, reply, draft, labels, archive, trash, attachments |
| Outlook Mail | selected unread folder summaries and explicit body | `Mail.Read` | Send, reply, draft, move, delete |
| Slack | selected channel history or saved search | `channels:history` or `search:read` | Posting, reactions, uploads |

OAuth uses desktop PKCE with a temporary exact-loopback callback. Authorization state and
verifier values stay in memory; tokens and optional client secrets are stored only in the
Personal Hub credential namespace. Project-owned OAuth client IDs and test accounts are
required for live-provider release acceptance. Never put client secrets or tokens in repository
configuration.

## Offline, privacy, and lock behavior

- Global offline mode prevents provider adapters from reaching tokens, DNS, or transports.
- Last-good in-memory calendar/provider snapshots may be shown as stale/offline; local tasks
  and already retained items remain usable.
- Privacy masking replaces sensitive calendar, message, and Inbox text with a fixed label.
- Windows session lock clears message bodies and hides sensitive previews. Network work can
  pause while locked and resume without replaying every missed poll.
- Message HTML never renders as active HTML. Remote images, scripts, forms, embedded content,
  custom schemes, and automatic links are blocked.
- Quiet hours and notification settings affect operating-system delivery, while eligible
  attention items remain available in the Hub Inbox.
- Immediate watch alerts and task reminders use durable Windows-delivery claims. Delivery,
  failure, and quiet-hours suppression are recorded without storing notification content.

## Retention and deletion

Defaults are 90 days for archived Inbox data, 30 days for audit metadata, 30 days or 500
items per feed subscription, 90 days or 500 observations per watch, and 7 days or 500 message
summaries per source. Message summary retention can be set to 0 for session-only behavior and
is bounded at 30 days. Full message bodies and connected-calendar snapshots are memory-only;
explicitly imported local calendar items remain until the named import is removed. Calendar
event completion state is not stored; task completion remains part of each local task record.
Credentials remain until disconnect and are not included in ordinary database backup/export.

Retention cleanup runs once per local calendar day, including while global offline mode is on.
It removes eligible archived/dismissed Inbox history, audit metadata, feed entries, watch
history, message summaries, and old notification claims. One watch observation is retained as
the comparison baseline so cleanup cannot manufacture a false transition.

Uninstall always removes Personal Hub binaries and its startup registration. The uninstaller
asks whether to preserve `%LOCALAPPDATA%\PersonalHub` for reinstall or delete Hub settings,
tasks, summaries, watch history, credentials, logs, and backups. Silent uninstall preserves
data unless `/DELETEUSERDATA=yes` is supplied. Neither choice targets Local Assistant.

## Backup and recovery

The Hub database backup uses SQLite's online backup API. Restore first checks database
integrity and schema compatibility, restores through a temporary file, and atomically replaces
only the active Hub database. Credentials are excluded from ordinary backup. Keep a backup
outside the installation directory before upgrade or repair. The release runbook contains the
developer verification commands and rollback procedure.

**Export local data** writes an atomic JSON document containing bounded settings, tasks,
Inbox records, source metadata, feeds, watches, and retained
message summaries. It excludes the Hub data path, credentials, message bodies, and memory-only
calendar snapshots. It includes local tasks and explicitly imported calendar items. Treat the
export as sensitive because it intentionally contains your visible local content.

## Sync across devices

Personal Hub can share selected local state through a folder that you control. In **Settings**,
open **Sync across devices**, choose the same shared folder on each device, select **Save
folder**, enter a passphrase, and choose **Sync now**. Use the same passphrase on every device.
Sync is manual; the folder may be local, removable, an SMB share, or a user-managed cloud folder
such as OneDrive or Dropbox. Personal Hub does not create a hosted account or run an inbound
network service.

Sync includes local task lists and tasks, explicitly imported calendar items, feed subscriptions,
watch definitions, and selected shared preferences and view state. It does not include passwords,
OAuth tokens, the credential vault, provider connections or caches, message content, feed entries,
watch history, Inbox history, or private iCalendar feed URLs. The passphrase is used only for the
current sync and is not saved by Hub.

Changes merge by their UTC update time. A deletion is carried by an encrypted tombstone, so it
continues to propagate after another device has been offline. Equal-time conflicting edits use a
stable deterministic tie-breaker and are reported in the sync result. If the passphrase is wrong,
the folder is damaged, or it belongs to another Hub profile, the sync stops without replacing
your local data. Keep a separate backup before changing or removing a shared folder.

## Troubleshooting

- **Offline:** turn off Offline mode before retrying a remote source.
- **Permission required:** reconnect with only the scope shown above.
- **Stale:** the last successful snapshot is being shown after a safe refresh failure.
- **Disconnected:** select the provider under Sources, choose **Connect / Configure**, enter the
  provider's desktop OAuth client ID or read-only Slack token, and reconnect.
- **A source fails:** other sources continue; use Settings diagnostics for a content-free
  state, last success, and safe error category.
- **A body disappeared:** message bodies intentionally clear on lock, source clear, shutdown,
  and process exit; open the summary again while online.
- **Sync failed:** confirm that the folder is reachable, every device uses the same passphrase,
  and the folder was not paired with a different Personal Hub profile. Sync again after restoring
  access; a failed sync does not replace the local database.

See `PERSONAL_HUB_PRIVACY.md` for the exact data boundary and
`PERSONAL_HUB_RELEASE_RUNBOOK.md` for release-only acceptance steps.
