# Personal Hub user guide

Personal Hub is a separate Windows desktop application for tasks, calendar attention, news,
web watches, messages, and durable alerts. It is not a conversational assistant: it has no
prompt, microphone, transcript, command executor, or model requirement.

## Install and start

For development:

```powershell
python -m pip install -e ".[desktop,test]"
python -m personal_hub.app
```

Release candidates install per user to `%LOCALAPPDATA%\Programs\PersonalHub` and keep user
data under `%LOCALAPPDATA%\PersonalHub`. Personal Hub can be installed and run beside Local
Assistant. The optional installer task registers only the `PersonalHub` sign-in entry.

The current planning-branch installer is an unsigned development candidate. Do not treat it
as a public release until the signing and release-evidence gates in the runbook pass.

## Main screens

### Today

Today is the default view. It offers calendar events, overdue/due/upcoming/unscheduled tasks,
attention messages, unread news, and watch alerts. One refresh request runs the provider passes
through one background worker and shows one stable refresh state; repeat clicks are coalesced.
The main panel is a widget dashboard, including Calendar, task sections, Inbox, Watchlist,
attention messages, news, and watch alerts. Each widget opens its corresponding full page.
Calendar, due and overdue tasks, Inbox, and Watchlist start on the board; the other widgets
are available from **Add widget**.

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

Canvas Student can also create tasks from a private iCalendar feed. Open **Sources**, select
**Canvas Student**, and choose **Connect / Configure**. Paste the calendar feed URL from Canvas.
Every concrete calendar item is imported as a local task using the event start as its due date;
the existing due-task projection then places it on Calendar. Refreshes update the same tasks
without duplicating them or reopening completed/archived tasks.

### Calendar

Calendar supports Month, Week, and Agenda views, selected calendars/accounts, timed and
all-day events, freshness state, privacy masking, and provider deep links. Provider calendar
events are read-only and have no create, edit, response, completion, or delete action. A
successful Google refresh removes events that were deleted or cancelled at Google; stale,
offline, and permission/error results keep the last successful in-memory snapshot because they
cannot prove a deletion. Local to-do items with due dates also appear on Calendar and include a
checkbox that completes or reopens the underlying task. Use Import iCal feed to add a direct
HTTPS iCalendar URL for read-only display; feed definitions and validators are stored locally,
while event snapshots remain in memory.

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

Inbox contains safe, durable task reminders, digests, watch alerts, and source failures.
Select an Inbox row to mark it read or unread, dismiss it, or archive it. These actions
change only the Hub attention record; they never mutate the originating provider item.
Message summaries are a separate bounded section. Select a summary and choose **View body**
to fetch that one body; listing summaries never fetches all bodies. The displayed body is
sanitized plain text with inert, separately listed HTTP(S) links. **Clear summaries** deletes
the Hub's local summaries and memory-only bodies, not provider mail or Slack content. The
message section starts in **Primary** and provides local **Promotions**, **Social**,
**Updates**, and **Spam** tabs. Gmail labels and Outlook categories are used when present;
otherwise conservative local sender/subject rules classify the summary. Selecting a tab only
changes the Hub view—nothing is moved, deleted, labeled, or marked in the provider mailbox.
Spam is hidden from the default Primary view, Today, and scheduled message digests, but remains
available in its own tab for review.

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
digest, refresh, retention, and accessibility controls plus redacted diagnostics. Offline mode
can also be toggled from the tray menu and is persisted before later refreshes. Database backup,
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
| Google Calendar | selected read-only calendars | `https://www.googleapis.com/auth/calendar.readonly` | Event writes or responses |
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
is bounded at 30 days. Full message bodies and calendar snapshots are memory-only. Calendar
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
calendar snapshots. Treat the export as sensitive because it intentionally contains your visible
local content.

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

See `PERSONAL_HUB_PRIVACY.md` for the exact data boundary and
`PERSONAL_HUB_RELEASE_RUNBOOK.md` for release-only acceptance steps.
