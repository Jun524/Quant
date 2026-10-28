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


def build_registry_result(report: dict, *, report_file_sha256: str) -> dict:
    """Project a sealed Alpha #16 development FAIL into the common registry view."""
    if report.get("candidate") != CANDIDATE or report.get("status") != "FAIL":
        raise ValueError("Alpha #16 result projection requires a sealed development FAIL")
    base = report.get("base") or {}
    evidence = report.get("evidence") or {}
    failed_checks = [
        str(item.get("name"))
        for item in report.get("checks") or []
        if isinstance(item, dict) and item.get("passed") is not True
    ]
    if not failed_checks:
        raise ValueError("Alpha #16 FAIL has no failed gate")
    return {
        "status": "FAIL",
        "development_data_acquired": True,
        "validation_opened": False,
        "locked_validation_opened": False,
        "promotion_eligible": False,
        "live_promotion_eligible": False,
        "source_report": "reports/alpha016-development.json",
        "source_sha256": report_file_sha256,
        "source_manifest": report.get("source_manifest"),
        "source_manifest_sha256": report.get("source_manifest_sha256"),
        "report_sha256": report.get("report_sha256"),
        "development": {
            "organic": {
                "trades": int(base.get("trades") or 0),
                "net_profit": float(base.get("sum_net_return") or 0.0),
                "net_return": float(base.get("compounded_net_return") or 0.0),
                "profit_factor": base.get("profit_factor"),
            },
            "pnl_components": {
                "price_pnl_estimate": float(base.get("gross_price_return") or 0.0),
                "trading_fees": -float(base.get("trading_fee_drag") or 0.0),
                "funding_pnl": float(base.get("funding_return") or 0.0),
                "modeled_slippage": -float(base.get("slippage_drag") or 0.0),
                "net_pnl": float(base.get("sum_net_return") or 0.0),
            },
            "breakout_metrics": {
                "maximum_drawdown": base.get("maximum_drawdown"),
                "largest_winner_profit_concentration": base.get(
                    "largest_winner_profit_concentration"
                ),
            },
        },
        "gate": {
            "status": "FAIL",
            "stage": "sealed_historical_development",
            "policy": {"min_oos_trades": 100},
            "checks": [
                {
                    "name": item["name"],
                    "passed": item["passed"],
                    "actual": evidence.get(item["name"]),
                    "required": True,
                    "severity": "fail",
                }
                for item in report.get("checks") or []
                if isinstance(item, dict)
            ],
        },
        "failure_reasons": [
            "INSUFFICIENT_SAMPLE",
            "NO_SIGNAL_EDGE",
            "COST_STRESS_FAILURE",
            "RANDOM_CONTROL_UNDERPERFORMANCE",
            "BOOTSTRAP_CONFIDENCE_FAILURE",
        ],
        "locked_oos": "UNOPENED_DEVELOPMENT_FAIL",
        "orders_submitted": False,
        "live_order_authorization": "CLOSED",
        "warning": (
            "Alpha #16 failed its sealed development gate. Do not open locked OOS, "
            "retune the hypothesis, or use this simulation-only result for promotion."
        ),
    }


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
    result = build_registry_result(report, report_file_sha256=report_digest)
    for event in snapshot.get("events") or []:
        if (
            event.get("experiment_id") == experiment_id
            and event.get("event_type") == "result_update"
            and (event.get("payload") or {}).get("source_sha256") == report_digest
        ):
            print(json.dumps({"status": "ALREADY_MATERIALIZED", "event_id": event["event_id"]}, indent=2))
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
    if not any(
        event.get("experiment_id") == experiment_id
        and event.get("event_type") == "alpha016_development_closed"
        and (event.get("payload") or {}).get("report_file_sha256") == report_digest
        for event in snapshot.get("events") or []
    ):
        registry.append_event(
            experiment_id=experiment_id,
            event_type="alpha016_development_closed",
            payload=payload,
        )
    event = registry.append_event(
        experiment_id=experiment_id,
        event_type="result_update",
        payload={"source_sha256": report_digest, "result": result},
    )
    print(json.dumps({"status": "MATERIALIZED", "event_id": event["event_id"], "verdict": "FAIL"}, indent=2))


if __name__ == "__main__":
    main()
