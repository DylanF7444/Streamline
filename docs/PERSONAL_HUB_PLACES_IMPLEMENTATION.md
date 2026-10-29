# Personal Hub Places implementation

Implemented October 29, 2026. Places is a Logging page with worldwide online maps and local
place/visit records. It preserves the independently developed Daily overview work in this
checkout and adds no Assistant or AI dependency.

## Application and storage

- `places/contracts.py`: strict Pydantic `Place`, `Visit`, and `MapPosition` records, finite
  coordinates, bounded plain text, UUID identities and UTC creation timestamps. Visits retain
  an explicitly selected calendar date; there is no inferred location or duration.
- `places/repository.py`: transactional SQLAlchemy persistence, 5,000 places and 20,000 visits
  maximum, repeat visits, stable edits, visit deletion and cascading place deletion.
- Schema 15 creates `hub_places` and `hub_visits` additively. Schema-version-specific backup
  validation still accepts schema 14 backups before migrating them. Backups/restores include
  both tables; `HubController.export_local_data` includes both collections. Sync and relay
  schemas remain unchanged and exclude Places.
- `places_page.py` and `places_coordinator.py`: native dialogs, labeled coordinate navigation,
  saved home/last view, local search, category/status/favorite filters, visit history and
  explicit deletion. Narrow layouts reflow, scroll, and retain keyboard alternatives to pins.
  Presentation masks and renderer lifecycle are connected to main-window state changes.

## Map and network boundary

The browser loads lazily from the packaged local `map_assets/index.html`. MapLibre GL JS
5.24.0 and its stylesheet/license are vendored; there is no runtime CDN or remote JavaScript.
The local script registers a custom resource protocol over a small Qt WebChannel bridge.
Only feature IDs, coordinates and visited/favorite flags enter the map overlay; names and
notes are rendered as native plain text.

The standard style is `https://tiles.openfreemap.org/styles/liberty`. Resource requests are
restricted to that provider's HTTPS style/sprite/font/planet/natural-earth paths, normalized
before transport, without credentials, alternate ports, queries, traversal or redirects.
DNS addresses must be public. The Python client caps responses at 5 MB, uses a 15-second
socket timeout plus a read deadline, and bounds its LRU to 32 MB. The bridge permits at most
64 pending requests and four workers. Cancellation is checked before DNS/transport and
during reads. Direct HTTP(S), arbitrary local file access, popups, browser storage and
hyperlink auditing are blocked in the dedicated off-the-record browser profile. A restrictive
Content Security Policy permits only bundled scripts/styles and required blob workers/images.
MapLibre's worker protocol also uses the bounded Python transport.

The map requests only resources needed by interactive browsing. Offline mode, consumer mode,
network pause, privacy masking/lock, leaving Places, hiding the window and shutdown stop the
renderer and prevent further requests. Local saved records remain usable offline. There is
no background location tracking, geocoder, route service or offline-area download. Provider
failures and unsupported graphics retain the native controls and offer Reload map.

MapLibre is BSD-3-Clause with bundled dependency notices in `map_assets/LICENSE.txt`.
Upstream assets were obtained from the pinned npm distribution:

| File | Source | SHA-256 |
| --- | --- | --- |
| maplibre-gl.js | https://unpkg.com/maplibre-gl@5.24.0/dist/maplibre-gl.js | 45a9b07a9189ce56054c620a947ccf41e291e58c95e9b61533b740aaa65ee5cb |
| maplibre-gl.css | https://unpkg.com/maplibre-gl@5.24.0/dist/maplibre-gl.css | ab1e70d59ec40465bae7e7030da2f3ccf28133fd502e62bd598eefbadfd7a732 |
| LICENSE.txt | https://unpkg.com/maplibre-gl@5.24.0/LICENSE.txt | ee5fc05a0677eaf69601d2c7db0d9ecd6cc27c3abc1d0733bc9ed34707cf8ef2 |

Setuptools package data and explicit source-relative PyInstaller data collection include
the browser files and license, without relying on editable-install package discovery.
Static Qt WebEngine imports in the lazy module activate PyInstaller's WebEngine hooks for
its helper executable, resources and translations. Unused Python Qt QML/Quick modules are
excluded; required linked native Qt DLLs remain included. A source-runtime map check also
passed with these Python modules blocked. No browser engine is imported by the headless
composition root.

Provider documentation: [OpenFreeMap quick start](https://openfreemap.org/quick_start/),
[OpenFreeMap attribution and public-instance information](https://openfreemap.org/),
[MapLibre custom protocols](https://maplibre.org/maplibre-gl-js/docs/API/functions/addProtocol/).
This implementation does not use OpenStreetMap's community raster tile server.

## Verification

Deterministic core and Qt tests cover storage, migration from schema 14, edits, repeat visits,
deletion, export, backup/restore/restart, hostile URLs, private DNS, oversize responses,
offline-before-DNS/cache/transport, and cancellation. Native UI tests use a fake renderer to
exercise lazy creation, leaving/returning, pause, offline, masking, lock, editor recovery and
home-view persistence without provider access.

Live development checks used a synthetic map view on the Windows Qt platform, with a hidden
test widget and no installed Hub profile. A city-scale map rendered and reported loaded
tiles; screenshot inspection confirmed streets/buildings/labels. Native layout inspection
covered 1200×900 and 720×460 at 175% text with high contrast, including scrolling to visit
controls and no horizontal scrolling. Offscreen Qt's graphics context cannot initialize
WebGL on this machine; this does not affect the native offline tests. Full installed/signed
release and manual assistive-technology acceptance remain the existing release gates.

Final deterministic checks: 277 core tests and 35 relevant UI cases passed across batches
and corrected targeted reruns. The final four-case Places UI rerun covers hiding/showing and
restoring the page while hidden, without creating a renderer. The two renderer-boundary UI
tests exercise local-file/request isolation, typed events, resource replies and shutdown
without actual network or WebGL. Ruff, documentation consistency, import isolation and Git
whitespace checks pass. A wheel contains every map runtime asset and the license; the
unsigned development executable includes those assets and QtWebEngineProcess/resources and
passes archive/analysis dependency inspection. No installed or signed-release smoke test is
claimed.
