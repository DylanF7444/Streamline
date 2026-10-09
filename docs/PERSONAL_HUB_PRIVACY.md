# Personal Hub privacy and data boundary

Personal Hub is local-first and uses a data, credential, process, installer, and update
boundary independent from Local Assistant. Provider content and model output are treated as
untrusted. Personal Hub 1.0 has no AI model integration.

## Data categories

| Category | Examples | Location and lifetime |
| --- | --- | --- |
| Hub-native local data | tasks, lists, tags, settings, feed subscriptions, watch definitions, Inbox state, imported and manually created calendar items, linked event changes, and provider-task deletion markers | Hub SQLite database under `%LOCALAPPDATA%\PersonalHub`; retained until explicit deletion or configured cleanup |
| Retained remote summaries | feed entries, watch observations/alerts, selected message headers/previews | Hub SQLite database; bounded by age and count |
| Memory-only provider content | connected-calendar snapshots and explicitly opened message bodies | Process memory only; cleared on lock/clear/shutdown/exit |
| Provider-canonical data | Canvas calendar items, calendar events, email, Slack messages, and public pages | Remains at the provider; Hub read adapters do not mutate it |
| Credentials | OAuth access/refresh tokens, the private Canvas iCalendar URL, or optional integration token | Hub-only encrypted vault/DPAPI namespace until disconnect |
| Operational metadata | source state, last success, safe failure class, notification claims, audit event type | Hub database/log boundary; no titles, bodies, tokens, or raw provider errors |
| Backups | Hub SQLite data and non-secret state | User-selected or Hub backup directory; ordinary backups exclude credentials |
| JSON exports | bounded visible local data and retained summaries | Explicit user-selected file; excludes credentials, data paths, message bodies, and calendar snapshots |
| Encrypted sync shards | selected Hub-owned local state for cross-device use | User-selected shared folder; each device shard is passphrase-encrypted and replaced on successful sync |

Local Assistant has a different default data root, `assistant.db`, vault namespace, mutex,
startup value, executable, installer ID, and update channel. Hub path validation rejects an
overlap with the default Local Assistant database/data root.

## Default retention

- Inbox: 90 days for cleanup-eligible history, configurable from 1–365 days.
- Audit metadata: 30 days, configurable from 1–365 days.
- Feed entries: 30 days and 500 items per subscription by default; configurable within
  enforced bounds.
- Watch observations: 90 days and 500 observations per watch by default; full pages are not
  persisted.
- Message summaries: 7 days and 500 summaries per source by default. Retention 0 is
  session-only; the maximum is 30 days.
- Message bodies: never stored in the database, cache, backup, audit, log, or notification.
- Connected-calendar snapshots: memory only. Explicitly imported local calendar items are
  stored in the Hub database until the user removes that import.
- Calendar event completion state: not stored. Provider snapshots remain memory-only, while
  completion for due-dated to-do items is stored on the underlying local task record.
- Credentials: until disconnect, encrypted for the current Windows user.

Cleanup is bounded, transactional, and scheduled once per local day even while offline. The
newest watch observation remains as a transition baseline when older watch history expires.
Clearing local summaries or uninstalling with data
deletion does not delete or change provider-canonical email, chat, calendar, or webpage data.

## Collection limits

Remote access is outbound and explicit. Only enabled sources and configured calendars,
folders, channels, queries, feeds, or exact public URLs are selected. Requests
have fixed provider endpoints or validated exact origins, concurrency and pagination limits,
response size/time bounds, cancellation, refresh floors, and safe backoff.

Direct feed/watch URLs are checked before DNS/transport use and across redirects. Private,
loopback, link-local, multicast, credential-bearing, or unsafe-scheme targets are rejected by
default. RSS XML disables external entities/entity expansion. Watched HTML is parsed as inert
content and cannot execute scripts, submit forms, automate login, or alter Hub settings.

## Minimum permissions

- Google Calendar: `https://www.googleapis.com/auth/calendar.readonly` by default; optional
  `https://www.googleapis.com/auth/calendar.events` only after explicit sync opt-in and
  reauthorization.
- Outlook Calendar: `Calendars.Read`.
- Gmail: `https://www.googleapis.com/auth/gmail.readonly`.
- Outlook Mail: `Mail.Read`.
- Slack: only the selected subset of `channels:history`, `groups:history`, and `search:read`.
- Canvas Student: private HTTPS iCalendar feed only; no Canvas API or OAuth scope.

Read adapters retain their exact read scopes. A separate Google sync path can insert and
patch individually selected local events, only during an explicit **Sync Google** action.
It uses fixed HTTPS endpoints, bounded JSON, redirect rejection, stable retry identities,
linked-event ownership markers, and version preconditions. It cannot send invitations,
respond to events, delete remote events, or modify unlinked events. Mail send, reply, draft,
label, move, delete, Slack post/reaction/upload, form submission, and purchase operations
remain unavailable.

Manually created calendar items and linked Google changes are stored with local imported
calendar items in the Hub database, backups, and explicit exports until local removal.
Google event IDs, calendar IDs, version markers, and sync fingerprints are device-local
settings; they contain no tokens or copies of notes. The provider event write scope is broader
than the app's linked-event policy and is shown at Google consent. Disabling sync or going
offline prevents credential and provider access by the sync path. Conflicts preserve the local and provider
versions until the user chooses a resolution. Google removal retains the local copy and
stops its outbound sync; local removal or stopping sync retains the Google copy. Ordinary
provider snapshots remain memory-only. Shared-folder sync includes the local item, but
excludes these Google bindings and credentials, so receiving another device's item does
not authorize provider writes.

Canvas iCalendar paths contain bearer-like calendar tokens. The full URL is encrypted in the
Hub vault and excluded from ordinary settings, logs, diagnostics, and exports. Canvas event
titles, descriptions, locations, and safe event links become local task fields. Refresh updates
those local tasks but never submits, edits, or completes work in Canvas. Disconnect deletes the
saved feed URL and retains the already-created local tasks. When completed past-due Canvas
assignments are removed by the opt-in cleanup setting, Personal Hub retains only a safe,
provider-scoped deletion marker so a later refresh cannot recreate the local task; the marker
does not change Canvas data.

## Credentials and OAuth

Desktop OAuth uses PKCE, a short-lived authorization TTL, exact state verification, and exact
approved scopes. The verifier and authorization state are memory-only. OAuth and integration tokens
are written only to the `PersonalHub` vault namespace; production Windows encryption uses current-user
DPAPI. Raw tokens, authorization codes, provider payloads, client secrets, and credential-
shaped strings are excluded from diagnostics and error messages. Ordinary database backup
does not include the secret vault.

The explicit JSON export includes user-visible local content, including imported calendar items,
and retained remote summaries, so
users should protect it like any personal-data export. It
never reads the secret vault or the memory-only message-body and connected-calendar snapshot
caches.

## Encrypted sync boundary

The optional **Sync across devices** feature uses a user-selected folder as a file transport.
Each device writes its own bounded shard beneath `.personal-hub-sync`; the snapshot is encrypted
with a passphrase-derived Scrypt key and AES-256-GCM. The passphrase is held only for the
explicit sync operation. The folder may be local, removable, SMB-backed, or managed by a cloud
file provider, but Personal Hub does not operate a hosted sync account, inbound listener, or
provider write path.

The encrypted snapshot may contain local task lists and tasks, explicitly imported calendar
items, feed subscriptions, watch definitions, and an allowlisted subset of shared preferences
and view state. It never contains vault credentials, OAuth tokens, provider connection metadata,
provider snapshots, message summaries or bodies, feed entries/read state, watch observations or
alerts, Inbox history, or private iCalendar feed URLs. The shard filename contains only a
random device identity; user records are inside the encrypted payload.

Sync pairs devices to one Hub profile and merges with deterministic UTC last-write-wins rules.
Durable tombstones carry deletions, equal-time payload conflicts are counted, and malformed,
tampered, wrong-passphrase, or foreign-profile shards are rejected before the local database is
changed. The shared-folder provider's account permissions and retention policies remain outside
Personal Hub's control, so users should protect the folder as they would any encrypted backup.

## Display and notifications

Privacy masking hides sensitive calendar, message, and Inbox fields. Session lock overlays
the content area, clears memory-only message bodies, and can pause network work. HTML message
bodies become bounded plain text; remote images, scripts, forms, embeds, and active links do
not render. Valid HTTP(S) links are shown inertly and open only after an explicit action.
Message categories are computed locally from bounded provider labels/categories and the already
retained sender, subject, and preview; choosing a category never changes provider mail. The
count-only attention notice at the top of the Hub window contains no provider subject, sender,
preview, or body, is held only for the current process session, and is never written to Inbox.

Windows notifications use durable, content-free delivery claims. Immediate watch alerts and
task reminders are handed to the Hub tray only after the enabled setting and local quiet hours
are evaluated. When masking or session lock applies, fixed generic text replaces the sensitive
title and summary. Delivered, unavailable, and policy-suppressed outcomes are recorded without
notification content; quiet-hours suppression never erases the durable Inbox item. Claims are
deduplicated across restart. The in-app message attention notice follows the separate
session-only behavior described above and does not use durable notification claims.

## Offline mode

Offline mode is persisted and checked before token retrieval, DNS, HTTP, or provider adapter
work. Local tasks, retained summaries, and the Inbox remain usable. Last-good memory snapshots
can be marked offline or stale. Turning offline mode off allows later refresh; it does not
replay every missed interval. An explicit local sync can still run when its shared folder is
reachable; it does not create a network relay or bypass provider/network gating.

## Deletion and uninstall

Users can clear Hub message summaries without mutating provider content. Disconnect removes
the Hub-scoped credential according to the connector policy. The Personal Hub uninstaller
always removes its binaries and startup value and presents an explicit choice:

- preserve the Hub data root for a later reinstall; or
- delete Hub settings, tasks, retained summaries, watch history, credentials, logs, and
  backups.

Silent uninstall preserves data unless explicitly given `/DELETEUSERDATA=yes`. Installer and
uninstaller logic never names or deletes Local Assistant artifacts.

## Logs, diagnostics, and telemetry

Diagnostics contain application/schema versions, source identity, state, last-success time,
bounded counts, and fixed safe messages. They exclude task notes, event titles/locations,
message subjects/previews/bodies, feed/page content, URLs containing credentials, and raw
exceptions. Telemetry is local-only; there is no default remote telemetry channel.

## Updates and supply chain

The update boundary accepts only the `personal-hub` product on the
`personal-hub-stable` channel, validates a bounded credential-free HTTPS manifest, verifies a
trusted Ed25519 signature, rejects downgrade/schema incompatibility, and verifies artifact
size and SHA-256 before staging. Apply and rollback touch only `PersonalHub.exe` and
`PersonalHub.previous.exe`. Release tooling produces checksums, a CycloneDX SBOM, release
manifest, and SLSA/in-toto provenance. Public releases also require Authenticode and external
release-key signatures.

## Current evidence boundary

Deterministic tests use synthetic provider-shaped data and ephemeral cryptographic keys. They
do not prove a real Google, Microsoft, or Slack tenant/test instance; possession of a
production signing key; a 24-hour elapsed tray soak; or a clean-VM user experience. Those checks are mandatory in
`PERSONAL_HUB_RELEASE_RUNBOOK.md` and must not be inferred from offline test success.
