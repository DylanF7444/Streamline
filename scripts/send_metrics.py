"""Example external sender; Windows credentials stay in a DPAPI-protected vault.

Pair: python scripts/send_metrics.py --state <private-directory> --pair
Send: python scripts/send_metrics.py --state <private-directory> --file readings.json
The input file is retained, so failed or interrupted transfers can be retried.
"""

from __future__ import annotations

import argparse
import getpass
import json
import time
from pathlib import Path

from personal_hub.config import HubSettings
from personal_hub.metrics.client import post
from personal_hub.metrics.contracts import MeasurementBatch
from personal_hub.storage.secrets import HubSecretVault


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--pair", action="store_true")
    parser.add_argument("--file", type=Path)
    args = parser.parse_args()
    vault = HubSecretVault(HubSettings(data_dir=args.state))
    if args.pair:
        enrollment = json.loads(getpass.getpass("Paste the root pairing string: "))
        response = post(
            enrollment,
            "/v1/pair",
            {"token": enrollment["token"], "source_id": enrollment["source_id"]},
        )
        enrollment.pop("token")
        enrollment["credential"] = response["credential"]
        vault.set("metrics-sender", json.dumps(enrollment))
        print("Paired. Credential saved in the private Windows vault.")
    if args.file:
        connection = json.loads(vault.get("metrics-sender") or "null")
        if not connection:
            raise ValueError("pair the sender first")
        with args.file.open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("input exceeds the batch limit")
        batch = MeasurementBatch.model_validate_json(raw)
        if batch.source_id != connection["source_id"]:
            raise ValueError("input source must match the paired sender")
        for attempt in range(5):
            try:
                result = post(connection, "/v1/measurements/batches", batch.model_dump(mode="json"))
                print(json.dumps(result))
                return
            except (OSError, ValueError):
                if attempt == 4:
                    raise ValueError(
                        "Transfer failed; retain the input and retry after checking the root."
                    ) from None
                time.sleep(min(2**attempt, 8))


if __name__ == "__main__":
    main()
