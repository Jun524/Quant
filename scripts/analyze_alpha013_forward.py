#!/usr/bin/env python3
"""Evaluate Alpha #13 using only current-protocol sealed forward days."""

from __future__ import annotations

from datetime import datetime, timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.funding_dispersion_reversal import evaluate_development, load_sealed_markets
from scripts.reconcile_alpha013_funding import reconciled_coverage


PREREGISTRATION = PROJECT_ROOT / "research" / "alpha013-preregistration.json"
RAW_ROOT = PROJECT_ROOT / "research" / "alpha013" / "raw"
OUTPUT = PROJECT_ROOT / "reports" / "alpha013-forward.json"


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main() -> None:
    prereg = json.loads(PREREGISTRATION.read_text(encoding="utf-8"))
    markets, integrity = load_sealed_markets(RAW_ROOT)
    preliminary = evaluate_development(
        markets,
        first_decision=prereg["fresh_sample"]["first_eligible_decision"],
        funding_reconciled=False,
    )
    weeks = preliminary["development"]["base"]["weeks"]
    if weeks:
        first_decision = datetime.fromisoformat(
            prereg["fresh_sample"]["first_eligible_decision"].replace("Z", "+00:00")
        )
        start_day = (first_decision - timedelta(days=7)).date()
        end_day = datetime.fromisoformat(weeks[-1]["close_at"]).date()
        reconciliation = reconciled_coverage(
            RAW_ROOT, start_day=start_day, end_day=end_day
        )
    else:
        reconciliation = {
            "status": "NOT_APPLICABLE_YET",
            "required_days": 0,
            "covered_days": [],
            "failures": [],
        }
    report = evaluate_development(
        markets,
        first_decision=prereg["fresh_sample"]["first_eligible_decision"],
        funding_reconciled=reconciliation["status"] == "PASS",
    )
    report["data_integrity"] = integrity
    report["funding_reconciliation"] = reconciliation
    report["preregistration_sha256"] = sha256(PREREGISTRATION.read_bytes()).hexdigest()
    report["preregistration_path"] = str(PREREGISTRATION.relative_to(PROJECT_ROOT))
    _atomic_write(OUTPUT, report)
    base = report["development"]["base"]
    print(
        json.dumps(
            {
                "candidate": report["candidate"],
                "status": report["status"],
                "sealed_days": integrity["sealed_days"],
                "eligible_weeks": base["eligible_weeks"],
                "closed_legs": base["closed_legs"],
                "net_return": base["net_return"],
                "raw_relative_price_pnl": base["pnl_components"]["raw_relative_price_pnl"],
                "maximum_mark_to_market_drawdown": base["maximum_mark_to_market_drawdown"],
                "funding_reconciled": integrity["funding_reconciled"],
                "locked_validation_opened": False,
                "report": str(OUTPUT.relative_to(PROJECT_ROOT)),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
