#!/usr/bin/env python3
"""Evaluate only Alpha #12's preregistered development interval."""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.spot_perp_carry import evaluate_development


PREREGISTRATION = PROJECT_ROOT / "research" / "alpha012-preregistration.json"
OUTPUT = PROJECT_ROOT / "reports" / "alpha012-development.json"


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
    if prereg.get("candidate") != "ALPHA_012_SPOT_PERP_CASH_AND_CARRY":
        raise SystemExit("FAIL: Alpha #12 preregistration is missing or invalid")
    report = evaluate_development(
        PROJECT_ROOT / "user_data" / "data" / "binance" / "futures"
    )
    report["preregistration_path"] = str(PREREGISTRATION.relative_to(PROJECT_ROOT))
    report["preregistration_sha256"] = digest(PREREGISTRATION)
    report["locked_validation_timerange"] = "2024-01-01--2026-09-01"
    report["locked_validation_opened"] = False
    atomic_write(OUTPUT, report)
    base = report["development"]["base"]
    print(
        json.dumps(
            {
                "candidate": report["candidate"],
                "status": report["status"],
                "closed_spreads": base["closed_spreads"],
                "net_return": base["net_return"],
                "geometric_monthly_return": base["monthly"]["geometric_monthly_return"],
                "profit_factor": base["profit_factor"],
                "maximum_mark_to_market_drawdown": base["maximum_mark_to_market_drawdown"],
                "funding_pnl": base["pnl_components"]["funding_pnl"],
                "raw_basis_price_pnl": base["pnl_components"]["raw_basis_price_pnl"],
                "gate": report["gate"],
                "locked_validation_opened": False,
                "report": str(OUTPUT.relative_to(PROJECT_ROOT)),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
