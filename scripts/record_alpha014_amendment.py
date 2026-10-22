#!/usr/bin/env python3
"""Record Alpha #14's pre-result data-boundary amendment once."""

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
    prereg_path = PROJECT_ROOT / "research" / "alpha014-preregistration.json"
    data = json.loads(registry_path.read_text(encoding="utf-8"))
    matches = [
        item for item in data.get("experiments", [])
        if item.get("strategy") == "ALPHA_014_CROSS_SECTIONAL_PERP_BASIS_CATCHUP"
    ]
    if len(matches) != 1:
        raise SystemExit("FAIL: Alpha #14 must be registered exactly once")
    experiment_id = int(matches[0]["experiment_id"])
    existing = [
        event for event in data.get("events", [])
        if event.get("experiment_id") == experiment_id
        and event.get("event_type") == "PRERESULT_DATA_BOUNDARY_AMENDMENT"
    ]
    if existing:
        print(json.dumps({"status": "ALREADY_RECORDED", "event_id": existing[0]["event_id"]}))
        return
    event = ExperimentRegistry(registry_path).append_event(
        experiment_id=experiment_id,
        event_type="PRERESULT_DATA_BOUNDARY_AMENDMENT",
        payload={
            "first_eligible_decision": "2022-01-02T00:00:00Z",
            "reason": "The first attempted run raised on the absent 2021-12-31T23:00:00Z spot signal candle before any cross-sectional rank, trade, or return was computed.",
            "parameters_changed": ["first_eligible_decision"],
            "economic_parameters_changed": [],
            "return_observed_before_amendment": False,
            "preregistration_sha256": sha256(prereg_path.read_bytes()).hexdigest(),
        },
    )
    print(json.dumps({"status": "RECORDED", "event_id": event["event_id"]}, indent=2))


if __name__ == "__main__":
    main()
