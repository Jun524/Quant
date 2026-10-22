#!/usr/bin/env python3
"""Bind the Alpha #7 partial-fill veto policy to the append-only registry."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.registry import ExperimentRegistry


REGISTRY_PATH = PROJECT_ROOT / "research" / "registry.json"
POLICY_PATH = PROJECT_ROOT / "research" / "alpha007-partial-fill-stress-policy.json"
STRATEGY = "ALPHA_007_IMBALANCE_GUIDED_MAKER"
GATE = "ALPHA007_PARTIAL_FILL_VETO_V1"


def main() -> None:
    policy_bytes = POLICY_PATH.read_bytes()
    policy = json.loads(policy_bytes)
    if policy.get("candidate") != STRATEGY or policy.get("policy") != GATE:
        raise SystemExit("partial-fill policy identity mismatch")
    policy_hash = sha256(policy_bytes).hexdigest()
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    experiment = next(
        (item for item in registry.get("experiments") or [] if item.get("strategy") == STRATEGY),
        None,
    )
    if experiment is None:
        raise SystemExit("Alpha #7 experiment is not registered")
    prior = [
        event
        for event in registry.get("events") or []
        if event.get("experiment_id") == experiment["experiment_id"]
        and event.get("event_type") == "auxiliary_gate_preregistered"
        and ((event.get("payload") or {}).get("gate")) == GATE
    ]
    if prior:
        hashes = {(event.get("payload") or {}).get("policy_sha256") for event in prior}
        if hashes != {policy_hash}:
            raise SystemExit("Alpha #7 partial-fill policy was already registered with another hash")
        print(
            json.dumps(
                {
                    "status": "UNCHANGED",
                    "event_id": prior[-1]["event_id"],
                    "policy_sha256": policy_hash,
                },
                indent=2,
            )
        )
        return
    payload = {
        "candidate": STRATEGY,
        "gate": GATE,
        "registered_at_utc": policy["registered_at_utc"],
        "policy_path": str(POLICY_PATH.relative_to(PROJECT_ROOT)),
        "policy_sha256": policy_hash,
        "role": policy["role"],
        "promotion_eligible": False,
        "can_only_veto": True,
    }
    event = ExperimentRegistry(REGISTRY_PATH).append_event(
        experiment_id=int(experiment["experiment_id"]),
        event_type="auxiliary_gate_preregistered",
        payload=payload,
    )
    print(
        json.dumps(
            {
                "status": "REGISTERED",
                "event_id": event["event_id"],
                "policy_sha256": policy_hash,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
