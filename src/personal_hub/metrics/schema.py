"""Print the public v1 JSON Schema: python -m personal_hub.metrics.schema."""

import json

from pydantic import TypeAdapter

from personal_hub.metrics.contracts import DeviceEnrollment, MeasurementBatch, Snapshot


def schema():
    return TypeAdapter(MeasurementBatch | Snapshot | DeviceEnrollment).json_schema() | {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Personal Hub Metrics v1",
    }


if __name__ == "__main__":
    print(json.dumps(schema(), indent=2))
