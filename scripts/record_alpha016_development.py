#!/usr/bin/env python3
"""Append Alpha 016's sealed development verdict to the experiment registry."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.registry import ExperimentRegistry
from quant.research.spot_perp_flow_protocol import CANDIDATE, canonical_sha256


def _sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def main() -> None:
    report_path = PROJECT_ROOT / "reports/alpha016-development.json"
    preregistration_path = PROJECT_ROOT / "research/alpha016-preregistration.json"
    if not report_path.is_file():
        raise SystemExit("FAIL: development report is absent")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    preregistration = json.loads(preregistration_path.read_text(encoding="utf-8"))
    expected_report_sha = canonical_sha256({key: value for key, value in report.items() if key != "report_sha256"})
    if report.get("report_sha256") != expected_report_sha:
        raise SystemExit("FAIL: development report hash mismatch")
    if report.get("candidate") != CANDIDATE or report.get("phase") != "development":
        raise SystemExit("FAIL: development report candidate or phase mismatch")
    if report.get("preregistration_sha256") != canonical_sha256(preregistration):
        raise SystemExit("FAIL: development report does not bind current preregistration")
    if report.get("orders_submitted") is not False or report.get("live_promotion_eligible") is not False:
        raise SystemExit("FAIL: simulation-only boundary violated")
    if report.get("status") != "FAIL":
        raise SystemExit("FAIL: this recorder only closes a failed development gate")

    registry_path = PROJECT_ROOT / "research/registry.json"
    registry = ExperimentRegistry(registry_path)
    snapshot = registry._load()  # Read-only lookup; append_event performs the locked mutation.
    experiments = [item for item in snapshot["experiments"] if item.get("strategy") == CANDIDATE]
    if len(experiments) != 1:
        raise SystemExit("FAIL: Alpha 016 must have exactly one registered experiment")
    experiment_id = int(experiments[0]["experiment_id"])
    report_digest = _sha256(report_path)
    for event in snapshot.get("events") or []:
        if (
            event.get("experiment_id") == experiment_id
            and event.get("event_type") == "alpha016_development_closed"
            and event.get("payload", {}).get("report_file_sha256") == report_digest
        ):
            print(json.dumps({"status": "ALREADY_RECORDED", "event_id": event["event_id"]}, indent=2))
            return
    evidence = report["evidence"]
    payload = {
        "report": "reports/alpha016-development.json",
        "report_file_sha256": report_digest,
        "report_sha256": report["report_sha256"],
        "source_manifest": report["source_manifest"],
        "source_manifest_sha256": report["source_manifest_sha256"],
        "verdict": "FAIL",
        "failed_checks": [item["name"] for item in report["checks"] if not item["passed"]],
        "evidence": {
            "natural_virtual_trades": evidence["natural_virtual_trades"],
            "net_return": evidence["net_return"],
            "profit_factor": evidence["profit_factor"],
            "double_fee_net_return": evidence["double_fee_net_return"],
            "slippage_stress_net_return": evidence["slippage_stress_net_return"],
            "random_control_excess_return": evidence["random_control_excess_return"],
            "bootstrap_ci95_lower": evidence["bootstrap_ci95_lower"],
            "data_integrity_pass": evidence["data_integrity_pass"],
            "lookahead_pass": evidence["lookahead_pass"],
        },
        "locked_oos": "UNOPENED_DEVELOPMENT_FAIL",
        "orders_submitted": False,
        "live_order_authorization": "CLOSED",
    }
    event = registry.append_event(
        experiment_id=experiment_id,
        event_type="alpha016_development_closed",
        payload=payload,
    )
    print(json.dumps({"status": "RECORDED", "event_id": event["event_id"], "verdict": "FAIL"}, indent=2))


if __name__ == "__main__":
    main()
