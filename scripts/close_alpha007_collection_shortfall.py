#!/usr/bin/env python3
"""Seal Alpha #7 when its bounded collector ends before 17 complete UTC days.

This is deliberately outside the frozen Alpha #7 evaluator.  It records a
collection failure without changing the signal, cost model, or evaluator hash
that was bound into the collected manifests.
"""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.registry import ExperimentRegistry


PRIMARY_PATH = PROJECT_ROOT / "reports" / "alpha007-accumulation.json"
CAMPAIGN_STATE_PATH = PROJECT_ROOT / "research" / "microstructure" / "campaign-state.json"
CLOSURE_PATH = PROJECT_ROOT / "reports" / "alpha007-collection-closure.json"
REGISTRY_PATH = PROJECT_ROOT / "research" / "registry.json"
REQUIRED_COMPLETE_DAYS = 17


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def build_collection_closure(
    primary: dict[str, Any], campaign: dict[str, Any], *, primary_sha256: str, campaign_sha256: str
) -> dict[str, Any]:
    """Build a fail-closed receipt without interpreting incomplete PnL."""
    candidate = "ALPHA_007_IMBALANCE_GUIDED_MAKER"
    if primary.get("candidate") != candidate:
        raise ValueError("unexpected Alpha #7 candidate")
    if primary.get("status") != "ACCUMULATING":
        raise ValueError("primary report is not an accumulating frozen-evaluator report")
    try:
        complete_days = int(primary.get("complete_days"))
    except (TypeError, ValueError) as error:
        raise ValueError("primary report has invalid complete-day count") from error
    if complete_days >= REQUIRED_COMPLETE_DAYS:
        raise ValueError("collection shortfall closure is forbidden after sufficient coverage")
    complete_gate = next(
        (item for item in primary.get("checks") or [] if item.get("name") == "complete_collection_days"),
        None,
    )
    if not isinstance(complete_gate, dict) or complete_gate.get("passed") is not False:
        raise ValueError("primary complete-day gate is inconsistent")
    if (primary.get("complete_day_protocol_audit") or {}).get("status") != "PASS":
        raise ValueError("complete-day protocol audit must pass before collection closure")
    if campaign.get("status") not in {"FINISHED", "STOPPED", "PREFLIGHT_FAILED"}:
        raise ValueError("campaign is not terminal")
    if not campaign.get("ended_at") or not campaign.get("terminal_reason"):
        raise ValueError("campaign terminal metadata is incomplete")
    if campaign.get("status") == "FINISHED" and campaign.get("terminal_reason") != "deadline_reached":
        raise ValueError("finished campaign must have deadline_reached reason")
    return {
        "schema_version": 1,
        "kind": "ALPHA007_BOUNDED_COLLECTION_SHORTFALL_CLOSURE",
        "candidate": candidate,
        "status": "FAIL",
        "failure_class": "INSUFFICIENT_COMPLETE_DAY_COLLECTION",
        "promotion_eligible": False,
        "locked_oos_opened": False,
        "orders_submitted": False,
        "required_complete_days": REQUIRED_COMPLETE_DAYS,
        "actual_complete_days": complete_days,
        "primary_report": {
            "path": "reports/alpha007-accumulation.json",
            "sha256": primary_sha256,
            "status": primary.get("status"),
            "complete_day_protocol_audit": "PASS",
        },
        "campaign": {
            "path": "research/microstructure/campaign-state.json",
            "sha256": campaign_sha256,
            "status": campaign.get("status"),
            "terminal_reason": campaign.get("terminal_reason"),
            "started_at": campaign.get("started_at"),
            "deadline_at": campaign.get("deadline_at"),
            "ended_at": campaign.get("ended_at"),
            "completed_chunks": campaign.get("completed_chunks"),
            "failed_chunks": campaign.get("failed_chunks"),
        },
        "warning": (
            "The fixed campaign ended before 17 complete UTC days. Do not extend, backfill, retune, "
            "open locked OOS, or use this candidate as promotion evidence."
        ),
    }


def validate_collection_closure(
    closure: dict[str, Any], primary: dict[str, Any], campaign: dict[str, Any], *, primary_sha256: str, campaign_sha256: str
) -> bool:
    try:
        expected = build_collection_closure(
            primary, campaign, primary_sha256=primary_sha256, campaign_sha256=campaign_sha256
        )
    except ValueError:
        return False
    return closure == expected


def write_once(path: Path, payload: dict[str, Any]) -> bool:
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing != payload:
            raise ValueError("Alpha #7 collection closure is already sealed with different evidence")
        return False
    _atomic_json(path, payload)
    return True


def main() -> None:
    primary = json.loads(PRIMARY_PATH.read_text(encoding="utf-8"))
    campaign = json.loads(CAMPAIGN_STATE_PATH.read_text(encoding="utf-8"))
    closure = build_collection_closure(
        primary,
        campaign,
        primary_sha256=digest(PRIMARY_PATH),
        campaign_sha256=digest(CAMPAIGN_STATE_PATH),
    )
    created = write_once(CLOSURE_PATH, closure)
    closure_sha256 = digest(CLOSURE_PATH)
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    experiment = next(
        (
            item
            for item in registry.get("experiments") or []
            if item.get("strategy") == closure["candidate"]
        ),
        None,
    )
    if experiment is None:
        raise ValueError("Alpha #7 is not registered")
    result = {
        "status": "FAIL",
        "shadow_status": "FAIL_CLOSED_INSUFFICIENT_COLLECTION",
        "promotion_eligible": False,
        "validation_opened": False,
        "locked_validation_opened": False,
        "source_report": "reports/alpha007-collection-closure.json",
        "source_sha256": closure_sha256,
        "gate": {
            "status": "FAIL",
            "stage": "bounded_collection_shortfall",
            "checks": [
                {"name": "complete_collection_days", "passed": False, "actual": closure["actual_complete_days"], "required": REQUIRED_COMPLETE_DAYS},
                {"name": "complete_day_protocol_audit", "passed": True, "actual": "PASS", "required": "PASS"},
            ],
        },
        "collection_closure": closure,
        "warning": closure["warning"],
    }
    existing = next(
        (
            item
            for item in registry.get("events") or []
            if item.get("experiment_id") == experiment.get("experiment_id")
            and item.get("event_type") == "result_update"
            and (((item.get("payload") or {}).get("result") or {}).get("source_sha256")) == closure_sha256
        ),
        None,
    )
    event = existing or ExperimentRegistry(REGISTRY_PATH).append_event(
        experiment_id=int(experiment["experiment_id"]),
        event_type="result_update",
        payload={"source_sha256": closure_sha256, "result": result},
    )
    print(json.dumps({"status": "CREATED" if created else "ALREADY_SEALED", "event_id": event["event_id"], "closure_sha256": closure_sha256}, indent=2))


if __name__ == "__main__":
    main()
