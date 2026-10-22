#!/usr/bin/env python3
"""Replay deterministic archival funding intervals without calculating strategy PnL."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import json
import os
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.data.historical_universe import verify_historical_daily_universe
from quant.research.historical_execution_coverage import _periods
from quant.research.historical_funding_ledger import funding_return_for_interval


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


def _timestamp(value: date) -> int:
    return int(datetime.combine(value, time.min, tzinfo=timezone.utc).timestamp() * 1000)


def main() -> None:
    universe = json.loads((PROJECT_ROOT / "research/historical_universe/daily-membership-2024H1.json").read_text(encoding="utf-8"))
    policy = json.loads((PROJECT_ROOT / "research/historical-universe-reconstruction-policy.json").read_text(encoding="utf-8"))
    manifest = json.loads((PROJECT_ROOT / "research/historical_funding/manifest-2024H1.json").read_text(encoding="utf-8"))
    receipt = verify_historical_daily_universe(universe)
    membership = universe["membership"]
    samples = []
    for label, start, end in _periods(policy):
        candidate = next(
            (date.fromisoformat(raw_day), row["selected"][0])
            for raw_day, row in membership.items()
            if start <= date.fromisoformat(raw_day) <= end and row["selected"]
        )
        decision_day, symbol = candidate
        entry_day = decision_day + timedelta(days=1)
        entry, exit_time = _timestamp(entry_day), _timestamp(entry_day + timedelta(days=1))
        long = funding_return_for_interval(manifest, archive_root=PROJECT_ROOT / "research/historical_funding/monthly", symbol=symbol, entry_time_ms=entry, exit_time_ms=exit_time, side="long")
        short = funding_return_for_interval(manifest, archive_root=PROJECT_ROOT / "research/historical_funding/monthly", symbol=symbol, entry_time_ms=entry, exit_time_ms=exit_time, side="short")
        samples.append({"period": label, "decision_day": decision_day.isoformat(), "entry_day": entry_day.isoformat(), "symbol": symbol, "events": len(long["events"]), "long_funding_return": long["funding_return"], "short_funding_return": short["funding_return"], "sign_symmetry": long["funding_return"] + short["funding_return"] == 0.0})
    report = {"schema_version": 1, "status": "PASS" if all(item["events"] > 0 and item["sign_symmetry"] for item in samples) else "FAIL", "kind": "HISTORICAL_FUNDING_LEDGER_REPLAY_ONLY", "universe_sha256": receipt["universe_sha256"], "samples": samples, "promotion_capability": "DIAGNOSTIC_ONLY", "live_order_authorization": "CLOSED", "warning": "These are deterministic funding-ledger replays, not strategy returns."}
    _atomic_write(PROJECT_ROOT / "reports/historical-funding-ledger-audit.json", report)
    print(json.dumps(report, indent=2))
    if report["status"] != "PASS":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
