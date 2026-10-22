#!/usr/bin/env python3
"""Record Alpha #13's pre-sample accounting clarification once."""

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
    prereg_path = PROJECT_ROOT / "research" / "alpha013-preregistration.json"
    prereg = json.loads(prereg_path.read_text(encoding="utf-8"))
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    experiment = next(
        item for item in registry["experiments"]
        if item.get("strategy") == "ALPHA_013_FUNDING_DISPERSION_REVERSAL"
    )
    digest = sha256(prereg_path.read_bytes()).hexdigest()
    existing = [
        event for event in registry.get("events", [])
        if event.get("experiment_id") == experiment["experiment_id"]
        and event.get("event_type") == "PREREGISTRATION_AMENDMENT"
        and (event.get("payload") or {}).get("preregistration_sha256") == digest
    ]
    if existing:
        print(json.dumps({"status": "ALREADY_RECORDED", "event_id": existing[0]["event_id"]}))
        return
    event = ExperimentRegistry(registry_path).append_event(
        experiment_id=int(experiment["experiment_id"]),
        event_type="PREREGISTRATION_AMENDMENT",
        payload={
            "amended_before_first_sample_at_utc": prereg["amended_before_first_sample_at_utc"],
            "first_eligible_decision": prereg["fresh_sample"]["first_eligible_decision"],
            "preregistration_sha256": digest,
            "changes": [
                "weekly two-leg spread is the profit-factor unit",
                "mark-to-market drawdown is measured after each completed 1h candle with exit friction",
                "an 80% adverse open gap is a modeled liquidation hard failure",
                "all funding rows must reconcile to publisher monthly archives",
                "forward reports are bound to the exact ordered day-manifest inventory and current protocol digest",
            ],
            "performance_data_observed_before_change": False
        },
    )
    print(json.dumps({"status": "RECORDED", "event_id": event["event_id"]}, indent=2))


if __name__ == "__main__":
    main()
