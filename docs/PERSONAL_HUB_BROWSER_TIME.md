# Browser site screen time

Implemented November 10, 2026 for Windows Chrome and Microsoft Edge. This optional
extension adds hostname-level review to Logging > Screen time. It requires no account,
cloud service, AI, page scripts, browser-history access or network listener.

## Setup and daily use

1. In Logging > Screen time, scroll to Browser sites and choose **Set up browser extension**.
   Choose a persistent local folder. Hub exports the four bundled extension files into
   its PersonalHubBrowserTime subfolder.
2. Open chrome://extensions or edge://extensions, enable Developer mode, choose **Load
   unpacked**, and select that subfolder. Copy its 32-letter extension ID.
3. Continue the Hub setup dialog: select the browser and paste its extension ID. Packaged
   installs find PersonalHubBrowserHost.exe beside PersonalHub.exe. Development installs
   find dist/PersonalHubBrowserHost.exe or ask you to select it. No administrator is needed.
4. Open the extension popup and choose **Enable browser tracking**. The browser requests
   tabs and nativeMessaging permissions. Tab metadata permission can expose URLs and titles;
   this collector reads only the active tab URL and immediately reduces it to its hostname.
5. Choose **Start browser recording** in Hub. Both consent controls are required. Browser
   consent persists; Hub recording requires a fresh Start after launch, Hub session lock or restore.
   App recording is independent. Either recorder can operate without the other.

The helper is built in the normal release script. For a development checkout:

```powershell
python -m pip install -e ".[desktop,build,test,dev,ui-test]"
python -m PyInstaller --clean --noconfirm packaging/personal-hub-browser-host.spec
python -m personal_hub.app
```

Use **Pause browser recording** to stop collection and remove the live mailbox. Extension
pause stops sending. **Disconnect browser helper** removes both browser registrations and
revokes the live lease, while retaining history. Remove the extension through the browser's
extensions page. The installer removes native-host registry entries at uninstall.
Setup targets one Hub data root per Windows user. Changing that root requires repeating
setup; the old Hub must be stopped first. Chrome and Edge may share the same extension ID.
The exported extension folder must remain in place. Reload it after updating its files;
repeat setup if the browser assigns a different ID. There is no store publication or silent
extension installation. Firefox and mobile browsers are not supported by this implementation.

## Collection and accuracy

The extension observes active-tab changes, tab URL changes, browser-window focus and tab
closure. It samples every two seconds while connected to its allowlisted native host.
The host communicates over binary framed stdio; no TCP port or HTTP endpoint is opened.
When Hub is paused, closed, idle or observing another app, the host reports collection off
and the extension stops reading tab URLs. It continues sending hostname-free heartbeats
so it can notice a subsequent Start. A closed/crashed Hub's lease expires within 12 seconds.

Only HTTP(S) hostnames are eligible. Paths, queries, fragments, credentials, tab/page titles,
content, screenshots and history are never sent or saved. Private browsing is disabled in
the manifest and checked at window/tab level. Browser-internal pages, single-label/local
names, IP addresses and common local/reserved suffixes are excluded. Hostname normalization
uses browser URL parsing, ASCII DNS validation, lowercase and trailing-dot removal; it makes
no DNS requests. Full hostnames are separate (mail.example.com and example.com); no external
public-suffix service or heuristic grouping is used. Exclusions cover the entered hostname
and all its subdomains, and apply before live samples are persisted. Excluding a site does
not erase prior history; use deletion to remove it.

Hub accepts a fresh sample only if the actual Windows foreground process matches its browser
(chrome.exe or msedge.exe), the user is below the configured idle threshold and Hub is unlocked.
It checkpoints intervals every five seconds using the same monotonic/wall-clock safeguards
as app tracking. Missing, duplicate, stale, unavailable, excluded and paused observations
break continuity. Gaps over 15 seconds and clock changes are not inferred. Rapid visits may
be missed and boundary samples are conservatively omitted. Passive reading/video can be
undercounted by idle detection. Multiple profiles of the same browser share one latest-sample
slot, so switching profiles may cause gaps; use one enabled profile per browser for reliable
sampling. This measures sampled foreground use, not attention or productivity.

Site totals overlap browser app time and must not be added to it. The UI labels this explicitly.
Site review shares the main From/Through dates and timezone. It offers complete site/category
totals, aliases, literal filtering, seven-day trend, exact browser/hostname intervals and
Load more (200 initially, 10,000 maximum). Intervals are clipped for display at local dates,
including DST. The optional Daily overview screen-time card includes a separate site subtotal.

## Data and security boundary

Schema 19 adds hub_browser_time_sessions with browser, hostname, UUID and UTC interval.
History and local browser settings participate in SQLite backup/restore and explicit JSON
export. They are excluded from sync, provider relay, Metrics, audit metadata, diagnostics
and operational logs. Ordinary retention/task cleanup does not delete them. The separate
browser deletion action previews complete sessions overlapping selected dates; confirmation
deletes those sessions in full. Lease rotation prevents mailbox samples from recreating them.
Full Hub data deletion removes them with the database. Exports/backups contain private usage.

Privacy masking clears site tables, filters, rule fields and exclusions while recording
continues; Hub session lock revokes consent for the current Hub recording session. Restore and shutdown
also revoke it. No browser startup entry or always-on service is installed.

The native host is a separate Hub-owned executable with no Qt/Assistant dependency. Per-user
Chrome/Edge native manifests allow only explicitly registered extension origins; the helper
checks the caller origin against that local registration and validates all received messages.
Every message rechecks registration so disconnect also revokes already-open connections.
Frames are limited to 2,048 bytes, input schemas reject unknown fields and arbitrary intervals,
and errors return a fixed status without reflecting payloads. It keeps at most two latest
sample files under browser-time-live, never an unbounded event queue. These temporary samples
are overwritten, removed at pause/lock/restore/shutdown, and ignored after lease expiry. After a crash, residual samples are cleared on the next Hub launch.
The small lease file contains recording settings and a random generation, expires after
12 seconds and is renewed only by Hub. This is a per-user trust boundary; software already
running as the same Windows user can access that user's local usage data.

## Verification and release acceptance

Offline regression coverage lives in tests/test_browser_time.py,
tests/browser_time_extension.test.cjs and tests/ui/test_browser_time_ui.py. It covers permission
and consent gates, URL stripping/private tabs, foreground/idle/gap behavior, exclusions,
native framing, schema migration, backup/restore/export, sync exclusion, privacy masking,
category rules and deletion. JavaScript tests use Node's built-in test runner and fake APIs.
No browser account, real browsing records or registry registration is used in these tests.

Manual release acceptance: load the extension in both supported browsers, exercise permission
grant/revoke, focus/tab switches, private browsing, browser restart, Hub pause/lock/exit,
idle and sleep/wake, long passive use and extension reload. Verify the packaged helper origin
allowlist, setup/disconnect/reinstall/uninstall, Narrator and narrow-window review. Store
publication, signing and clean-VM acceptance are release work and cannot be claimed from fakes.
