#!/usr/bin/env python3
"""Append Alpha #12's immutable development verdict to the registry once."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.registry import ExperimentRegistry


CANDIDATE = "ALPHA_012_SPOT_PERP_CASH_AND_CARRY"


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def main() -> None:
    registry_path = PROJECT_ROOT / "research" / "registry.json"
    report_path = PROJECT_ROOT / "reports" / "alpha012-development.json"
    prereg_path = PROJECT_ROOT / "research" / "alpha012-preregistration.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    experiments = [
        item for item in registry.get("experiments", []) if item.get("strategy") == CANDIDATE
    ]
    if len(experiments) != 1:
        raise SystemExit("FAIL: Alpha #12 must be registered exactly once")
    experiment_id = int(experiments[0]["experiment_id"])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("candidate") != CANDIDATE:
        raise SystemExit("FAIL: Alpha #12 report candidate mismatch")
    if report.get("preregistration_sha256") != digest(prereg_path):
        raise SystemExit("FAIL: Alpha #12 report is not bound to the current preregistration")
    if report.get("locked_validation_opened") is not False:
        raise SystemExit("FAIL: locked validation must remain unopened")

    report_sha = digest(report_path)
    existing = [
        event
        for event in registry.get("events", [])
        if event.get("experiment_id") == experiment_id
        and event.get("event_type") == "result_update"
        and ((event.get("payload") or {}).get("result") or {}).get("report_sha256")
        == report_sha
    ]
    if existing:
        print(json.dumps({"status": "ALREADY_SYNCED", "event_id": existing[0]["event_id"]}))
        return

    base = report["development"]["base"]
    failed_checks = sorted(name for name, passed in report["gate"].items() if not passed)
    prior_verdicts = [
        event
        for event in registry.get("events", [])
        if event.get("experiment_id") == experiment_id
        and event.get("event_type") in {"DEVELOPMENT_VERDICT", "result_update"}
    ]
    checks = [
        {
            "name": name,
            "passed": bool(passed),
            "actual": passed,
            "required": True,
            "severity": "fail",
        }
        for name, passed in report["gate"].items()
    ]
    result = {
        "status": "FAIL",
        "primary_failure": "INSUFFICIENT_OPPORTUNITY_AFTER_FRICTION",
        "failure_reasons": [
            "INSUFFICIENT_OPPORTUNITY_AFTER_FRICTION",
            "INSUFFICIENT_SAMPLE",
            "RETURN_TARGET_MISALIGNED",
            "COST_STRESS_FAILURE",
            "CONCENTRATED_PNL",
        ],
        "validation_opened": False,
        "locked_validation_opened": False,
        "report_path": str(report_path.relative_to(PROJECT_ROOT)),
        "report_sha256": report_sha,
        "preregistration_path": str(prereg_path.relative_to(PROJECT_ROOT)),
        "preregistration_sha256": digest(prereg_path),
        "development": {
            "organic": {
                "trades": base["closed_spreads"],
                "net_profit": base["pnl_components"]["net_pnl"],
                "net_return": base["net_return"],
                "profit_factor": base["profit_factor"],
            },
            "pnl_components": {
                "price_pnl_estimate": base["pnl_components"]["raw_basis_price_pnl"],
                "trading_fees": (
                    base["pnl_components"]["spot_fees"]
                    + base["pnl_components"]["futures_fees"]
                ),
                "funding_pnl": base["pnl_components"]["funding_pnl"],
                "modeled_slippage": base["pnl_components"]["modeled_slippage"],
                "net_pnl": base["pnl_components"]["net_pnl"],
            },
            "carry_metrics": {
                "geometric_monthly_return": base["monthly"]["geometric_monthly_return"],
                "maximum_drawdown": base["maximum_mark_to_market_drawdown"],
                "largest_winner_profit_concentration": base[
                    "largest_winner_profit_concentration"
                ],
                "double_fee_net_return": report["development"]["double_fee"]["net_return"],
                "five_bps_slippage_net_return": report["development"][
                    "five_bps_slippage"
                ]["net_return"],
            },
        },
        "gate": {
            "status": "FAIL",
            "stage": "development",
            "checks": checks,
            "policy": {"min_oos_trades": 20},
        },
        "correction": {
            "supersedes_event_id": prior_verdicts[-1]["event_id"] if prior_verdicts else None,
            "reason": "Corrected complete-month boundary to include the final UTC day; trade PnL and FAIL verdict are unchanged.",
        },
    }
    event = ExperimentRegistry(registry_path).append_event(
        experiment_id=experiment_id,
        event_type="result_update",
        payload={"result": result},
    )
    print(
        json.dumps(
            {
                "status": "SYNCED",
                "experiment_id": experiment_id,
                "event_id": event["event_id"],
                "verdict": "FAIL",
                "failed_checks": failed_checks,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
