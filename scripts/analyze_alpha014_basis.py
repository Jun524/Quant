#!/usr/bin/env python3
"""Evaluate only Alpha #14's preregistered historical development interval."""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.perp_basis_catchup import evaluate_development


CANDIDATE = "ALPHA_014_CROSS_SECTIONAL_PERP_BASIS_CATCHUP"
PREREGISTRATION = PROJECT_ROOT / "research" / "alpha014-preregistration.json"
SCREENING = PROJECT_ROOT / "research" / "alpha-candidate-screening-20261010.json"
PROTOCOL = PROJECT_ROOT / "quant" / "research" / "perp_basis_catchup.py"
OUTPUT = PROJECT_ROOT / "reports" / "alpha014-development.json"


def digest(path: Path) -> str:
    value = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def atomic_write(path: Path, payload: dict) -> None:
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
    prereg = json.loads(PREREGISTRATION.read_text(encoding="utf-8"))
    if prereg.get("candidate") != CANDIDATE:
        raise SystemExit("FAIL: Alpha #14 preregistration is missing or invalid")
    if prereg.get("status") != "PREREGISTERED_DEVELOPMENT_ONLY_TRANSFER_TEST":
        raise SystemExit("FAIL: Alpha #14 protocol role changed")
    report = evaluate_development(
        PROJECT_ROOT / "user_data" / "data" / "binance" / "futures"
    )
    report["evidence_binding"] = {
        "preregistration_path": str(PREREGISTRATION.relative_to(PROJECT_ROOT)),
        "preregistration_sha256": digest(PREREGISTRATION),
        "screening_path": str(SCREENING.relative_to(PROJECT_ROOT)),
        "screening_sha256": digest(SCREENING),
        "protocol_path": str(PROTOCOL.relative_to(PROJECT_ROOT)),
        "protocol_sha256": digest(PROTOCOL),
    }
    report["development_timerange"] = "2022-01-01--2024-01-01"
    report["historical_validation_opened"] = False
    report["live_promotion_eligible"] = False
    atomic_write(OUTPUT, report)
    base = report["development"]["base"]
    print(json.dumps({
        "candidate": CANDIDATE,
        "status": report["status"],
        "closed_daily_spreads": base["closed_daily_spreads"],
        "net_return": base["net_return"],
        "geometric_monthly_return": base["monthly"]["geometric_monthly_return"],
        "profit_factor": base["profit_factor_by_day"],
        "maximum_mark_to_market_drawdown": base["maximum_mark_to_market_drawdown"],
        "raw_relative_price_pnl": base["pnl_components"]["raw_relative_price_pnl"],
        "funding_pnl": base["pnl_components"]["funding_pnl"],
        "gate": report["gate"],
        "historical_validation_opened": False,
        "live_promotion_eligible": False,
        "report": str(OUTPUT.relative_to(PROJECT_ROOT)),
    }, indent=2))


if __name__ == "__main__":
    main()
