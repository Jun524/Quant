#!/usr/bin/env python3
"""Evaluate only the immutable 30-day liquidation-overlay validation slice."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.liquidation_history import assess_liquidation_history
from quant.research.liquidation_overlay import evaluate_overlay
from quant.research.liquidation_telemetry import protocol_digests
from scripts.analyze_liquidation_overlay import _stream_selected_records, _valid_sources


ROOT = PROJECT_ROOT / "research" / "liquidation_telemetry"
POLICY_PATH = PROJECT_ROOT / "research" / "liquidation-telemetry-policy.json"
LOCK_PATH = ROOT / "validation-lock.json"


def main() -> None:
    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    protocol = protocol_digests(PROJECT_ROOT)
    lock_bytes = LOCK_PATH.read_bytes()
    lock = json.loads(lock_bytes)
    development_path = PROJECT_ROOT / str(lock["development_report"])
    if (
        lock.get("status") != "OPEN"
        or lock.get("immutable") is not True
        or lock.get("protocol_sha256") != protocol
        or sha256(development_path.read_bytes()).hexdigest()
        != lock.get("development_report_sha256")
    ):
        raise SystemExit("validation lock is stale, mutable, or not development-bound")
    validation_start = datetime.fromisoformat(str(lock["validation_start_utc"])).astimezone(timezone.utc).date()
    coverage = assess_liquidation_history(ROOT, policy=policy, expected_protocol=protocol)
    if coverage["invalid_chunks"]:
        report = {
            "schema_version": 1,
            "candidate": policy["candidate"],
            "status": "DATA_INTEGRITY_FAILURE",
            "performance_calculated": False,
            "lock_sha256": sha256(lock_bytes).hexdigest(),
            "coverage": coverage,
        }
    else:
        eligible = [
            date.fromisoformat(value)
            for value in coverage["complete_utc_days"]
            if date.fromisoformat(value) >= validation_start
        ]
        required = int(policy["locked_validation_gate"]["complete_utc_days"])
        if len(eligible) < required:
            report = {
                "schema_version": 1,
                "candidate": policy["candidate"],
                "status": "ACCUMULATING_LOCKED_VALIDATION",
                "performance_calculated": False,
                "validation_complete_days": len(eligible),
                "validation_days_required": required,
                "lock_sha256": sha256(lock_bytes).hexdigest(),
            }
        else:
            selected = set(sorted(eligible)[:required])
            sources = _valid_sources(protocol, selected)
            liquidations, open_interest, marks = _stream_selected_records(sources, selected)
            validation_policy = deepcopy(policy)
            validation_policy["development_gate"] = deepcopy(policy["locked_validation_gate"])
            validation = evaluate_overlay(
                liquidations,
                open_interest,
                marks,
                policy=validation_policy,
                complete_days=selected,
            )
            report = {
                "schema_version": 1,
                "candidate": policy["candidate"],
                "status": "PASS_RISK_OVERLAY_ONLY_NOT_LIVE_AUTHORIZATION" if validation["status"] == "PASS" else "FAIL",
                "performance_calculated": True,
                "promotion_eligible": False,
                "protocol_sha256": protocol,
                "lock_sha256": sha256(lock_bytes).hexdigest(),
                "selected_validation_days": [day.isoformat() for day in sorted(selected)],
                "latest_opened_utc_day": max(selected).isoformat(),
                "validation": validation,
            }
    output = PROJECT_ROOT / "reports" / "liquidation-overlay-validation.json"
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    if report["status"] in {"DATA_INTEGRITY_FAILURE", "FAIL"}:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
