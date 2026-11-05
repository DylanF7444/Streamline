# Personal Hub usability improvements

## Charcoal and mint presentation — November 4, 2026

Applied the reviewed dark dashboard direction to the native Qt application: layered charcoal
surfaces, mint navigation and primary actions, shared rounded controls, scalable native line
icons, and consistent card surfaces across Tracking and Logging. Removed redundant page
introductions and decorative writing prompts while retaining validation, privacy, freshness,
autosave, and recovery guidance. Light/sage presets, custom colors, high contrast, text scaling,
and reduced motion remain supported.

Home provides database-backed open-task and unread-alert counts plus today's factual Logging
count, masked on privacy/lock. The local projection timestamp says **Overview updated** rather
than implying provider synchronization. New default widget positions prioritize due tasks
and Agenda, while existing saved rectangles and visibility are retained. Customization actions
remain available under **Customize dashboard**, with an explicit **Done editing** control.
Watchlists starts with the tracked list and an **Add watch** disclosure; editing by keyboard
or pointer reveals the existing conditional form. No provider operations, stored content,
permission scopes, or data schemas changed.

Validation results are recorded in the implementation status after the final checks. Local
synthetic screenshots and per-file test logs are under ignored
`.benchmark-data/style-implementation/`. Native DPI and assistive-technology release checks
remain separate from offscreen layout verification.

Review date: October 28, 2026. Scope: focused desktop presentation and keyboard fixes.
The implementation plan, handoff, accessibility review, implementation status, progress and
activity plan, current desktop code, and existing UI tests were reviewed before editing.
Synthetic offscreen Qt screenshots were inspected across every primary destination and both
workspace modes. No live accounts or installed user data were used.

## Findings and implemented changes

| Journey | Observed friction | Change |
| --- | --- | --- |
| Home / Tracking | An empty dashboard offered layout customization before a clear starting task. The mode names offered little explanation. | Direct **Add a task** and **Log progress** actions, a short orientation subtitle, and a mode description explain the paths into Tracking and Logging. |
| Calendar | The single-line header and toolbar competed for width. Failure states named the problem without a recovery route. | Stacked headers, compact toolbar rows, a scrolling page, and a vertical event inspector in compact windows. The local authoring action says **Add calendar item**. Permission/disconnection/failure text points to Sources. Required-title validation keeps the editor open and focuses the field. |
| Tasks | Empty results did not distinguish first use from active filters. Hidden state filters were easy to overlook. At 720×460 and 175% text, the task editor's fields disappeared and its buttons clipped. | Next-step empty text, visible active state/due context, and **Clear task filters**, preserving the selected list and sort order. Capture/search controls wrap into rows. The editor stacks below the list in compact windows and scrolls. Focused controls are revealed through their enclosing scroll areas. Notes allow Tab to continue through the form. |
| Watchlists | Numeric values had no persistent visible labels; unrelated threshold, term, and selector inputs appeared together. The baseline behavior was unexplained. | A labeled form with units and required name/URL labels, condition-specific fields, and a baseline/Inbox explanation. Hidden fields retain entered values. Existing validation, conditions, polling, and network requests are unchanged. |
| Inbox / Sources | Empty alerts gave no explanation of their purpose. Source rows exposed machine-style state names. A source-only failure notice had no action. | Empty alerts explain reminders, watches, source problems, and the separate Messages tab. Source state text uses spaces and selected-source guidance explains refresh, reconnection, disabled, paused, and offline states. **Review sources** opens the management page without refreshing or dismissing failures. |
| Settings | The read-only summary duplicated the editable preferences, save outcomes appeared only beside a distant save button, and loading values could incorrectly report unsaved changes. | **Current settings** discloses the summary. Instructions distinguish **Apply appearance** from **Save settings**. Save outcomes also appear at the top. Loading saved values no longer fires edit signals. |
| Logging | Disabled date inputs looked like an editable range. Horizontal filter/action groups overflowed at large text sizes. Manual progress did not mark its required description clearly. | From/Through inputs appear only for a custom range. Filters and actions stack in compact windows. Manual progress labels required/optional fields and focuses the missing description. Existing automatic completion history, optional durations, reflection autosave, and mode memory remain intact. |

Shared page titles sit above their descriptions. Changed outcome labels use plain text and emit
Qt accessibility announcements for visible, changed messages; hidden or repeated messages do
not announce. Accessible names, keyboard paths, high contrast, reduced motion, privacy masking,
and lock behavior retain automated coverage. Announcements contain only the displayed outcome.

## Verification

- `python -m pytest tests --ignore=tests/ui -q`: **252 passed**.
- Initial focused Tasks/watch/Logging/calendar editor regression run: **21 passed**.
- Final UI inventory: **95 passed** across fresh-process file batches and the final focused
  rerun. All 94 tests present before the last Settings regression passed in 20 file batches;
  the final eight usability tests plus the existing Settings persistence test passed together
  (**9 passed**). The collected inventory was checked against 95, with no tests omitted.
  The batch run reported one existing Qt date/time constructor deprecation warning.
- New `tests/ui/test_usability.py` covers offline Home capture, filtered-result recovery with
  list/sort preservation, source recovery guidance and navigation without refresh, watch field
  disclosure, required-title validation, Settings load/edit/save feedback, visible-only accessibility announcements, and keyboard
  editing at 720×460 with 175% text, high contrast, and reduced motion.
- Ruff format/lint, documentation consistency, import isolation, and Git whitespace checks
  passed for the final source. Local screenshot/test evidence is under ignored
  `.benchmark-data/ux-review/`; it is not product or release data.

## Remaining manual review

Offscreen screenshots confirm the inspected synthetic layouts, including a visible focused
Save task button at the minimum window size. They do not establish Windows display/DPI,
screen-reader interoperability, real focus announcements, or usability with first-time users.
The Narrator/additional-screen-reader, Windows scaling/high-contrast, and signed-artifact
checks in `PERSONAL_HUB_ACCESSIBILITY_REVIEW.md` remain pending. Live provider consent and
reconnection should be checked with test accounts as part of the existing release process.

No provider permissions, external write actions, database schema, saved preference formats,
retention rules, synchronization boundary, or Local Assistant dependency were added or changed.

## Action inventory and motion review — November 2, 2026

The current desktop action surface is grouped below so each workflow can be reviewed without
confusing local display changes with provider operations.

| Surface | Available user actions |
| --- | --- |
| Navigation and collections | Open the configured Workspace destinations; rename, reorder, show, or hide Workspace pages; open task-list shortcuts; manage named Calendar views; switch Tracking/Logging modes. Sources and Settings stay in fixed Management navigation. |
| Home | Refresh available sources; jump to task capture or progress logging; open a projected item in its owning page; browse months in the mini calendar, return to today, and open a date in Calendar; add/hide widgets, drag them by the heading, resize by handle, and reset the layout. |
| Calendar | Refresh sources; move between periods, return to today, choose month/day/week/agenda, choose timezone and calendars, manage named views, iCalendar feeds, and local imports, create/edit/remove local events, complete projected local tasks, open safe provider links, and explicitly sync selected linked local events to Google with conflict review. The **Calendars** disclosure contains source, feed, and import controls; selecting an event opens its inspector. |
| Tasks | Quick add, import, and create lists; switch quick views; search, filter, sort, and clear filters; create/edit/save/cancel; complete, undo, archive, log progress, and open a safe source link. Row menus expose secondary task actions. |
| Watchlists and news | Add/edit/remove/enable feeds; choose subscriptions and include/exclude terms; filter unread entries; mark entries read and open safe links; create/edit/remove/enable page and product watches; test a watch and refresh sources. Conditional watch fields follow the selected watch type and condition. |
| Inbox and messages | Switch between alerts and messages; mark alerts read/unread, dismiss, or archive; open an alert's destination; choose a local message category; explicitly open a sanitized message body or provider link. |
| Sources | Select a source; connect, refresh, enable/disable, reconnect, disconnect, or clear retained summaries; review granted capabilities, health, refresh interval, retention, and the pages that use the source. |
| Settings | Edit and save startup, timezone, refresh, notification, quiet-hour, digest, privacy, retention, accessibility, appearance, and update preferences; pause network refresh for the session; back up, restore, export, preview/apply history deletion, configure encrypted sync or provider relay, sync now, and review/copy diagnostics. |
| Tracking and Logging | Return to each mode's remembered page; capture manual progress and filter/search Activity; create, edit, search, or delete Reflections; browse a Daily overview date and edit its existing Reflection; search/filter Places, add/edit/delete places and visits, favorite places, save/return to a home map view, and navigate the map. |
| Tray | Open the window, open Home or Inbox, refresh, pause/resume network refresh, and quit. |

Motion is limited to transitions that help users understand a change of view or reveal a
secondary surface:

| Interaction | Treatment | Reason |
| --- | --- | --- |
| Main destination navigation | Existing 180 ms content fade | Confirms the selected page while keeping the navigation rail stable. |
| Home widget move, resize, add, or hide | Existing eased geometry transition | Shows how the dashboard reflows around direct layout edits. |
| Calendar month/day/week/agenda switch | New 180 ms content fade | Softens the change between distinct calendar layouts without animating refreshes. |
| Calendar **Calendars** disclosure | New 180 ms height reveal/collapse | Makes the source and import controls appear as one coherent group. |
| Tasks **Filters** disclosure | New 180 ms height reveal/collapse | Keeps advanced filters compact until requested while making their appearance clear. |
| Inbox Alerts/Messages switch | New 180 ms content fade | Keeps the selected Inbox mode change easy to follow. |

The appearance preference for reduced motion cancels running transitions and makes later
changes immediate. Data edits, completion, archive, alert state, privacy masking/lock,
connection, refresh, backup/restore, and deletion outcomes remain immediate; their meaning is
communicated through existing control state and text. No transition is required to understand
or finish an action. Automated UI tests and native assistive-technology review were not run for
this motion update.
