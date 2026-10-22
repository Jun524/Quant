#!/usr/bin/env python3
"""Append Alpha #14's immutable transfer-test verdict to the registry once."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.registry import ExperimentRegistry


CANDIDATE = "ALPHA_014_CROSS_SECTIONAL_PERP_BASIS_CATCHUP"


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def main() -> None:
    registry_path = PROJECT_ROOT / "research" / "registry.json"
    report_path = PROJECT_ROOT / "reports" / "alpha014-development.json"
    prereg_path = PROJECT_ROOT / "research" / "alpha014-preregistration.json"
    protocol_path = PROJECT_ROOT / "quant" / "research" / "perp_basis_catchup.py"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    experiments = [
        item for item in registry.get("experiments", [])
        if item.get("strategy") == CANDIDATE
    ]
    if len(experiments) != 1:
        raise SystemExit("FAIL: Alpha #14 must be registered exactly once")
    experiment_id = int(experiments[0]["experiment_id"])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("candidate") != CANDIDATE:
        raise SystemExit("FAIL: Alpha #14 report candidate mismatch")
    binding = report.get("evidence_binding") or {}
    if binding.get("preregistration_sha256") != digest(prereg_path):
        raise SystemExit("FAIL: Alpha #14 preregistration changed after evaluation")
    if binding.get("protocol_sha256") != digest(protocol_path):
        raise SystemExit("FAIL: Alpha #14 simulator changed after evaluation")
    if report.get("historical_validation_opened") is not False:
        raise SystemExit("FAIL: Alpha #14 has no historical validation")
    if report.get("live_promotion_eligible") is not False:
        raise SystemExit("FAIL: Alpha #14 development cannot authorize live promotion")

    report_sha = digest(report_path)
    existing = [
        event for event in registry.get("events", [])
        if event.get("experiment_id") == experiment_id
        and event.get("event_type") == "result_update"
        and (((event.get("payload") or {}).get("result") or {}).get("report_sha256"))
        == report_sha
    ]
    if existing:
        print(json.dumps({"status": "ALREADY_SYNCED", "event_id": existing[0]["event_id"]}))
        return

    passed = report.get("status") == "PASS_DEVELOPMENT_TRANSFER_ONLY"
    base = report["development"]["base"]
    checks = [
        {
            "name": name,
            "passed": bool(value),
            "actual": value,
            "required": True,
            "severity": "fail",
        }
        for name, value in report["gate"].items()
    ]
    result = {
        "status": "REVIEW" if passed else "FAIL",
        "development_verdict": report["status"],
        "primary_failure": None if passed else "PERPETUAL_BASIS_TRANSFER_FAILED",
        "validation_opened": False,
        "historical_validation_exists": False,
        "live_promotion_eligible": False,
        "family": "cross_sectional_funding_dispersion",
        "family_trial": 3,
        "independent_alpha": False,
        "report_path": str(report_path.relative_to(PROJECT_ROOT)),
        "report_sha256": report_sha,
        "preregistration_path": str(prereg_path.relative_to(PROJECT_ROOT)),
        "preregistration_sha256": digest(prereg_path),
        "protocol_path": str(protocol_path.relative_to(PROJECT_ROOT)),
        "protocol_sha256": digest(protocol_path),
        "development": {
            "organic": {
                "trades": base["closed_daily_spreads"],
                "net_profit": base["pnl_components"]["net_pnl"],
                "net_return": base["net_return"],
                "profit_factor": base["profit_factor_by_day"],
            },
            "monthly": base["monthly"],
            "max_drawdown": base["maximum_mark_to_market_drawdown"],
            "pnl_components": base["pnl_components"],
        },
        "gate": {
            "status": "REVIEW" if passed else "FAIL",
            "stage": "historical_development_transfer_test",
            "checks": checks,
        },
        "next_step": (
            "Wait for the point-in-time universe gate, then preregister a fresh forward protocol."
            if passed else
            "Close family trial 3; do not tune or open historical validation."
        ),
    }
    event = ExperimentRegistry(registry_path).append_event(
        experiment_id=experiment_id,
        event_type="result_update",
        payload={"result": result},
    )
    failed_checks = sorted(name for name, value in report["gate"].items() if not value)
    print(json.dumps({
        "status": "SYNCED",
        "experiment_id": experiment_id,
        "event_id": event["event_id"],
        "verdict": result["status"],
        "development_verdict": report["status"],
        "failed_checks": failed_checks,
    }, indent=2))


if __name__ == "__main__":
    main()
