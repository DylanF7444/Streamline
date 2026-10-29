# Personal Hub usability improvements

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
