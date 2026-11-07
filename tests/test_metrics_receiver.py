import json
import ssl
import urllib.error
import urllib.request

import pytest

from personal_hub.bootstrap import build_hub_container
from personal_hub.config import HubSettings
from personal_hub.metrics.contracts import MeasurementBatch, MetricSource
from personal_hub.storage.secrets import DeterministicTestCipher, HubSecretVault


@pytest.fixture
def root(tmp_path):
    container = build_hub_container(HubSettings(data_dir=tmp_path / "root"))
    receiver = container.controller.metrics_receiver
    receiver.vault = HubSecretVault(container.settings, cipher=DeterministicTestCipher())
    container.controller.metrics_repository.enroll(
        MetricSource(source_id="phone", name="Phone", metric_ids=("steps",))
    )
    receiver.start("127.0.0.1", 0)
    yield container, receiver
    container.close()


def request(receiver, path, data=None, *, credential=None, source="phone"):
    headers = {"X-Hub-Source": source}
    if credential:
        headers["Authorization"] = "Bearer " + credential
    raw = json.dumps(data).encode() if data is not None else None
    headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"https://127.0.0.1:{receiver.port}{path}", data=raw, headers=headers
    )
    # Production sender pins the fingerprint; this isolated test exercises the
    # receiver against its generated self-signed identity on loopback only.
    with urllib.request.urlopen(req, context=ssl._create_unverified_context(), timeout=5) as result:
        return json.load(result)


def pair(receiver):
    enrollment = json.loads(receiver.enrollment("phone"))
    return request(receiver, "/v1/pair", {k: enrollment[k] for k in ("token", "source_id")})[
        "credential"
    ]


def test_pair_retry_revocation_and_source_impersonation(root):
    container, receiver = root
    credential = pair(receiver)
    body = MeasurementBatch(source_id="phone", records=()).model_dump(mode="json")
    assert (
        request(receiver, "/v1/measurements/batches", body, credential=credential)["accepted"] == 0
    )
    body["source_id"] = "someone-else"
    with pytest.raises(urllib.error.HTTPError) as error:
        request(receiver, "/v1/measurements/batches", body, credential=credential)
    assert error.value.code == 403
    receiver.revoke("phone")
    with pytest.raises(urllib.error.HTTPError) as error:
        request(receiver, "/v1/status", credential=credential)
    assert error.value.code == 401


def test_enrollment_is_single_use_and_expires(root):
    _, receiver = root
    enrollment = json.loads(receiver.enrollment("phone"))
    data = {k: enrollment[k] for k in ("token", "source_id")}
    request(receiver, "/v1/pair", data)
    with pytest.raises(urllib.error.HTTPError):
        request(receiver, "/v1/pair", data)
    enrollment = json.loads(receiver.enrollment("phone"))
    receiver.enrollments[enrollment["token"]] = ("phone", 0)
    with pytest.raises(urllib.error.HTTPError):
        request(receiver, "/v1/pair", {k: enrollment[k] for k in ("token", "source_id")})


def test_pause_closes_listener_and_clears_pairing(root):
    container, receiver = root
    receiver.enrollment("phone")
    container.controller.set_network_paused(True)
    assert receiver.thread is None and not receiver.enrollments
    with pytest.raises(ValueError):
        receiver.start("127.0.0.1", 0)


def test_public_and_wildcard_addresses_rejected(root):
    _, receiver = root
    for host in ("0.0.0.0", "::", "8.8.8.8", "224.0.0.1"):
        with pytest.raises(ValueError):
            receiver.start(host, 0)


def test_wrong_credential_and_oversized_request(root):
    _, receiver = root
    with pytest.raises(urllib.error.HTTPError) as error:
        request(receiver, "/v1/status", credential="incorrect")
    assert error.value.code == 401
    credential = pair(receiver)
    with pytest.raises(urllib.error.HTTPError) as error:
        request(
            receiver,
            "/v1/measurements/batches",
            {"large": "x" * (1024 * 1024 + 1)},
            credential=credential,
        )
    assert error.value.code == 413


def test_production_client_pins_certificate_before_pairing_and_deduplicates_files(root):
    from datetime import UTC, date, datetime

    from personal_hub.metrics.client import post
    from personal_hub.metrics.contracts import STEPS, Measurement, Snapshot
    from personal_hub.metrics.files import decrypt_snapshot, encrypt_snapshot, import_snapshot

    container, receiver = root
    connection = json.loads(receiver.enrollment("phone"))
    claim = {key: connection[key] for key in ("token", "source_id")}
    with pytest.raises(ValueError, match="fingerprint"):
        post(connection | {"fingerprint": "0" * 64}, "/v1/pair", claim)
    connection["credential"] = post(connection, "/v1/pair", claim)["credential"]
    connection.pop("token")
    record = Measurement(
        record_id="steps:UTC:2026-11-01",
        metric_id="steps",
        revision=1,
        value=1234,
        unit="count",
        start=datetime(2026, 11, 1, tzinfo=UTC),
        end=datetime(2026, 11, 2, tzinfo=UTC),
        reporting_date=date(2026, 11, 1),
    )
    batch = MeasurementBatch(source_id="phone", records=(record,))
    assert (
        post(connection, "/v1/measurements/batches", batch.model_dump(mode="json"))["accepted"] == 1
    )
    repo = container.controller.metrics_repository
    snapshot = Snapshot(source=repo.source("phone"), definitions=(STEPS,), batches=(batch,))
    result = import_snapshot(
        repo,
        decrypt_snapshot(
            encrypt_snapshot(snapshot, "long test passphrase"), "long test passphrase"
        ),
    )
    assert result["unchanged"] == 1
    assert repo.summary("steps", date(2026, 11, 1), date(2026, 11, 1)).value == 1234
