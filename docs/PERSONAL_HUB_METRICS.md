# Personal metrics center

The Windows root aggregates Android Health Connect steps and user-defined numeric
measurements or timed sessions. Logging > Metrics contains Overview, Devices and transfer,
and Custom metrics and files. No account or AI is required. Ring connectors and automatic
sleep collection are deferred.

## First use

1. Install the Android companion built from `mobile/android`. Grant **Allow step reading**.
   Background access is optional. Health Connect consolidates available steps; on phones
   without built-in counting, configure another step-recording app to write to Health Connect.
2. Collect on the phone. Readings remain in its private durable queue until LAN acknowledgment;
   encrypted file export preserves that queue. The displayed reporting timezone is fixed on
   first launch so travel cannot silently change daily record identities.
3. Choose either transfer method below. Both can be used for the same source without duplication.

### Manual encrypted snapshots

Choose Manual encrypted snapshots on Android. Enter an export passphrase of at least twelve
characters and choose Export encrypted snapshot parts. The system document picker writes
numbered `.phmetrics` files; exports larger than 1,000 readings require additional selections.
Move these files using a shared folder, USB, or another user-chosen file transport.

On Windows, open Metrics > Devices and transfer > Import encrypted snapshot file, or explicitly
import a folder. Enter the passphrase. Review and approve an unknown source and its definitions.
Files from an approved source still undergo the same metric permission and revision checks.
Folder operations handle at most 100 files at a time. Re-importing files is safe.

Snapshots use Scrypt (N=16384, r=8, p=1, 32-byte key), a random 16-byte salt, and AES-256-GCM
with a random 12-byte nonce and 128-bit tag. Standard padded Base64 encodes binary fields.
The UTF-8 authenticated metadata is `personal-hub-metrics-encrypted-v1`.
The passphrase is entered for each operation and is not stored by Hub. Plaintext bundles are
bounded to 16 MiB. Missing records or missing parts never imply deletion.

Windows can also export any selected source into independent numbered encrypted files.
Exports refuse to overwrite an existing numbered part.

### Paired local-network transfer

Copy the Android Source ID. On Windows, register that exact ID, a display name, and `steps`
as its permitted metric. Choose the PC's specific LAN interface address and port, then start
the receiver. The default loopback address is useful only for senders on the same PC.
If Windows Firewall blocks access, allow the chosen port for the private network through
Windows settings. Hub does not add firewall rules or configure router forwarding.

Select the approved source and create a five-minute pairing string. Paste it into Android
and pair. The string contains a single-use token, root URL and TLS certificate fingerprint;
keep it private. Select Paired local network and Sync now.

The root runs only inside the Hub process, including its tray lifetime. It must be reachable
on the LAN. Readings queue while it is closed. Start the receiver explicitly after restarting
Hub. Network pause and lock close it and invalidate pending enrollment tokens; an enabled
receiver resumes when those restrictions clear. Masking clears private presentation and
pending pairing strings. Credentials are source-specific and revocable; Windows stores them
with DPAPI and Android protects its connection using Android Keystore.

Background collection is best effort, scheduled about every fifteen minutes when permitted.
The open companion refreshes about once a minute. Health Connect and Android battery policy
control the actual availability and timing of readings. Today is re-read routinely and the
previous thirty calendar days are reconciled daily within granted access.

## Measurement semantics

Definitions are immutable after creation: ID, name, canonical unit, kind and aggregation.
Create another metric ID to change meaning or units. Numeric values must be finite and between -10^15 and 10^15 to keep calculations and charts
bounded. Numeric points support sum, latest,
minimum, maximum and arithmetic mean; interval totals require explicit bounds; timed sessions
use duration in seconds. These are record-based summaries. Session duration belongs to its
recorded start day; the first release does not split sessions across days or infer a
time-weighted average. Overlapping custom records are the source author's responsibility.

Steps are consolidated totals for a complete reporting day, with a fixed identity
`steps:<IANA timezone>:<YYYY-MM-DD>`. An update replaces that day's earlier revision.
The start and end represent local midnights, including 23- and 25-hour DST days. The current
day may be incomplete and is still revised under the same identity. Reporting dates retain
their recorded timezone; charts do not silently redistribute totals into another timezone.

Every source owns its record IDs and monotonic positive revisions. Identical retries and
older revisions are harmless. Conflicting content at the same revision fails the entire
batch. Sources can report only approved metric IDs with matching units. The first source
becomes primary for that metric; choose another explicitly to change combined summaries.
Multiple device totals are never automatically summed. Missing readings remain unavailable;
explicit zero values remain zero. Mean is the arithmetic mean of recorded samples.

Overview offers selected dates, source comparison, trend and exact daily values. The complete
range summary covers all matching records; the detailed record list shows the first 200 and
the daily chart/list covers at most 366 days. Use a narrower range for longer history.
Optional Daily overview cards show chosen primary-source metrics.

History is retained until explicitly deleted. Preview deletion by selected source, metric
and date range. Deletion clears measurement payloads and retains opaque identity suppression
markers so old files and newer revisions cannot restore them. **Allow a deleted record to be
imported again** clears the selected suppression marker. Revoke a source to stop both its LAN
uploads and future file imports; existing accepted history is retained until deleted.
To remove a source's readings, select it and delete its date range, then revoke it. Source
identity remains to reject stale files. Health Connect originals and exported files require
their own explicit deletion outside Hub.

Database backup/restore and explicit local JSON export include metrics. Restoring a backup
restores its historical state, including its then-current suppression markers. Device TLS
keys and credentials remain in the local vault and are excluded from data export and sync.
Metrics are excluded from existing shared-state sync and the provider collector relay.
Operational logs and diagnostics never contain measurement values or payloads.

## Custom sources and API

Define a metric, register a source, and approve its metric IDs. JSON imports accept the batch
format below. CSV imports prompt for a JSON field-to-column mapping, timezone and selected
metric/unit. The default mapping is `{"start":"timestamp","value":"value"}`. Timestamp
columns must contain an explicit UTC offset; ambiguous local times are rejected. Optional
columns can map record_id, revision, end, timezone and reporting_date. Without record IDs,
CSV identity uses the exact file hash and row number; repeated identical files deduplicate.
For corrected or overlapping exports, provide stable record_id and revision columns.
CSV mappings are saved locally after successful import. Both formats are limited to 1 MiB
and 1,000 records per import; a preview precedes persistence.

```json
{
  "format": "personal-hub-metrics-v1",
  "source_id": "workshop-sensor",
  "records": [{
    "record_id": "temperature-00001",
    "metric_id": "temperature",
    "revision": 1,
    "value": 23.5,
    "unit": "celsius",
    "start": "2026-11-06T14:30:00Z",
    "timezone": "America/Chicago"
  }]
}
```

Public schema: `metrics-v1.schema.json`, generated by
`python -m personal_hub.metrics.schema`. Unknown fields, NaN/infinity, incompatible units,
naive timestamps, mixed source identities and unsupported formats are rejected.

| API | Input and authorization | Response |
| --- | --- | --- |
| POST /v1/pair | JSON token and source_id from enrollment string | source_id and credential |
| POST /v1/measurements/batches | Batch JSON; Bearer credential and X-Hub-Source | accepted, unchanged, suppressed counts after commit |
| GET /v1/status | Bearer credential and X-Hub-Source | format and ready state |

The client must verify the certificate SHA-256 fingerprint before sending secrets. No redirects
are followed. All endpoints are HTTPS. Enroll only against the explicitly chosen root device.
Uploads are bounded to 1 MiB/1,000 records, and requests have a ten-second application timeout
and a per-address sixty-per-minute limit. Authorization failures return 401/403, invalid
content 400, oversized content 413, rate limits 429, and pause 503. Retain and retry the same
record identities after interruption; acknowledgments are sent only after a committed batch.

Example external Python sender (Windows DPAPI credential storage):

```powershell
python scripts/send_metrics.py --state <private-sender-directory> --pair
python scripts/send_metrics.py --state <private-sender-directory> --file readings.json
```

The sender asks privately for the pairing string, pins the root certificate, retries bounded
failures and retains the input file. Source authors run their collectors externally; the Hub
process does not load their code. Local API access is a deliberate opt-in extension to the
previous outbound-only network boundary.

## Building and acceptance

The companion requires JDK 17, Android SDK platform 36, build tools 35.0.0 and Gradle 8.11.1.
Set JAVA_HOME and ANDROID_HOME to local installations, then run from `mobile/android`:

```text
gradle --no-daemon :app:testDebugUnitTest :app:assembleDebug
gradle --no-daemon :app:connectedDebugAndroidTest
```

Debug APK output: `app/build/outputs/apk/debug/app-debug.apk`. Release signing uses a private
keystore supplied outside version control. No production signing key is included.

Before device release, install on a supported phone, grant/deny/revoke permissions, walk to
generate actual steps, verify corrections and queues across phone/root restarts, then deliver
the same day through both transports. Confirm one root total, masking/lock, pairing revocation,
root offline recovery and explicit deletion. Automated fakes and loopback tests are recorded
separately from this physical-device acceptance.
