#!/usr/bin/env python3
"""Append the sealed Alpha #10 development verdict as an immutable registry event."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.registry import ExperimentRegistry


def main() -> None:
    registry_path = PROJECT_ROOT / "research" / "registry.json"
    report_path = PROJECT_ROOT / "reports" / "alpha010-development.json"
    data = json.loads(registry_path.read_text(encoding="utf-8"))
    experiment = next(
        (
            item
            for item in data.get("experiments", [])
            if item.get("strategy") == "ALPHA_010_LEVERAGE_CROWDING_CRASH_SHORT"
        ),
        None,
    )
    if experiment is None:
        raise SystemExit("Alpha #10 is not registered; run make alpha010-register first")
    source_hash = sha256(report_path.read_bytes()).hexdigest()
    prior = [
        event
        for event in data.get("events", [])
        if event.get("experiment_id") == experiment["experiment_id"]
        and event.get("event_type") == "result_update"
    ]
    if prior and (prior[-1].get("payload") or {}).get("source_sha256") == source_hash:
        print(json.dumps({"status": "UNCHANGED", "source_sha256": source_hash}, indent=2))
        return

    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("validation_opened") is not False:
        raise SystemExit("Alpha #10 development report unexpectedly opened validation")
    base = report["development"]["base"]
    development = {
        "organic": {
            "trades": base["trades"],
            "wins": base["wins"],
            "losses": base["losses"],
            "net_profit": base["sum_net_return"],
            "net_return": base["compounded_net_return"],
            "profit_factor": base["profit_factor"],
        },
        "max_drawdown": base["maximum_drawdown"],
        "estimated_fee_cost": base["trading_fee_drag_sum"],
        "funding_net": base["funding_return_sum"],
        "pnl_components": {
            "basis": "fractions of 1x trade notional; price + fees + funding + modeled slippage = arithmetic net",
            "price_pnl_estimate": base["price_return_sum"],
            "trading_fees": -base["trading_fee_drag_sum"],
            "funding_pnl": base["funding_return_sum"],
            "modeled_slippage": -base["slippage_drag_sum"],
            "net_pnl": base["sum_net_return"],
        },
        "leverage_crowding_metrics": {
            "three_percent_crash_precision": base["three_percent_crash_precision"],
            "unconditional_three_percent_crash_rate": base[
                "unconditional_three_percent_crash_rate"
            ],
            "three_percent_crash_precision_lift": base[
                "three_percent_crash_precision_lift"
            ],
            "top_three_winner_profit_concentration": base[
                "top_three_winner_profit_concentration"
            ],
            "stop_exits": base["stop_exits"],
            "time_exits": base["time_exits"],
        },
        "data_integrity": report["data_integrity"],
    }
    result = {
        "source_report": "reports/alpha010-development.json",
        "validation_opened": False,
        "development": development,
        "cost_stress": {
            "double_fee": report["development"]["double_fee"],
            "five_bps_slippage": report["development"]["five_bps_slippage"],
        },
        "gate": {
            "status": "FAIL",
            "stage": "development",
            "checks": report["development_gate"]["checks"],
        },
        "locked_oos": report["locked_oos"],
    }
    event = ExperimentRegistry(registry_path).append_event(
        experiment_id=int(experiment["experiment_id"]),
        event_type="result_update",
        payload={"source_sha256": source_hash, "result": result},
    )
    print(
        json.dumps(
            {"status": "APPENDED", "event_id": event["event_id"], "source_sha256": source_hash},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
