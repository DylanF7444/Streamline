# Personal Hub accessibility review

Review date: September 1, 2026. Candidate: planning-branch source and unsigned local Windows
package. This is an automated/source review plus a manual-review checklist; it is not a claim
of completed testing with assistive-technology users.

## Automated and source results

| Area | Result |
| --- | --- |
| Minimum supported window | Pass: 720×460 minimum is enforced and primary navigation/content remain reachable. |
| Text scaling | Pass: 1.00–1.75 range is validated and applied to application font sizing. |
| High contrast | Pass: high-contrast palette/style path has UI coverage. |
| Reduced motion | Pass: preference is represented/applied; no workflow depends on animation. |
| Keyboard navigation | Pass for primary navigation and tested Tasks, Calendar, News, Watches, Inbox/message actions. |
| Accessible names | Pass for window, navigation, page regions, status, controls, lists, message body, and safe-link list in automated checks. |
| Plain-text safety | Pass: remote content is rendered as plain text; sanitized links remain separate explicit actions. |
| Privacy/lock | Pass: sensitive text is masked, lock overlay is named, body memory is cleared, and actions disable while locked. |
| Background work | Pass: fake slow/failing refresh work runs off the Qt event loop in UI coverage. |

Automated command:

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
python -m pytest tests/ui -q
```

The Phase 7 focused accessibility tests verify minimum size, supported scale cap, high
contrast, reduced motion, navigable destinations, and accessible names for the message
summary/body controls.

## Manual review still required before release

Test the signed candidate on a clean Windows VM with Narrator and one additional screen reader:

1. Navigate every primary destination and return to Today using only keyboard input.
2. Add/edit/complete/undo/archive tasks, including recurrence and validation errors.
3. Change Calendar views/selections and open an explicit provider link.
4. Add/filter/read/open feeds and create/test/edit/disable/remove watches.
5. Traverse Inbox and message summaries, open one body, review safe links, clear summaries,
   lock/unlock, and verify focus/state announcements.
6. Review Sources health and Settings diagnostics in ready, empty, stale, offline,
   permission-required, disconnected, and error states.
7. Repeat at Windows 100%, 150%, and 200% display scaling and Hub 100%, 125%, 150%, and
   175% text scale at 720×460 and a common 1366×768 display.
8. Enable Windows high contrast and verify every status has text/icon semantics, focus is
   visible, and no content depends on color.
9. Enable reduced motion and verify no state change depends on animation or timeout.
10. Run a slow/failing refresh and confirm focus, reading cursor, keyboard input, and screen
    reader output remain responsive.

Record tool versions, display/DPI, keyboard path, announcement problems, screenshots where
safe, severity, owner, and retest result. Critical/high findings block public release.

## Known evidence limits

- Offscreen Qt tests cannot establish real screen-reader interoperability or user usability.
- Automated accessible names do not prove correct role/state/value announcements.
- High contrast and scaling need visual inspection on Windows, including text clipping and
  focus visibility.
- Live OAuth browser/consent surfaces are provider-owned and need their own keyboard/screen-
  reader check during live-provider acceptance.

The release checklist must continue to report manual accessibility review as pending until
those results are recorded against the signed artifact.
