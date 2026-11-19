# Screen time

Implemented November 9, 2026 as a Windows-only local Logging extension. Screen time is
observed foreground application use, independent from task Activity, manual durations,
Reflections and Metrics. Android and iOS remain deferred. November 10 adds optional
Chrome/Edge/Brave hostname collection described in `PERSONAL_HUB_BROWSER_TIME.md`; the Windows
app observer below still collects no browser URLs.

## Recording

App recording starts automatically when Hub opens on Windows, unless you have saved a
startup opt-out. In Logging > Screen time, uncheck **Start recording when Hub opens** to
disable it for future launches. This visible preference saves immediately. Use **Pause
recording** or the tray control to stop the current session, and **Start recording screen
time** to resume. Collection runs only while Hub runs, including its tray lifetime;
it does not install a service or Windows startup entry.

Every five seconds the Windows observer reads the foreground executable basename and OS
input idle duration. It never reads window titles, screenshots, clipboard contents, typed
text, page titles, browser URLs or document contents. Process paths are used transiently to
resolve the executable basename and are never retained. Applications that Windows denies
access to remain unavailable. The recorder uses no input hook or keyboard state capture.

By default recording pauses after three minutes without input; the setting accepts 30–1800
seconds. This includes the idle grace period and may undercount passive reading or video.
Excluded executable names match exactly, ignoring case, and suppress future observations;
adding an exclusion does not erase earlier history. Use explicit deletion for that.

Each consecutive sample of the same eligible app extends one durable session. App changes,
exclusions, lock, unavailable samples, gaps longer than fifteen seconds, and wall-clock jumps
close continuity. Durations use UTC timestamps validated against a monotonic clock.
Checkpoints are saved every sample. Pause, exit and crash recovery do not infer time since
the last checkpoint. The conservative sampling approach can omit a short visit entirely and
lose approximately one sample at each boundary. It cannot measure attention or work quality.
Sessions restart before exceeding 24 hours. No sampling resumes automatically after database
restore; a new explicit Start action is required. An opted-in future app launch can start it.

## Review and controls

Choose From/Through dates or Today. Top apps, category totals and the seven-day trend cover
all matching history. A literal app/display-name/category filter narrows review. The timeline
and exact interval table start with 200 intervals, with Load more up to 10,000 and a visible
partial-list explanation. Gaps remain blank. App rules provide local display aliases and
categories, applied to historical summaries without rewriting observations. The default is
Uncategorized. No productivity score, focus score, streak or notification is generated.

Usage rows show a Windows application icon when the executable is available during app
recording; otherwise they use a locally generated app monogram. Website rows use a local
monogram derived from the hostname. Website icons are not downloaded, so rendering them
does not contact the sites or a favicon service. Executable paths are used only in memory to
resolve app icons and are not included in screen-time history or settings. Privacy masking
and lock clear the icon cache along with the visible usage details.

UTC intervals are clipped at configured-timezone local midnights, including 23- and 25-hour
daylight-saving dates. Daily overview shows separate app and website foreground-time cards
for its selected date by default; recording settings can hide both cards. Presentation
masking clears the tables, filters, rule fields and cards;
masking does not disable recording. Session lock suppresses collection as well as presentation.

## Data boundary

Schema 18 adds `hub_screen_time_sessions`, independently of task and Logging cleanup.
It follows schema 17's focus-session extension. Records stay until explicit deletion.
Preview deletion shows the number of complete sessions overlapping the selected local
dates. Confirmation deletes those sessions in full, including any boundary-crossing session.
Deleting history resets the active checkpoint so it cannot recreate the removed session.

SQLite backup/restore includes history and local settings. Explicit JSON export includes
all screen-time sessions plus screen-time settings (including aliases, exclusions and the
startup preference). Backups and exports contain private application usage and should be
protected accordingly. Full Hub data deletion removes these records with the database.
Ordinary completed-task and summary cleanup never delete them. Screen time and its settings
are excluded from shared-folder sync, provider requests, the provider relay, Metrics uploads,
operational logs, audit payloads and diagnostics. There is no new server or network permission,
runtime dependency, Tracker import, AI dependency or separate application identity.

## Verification

`tests/test_screen_time.py` covers consent, checkpoints, exclusion/idle/lock gaps, clock
changes and sleep, restart, DST, complete totals, deletion preview/apply, migration,
backup/restore/export and settings bounds. `tests/ui/test_screen_time_ui.py` covers the
actual navigation, start/pause and tray controls, settings/rules, privacy masking, review,
deletion confirmation and restore pause. All collectors in automated UI workflows use fakes.

Manual release acceptance remains: sample app switching on Windows, verify lock/unlock and
sleep/wake with the actual OS, check long passive use under the chosen idle setting, inspect
screen-reader names and narrow-window navigation, and confirm tray lifetime/startup choices
in a packaged build. These checks must not be claimed from fake observations.

Final automated validation: 331 core tests passed; 39 distinct relevant UI cases are
covered by the passing batches/reruns recorded in `PERSONAL_HUB_IMPLEMENTATION_STATUS.md`.
Screen-time-specific validation passed eight core and three UI tests. Repository Ruff
lint/format, documentation, whitespace and pre-desktop import isolation checks pass.
The native Windows observer also completed a basename-only smoke sample without persisting
or printing the observed application identity. No installer rebuild is claimed for this change.


## Background recording indicators — November 15, 2026

While Hub is in the tray, its icon shows two status marks: apps on the left and
browser sites on the right. **R** in green means an interval was saved; **W** in amber
means enabled and waiting (idle, excluded/unavailable app, browser focus or extension
connection); **P** in gray means paused; **!** in red means a storage error.
Hover for separate app/site details. The tray menu provides independent app/site
start and pause controls, last-save times and **Review screen time**. Last-save times
are hidden under privacy masking or lock. No app names or hostnames appear in the tray.
Windows may place the tray icon in its overflow area; pin it using Windows tray settings.

Pin the browser extension to its toolbar to see **REC**, **WAIT**, **OFF**, or **!**.
Hover for the reason or click for the status popup. REC requires a recent successful
Hub checkpoint for the current eligible site. Connection and consent alone show WAIT.
Changing tabs, losing browser focus, idle, exclusions, pause, lock and disconnect clear
REC on the next observation/response. Hub samples every five seconds, the extension
checks every two seconds, and a silent helper clears REC after twelve seconds without
a response (on the next timer tick). Status contains no hostname or page content and
never opens a popup automatically. Older helpers show WAIT until updated.

Update/rebuild Hub and PersonalHubBrowserHost.exe together, export the updated extension
files to the existing extension folder, then reload it on the browser extensions page.
Extension version 0.1.3 includes these indicators. Repeat setup if the extension ID or
helper location changes. No new permission or collection opt-in is required.
