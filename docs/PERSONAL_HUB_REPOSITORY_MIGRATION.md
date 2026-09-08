# Personal Hub repository migration

Updated September 8, 2026.

## Outcome

Personal Hub now occupies a self-contained repository-ready directory. Its Python package,
tests, project metadata, CI workflow, documentation, build scripts, PyInstaller target,
installer, update manifest template, and release tools are all under this directory. The
Local Assistant project no longer exposes the Hub entry point, test markers, CI job, source
package, tests, documentation, or release files.

The Hub production source has no `assistant` imports. Standalone architecture tests scan the
entire Hub source tree and probe startup in a clean interpreter. Coexistence checks use
published Local Assistant identity/path contracts or explicitly supplied external artifacts;
the Hub test suite does not install or import Local Assistant.

## Validation performed for the extraction

- Pre-migration complete Hub gate: 240 passed.
- Post-migration import-boundary report: passed with no Assistant or pre-launch Qt modules.
- Post-migration architecture lane: 9 passed.
- Post-migration release lane: 13 passed.
- Post-migration complete Hub gate: 241 passed in 933.94 seconds.
- Fresh-environment editable install, console entry point, and bootstrap: passed with no
  resolvable `assistant` package.
- Ruff format and lint: passed for all 131 standalone files.
- Standalone PyInstaller build and archive/analysis dependency verification: passed.
- Local Assistant regression after removal of Hub wiring: 1,297 passed in 321.23 seconds.
- Documentation consistency, Git whitespace, and stale-reference scans: passed.

## Tests still required before a public 1.0 release

Repository migration does not satisfy environment- or credential-dependent release gates:

1. Run the signed candidate for an actual 24-hour tray soak with representative active
   sources; accelerated virtual time does not qualify.
2. Compile the standalone Inno Setup installer (Inno Setup was unavailable during migration),
   then exercise clean install, upgrade, backup/restore, signed update, rollback, both
   uninstall choices, and Local Assistant coexistence on clean Windows VM snapshots.
3. Verify Google Calendar/Gmail, Microsoft Calendar/Outlook Mail, and Slack with project-owned
   OAuth registrations, minimum scopes, and non-production accounts.
4. Authenticode-sign the executable and installer; sign and verify the update, release, SBOM,
   and provenance metadata using protected release keys and the production HTTPS origin.
5. Complete keyboard, high-contrast, 100–175% scaling, Narrator, a second screen reader, and
   user review against the signed candidate.

Optional Phase 8 AI work remains intentionally deferred and is not a non-AI 1.0 release test.
