#!/usr/bin/env python3
"""Audit the completed historical daily universe against its raw archives."""

from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.data.historical_universe import (
    HistoricalUniverseError,
    load_historical_daily_price_panel,
    verify_historical_daily_universe,
)


def _expected_days(start: date, end_exclusive: date) -> list[str]:
    if start >= end_exclusive:
        raise HistoricalUniverseError("historical audit calendar is empty")
    values: list[str] = []
    current = start
    while current < end_exclusive:
        values.append(current.isoformat())
        current += timedelta(days=1)
    return values


def audit(artifact: dict, policy: dict, *, archive_root: Path) -> dict:
    """Reconcile every selected member to one verified historical OHLC row."""
    verified = verify_historical_daily_universe(artifact)
    try:
        calendar = policy["calendar"]
        expected_days = _expected_days(
            date.fromisoformat(str(calendar["archive_start"])),
            date.fromisoformat(str(calendar["archive_end_exclusive"])),
        )
    except (KeyError, ValueError, TypeError) as error:
        raise HistoricalUniverseError("historical audit policy calendar is invalid") from error
    membership = artifact["membership"]
    assert isinstance(membership, dict)
    actual_days = list(membership)
    panel = load_historical_daily_price_panel(artifact, archive_root=archive_root)
    expected_counts = {day: len(membership[day]["selected"]) for day in actual_days}
    actual_counts = panel.groupby("decision_day")["symbol"].count().to_dict()
    mismatch_days = [
        day for day, count in expected_counts.items() if int(actual_counts.get(day, 0)) != count
    ]
    selected_symbols = sorted({symbol for row in membership.values() for symbol in row["selected"]})
    checks = [
        {"name": "policy_day_coverage", "passed": actual_days == expected_days, "required": len(expected_days), "actual": len(actual_days)},
        {"name": "selected_rows_reconciled", "passed": not mismatch_days, "required": verified["selected_symbol_days"], "actual": len(panel)},
        {"name": "nonempty_selected_universe", "passed": bool(selected_symbols), "required": "> 0", "actual": len(selected_symbols)},
        {"name": "diagnostic_only", "passed": artifact.get("promotion_capability") == "DIAGNOSTIC_ONLY", "required": "DIAGNOSTIC_ONLY", "actual": artifact.get("promotion_capability")},
    ]
    return {
        "schema_version": 1,
        "status": "PASS" if all(item["passed"] for item in checks) else "FAIL",
        "kind": "HISTORICAL_UNIVERSE_SOURCE_RECONCILIATION",
        "checks": checks,
        "universe_sha256": verified["universe_sha256"],
        "days": verified["days"],
        "selected_symbol_days": verified["selected_symbol_days"],
        "unique_selected_symbols": len(selected_symbols),
        "minimum_daily_selected_symbols": min(expected_counts.values()),
        "median_daily_selected_symbols": int(sorted(expected_counts.values())[len(expected_counts) // 2]),
        "maximum_daily_selected_symbols": max(expected_counts.values()),
        "mismatch_days": mismatch_days[:20],
        "promotion_capability": "DIAGNOSTIC_ONLY",
        "live_order_authorization": "CLOSED",
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, default=Path("research/historical_universe/daily-membership-2024H1.json"))
    parser.add_argument("--policy", type=Path, default=Path("research/historical-universe-reconstruction-policy.json"))
    parser.add_argument("--archive-root", type=Path, default=Path("research/historical_universe/monthly_1d"))
    parser.add_argument("--output", type=Path, default=Path("reports/historical-universe-audit.json"))
    args = parser.parse_args()
    resolve = lambda path: path if path.is_absolute() else PROJECT_ROOT / path
    report = audit(
        json.loads(resolve(args.artifact).read_text(encoding="utf-8")),
        json.loads(resolve(args.policy).read_text(encoding="utf-8")),
        archive_root=resolve(args.archive_root),
    )
    output = resolve(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] != "PASS":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
