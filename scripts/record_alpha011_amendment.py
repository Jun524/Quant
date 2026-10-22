#!/usr/bin/env python3
"""Record the pre-result Alpha #11 acquisition amendment once."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.registry import ExperimentRegistry


def main() -> None:
    registry_path = PROJECT_ROOT / "research" / "registry.json"
    prereg_path = PROJECT_ROOT / "research" / "alpha011-preregistration.json"
    data = json.loads(registry_path.read_text(encoding="utf-8"))
    experiment = next(
        item
        for item in data["experiments"]
        if item.get("strategy") == "ALPHA_011_QUARTER_HOUR_FLOW_CONTINUATION"
    )
    source_hash = sha256(prereg_path.read_bytes()).hexdigest()
    prior = [
        event
        for event in data.get("events", [])
        if event.get("experiment_id") == experiment["experiment_id"]
        and event.get("event_type") == "pre_result_protocol_amendment"
        and (event.get("payload") or {}).get("source_sha256") == source_hash
    ]
    if prior:
        print(json.dumps({"status": "UNCHANGED", "source_sha256": source_hash}, indent=2))
        return
    event = ExperimentRegistry(registry_path).append_event(
        experiment_id=int(experiment["experiment_id"]),
        event_type="pre_result_protocol_amendment",
        payload={
            "source": "research/alpha011-preregistration.json",
            "source_sha256": source_hash,
            "sample_state": "no Alpha #11 observation or result evaluated",
            "scope": "acquisition transport only; economic protocol unchanged",
        },
    )
    print(json.dumps({"status": "APPENDED", "event_id": event["event_id"], "source_sha256": source_hash}, indent=2))


if __name__ == "__main__":
    main()
