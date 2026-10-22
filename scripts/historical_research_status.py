#!/usr/bin/env python3
"""Validate and report the historical-versus-forward evidence policy."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.historical_admission import evaluate_historical_admission
from quant.research.historical_cost_guard import evaluate_historical_cost_policy
from quant.research.historical_release_train import evaluate_historical_release_train
from quant.data.historical_universe import verify_historical_daily_universe


def report_is_bound_to_universe(report: object, universe_sha256: object) -> bool:
    """Return true only for a report explicitly bound to the sealed universe."""
    return bool(
        isinstance(report, dict)
        and isinstance(universe_sha256, str)
        and len(universe_sha256) == 64
        and report.get("universe_sha256") == universe_sha256
    )


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def main() -> None:
    policy_path = PROJECT_ROOT / "research" / "historical-research-policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    report = evaluate_historical_admission(policy)
    report["policy_path"] = str(policy_path.relative_to(PROJECT_ROOT))
    audit_path = PROJECT_ROOT / "reports" / "historical-universe-audit.json"
    execution_coverage_path = PROJECT_ROOT / "reports" / "historical-execution-coverage.json"
    funding_coverage_path = PROJECT_ROOT / "reports" / "historical-funding-coverage.json"
    funding_ledger_audit_path = PROJECT_ROOT / "reports" / "historical-funding-ledger-audit.json"
    cost_path = PROJECT_ROOT / "research" / "historical-cost-accounting-policy.json"
    release_policy_path = PROJECT_ROOT / "research" / "historical-release-train-policy.json"
    baseline_path = PROJECT_ROOT / "research" / "historical_universe" / "daily-membership-2024H1.json"
    try:
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        audit = None
    try:
        execution_coverage = json.loads(execution_coverage_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        execution_coverage = None
    try:
        funding_coverage = json.loads(funding_coverage_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        funding_coverage = None
    try:
        funding_ledger_audit = json.loads(funding_ledger_audit_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        funding_ledger_audit = None
    try:
        cost = evaluate_historical_cost_policy(json.loads(cost_path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValueError):
        cost = None
    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        baseline_receipt = verify_historical_daily_universe(baseline)
        release_train = evaluate_historical_release_train(
            json.loads(release_policy_path.read_text(encoding="utf-8")),
            baseline_universe_sha256=baseline_receipt["universe_sha256"],
            baseline_audit_status=audit.get("status") if isinstance(audit, dict) else None,
        )
    except (OSError, json.JSONDecodeError, ValueError):
        baseline_receipt = None
        release_train = None
    universe_sha256 = (
        baseline_receipt.get("universe_sha256")
        if isinstance(baseline_receipt, dict)
        else None
    )
    funding_coverage_bound = report_is_bound_to_universe(funding_coverage, universe_sha256)
    audit_bound = report_is_bound_to_universe(audit, universe_sha256)
    execution_coverage_bound = report_is_bound_to_universe(execution_coverage, universe_sha256)
    funding_ledger_bound = report_is_bound_to_universe(funding_ledger_audit, universe_sha256)
    archival_ready = bool(
        isinstance(audit, dict)
        and audit.get("status") == "PASS"
        and audit_bound
        and isinstance(execution_coverage, dict)
        and execution_coverage.get("status") == "PASS"
        and execution_coverage_bound
        and isinstance(funding_coverage, dict)
        and funding_coverage.get("status") == "PASS"
        and funding_coverage_bound
        and isinstance(funding_ledger_audit, dict)
        and funding_ledger_audit.get("status") == "PASS"
        and funding_ledger_bound
        and isinstance(cost, dict)
        and cost.get("status") == "PASS"
        and isinstance(release_train, dict)
        and release_train.get("status") == "PASS"
    )
    report["archival_diagnostic_readiness"] = {
        "status": "READY_DIAGNOSTIC_ONLY" if archival_ready else "NOT_READY",
        "universe_audit": audit.get("status") if isinstance(audit, dict) else "MISSING",
        "universe_audit_universe_bound": audit_bound,
        "next_open_execution_coverage": execution_coverage.get("status") if isinstance(execution_coverage, dict) else "MISSING",
        "next_open_execution_coverage_universe_bound": execution_coverage_bound,
        "one_day_funding_coverage": funding_coverage.get("status") if isinstance(funding_coverage, dict) else "MISSING",
        "one_day_funding_coverage_universe_bound": funding_coverage_bound,
        "funding_ledger_replay": funding_ledger_audit.get("status") if isinstance(funding_ledger_audit, dict) else "MISSING",
        "funding_ledger_replay_universe_bound": funding_ledger_bound,
        "cost_policy": cost.get("status") if isinstance(cost, dict) else "MISSING",
        "release_train": release_train.get("status") if isinstance(release_train, dict) else "MISSING",
        "promotion_capability": "DIAGNOSTIC_ONLY",
    }
    report["historical_release_train"] = {
        "status": release_train.get("status") if isinstance(release_train, dict) else "NOT_READY",
        "next_withheld_partition": (
            release_train.get("next_withheld_partition", {}).get("id")
            if isinstance(release_train, dict) else None
        ),
        "warning": "A withheld partition may be accessed only after an independently preregistered candidate and a sealed prior-partition verdict.",
    }
    report["historical_point_in_time_replication"] = {
        "status": "NOT_READY",
        "missing": [
            "contemporaneous historical contract-status source",
            "historical bid-ask spread source",
            "historical orderbook-depth source",
        ],
        "warning": "The reconstructed daily archive universe is suitable for archival diagnostics, not point-in-time promotion evidence.",
    }
    _atomic_write(PROJECT_ROOT / "reports" / "historical-research-status.json", report)
    print(json.dumps(report, indent=2))
    if report["status"] != "PASS":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
