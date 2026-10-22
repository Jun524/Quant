#!/usr/bin/env python3
"""Preregister Alpha #7 order-path conformance and its implementation hash."""

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
POLICY_PATH = PROJECT_ROOT / "research" / "alpha007-order-path-policy.json"
IMPLEMENTATION_PATH = PROJECT_ROOT / "quant" / "execution" / "alpha007_order_path.py"
FILTER_AUDIT_PATH = PROJECT_ROOT / "reports" / "alpha007-exchange-filter-audit.json"
STRATEGY = "ALPHA_007_IMBALANCE_GUIDED_MAKER"
GATE = "ALPHA007_ORDER_PATH_CONFORMANCE_V1"


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def main() -> None:
    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    audit = json.loads(FILTER_AUDIT_PATH.read_text(encoding="utf-8"))
    if policy.get("candidate") != STRATEGY or policy.get("policy") != GATE:
        raise SystemExit("order-path policy identity mismatch")
    if audit.get("candidate") != STRATEGY or audit.get("promotion_eligible") is not False:
        raise SystemExit("initial exchange-filter audit identity mismatch")
    policy_hash = _digest(POLICY_PATH)
    implementation_hash = _digest(IMPLEMENTATION_PATH)
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
    identity = (policy_hash, implementation_hash)
    if prior:
        identities = {
            (
                (event.get("payload") or {}).get("policy_sha256"),
                (event.get("payload") or {}).get("implementation_sha256"),
            )
            for event in prior
        }
        if identities != {identity}:
            raise SystemExit("Alpha #7 order-path gate was already registered differently")
        print(
            json.dumps(
                {
                    "status": "UNCHANGED",
                    "event_id": prior[-1]["event_id"],
                    "policy_sha256": policy_hash,
                    "implementation_sha256": implementation_hash,
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
        "implementation_path": str(IMPLEMENTATION_PATH.relative_to(PROJECT_ROOT)),
        "implementation_sha256": implementation_hash,
        "role": policy["role"],
        "promotion_eligible": False,
        "can_only_veto": True,
        "initial_exchange_filter_audit": {
            "path": str(FILTER_AUDIT_PATH.relative_to(PROJECT_ROOT)),
            "sha256": _digest(FILTER_AUDIT_PATH),
            "status": audit["status"],
            "generated_at_utc": audit["generated_at_utc"],
        },
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
                "implementation_sha256": implementation_hash,
                "initial_exchange_filter_status": audit["status"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
