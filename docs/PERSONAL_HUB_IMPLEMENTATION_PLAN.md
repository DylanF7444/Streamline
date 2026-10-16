# Personal Hub Application Implementation Plan

- **Status:** Phases 0–7 implemented at deterministic source gates; release approval pending external evidence
- **Date:** September 2, 2026
- **Working product name:** Personal Hub
- **Repository strategy:** Standalone repository
- **Primary platform:** Windows desktop, Python 3.12, PySide6
- **Implementation handoff:** `docs/PERSONAL_HUB_AGENT_HANDOFF.md`
- **Acceptance evidence:** `docs/PERSONAL_HUB_IMPLEMENTATION_STATUS.md`

## Executive Decision

Build Personal Hub as a separate desktop application beside Local Assistant. Do not
replace, rebrand, or modify the current assistant experience while the Hub is being
validated.

The applications may use the same independently versioned third-party libraries, but they
have separate source trees, project metadata, composition roots, processes, application
identities, settings, databases, credential namespaces, logs, installers, and release
artifacts. Personal Hub must never depend on a Local Assistant package or runtime.

The first Personal Hub release is a local-first attention dashboard centered on:

- a unified Today view;
- a native task manager;
- a provider-neutral calendar;
- news and RSS subscriptions;
- deal, restock, and webpage-change watchlists;
- a read-focused message and email view;
- a durable alert Inbox and scheduled digests;
- explicit connection health, privacy, and offline controls.

Personal Hub should remain useful with no AI model installed. Intelligence can be added
later through a narrow optional interface for summarization and prioritization. A future
full assistant may consume the Hub's typed data, but the Hub must not be designed as a
temporary assistant UI.

## Why This Is a Separate Application

Personal Hub and Local Assistant have different primary promises:

| Application | Primary promise | Primary interaction |
| --- | --- | --- |
| Local Assistant | Interpret requests and perform permission-gated work | Voice or typed conversation |
| Personal Hub | Show what needs attention and what changed | Direct visual navigation and controls |

Keeping them separate protects both products:

- Local Assistant can continue evolving without a product-level navigation rewrite.
- Personal Hub can stay fast, quiet, and understandable without loading assistant models.
- Hub permissions can be predominantly read-only and provider-specific.
- Users can install either application independently.
- Hub data retention can be designed around events, tasks, and alerts rather than
  transcripts.
- Product-market validation does not depend on the larger assistant roadmap.

This is an independent product and repository, not a Local Assistant target or code fork.

## Non-Negotiable Isolation Rules

During initial development, the current assistant remains behaviorally unchanged.

1. `assistant.app:main` remains the Local Assistant entry point.
2. `src/assistant/app.py` and the existing assistant desktop navigation are not repurposed.
3. Personal Hub receives a new `personal_hub.app:main` entry point.
4. Personal Hub uses a different platform data directory, such as
   `%LOCALAPPDATA%/PersonalHub/`.
5. Personal Hub uses a separate SQLite database, initially `personal_hub.db`.
6. Personal Hub uses a separate encrypted secret-vault directory and secret-name namespace.
7. Personal Hub never reads or migrates assistant data automatically.
8. Personal Hub gets a separate Windows application ID, process name, mutex, tray icon,
   startup entry, logs, update channel, executable, and installer.
9. The Hub must not import Local Assistant services; every production dependency is declared
   by the Personal Hub project.
10. Local Assistant and Personal Hub repositories must not reference one another as runtime
    packages.
11. Future shared code requires a neutral, independently versioned package with reviewed
    contracts and privacy behavior.
12. No feature is considered shared merely because both applications need something with
    the same name; contracts and privacy requirements must match first.

Add an architecture-boundary test that fails if Hub source imports any `assistant` module.

## Product Thesis

Personal Hub is an attention system, not a generic dashboard and not a universal inbox.
Its home screen answers four questions:

1. What is scheduled today?
2. What do I need to do next?
3. What changed since I last looked?
4. Which connected sources need my attention?

The product should favor a small number of meaningful items over high-volume aggregation.
Every displayed item must have a source, freshness state, and clear route to its full
context. Every alert must explain why it fired.

### Product principles

1. **Useful without AI.** Rules, schedules, sorting, and direct provider data provide the
   core value.
2. **Local control.** Configuration, normalized state, task data, alert history, and
   credentials remain on the user's device by default.
3. **Outbound connections first.** The desktop application connects to approved provider
   APIs. It does not expose a public inbound server in the first release.
4. **Read before write.** External sources begin read-only. External writes are separate,
   later features.
5. **Metadata before content.** Fetch and retain the least sensitive representation that
   supports the current screen.
6. **Quiet by default.** Deduplication, cooldowns, thresholds, digests, and quiet hours are
   core behavior.
7. **Freshness is visible.** Ready, refreshing, stale, offline, permission, disconnected,
   empty, and error states are first-class.
8. **No invisible authority.** Connecting an account does not authorize sending, deleting,
   purchasing, or modifying remote data.
9. **One source of truth per item.** Provider-backed data retains its provider identity and
   deep link instead of silently becoming a divergent local copy.
10. **Accessible direct manipulation.** Every task and watchlist operation has a visible,
    keyboard-accessible UI path.

## Target Users and Jobs

### Primary user

A single Windows user who wants a calm daily overview across personal calendars, tasks,
selected messages, news, and tracked webpages without sending all of that information to a
new cloud service.

### Core jobs

- See today's events and due tasks in one place.
- Capture and complete tasks without writing a command.
- Notice a price drop, restock, or meaningful webpage change.
- Follow selected topics without opening several news sites.
- See a bounded list of unread or important messages.
- Receive one digest instead of many low-value notifications.
- Understand when a source is stale, disconnected, or blocked by offline mode.
- Open the original source when more context or an external action is required.

### Explicitly unsupported users in the first release

- Teams requiring shared workspaces, assignment workflows, or centralized administration.
- Users expecting a full email client or team-chat replacement.
- Users requiring mobile synchronization hosted by Personal Hub.
- Users requiring a publicly accessible dashboard.
- Users expecting autonomous purchases, replies, or account changes.

## Information Architecture

The main window uses a persistent left navigation rail and a content area. The **Workspace**
group contains Home, Calendar, Tasks, Watchlists, and Inbox. Users can rename, reorder, show,
or hide these pages. Connected and imported calendars and local task lists also appear as
individual shortcuts that open the existing Calendar or Tasks page scoped to that collection.
Users can create an empty task list from Tasks; calendars are added through connected account
selection or named local imports. Workspace navigation preferences are stored in Hub settings.
The separate **Management** group contains fixed Sources and Settings pages; these controls
do not participate in workspace customization. The initial navigation is:

1. **Home** (the Today dashboard)
2. **Calendar**
3. **Tasks**
4. **Watchlists**
5. **Inbox**
6. **Sources**
7. **Settings**

There is no Assistant tab, transcript, prompt box, microphone control, command palette, or
model status in Personal Hub.

### Home (Today dashboard)

Home is the default screen and contains ordered sections rather than arbitrary monitoring
metrics:

- date, local time, and last refresh;
- current and next calendar event;
- overdue and due-today tasks;
- upcoming tasks and events within a configurable horizon;
- unread message summaries from explicitly enabled folders or queries;
- newly triggered watchlist alerts;
- a bounded news digest;
- source problems that require attention.

Today provides a responsive widget dashboard. Users can add or hide available widgets,
place them directly on a bounded 48-unit horizontal grid by dragging the visible move handle
in a heading, and resize from any edge or corner when the multi-column layout is available.
The main widget panel has a visible outline that becomes an accent dashed boundary in edit
mode. Vertical positions and sizes snap in 6-pixel increments. Focused headings and resize
handles support arrow-key movement and resizing. Narrow windows stack widgets at full width
and retain vertical resizing. Layouts persist locally, restore their wide-window positions
when space returns, and can be reset. Older saved layouts migrate automatically. The
dashboard projects existing Hub data; layout settings contain no provider content.
Theme presets, custom accent/background/surface colors, and optional page and layout
animations are Hub-owned preferences. Reduced motion disables all Hub transitions.

The Home dashboard also includes a compact month-grid mini calendar alongside the Agenda
event list. It marks today and dates with loaded selected-calendar events or due tasks,
supports month navigation and returning to today, and opens a selected date in Calendar's
day view. It shares Calendar's timezone, source selection, and freshness/offline state.
The existing Today projection APIs and saved dashboard preferences remain compatible.

### Calendar

October 9, 2026 extension: Calendar supports direct local event creation, editing, and local
removal in **My calendar**, independent of AI and connectivity. Google is the first optional
outbound sync provider: account-level opt-in and event-write reauthorization are required,
followed by per-item target selection and explicit **Sync Google**. Only linked local
events can be inserted or patched. Stable IDs prevent duplicate retries, version checks
protect concurrent changes, and conflicts require choosing a version. Incoming changes to
linked events update their local records. Remote removals retain a local copy without
recreating the remote event; local removal retains the provider copy. Outlook, feed, and
unlinked Google data remain read-only. Sync bindings stay device-local.

Calendar provides month, day, week, and agenda views with:

- multiple selected calendars;
- source and account indicators;
- timezone-aware timed and all-day events;
- privacy masking;
- explicit stale and offline states;
- deep links to the provider;
- an hour-by-hour day/week schedule where an empty hour can start a local scheduled task;
- local refresh without writing remote event data.

Connected-provider calendar events are read-only in the MVP: no completion, editing, response,
or deletion action is available for provider data. A successful refresh replaces the in-memory
provider snapshot, so events deleted or cancelled in Google Calendar disappear from Personal
Hub. Users may explicitly create local calendar items by importing a bounded UTF-8 CSV or text
list; each named import is a selectable local calendar and never writes to a provider. Local
to-do items with due dates are projected onto Calendar and retain their normal task completion
and undo controls.

### Tasks

Tasks is a complete local visual workflow, not a summary card. It supports:

- quick add;
- title and notes;
- open, completed, and archived states;
- optional due date and due time;
- all-day due dates;
- priority;
- tags or lists;
- recurrence;
- sorting and filtering;
- keyboard completion and editing;
- undo for completion and archive;
- overdue, today, upcoming, and unscheduled views.
- bounded CSV and one-title-per-line text-list import with a downloadable template.

Provider-backed tasks are deferred. The first task system is fully local so its behavior and
data model can stabilize before synchronization semantics are introduced.

### Watchlists

Watchlists is the authoring and status surface for news, deals, restocks, and webpage
changes. Each watch displays:

- a human-readable name;
- source type and exact origin;
- condition;
- polling interval;
- last checked and last changed times;
- current state or value;
- enabled, paused, error, and backoff state;
- notification or digest policy;
- recent alert history;
- test-now action.

Initial watch types are:

1. RSS or Atom feed subscription.
2. Feed keyword or phrase match.
3. Exact public webpage text change.
4. Exact webpage selector text change.
5. Structured product price below a threshold.
6. Structured product price percentage decrease.
7. Product availability or restock transition.
8. Optional changedetection.io connection for advanced page monitoring.

Watchlists do not execute arbitrary scripts, browser macros, purchases, form submissions,
or general webhooks.

### Inbox

Inbox is the durable attention queue for:

- watchlist alerts;
- scheduled digests;
- source failures requiring user action;
- connection-expiry warnings;
- task reminders.

Unread selected message attention is a session-only, count-only notice at the top of the
Hub window. Message summaries remain in the bounded Messages view and do not become
durable Inbox items.

Inbox items support unread, read, dismissed, and archived states. Dismissal does not delete
the originating task, watch, event, or provider message. Each item has a safe summary,
reason, source, timestamp, severity, and exact navigation target.

### Sources

Sources manages accounts and data inputs. It is deliberately separate from Settings.

Each source shows:

- provider and account label;
- capabilities available to the Hub;
- granted OAuth scopes or token purpose in plain language;
- last successful sync;
- current health;
- reconnect, disable, and disconnect actions;
- per-source refresh floor;
- retention policy;
- the screens using that source.

### Settings

Settings includes:

- startup and tray behavior;
- global offline mode;
- default timezone and week start;
- refresh and battery behavior;
- notification and quiet-hour policy;
- digest schedule;
- privacy masking;
- content-retention controls;
- accessibility options;
- backup and export;
- diagnostics;
- about, version, and update channel.

It contains no model, voice, planner, command, or assistant settings.

## Scope by Release

### Minimum viable product

- Separate Personal Hub executable and isolated storage.
- Today screen.
- Local tasks with due dates, priorities, recurrence, and reminders.
- Provider-neutral calendar with Google Calendar read support.
- RSS/Atom subscriptions and keyword filters.
- Durable alert Inbox.
- Windows notifications and quiet hours.
- Source health and global offline mode.
- Tray operation and start-at-login option.
- Backup, restore, and local data deletion.

### First complete release

- Outlook Calendar read support.
- Structured price and restock watches.
- Generic bounded webpage-change watches.
- Optional changedetection.io connector.
- Gmail and Outlook attention-inbox summaries.
- Slack selected-channel or saved-query summaries.
- Direct iCalendar feed import for read-only external calendar feeds.
- Canvas Student task import through a private iCalendar feed URL.
- Local CSV/text import for tasks and read-only calendar items with downloadable templates.
- Scheduled morning or evening digest.
- Full packaging, installer, update, and recovery paths.
- Optional encrypted shared-folder synchronization for Hub-owned local state; credentials and
  provider caches remain device-local.

### Later candidates

- CalDAV calendars and task providers.
- IMAP read-only mail.
- Provider task synchronization.
- Additional calendar write providers and background provider sync.
- Draft-only email workflows.
- Local-only LAN display mode.
- Hosted/mobile synchronization service; encrypted shared-folder synchronization is available
  as an optional local transport.
- Optional local summaries and relevance ranking.
- Optional assistant interoperability.

### Non-goals

- Conversational or voice commands.
- General-purpose computer control.
- Autonomous email or chat replies.
- Email deletion, mailbox cleanup, or bulk operations.
- Purchases, carts, checkout, or financial transactions.
- Arbitrary code, SQL, expressions, or user-authored scripts.
- Public inbound webhooks in the desktop process.
- A marketplace for executable third-party plugins.
- Mirroring entire mailboxes or chat workspaces locally.
- Persisting full webpage snapshots indefinitely.
- Training models on user data.

## Repository and Packaging Strategy

### Standalone repository layout

```text
personal-hub/
  pyproject.toml
  src/
    personal_hub/
      __init__.py
      app.py                       # Personal Hub CLI/desktop entry point
      bootstrap.py                 # Hub-only composition root
      config.py                    # Hub-only settings and data paths
      contracts.py                 # Shared Hub display/source contracts
      lifecycle.py                 # Startup, tray, shutdown, lock handling
      calendar/
      tasks/
      today/
      watches/
      feeds/
      messages/
      inbox/
      notifications/
      sources/
      storage/
      integrations/
      ui/
        desktop/
  tests/
    ui/
  packaging/
    personal-hub.spec
    personal-hub-installer.iss
  scripts/
  docs/
    PERSONAL_HUB_IMPLEMENTATION_PLAN.md
    PERSONAL_HUB_USER_GUIDE.md
    PERSONAL_HUB_PRIVACY.md
    PERSONAL_HUB_RELEASE_RUNBOOK.md
```

### Entry points

The standalone project declares only its own entry point:

```toml
[project.scripts]
personal-hub = "personal_hub.app:main"
```

Development launch:

```powershell
python -m personal_hub.app
```

Packaged artifact:

```text
PersonalHub.exe
```

### Data isolation

Recommended defaults:

```text
%LOCALAPPDATA%/LocalAssistant/      # Existing assistant
%LOCALAPPDATA%/PersonalHub/         # New Hub
  personal_hub.db
  secrets/
  logs/
  cache/
  backups/
```

The Hub must not point at `assistant.db`, even for development. Tests must use temporary Hub
directories and databases.

Credentials are not silently shared between products. A future explicit "Import connection"
workflow may exchange a provider account identity or initiate reauthorization, but it must
not copy encrypted files behind the user's back.

## Dependency Strategy

Personal Hub owns its application, domain, storage, provider, OAuth, security, lifecycle,
and UI contracts. It may depend on ordinary declared third-party packages, but it has no
Local Assistant dependency and does not rely on that repository being adjacent on disk.

```text
personal_hub UI -> personal_hub controllers/contracts -> personal_hub services/adapters
personal_hub -X-> assistant
assistant -X-> personal_hub
```

Concepts originally informed by reviewed Local Assistant behavior were implemented behind
Hub-owned contracts. Any later reuse must be extracted into a neutral package and consumed
through explicit versions by both products; neither application may import the other.

## High-Level Architecture

```text
                         +----------------------+
                         |   Personal Hub UI    |
                         | Today / Tasks / etc. |
                         +----------+-----------+
                                    |
                         typed application commands
                                    |
             +----------------------+----------------------+
             |                      |                      |
      +------v------+        +------v------+        +------v------+
      | Query       |        | Mutation    |        | Navigation  |
      | services    |        | services    |        | services    |
      +------+------+
             |                      |
      +------v----------------------v-----------------------+
      |             Hub domain and repositories            |
      | tasks / events / feeds / watches / alerts / inbox   |
      +------+----------------------+-----------------------+
             |                      |
      +------v------+        +------v-----------------------+
      | Scheduler   |        | Source registry and adapters |
      | and pollers |        | Google / Microsoft / RSS /   |
      +------+------+        | Slack / web / changedetection|
             |               +--------------+---------------+
             +------------------------------+
                                            |
                                    outbound network only
```

The UI issues typed application operations, such as `create_task`, `complete_task`,
`refresh_calendar`, `test_watch`, or `mark_inbox_item_read`. It never builds an assistant
prompt or generic command string.

## Core Contracts

All external and persisted data is validated with Pydantic models using
`extra="forbid"`. IDs are stable strings or UUIDs. Timestamps are timezone-aware UTC values
at storage boundaries and localized only for display.

### Source identity

```python
class SourceRef(BaseModel):
    provider_id: str
    connection_id: str | None
    resource_id: str | None
    display_name: str
    origin: str | None
```

The source reference appears on every remote event, message, feed item, watch observation,
and alert.

### Freshness state

```python
class Freshness(BaseModel):
    state: Literal[
        "ready",
        "refreshing",
        "stale",
        "offline",
        "permission_required",
        "disconnected",
        "empty",
        "error",
    ]
    collected_at: datetime | None
    expires_at: datetime | None
    safe_message: str | None
```

Provider errors shown to users are bounded and scrubbed. Raw response bodies, credentials,
and query secrets never enter `safe_message`.

### Today item

```python
class TodayItem(BaseModel):
    item_id: str
    kind: Literal["event", "task", "message", "alert", "news", "source_problem"]
    title: str
    summary: str | None
    starts_at: datetime | None
    due_at: datetime | None
    severity: Literal["info", "attention", "urgent"]
    source: SourceRef
    navigation_target: str
    sensitive: bool = False
```

Today is a projection over source domains. It does not own or duplicate their records.

### Task

```python
class HubTask(BaseModel):
    task_id: UUID
    title: str
    notes: str = ""
    state: Literal["open", "completed", "archived"]
    priority: Literal["none", "low", "normal", "high"]
    due_at: datetime | None
    due_is_all_day: bool
    timezone: str
    list_id: UUID | None
    tags: tuple[str, ...]
    recurrence: Recurrence | None
    reminder_offsets_minutes: tuple[int, ...]
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
```

Recurrence is represented by a bounded typed model for daily, weekly, monthly, and yearly
patterns. Do not accept arbitrary executable expressions. If RFC 5545 recurrence rules are
stored later, parse and validate a supported subset before persistence.

Completing a recurring task creates the next occurrence idempotently. A unique occurrence
key prevents duplicates after restart.

### Feed subscription and entry

```python
class FeedSubscription(BaseModel):
    subscription_id: UUID
    title: str
    feed_url: AnyHttpUrl
    site_url: AnyHttpUrl | None
    enabled: bool
    refresh_minutes: int
    include_terms: tuple[str, ...]
    exclude_terms: tuple[str, ...]
    digest_only: bool

class FeedEntry(BaseModel):
    entry_id: str
    subscription_id: UUID
    title: str
    url: AnyHttpUrl
    author: str | None
    published_at: datetime | None
    summary_text: str | None
    first_seen_at: datetime
```

HTML is converted to bounded safe text before storage. Remote images are not loaded by
default.

### Watch definition

```python
class WatchDefinition(BaseModel):
    watch_id: UUID
    name: str
    kind: Literal[
        "feed_match",
        "page_text",
        "page_selector",
        "product_price",
        "product_restock",
        "changedetection",
    ]
    source_url: AnyHttpUrl
    condition: WatchCondition
    refresh_minutes: int
    enabled: bool
    notification_policy: Literal["immediate", "digest", "inbox_only"]
    cooldown_minutes: int
    created_at: datetime
    updated_at: datetime
```

### Observation and alert

```python
class WatchObservation(BaseModel):
    watch_id: UUID
    checked_at: datetime
    fingerprint: str
    normalized_value: str | float | bool | None
    metadata: dict[str, str | float | bool | None]

class AlertEvent(BaseModel):
    alert_id: UUID
    watch_id: UUID
    reason_code: str
    title: str
    safe_summary: str
    previous_value: str | float | bool | None
    current_value: str | float | bool | None
    observed_at: datetime
    deduplication_key: str
```

Observations retain only the normalized value, bounded metadata, and fingerprint needed for
comparison. Full page bodies are transient unless the user explicitly opens a diagnostic
preview.

### Message summary

```python
class MessageSummary(BaseModel):
    message_id: str
    thread_id: str | None
    provider: Literal["gmail", "outlook", "slack"]
    account_id: str
    sender: str
    title: str
    preview: str | None
    received_at: datetime
    unread: bool
    importance: Literal["normal", "important"]
    deep_link: str | None
    sensitive: bool = True
```

The first release stores message summaries only long enough to support the selected view and
configured retention. Full bodies are fetched on explicit open and kept in memory unless the
user saves content through a later explicit feature.

## Storage Design

Use a Hub-owned SQLite database with SQLAlchemy and idempotent migrations. Enable foreign
keys, WAL mode where appropriate, bounded busy timeouts, and explicit backup/restore checks.

### Proposed tables

- `hub_settings`
- `hub_source_connections`
- `hub_source_sync_state`
- `hub_tasks`
- `hub_task_lists`
- `hub_task_tags`
- `hub_task_occurrences`
- `hub_calendar_layout`
- `hub_feed_subscriptions`
- `hub_feed_entries`
- `hub_feed_read_state`
- `hub_watches`
- `hub_watch_state`
- `hub_watch_observations`
- `hub_alert_events`
- `hub_inbox_items`
- `hub_message_summaries`
- `hub_digest_runs`
- `hub_notification_deliveries`
- `hub_audit_events`
- `hub_schema_migrations`

### Retention defaults

| Data | Default retention |
| --- | --- |
| Open tasks | Until completed or deleted |
| Completed tasks | 90 days, user configurable |
| Calendar snapshots | Memory only in MVP |
| Imported local calendar items | Until the named import is removed |
| Feed entries | 30 days or bounded item count |
| Watch observations | Latest value plus 30 days of triggered values |
| Full webpage content | Not persisted by default |
| Alert and Inbox history | 90 days |
| Message summaries | 7 days, user configurable down to session-only |
| Full message bodies | Memory only |
| Audit metadata | 30 days |
| Credentials | Until disconnect, encrypted at rest |
| Encrypted sync shards | Until the user removes the shared-folder copy or a device shard is replaced |

Retention cleanup is transactional, scheduled, bounded, and visible in diagnostics.

### Backup and restore

- Back up the SQLite database with SQLite's online backup API.
- Include non-secret settings, local tasks, explicitly imported calendar items, and watch
  definitions in an optional JSON export.
- Exclude credentials from ordinary export.
- Offer a separate encrypted credential export only after a dedicated threat review.
- Verify database integrity before replacing active data during restore.
- Restore into a temporary database, validate schema, then atomically switch.
- Never overwrite the Local Assistant database or data directory.

### Encrypted shared-folder synchronization

Personal Hub can synchronize a bounded subset of Hub-owned state through a user-selected shared
folder. The transport may be a local directory, removable drive, SMB share, or a user-managed
cloud folder such as OneDrive or Dropbox; the folder is only a file transport and is not a
Personal Hub account or hosted service.

- Each device publishes its own encrypted shard. The plaintext snapshot uses strict schema
  validation and is encrypted with a passphrase-derived AES-256-GCM key using Scrypt. The
  passphrase is entered for an explicit sync and is never persisted by Hub.
- The synchronized collections are local task lists and tasks, explicitly imported calendar
  items, feed subscriptions, watch definitions, and an allowlisted subset of shared preferences
  and view state.
- Credentials, vault contents, provider connection metadata, provider snapshots, message
  summaries and bodies, feed entries and read state, watch observations and alerts, Inbox
  history, and private iCalendar feed URLs are excluded. Sync never writes to a provider.
- Merging is deterministic UTC last-write-wins. Equal-time payload conflicts use a canonical
  JSON tie-breaker and are counted. Deletions use durable tombstones so a removed record cannot
  reappear from an older device shard.
- The first device establishes a profile identity. A new device pairs only with that profile;
  malformed, tampered, wrong-passphrase, or foreign-profile shards fail safely without
  replacing the local database.
- Sync is manual and explicit. It adds no inbound listener, provider network scope, or
  automatic background upload, and remains useful when the shared folder is unavailable.

## Provider Architecture

### Provider protocols

Personal Hub owns small protocols rather than exposing assistant skills to UI code:

```python
class CalendarSource(Protocol):
    async def status(self) -> SourceStatus: ...
    async def list_events(self, query: CalendarQuery) -> CalendarSnapshot: ...

class MessageSource(Protocol):
    async def status(self) -> SourceStatus: ...
    async def list_summaries(self, query: MessageQuery) -> MessageSnapshot: ...
    async def get_body(self, message_id: str) -> MessageBody: ...

class FeedSource(Protocol):
    async def fetch(self, subscription: FeedSubscription) -> FeedSnapshot: ...

class WatchCollector(Protocol):
    async def collect(self, definition: WatchDefinition) -> WatchObservation: ...
```

Provider adapters validate configuration, enforce fixed or reviewed origins, bound page and
item counts, honor cancellation, and return typed snapshots. They do not return credentials,
raw exceptions, or unbounded response bodies.

### Initial connectors

#### Google Calendar

- Desktop OAuth with PKCE.
- Read-only Calendar scope for the Hub OAuth client where possible.
- Selected calendars and bounded time windows.
- No event writes in MVP.
- In-memory last-good snapshot for stale/offline display.

#### Microsoft Outlook Calendar

- Microsoft Graph desktop/public-client OAuth.
- Minimum calendar-read scope.
- Bounded event windows.
- No event writes in first complete release.

#### Gmail

- Read-only or metadata scope sufficient for the selected UX.
- Explicitly selected unread folder/query.
- Bounded summaries, then explicit body fetch.
- No label changes, archive, trash, draft, reply, or send in the first release.
- No attachment download.

#### Outlook Mail

- Minimum Graph mail-read scope.
- Explicit folder and unread/importance filters.
- Bounded summaries and explicit body fetch.
- No draft or send operations.

#### Slack

- Selected channels or saved searches only.
- Bounded result windows.
- No broad workspace mirroring.
- No posting, reactions, or file upload.

#### RSS and Atom

- HTTPS by default; HTTP requires explicit warning and opt-in.
- Redirect and response-size limits.
- XML parser configured against entity expansion and external entities.
- Stable entry identity derived from feed GUID or canonical URL.
- Conditional requests with ETag and Last-Modified where supported.
- Per-origin refresh floors and exponential backoff.

#### Direct webpage monitoring

- Public HTTP(S) pages only in the initial release.
- DNS and redirect checks against local/private network targets unless a later LAN-source mode
  is explicitly enabled.
- Bounded download and render time.
- Selector or structured-data extraction, not arbitrary user code.
- Text normalization that excludes known volatile regions only through reviewed rules.
- Per-origin rate limiting, robots and terms awareness, and clear user responsibility.
- No login automation in the initial release.

#### changedetection.io

- Optional connection to a user-controlled instance.
- API token stored in the Hub vault.
- Exact configured origin.
- Read watch status/history and optionally create or update watches only after a separate
  write-scope design review.
- Treat remote watch output as untrusted data.

#### Canvas Student

- User supplies the private Canvas iCalendar feed URL; no Canvas API token or OAuth scope is
  requested.
- Store the full feed URL in the Hub secret vault because its path contains a bearer-like
  calendar token; persist only its HTTPS origin in ordinary settings.
- Import every bounded concrete `VEVENT` as one stable local task whose due date is `DTSTART`.
- Repeated refreshes update the same task and never reopen a completed or archived task.
- When the opt-in completed-assignment cleanup is enabled, a completed past-due assignment
  is removed from the local task list and leaves a durable Personal Hub deletion marker, so
  later refreshes and restarts do not recreate it as an open task.
- Due-dated imported tasks use the existing task-to-Calendar projection; Canvas is not given a
  calendar write or task write operation.
- Disconnect removes the private feed URL while retaining the local tasks already created.

## Polling, Scheduling, and Change Detection

### Scheduler requirements

- One scheduler service for all source types.
- A priority queue ordered by next eligible run.
- Per-source and per-origin concurrency limits.
- No second run while the same source is in flight.
- Minimum refresh floors enforced below the UI layer.
- Exponential backoff with jitter after recoverable failures.
- Authentication failures pause the source and request reconnection.
- Offline mode prevents network work before DNS or token refresh.
- Sleep and restart reconciliation does not replay every missed poll.
- Manual refresh bypasses due time but not permissions, offline mode, backoff safety, or
  concurrency limits.
- Battery-aware policy can suspend non-urgent refresh when configured.

### Polling defaults

| Source | Initial default | Minimum |
| --- | --- | --- |
| Calendar | 15 minutes | 5 minutes |
| Mail summaries | 10 minutes | 5 minutes |
| Slack summaries | 15 minutes | Provider-safe floor |
| RSS/Atom | 30 minutes | 15 minutes |
| Product price/restock | 60 minutes | 15 minutes |
| Generic webpage | 60 minutes | 30 minutes |
| Source health | 30 minutes | 5 minutes |

Defaults must be reviewed against provider policies before release.

### Change-evaluation pipeline

```text
fetch
-> validate response
-> extract bounded value
-> normalize
-> compute fingerprint
-> compare with prior state
-> evaluate typed condition
-> apply baseline/dedup/cooldown
-> persist observation and alert atomically
-> enqueue Inbox item
-> deliver notification or digest
```

The first successful observation establishes a baseline and does not alert unless the user
explicitly chooses "alert on current match." Failed observations do not replace the last
good baseline.

### Deduplication

An alert deduplication key includes:

- watch ID;
- reason code;
- normalized threshold bucket or state transition;
- stable source item ID where applicable.

Repeated identical matches update last-seen metadata but do not create repeated Inbox or
Windows notifications during cooldown.

## Notifications and Digests

### Delivery policies

Each source or watch chooses one:

- **Immediate:** Inbox plus Windows notification.
- **Digest:** Inbox event grouped into the next scheduled digest.
- **Inbox only:** No operating-system notification.

Urgency is rule-driven, not inferred by an AI model in the initial release.

### Quiet hours

- Quiet hours suppress ordinary Windows notifications.
- Suppressed items remain unread in Inbox.
- User-designated urgent rules may bypass quiet hours only after explicit opt-in.
- Connection failures and repeated source errors are grouped rather than repeated.

### Digest composition

The deterministic digest groups:

1. Overdue and due-today tasks.
2. Today's remaining calendar events.
3. New deal or restock alerts.
4. A count of selected unread messages for the in-app attention notice.
5. New feed entries grouped by subscription.
6. Sources needing attention.

It uses templates and bounded counts. An optional summarizer can replace or augment section
text later, but deterministic item lists remain available.

## Message and Email Safety

Messages and email are untrusted external content.

- Render plain text by default.
- Sanitize HTML using an allowlist before display.
- Block remote images, tracking pixels, scripts, forms, and embedded content.
- Make external links visible and require ordinary user navigation.
- Do not execute `mailto`, custom schemes, or attachments automatically.
- Do not treat message text as configuration, a rule, or authority.
- Do not include message bodies in logs, errors, diagnostics, notifications, or audit details.
- Do not send message content to any model unless the user enables a later summarization
  feature and approves its data boundary.
- Keep sender addresses and subjects out of Windows notification text when privacy masking is
  enabled.
- Do not cache full bodies by default.
- Never download attachments during list refresh.

The initial message view is an attention reader, not a mailbox replica. The canonical copy
remains with the provider.

## External Connectivity Model

Personal Hub uses outbound provider connections in its first release:

```text
Personal Hub -> provider HTTPS API -> bounded response -> local normalized state
```

This works behind ordinary home NAT and does not require opening a port.

### Push updates

Provider push subscriptions are deferred. Many cloud webhook systems require a publicly
reachable HTTPS callback, subscription renewal, and request-signature validation. Supporting
them safely would require either:

- a user-hosted public endpoint;
- an optional Personal Hub relay service;
- a provider-specific desktop push mechanism; or
- a secure tunnel managed outside the application.

Polling is the supported first-release strategy. A future push design must be optional and
must not make a Personal Hub cloud account mandatory for local features.

### Local inbound API

Do not expose a general local HTTP server initially. If later required for local automation,
prefer authenticated Windows IPC or a loopback-only API with:

- random per-install credentials;
- origin protection;
- explicit capability scopes;
- no wildcard CORS;
- request-size and rate limits;
- audit metadata;
- a visible enable/disable control;
- no remote network binding by default.

## Security and Privacy Model

### Threats

- Malicious HTML, XML, feed data, messages, and webpages.
- Prompt injection if optional AI features are added later.
- OAuth token or API-key disclosure.
- SSRF through user-entered URLs and redirects.
- Notification leakage on a locked or shared screen.
- Excessive provider permissions.
- Unbounded polling, response sizes, or storage growth.
- Duplicate alerts after crash or restart.
- Malicious deep links or custom URL schemes.
- Cross-application data leakage between Personal Hub and Local Assistant.
- Local database corruption or partial migrations.

### Required controls

- Separate Hub data and secret roots.
- DPAPI-bound encryption for credentials on Windows.
- Provider-specific minimum OAuth scopes.
- Fixed-origin or reviewed-origin network clients.
- Redirect revalidation and private-address blocking for public web watches.
- Response byte, item count, pagination, and timeout limits.
- Schema validation at every provider boundary.
- HTML and XML hardening.
- Safe error summaries.
- Offline mode checked before token refresh or network access.
- Transactional observation-to-alert persistence.
- Idempotency keys for recurring tasks, alert delivery, and digest runs.
- Privacy masking for screen and notifications.
- Explicit retention settings and immediate local deletion workflows.
- No secrets or private bodies in logs, metrics, crash reports, or exports.
- No assistant/model access in the default Hub process.

### Permission model

Connection, collection, display, notification, and mutation are distinct concepts.

The MVP needs a simpler permission UX than Local Assistant, but it must preserve exact
boundaries:

- Connect Google account.
- Allow Calendar read for selected calendars.
- Allow Gmail summary read for a selected query.
- Allow Windows notifications.
- Allow a watch to contact one exact origin.

External write scopes should not be requested until a shipping feature requires them.

## Offline Behavior

Personal Hub is local-first, not network-free.

When offline mode is enabled:

- local tasks remain fully available;
- cached feed entries and alert history remain available;
- last-good calendar and message summaries display as stale if retained;
- no DNS, token refresh, provider API, feed, page, or update request is attempted;
- source cards explain that refresh is paused by offline mode;
- scheduled network polls advance safely without building an unbounded backlog;
- local reminders and deterministic digests still run from available data.
- encrypted shared-folder synchronization is available only as an explicit local file operation
  when the user-selected folder is reachable; offline mode never creates a Personal Hub network
  relay or bypasses provider/network gating.

## Desktop Lifecycle

### Startup

1. Acquire the Personal Hub single-instance lock.
2. Load and validate Hub settings.
3. Open and migrate the Hub database.
4. Initialize the Hub secret vault.
5. Register source adapters.
6. Restore source and scheduler state.
7. Reconcile due local reminders and polls.
8. Show Home or start hidden in tray according to settings.

No language model, audio model, browser engine, or assistant skill package is preloaded.

### Tray behavior

Tray actions:

- Open Personal Hub.
- Open Home.
- Open Inbox.
- Refresh enabled sources.
- Pause or resume network refresh.
- Toggle offline mode.
- Quit.

Closing the window hides to tray only when the setting is enabled. Quit shuts down pollers,
waits for bounded cancellation, checkpoints the database, and removes sensitive in-memory
content.

### Sleep, lock, and restart

- On lock, hide sensitive previews and optionally pause network refresh.
- On sleep, stop scheduling new work and cancel or bound active work.
- On resume, perform jittered health refresh rather than simultaneous full refresh.
- On restart, do not replay missed news or page polls one by one.
- Local task reminders use durable occurrence identities to avoid duplicates.

## Performance and Reliability Budgets

Initial engineering targets:

| Measure | Target |
| --- | --- |
| Warm UI launch | Under 2 seconds on reference hardware |
| Idle memory without active refresh | Under 200 MB |
| Today navigation response | Under 100 ms from local state |
| Local task mutation | Under 100 ms p95 |
| UI thread network work | Zero |
| External request timeout | Provider-specific, normally 10-30 seconds |
| Concurrent external refreshes | Maximum 2 initially |
| Feed response limit | Bounded and configurable below 5 MB |
| Generic webpage response limit | Bounded and configurable below 10 MB |
| Startup with unavailable providers | Fully usable local UI |
| Duplicate notification after clean restart | Zero |

Targets should be measured with benchmark scripts before release rather than treated as
documentation-only claims.

## Observability and Diagnostics

Record safe operational metadata:

- source type and opaque source ID;
- refresh start/end and outcome;
- duration and item count;
- response-size bucket;
- backoff state;
- freshness transitions;
- alert reason code;
- notification delivery outcome;
- database migration and cleanup outcomes;
- application version and schema version.

Do not record:

- task notes;
- event titles or locations;
- message sender, subject, preview, or body;
- feed article titles;
- watched page text;
- full URLs containing query strings;
- OAuth tokens, API keys, authorization codes, or cookies.

Diagnostics provides a user-copyable redacted report and per-source "last successful sync"
information. Telemetry is local-only unless a later separate opt-in design is approved.

## Testing Strategy

### Unit tests

- Task validation, recurrence, reminders, and idempotent next occurrence.
- Feed parsing, identity, sanitization, filters, and conditional requests.
- Watch normalization, fingerprinting, thresholds, state transitions, and cooldowns.
- Alert deduplication and Inbox state.
- Message HTML sanitization and privacy masking.
- Today projection ordering and limits.
- Retention cleanup.
- Settings and URL validation.
- Provider error normalization.

### Contract tests

Every provider runs against the same protocol suite:

- ready, empty, stale, disconnected, offline, and error states;
- cancellation;
- timeout;
- bounded pagination;
- malformed item omission;
- no secret-bearing output;
- stable source identity;
- deterministic item ordering.

### Integration tests

Use fake HTTP servers, fake OAuth stores, and recorded schema-shaped fixtures with no real
private content. Tests remain offline and deterministic.

- Google calendar mapping and pagination.
- Gmail summary and explicit body fetch.
- Microsoft calendar and mail mapping.
- Slack bounded channel/query results.
- iCalendar feed parsing, bounded refresh, and safe failure mapping.
- Canvas iCalendar-to-task mapping, stable deduplication, vault isolation, and completion
  preservation.
- RSS redirects, ETags, malformed XML, and oversize responses.
- Product structured-data extraction.
- changedetection.io API mapping.
- scheduler backoff, sleep/resume, and offline lock.
- database backup/restore and migration rollback.

### UI tests

- Keyboard-only navigation across every primary surface.
- Today sections at minimum supported window size.
- Task add/edit/complete/undo.
- Calendar month/day/hourly week/agenda views, including slot-based local task scheduling.
- Watch authoring validation and test result.
- Inbox read/dismiss/archive.
- source connect/reconnect/disconnect states;
- privacy masking and locked state;
- high contrast, text scaling, and reduced motion;
- slow or failing source never freezes other screens.

### Security tests

- SSRF attempts through direct URLs and redirect chains.
- XML external entities and entity expansion.
- Malicious HTML, remote images, scripts, forms, and custom schemes.
- Credential-shaped strings in logs and error paths.
- Path traversal in cache, export, and backup names.
- Cross-database and cross-vault isolation from Local Assistant.
- OAuth state, PKCE, callback, and token-refresh validation.
- Notification privacy masking.
- Untrusted content cannot mutate rules or settings.

### Reliability tests

- Repeated sleep/resume.
- Network loss during every collector stage.
- Provider rate limiting and expiring credentials.
- Process termination between observation and notification.
- Corrupt cache with healthy database.
- Corrupt database recovery path.
- Clock and timezone changes.
- Daylight-saving transitions for tasks and digests.
- 24-hour tray soak with active sources.
- Seven-day bounded-state growth test with synthetic feeds and watches.

### Architecture tests

- Hub source never imports `assistant`.
- Hub UI imports only Hub controllers/contracts.
- Hub startup does not import any Assistant, STT, TTS, model, router, or orchestration module.
- Hub packaging excludes assistant-only heavy optional dependencies.
- Hub data paths cannot resolve to the Local Assistant data path by default.

### Local iteration lanes

The Hub test suite is collected into stable markers by `tests/conftest.py` so normal
iteration can stay focused. Use `scripts/test_personal_hub.ps1` for the supported lanes:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/test_personal_hub.ps1                 # fast core lane
powershell -ExecutionPolicy Bypass -File scripts/test_personal_hub.ps1 -Lane phase3    # one phase
powershell -ExecutionPolicy Bypass -File scripts/test_personal_hub.ps1 -Lane integration
powershell -ExecutionPolicy Bypass -File scripts/test_personal_hub.ps1 -Lane architecture
powershell -ExecutionPolicy Bypass -File scripts/test_personal_hub.ps1 -Lane ui
powershell -ExecutionPolicy Bypass -File scripts/test_personal_hub.ps1 -Lane release
powershell -ExecutionPolicy Bypass -File scripts/test_personal_hub.ps1 -Lane all
```

The default fast lane ignores the Qt test directory and excludes only explicitly marked
release-slow checks. A full `-Lane all` run remains required before release; focused phase,
integration, architecture, and UI lanes are the expected development workflow.

## Implementation Phases

Each phase ends with a runnable vertical slice and explicit acceptance gate. Do not build all
provider integrations before the local product loop is usable.

### Phase 0: Boundary proof and application skeleton

Deliverables:

- `src/personal_hub/` package.
- Hub settings with isolated data path.
- Hub-only composition root and container.
- Minimal PySide6 main window with Today placeholder.
- Separate entry point.
- Separate process identity and single-instance lock.
- Architecture-boundary tests.
- Packaging dependency report proving assistant models/audio are not loaded.

Acceptance gate:

- Local Assistant starts and behaves exactly as before.
- Personal Hub starts independently with an empty temporary database.
- Both applications can run at the same time without sharing state or locks.
- Personal Hub imports no assistant conversational, voice, or model stack.

### Phase 1: Hub storage, shell, Inbox, and lifecycle

Deliverables:

- Hub database and migration mechanism.
- settings, source health, Inbox, audit, and notification repositories.
- main navigation and responsive shell.
- tray, startup, lock, sleep/resume, and shutdown behavior.
- offline mode, privacy masking, quiet hours, and diagnostics skeleton.
- backup and restore foundation.

Acceptance gate:

- Inbox state survives restart.
- offline mode prevents all test network adapters before connection.
- a fake source failure appears safely without freezing UI.
- backup and restore round-trip an isolated Hub database.

### Phase 2: Native tasks and Today

Deliverables:

- full Hub task schema and repository.
- task lists, tags, filters, priorities, due dates, recurrence, and reminders.
- Tasks UI.
- Today projection and ordered sections.
- task reminder notifications and Inbox items.
- deterministic daily digest foundation.

Acceptance gate:

- user can add, edit, complete, undo, archive, and filter without commands.
- recurring tasks create exactly one next occurrence across restart.
- overdue and due-today tasks appear correctly across timezone and DST tests.
- Today remains responsive with at least 10,000 synthetic completed tasks.

### Phase 3: Calendar

Deliverables:

- Hub calendar protocol and service.
- month, day, hourly week, and agenda UI.
- Google Calendar read-only connection.
- selected calendars, freshness, masking, and offline behavior.
- Today event projection.
- Outlook Calendar read adapter after Google acceptance.

Acceptance gate:

- bounded multi-calendar view works with timed and all-day events.
- due-dated local tasks appear in the correct display day and hour, and an hourly slot opens a
  local scheduled-task editor.
- disconnected, permission, stale, offline, empty, and error states are distinct.
- calendar writes remain unavailable by default; the October 9 extension permits only
  explicit, reauthorized synchronization of individually linked local Google events.
- calendar content is not stored in logs, metrics, or audit details.

### Phase 4: News feeds

Deliverables:

- secure RSS/Atom fetcher and parser.
- subscription and feed-entry storage.
- keyword include/exclude filters.
- News section in Today and Watchlists.
- read/unread state, deep links, and digest grouping.
- ETag/Last-Modified support and per-origin backoff.

Acceptance gate:

- malformed or hostile XML fails safely.
- duplicate entries are not created after restart or feed reordering.
- high-volume feeds remain bounded by count and retention.
- one failed feed does not stale successful feeds.

### Phase 5: Deal, restock, and webpage watches

Deliverables:

- watch definitions, observations, alert events, and scheduler integration.
- structured product-data extraction.
- page text and selector watches.
- price threshold, percentage change, and restock conditions.
- baseline, deduplication, cooldown, and test-now flows.
- optional changedetection.io read integration.

Acceptance gate:

- first observation establishes a baseline without false alert.
- identical observations create no duplicate alerts.
- a real typed state transition creates one explainable Inbox item.
- SSRF, redirect, size, timeout, and private-network tests pass.
- watches do not execute scripts or submit forms.

### Phase 6: Attention messages

Deliverables:

- Hub message-source protocol.
- Gmail unread or selected-query summaries.
- Outlook unread folder summaries.
- selected Slack channel/query summaries.
- explicit in-memory body view.
- deterministic Gmail-style Primary, Promotions, Social, Updates, and Spam categories;
  category filters are local display state and never mutate provider mail.
- sanitization, remote-content blocking, masking, and deep links.
- message sections in Today and a session-only count notice at the top of the Hub window.

Acceptance gate:

- only configured folders, channels, or queries are read.
- body content is fetched only after explicit open.
- no send, reply, mailbox mutation, reaction, or upload is available.
- bodies and sensitive metadata do not appear in logs or Windows notifications when masked;
  the in-app attention notice contains only bounded counts and is not persisted in Inbox.

### Phase 7: Packaging and release hardening

Deliverables:

- `PersonalHub.exe` PyInstaller specification.
- separate Inno Setup installer and uninstall identity.
- start-at-login option.
- update manifest, signing, SBOM, and provenance support.
- privacy and user documentation.
- accessibility review.
- 24-hour and seven-day reliability evidence.
- clean-install, upgrade, rollback, and uninstall verification.

Acceptance gate:

- installing or uninstalling Personal Hub does not alter Local Assistant.
- both applications can coexist and update independently.
- packaged Hub excludes assistant-only model/audio stacks.
- clean uninstall removes Hub binaries and startup registration while preserving or deleting
  user data according to an explicit choice.

### Phase 8: Optional intelligence

Begin only after the non-AI product has stable daily use.

Possible features:

- local digest summarization;
- duplicate-topic clustering;
- explainable importance suggestions;
- natural-language search over already visible Hub data;
- draft-only suggestions that never send automatically.

Required design:

```python
class SummaryProvider(Protocol):
    async def summarize(self, request: SummaryRequest) -> SummaryResult: ...
```

Rules:

- optional and disabled by default;
- core screens work without it;
- local provider preferred;
- remote provider requires explicit connection and per-source content approval;
- content sent to a model is previewed or clearly described;
- summaries retain citations to source items;
- untrusted source text cannot change permissions, destinations, rules, or settings;
- deterministic fallback remains available;
- no dependency on Local Assistant transcripts or runtime.

## Acceptance Workflows

### Fresh local use

1. Install Personal Hub without Local Assistant.
2. Launch with network offline.
3. Add three tasks, including one recurring task and one due today.
4. Restart.
5. Verify Today and Tasks are complete and responsive.
6. Complete the recurring task.
7. Verify one next occurrence and one Inbox record.

### Calendar connection

1. Connect a Google account with Hub calendar-read access.
2. Select one calendar.
3. Sync a bounded week.
4. Verify events appear in Calendar and Today.
5. Enable privacy masking and verify titles/locations are hidden.
6. Enable offline mode and verify the last-good snapshot becomes stale without network work.
7. Disconnect and verify credentials and source data are cleared according to policy.

### News digest

1. Add two RSS feeds and one keyword filter.
2. Refresh.
3. Verify stable deduplication and source labels.
4. Schedule a morning digest.
5. Simulate restart before delivery.
6. Verify exactly one digest and no duplicate feed items.

### Deal alert

1. Add an exact product URL with price-below threshold.
2. Test extraction and save.
3. Establish baseline above threshold.
4. Observe a price below threshold.
5. Verify one alert explains old value, new value, threshold, source, and observation time.
6. Repeat the same observation during cooldown and verify no duplicate notification.

### Message attention view

1. Connect Gmail with minimum approved read scope.
2. Configure unread Inbox with a limit.
3. Verify summaries appear without fetching bodies for every row.
4. Open one message and fetch its sanitized body.
5. Verify remote images and scripts do not load.
6. Verify there is no reply, send, delete, archive, or label action.
7. Clear retained summaries and verify provider mail remains untouched.

### Coexistence

1. Install Local Assistant and Personal Hub.
2. Run both simultaneously.
3. Connect different test accounts.
4. Change settings and tasks in each product.
5. Upgrade and uninstall Personal Hub.
6. Verify Local Assistant settings, database, credentials, startup entry, and behavior are
   unchanged.

## Release Criteria

Personal Hub 1.0 is ready only when:

- all MVP and first-complete-release features selected for 1.0 have acceptance evidence;
- Local Assistant regression tests remain green;
- Hub tests are offline and deterministic by default;
- no assistant model, voice, router, or command dependency loads in Hub startup;
- data, credentials, process identity, installer, and update channels are isolated;
- external providers use minimum read scopes for shipped read features;
- retention and deletion behavior is documented and tested;
- hostile feed, HTML, URL, and redirect tests pass;
- source failures cannot freeze the UI or erase last-good state;
- notification deduplication survives restart;
- accessibility checks pass at the minimum window size and supported text scales;
- clean install, upgrade, backup, restore, rollback, and uninstall are verified;
- SBOM, release manifest, signatures, and provenance are generated;
- the user guide accurately distinguishes local data, cached remote data, and provider data.

## Risks and Mitigations

| Risk | Mitigation |
| --- | --- |
| Hub becomes another assistant UI | Ban prompt/command surfaces from initial information architecture |
| Shared code destabilizes Local Assistant | Additive package, Hub-owned adapters, architecture tests, no assistant imports of Hub |
| Duplicated integration code drifts | Reuse reviewed provider clients behind narrow Hub protocols; extract shared package only after contracts stabilize |
| Email scope turns into full-client scope | Attention summaries and explicit body view only; no write operations in initial release |
| Alerts become noisy | Baselines, typed thresholds, cooldowns, deduplication, digests, and quiet hours |
| Web watches break frequently | Prefer RSS and structured data; show extraction health; optional changedetection.io integration |
| Provider polling causes rate limits | Minimum refresh floors, conditional requests, backoff, jitter, and per-origin concurrency |
| Sensitive content leaks on screen | Privacy mask, lock response, safe notification summaries, remote-image blocking |
| Sensitive content leaks to logs | Metadata-only observability and automated secret/private-content scans |
| Product requires a cloud relay too early | Outbound polling first; defer push and inbound APIs |
| Local database grows indefinitely | Bounded histories, count limits, retention jobs, diagnostics |
| Recurrence duplicates after restart | Occurrence identities, transactions, idempotency tests |
| Two installed products collide | Separate app IDs, paths, databases, secrets, mutexes, startup entries, installers, and update channels |
| Optional AI introduces prompt injection | Separate provider protocol, citations, no authority, explicit content boundary, deterministic fallback |

## Decisions to Confirm Before Phase 1

The plan recommends defaults so implementation can begin without waiting on every product
decision.

| Decision | Recommended default |
| --- | --- |
| Product name | Personal Hub as working name; rename before signing/installer freeze if desired |
| Code location | `src/personal_hub/` in the standalone Personal Hub repository |
| Data sharing with Local Assistant | None by default |
| UI technology | PySide6 to match existing Windows expertise and dependencies |
| Initial task source | Local only |
| Initial calendar | Google Calendar read-only, then Outlook |
| Initial news source | RSS/Atom |
| Initial watch implementation | Direct structured/text watches plus optional changedetection.io adapter |
| Initial messages | Gmail/Outlook summaries; Slack after mail contracts stabilize |
| Refresh strategy | Polling while process/tray runtime is active |
| Public inbound endpoint | None |
| AI dependency | None through 1.0 core |
| External writes | None by default; explicit opt-in Google event sync extension |
| Default message-body retention | Memory only |
| Default remote-image policy | Blocked |
| Default notification policy | Digest for news; immediate only for user-selected task/deal rules |

## Repository Extraction

The completed Phase 0–7 implementation was extracted into a standalone, repository-ready
directory on September 7, 2026. Project metadata, CI, source, tests, packaging, scripts, and
documentation now live under one root. See `PERSONAL_HUB_REPOSITORY_MIGRATION.md` for the
migration evidence and the remaining external release tests.

## Definition of Done for the Plan

This plan is successfully executed when Personal Hub is a useful standalone local desktop
product that can be installed without Local Assistant, can coexist with Local Assistant,
does not expose commands or require AI, and reliably presents a user's chosen tasks,
calendar events, news, watched changes, and bounded message summaries with explicit
freshness, privacy, and notification controls.

The later assistant roadmap remains available, but Personal Hub 1.0 does not depend on it.
