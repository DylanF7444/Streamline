# Personal Hub orientation and recovery review

November 13, 2026. Reviewed the current product references, desktop code, UI tests and
synthetic offscreen Qt previews before implementing this focused follow-up to the earlier
usability review. Existing working-tree changes were retained.

| Journey | Finding | Implemented follow-up |
| --- | --- | --- |
| Home | Capture shortcuts already work offline, but page instructions had become hidden. | Visible, wrapping guidance identifies due tasks, agenda, task capture and progress logging. Saved dashboard layouts stay intact. |
| Calendar | Local creation and source selection work, but their relationship is not explained on the page. | A short visible description points to local items and the Calendars disclosure for connected calendars and imports. |
| Tasks | Quick capture, filter recovery, required-title validation and save feedback already have focused coverage. Maximum text scale exposed slight editor overflow and squeezed reminder inputs. | Restore visible instructions about Enter, the task menu and automatic completion history. Stack form labels above fields in compact/large-text layouts and shorten timer option labels. Preserve the editor and task semantics. |
| Watchlists | Conditional watch fields are already disclosed appropriately; feeds are further down the same page. | Visible guidance distinguishes page/product watches from feeds and points to Inbox for alerts. |
| Inbox | Messages showed raw freshness keys, an empty category could look like missing data, and there was no direct recovery route. Side-by-side panes and horizontal actions could squeeze small windows. | Separate readable health guidance from selected-category results; update both when categories change. Add Open Sources for setup/recovery. Use a scrollable Inbox, stacked panes and controls in compact/large-text layouts. Keep explicit body fetch and provider opening. Explain that clearing Hub summaries keeps provider messages. |
| Sources | A total source count gave little indication of what needed attention. A disabled, disconnected platform could instruct a first-time user to Enable before reviewing setup. | Count enabled sources with stale/offline/permission/disconnected/error states; disabled connectors remain optional. Preserve source order and selection. Direct disabled, disconnected platforms to Connect / Configure. |
| Settings | The preference save action is far below the top-level save feedback. Backup actions share the same undifferentiated action group. | Add another Save settings action at the top using the existing payload and handler. Add Preferences and Backups and export headings. Keep Apply appearance distinct and preserve save feedback. |
| Tracking / Logging | The explanation of the modes was only in a tooltip; Logging pages lack a visible introduction. | Show the current mode's purpose below the selector. Explain automatic Activity capture, Reflection draft saving and date-based review on their pages. Keep remembered routes and existing draft guards. |

Guidance uses plain text and wrapping labels. Message outcomes use the existing
StatusLabel announcement path; category selection and refresh do not fetch a body. Controls
keep native keyboard focus and accessible names. The changes introduce no schema or saved
preference changes, provider scopes, external actions, AI, retention changes or Local
Assistant dependency.

## Verification and remaining review

Focused coverage in `tests/ui/test_orientation_ui.py` exercises visible page/mode guidance,
remembered routes, message setup and recovery without fetches, category-result updates,
pause/masking/lock, actionable source counts, Settings save payloads and keyboard access to
message actions at 720×460 with 175% text, high contrast and reduced motion.

Validation used this checkout's `src` explicitly on `PYTHONPATH`:

- `python -m pytest tests --ignore=tests/ui -q`: **365 passed**.
- **76 distinct relevant UI cases passed** across isolated-case runs and corrected reruns:
  orientation (5 new cases), usability, window, tasks and task timers, Calendar, feeds,
  watches, messages/categories, connections, accessibility, Logging, Home mini calendar and
  appearance. The initial 73-case run had 70 passes and three failures. Updated the obsolete
  hidden-description assertion, made the disabled-source fixture explicitly connected,
  and fixed the actual task-editor overflow; all three passed on rerun. Three additional
  task-timer cases passed. Final compact Inbox/text-scale and task-editor keyboard checks
  also passed after the last layout changes. This is focused coverage, not the full UI lane.
- `python -m ruff format --check .`: **225 files already formatted**.
- `python -m ruff check .`, `python scripts/check_documentation_consistency.py`,
  `python scripts/check_personal_hub_imports.py --output .benchmark-data/ux-orientation/imports.json`
  and `git diff --check`: passed. The import probe found no Assistant modules or Qt before
  desktop launch.

Synthetic previews and the initial per-case UI log are in the ignored
`.benchmark-data/ux-orientation/` directory. Initial failures in that log are superseded
by the passing targeted reruns described above.

Synthetic offscreen previews were inspected at 1100×760 and 720×460, including the message
setup action and visible focus at 175% text/high contrast. These are layout checks, not
native Windows DPI or assistive-technology acceptance. Narrator, another screen reader,
Windows display scaling/high contrast, first-time-user sessions and live-provider
reconnection remain manual review items from `PERSONAL_HUB_ACCESSIBILITY_REVIEW.md`.
No installed build, signing, or clean-VM release acceptance is claimed.
