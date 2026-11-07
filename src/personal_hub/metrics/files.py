"""Bounded imports and interoperable encrypted snapshots; no executable content."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from personal_hub.metrics.contracts import MeasurementBatch, Snapshot

FORMAT = "personal-hub-metrics-encrypted-v1"
MAX_PLAINTEXT = 16 * 1024 * 1024
MAX_FILE = 24 * 1024 * 1024


def _key(passphrase, salt):
    if not isinstance(passphrase, str) or not 12 <= len(passphrase) <= 1024:
        raise ValueError("use a passphrase between 12 and 1024 characters")
    return Scrypt(salt=salt, length=32, n=16384, r=8, p=1).derive(passphrase.encode("utf-8"))


def encrypt_snapshot(snapshot: Snapshot, passphrase: str, *, salt=None, nonce=None):
    raw = snapshot.model_dump_json().encode()
    if len(raw) > MAX_PLAINTEXT:
        raise ValueError("split the export into smaller bundles")
    salt, nonce = salt or os.urandom(16), nonce or os.urandom(12)
    if len(salt) != 16 or len(nonce) != 12:
        raise ValueError("invalid encryption parameters")
    encrypted = AESGCM(_key(passphrase, salt)).encrypt(nonce, raw, FORMAT.encode())
    return json.dumps(
        {
            "format": FORMAT,
            "salt": base64.b64encode(salt).decode(),
            "nonce": base64.b64encode(nonce).decode(),
            "ciphertext": base64.b64encode(encrypted).decode(),
        },
        separators=(",", ":"),
    ).encode()


def decrypt_snapshot(data: bytes, passphrase: str):
    if not 1 <= len(data) <= MAX_FILE:
        raise ValueError("snapshot exceeds the file limit")
    try:
        envelope = json.loads(data)
        if (
            set(envelope) != {"format", "salt", "nonce", "ciphertext"}
            or envelope["format"] != FORMAT
        ):
            raise ValueError("unsupported metrics bundle")
        salt, nonce, ciphertext = [
            base64.b64decode(envelope[k], validate=True) for k in ("salt", "nonce", "ciphertext")
        ]
        if len(salt) != 16 or len(nonce) != 12 or len(ciphertext) > MAX_PLAINTEXT + 16:
            raise ValueError("invalid bundle bounds")
        raw = AESGCM(_key(passphrase, salt)).decrypt(nonce, ciphertext, FORMAT.encode())
        return Snapshot.model_validate_json(raw)
    except (InvalidTag, KeyError, TypeError, UnicodeError, ValueError) as error:
        raise ValueError("cannot authenticate or validate this metrics bundle") from error


def import_snapshot(repository, snapshot):
    """Source enrollment/definition approval must already have happened in the UI."""
    known = {d.metric_id: d for d in repository.definitions()}
    if any(known.get(d.metric_id) != d for d in snapshot.definitions):
        raise ValueError("approve matching metric definitions before importing")
    return repository.ingest_many(snapshot.batches)


def export_snapshots(repository, source_id):
    source = repository.source(source_id)
    if source is None:
        raise ValueError("unknown source")
    definitions = tuple(d for d in repository.definitions() if d.metric_id in source.metric_ids)
    offset = 0
    # Deliberately small independent parts, well below the 16 MiB bound even
    # with maximum-length names/IDs. Parts never imply deletion by omission.
    while True:
        page = repository.records(source_id=source_id, limit=1000, offset=offset)
        if not page:
            break
        yield Snapshot(
            source=source,
            definitions=definitions,
            batches=(MeasurementBatch(source_id=source_id, records=tuple(r for _, r in page)),),
        )
        offset += len(page)


def parse_file(
    data: bytes, *, source_id: str, csv_mapping=None, timezone="UTC", metric_id=None, unit=None
):
    if not 1 <= len(data) <= 1024 * 1024:
        raise ValueError("imports must be between 1 byte and 1 MiB")
    if csv_mapping is None:
        batch = MeasurementBatch.model_validate_json(data)
        if batch.source_id != source_id:
            raise ValueError("file source does not match selected source")
        return batch
    rows = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
    records = []
    fingerprint = hashlib.sha256(data).hexdigest()
    for index, row in enumerate(rows):
        if index >= 1000:
            raise ValueError("CSV imports are limited to 1000 rows")
        record = {
            key: row[column]
            for key, column in csv_mapping.items()
            if column and row.get(column) not in (None, "")
        }
        record.setdefault("record_id", f"csv:{fingerprint}:{index}")
        record.setdefault("revision", 1)
        record.setdefault("timezone", timezone)
        record.setdefault("metric_id", metric_id)
        record.setdefault("unit", unit)
        # Require offsets; never guess how ambiguous local timestamps map to UTC.
        records.append(record)
    return MeasurementBatch.model_validate({"source_id": source_id, "records": records})
