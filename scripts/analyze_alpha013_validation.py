#!/usr/bin/env python3
"""Evaluate Alpha #13 only after its immutable 26-week validation lock opens."""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.funding_dispersion_reversal import (
    FundingDispersionConfig,
    load_sealed_markets,
    simulate_forward,
)
from scripts.download_alpha013_day import protocol_digests as acquisition_protocol_digests
from scripts.reconcile_alpha013_funding import reconciled_coverage
from scripts.sync_alpha013_registry import receipt_health as development_receipt_health


LOCK_PATH = PROJECT_ROOT / "research" / "alpha013" / "validation-lock.json"
RAW_ROOT = PROJECT_ROOT / "research" / "alpha013" / "raw"
OUTPUT = PROJECT_ROOT / "reports" / "alpha013-validation.json"
VALIDATION_WEEKS = 26
VALIDATION_PROTOCOL_PATHS = (
    Path("research/alpha013-preregistration.json"),
    Path("scripts/download_alpha013_day.py"),
    Path("quant/research/funding_dispersion_reversal.py"),
    Path("scripts/analyze_alpha013_forward.py"),
    Path("scripts/reconcile_alpha013_funding.py"),
    Path("scripts/sync_alpha013_registry.py"),
    Path("scripts/analyze_alpha013_validation.py"),
    Path("scripts/open_alpha013_validation.py"),
    Path("scripts/sync_alpha013_validation.py"),
)


def validation_protocol_digests(project_root: Path = PROJECT_ROOT) -> dict[str, str]:
    return {
        str(path): sha256((project_root / path).read_bytes()).hexdigest()
        for path in VALIDATION_PROTOCOL_PATHS
    }


def locked_validation_gate(
    base: dict[str, Any],
    double_fee: dict[str, Any],
    slippage: dict[str, Any],
    *,
    funding_reconciled: bool,
) -> dict[str, bool]:
    pnl = base["pnl_components"]
    concentration = base["largest_winning_week_concentration"]
    profit_factor = base["profit_factor_by_week"]
    return {
        "eligible_weeks_minimum_26": int(base["eligible_weeks"]) >= 26,
        "closed_legs_minimum_52": int(base["closed_legs"]) >= 52,
        "net_return_positive": float(base["net_return"]) > 0,
        "raw_relative_price_pnl_positive": float(pnl["raw_relative_price_pnl"]) > 0,
        "price_after_base_fees_and_slippage_positive": float(
            pnl["price_after_fees_and_slippage"]
        ) > 0,
        "profit_factor_minimum_1_25": profit_factor is not None
        and float(profit_factor) >= 1.25,
        "double_fee_net_positive": float(double_fee["net_return"]) > 0,
        "five_bps_slippage_net_positive": float(slippage["net_return"]) > 0,
        "maximum_mark_to_market_drawdown_at_most_0_12": float(
            base["maximum_mark_to_market_drawdown"]
        ) <= 0.12,
        "largest_winning_week_concentration_at_most_0_20": concentration is not None
        and float(concentration) <= 0.20,
        "zero_liquidations": int(base["liquidations"]) == 0,
        "all_funding_rows_reconciled": funding_reconciled,
    }


def funding_reconciliation_only_pending(checks: dict[str, bool]) -> bool:
    return bool(
        checks.get("all_funding_rows_reconciled") is False
        and all(
            value is True
            for name, value in checks.items()
            if name != "all_funding_rows_reconciled"
        )
    )


def validate_lock_chain(
    lock: dict[str, Any],
    *,
    lock_bytes: bytes,
    project_root: Path = PROJECT_ROOT,
    expected_validation_protocol: dict[str, str] | None = None,
    expected_acquisition_protocol: dict[str, str] | None = None,
    receipt_audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validation_protocol = expected_validation_protocol or validation_protocol_digests(
        project_root
    )
    acquisition_protocol = expected_acquisition_protocol or acquisition_protocol_digests()
    if (
        lock.get("schema_version") != 1
        or lock.get("status") != "OPEN"
        or lock.get("immutable") is not True
        or lock.get("candidate") != "ALPHA_013_FUNDING_DISPERSION_REVERSAL"
        or int(lock.get("required_eligible_weeks") or 0) != VALIDATION_WEEKS
        or lock.get("validation_protocol_sha256") != validation_protocol
        or lock.get("acquisition_protocol_sha256") != acquisition_protocol
        or lock.get("promotion_eligible") is not False
    ):
        raise ValueError("Alpha #13 validation lock is stale or structurally invalid")
    report_path = (project_root / str(lock.get("development_report") or "")).resolve()
    receipt_path = (project_root / str(lock.get("development_receipt") or "")).resolve()
    for path in (report_path, receipt_path):
        try:
            path.relative_to(project_root.resolve())
        except ValueError as error:
            raise ValueError("Alpha #13 lock evidence path escapes the project") from error
        if not path.is_file():
            raise ValueError("Alpha #13 lock evidence is missing")
    if sha256(report_path.read_bytes()).hexdigest() != lock.get("development_report_sha256"):
        raise ValueError("Alpha #13 locked development report has changed")
    if sha256(receipt_path.read_bytes()).hexdigest() != lock.get("development_receipt_sha256"):
        raise ValueError("Alpha #13 locked development receipt has changed")
    audit = receipt_audit or development_receipt_health(
        receipt_path=receipt_path,
        report_path=report_path,
        registry_path=project_root / "research" / "registry.json",
        preregistration_path=project_root / "research" / "alpha013-preregistration.json",
        synchronizer_path=project_root / "scripts" / "sync_alpha013_registry.py",
        project_root=project_root,
    )
    if (
        audit.get("valid") is not True
        or audit.get("report_status") != "PASS_DEVELOPMENT"
        or audit.get("registry_event_id") != lock.get("development_registry_event_id")
    ):
        raise ValueError("Alpha #13 locked development authorization is invalid")
    decision = datetime.fromisoformat(
        str(lock["first_validation_decision_utc"]).replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    if decision.weekday() != 0 or (decision.hour, decision.minute, decision.second) != (4, 0, 0):
        raise ValueError("Alpha #13 validation decision is not Monday 04:00 UTC")
    return {
        "status": "PASS",
        "first_validation_decision": decision,
        "lock_sha256": sha256(lock_bytes).hexdigest(),
        "validation_protocol_sha256": validation_protocol,
    }


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=LOCK_PATH)
    parser.add_argument("--raw-root", type=Path, default=RAW_ROOT)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    lock_bytes = args.lock.read_bytes()
    lock = json.loads(lock_bytes)
    try:
        audit = validate_lock_chain(lock, lock_bytes=lock_bytes)
    except ValueError as error:
        raise SystemExit(f"FAIL: {error}") from error
    markets, integrity = load_sealed_markets(args.raw_root)
    config = FundingDispersionConfig(maximum_development_weeks=VALIDATION_WEEKS)
    first_decision = audit["first_validation_decision"]
    base = simulate_forward(markets, first_decision=first_decision, config=config)
    if int(base["eligible_weeks"]) < VALIDATION_WEEKS:
        report = {
            "schema_version": 1,
            "candidate": lock["candidate"],
            "status": "ACCUMULATING_LOCKED_VALIDATION",
            "performance_calculated": False,
            "first_validation_decision_utc": first_decision.isoformat(),
            "eligible_weeks_closed": int(base["eligible_weeks"]),
            "required_eligible_weeks": VALIDATION_WEEKS,
            "lock_sha256": audit["lock_sha256"],
            "validation_protocol_sha256": audit["validation_protocol_sha256"],
            "source_inventory_sha256": integrity["source_inventory_sha256"],
            "promotion_eligible": False,
        }
    else:
        double_fee = simulate_forward(
            markets,
            first_decision=first_decision,
            config=replace(config, fee_per_fill=0.001),
        )
        slippage = simulate_forward(
            markets,
            first_decision=first_decision,
            config=replace(config, slippage_per_fill=0.0005),
        )
        start_day = (first_decision - timedelta(days=7)).date()
        end_day = datetime.fromisoformat(base["weeks"][-1]["close_at"]).date()
        reconciliation = reconciled_coverage(
            args.raw_root, start_day=start_day, end_day=end_day
        )
        checks = locked_validation_gate(
            base,
            double_fee,
            slippage,
            funding_reconciled=reconciliation["status"] == "PASS",
        )
        passed = all(checks.values())
        funding_only_pending = funding_reconciliation_only_pending(checks)
        report = {
            "schema_version": 1,
            "candidate": lock["candidate"],
            "status": (
                "PASS"
                if passed
                else "WAITING_LOCKED_VALIDATION_FUNDING_RECONCILIATION"
                if funding_only_pending
                else "FAIL"
            ),
            "performance_calculated": not funding_only_pending,
            "first_validation_decision_utc": first_decision.isoformat(),
            "required_eligible_weeks": VALIDATION_WEEKS,
            "lock_sha256": audit["lock_sha256"],
            "validation_protocol_sha256": audit["validation_protocol_sha256"],
            "acquisition_protocol_sha256": acquisition_protocol_digests(),
            "data_integrity": integrity,
            "funding_reconciliation": reconciliation,
            "validation": {
                "base": base,
                "double_fee": double_fee,
                "five_bps_slippage": slippage,
            },
            "gate": checks,
            "promotion_eligible": False,
            "warning": "A validation PASS remains a research sleeve until portfolio and live-preflight gates pass.",
        }
        if funding_only_pending:
            report.pop("validation")
            report["eligible_weeks_closed"] = VALIDATION_WEEKS
            report["warning"] = (
                "All economic gates pass provisionally; performance remains sealed until "
                "publisher monthly funding reconciliation is available."
            )
    _atomic_json(args.output, report)
    print(json.dumps({key: value for key, value in report.items() if key != "validation"}, indent=2))


if __name__ == "__main__":
    main()
