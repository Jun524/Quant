#!/usr/bin/env python3
"""Report backtest metrics with artificial end-of-test force exits removed."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from zipfile import ZipFile


def summarize(trades: list[dict], starting_balance: float) -> dict:
    profits = [float(trade["profit_abs"]) for trade in trades]
    gross_profit = sum(value for value in profits if value > 0)
    gross_loss = -sum(value for value in profits if value < 0)
    absolute_profit = sum(profits)
    return {
        "trades": len(trades),
        "wins": sum(value > 0 for value in profits),
        "losses": sum(value < 0 for value in profits),
        "profit_usdt": round(absolute_profit, 6),
        "profit_percent_of_start": round(100 * absolute_profit / starting_balance, 4),
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("result_zip", type=Path)
    parser.add_argument("--strategy", help="Required only when the archive has multiple strategies")
    args = parser.parse_args()

    with ZipFile(args.result_zip) as archive:
        expected = f"{args.result_zip.stem}.json"
        result_name = expected if expected in archive.namelist() else next(
            name
            for name in archive.namelist()
            if name.endswith(".json") and not name.endswith("_config.json")
        )
        result = json.loads(archive.read(result_name))

    strategies = result["strategy"]
    strategy_name = args.strategy
    if strategy_name is None:
        if len(strategies) != 1:
            parser.error("--strategy is required for a multi-strategy result")
        strategy_name = next(iter(strategies))
    if strategy_name not in strategies:
        parser.error(f"strategy not found: {strategy_name}")

    strategy = strategies[strategy_name]
    trades = strategy["trades"]
    organic = [trade for trade in trades if trade["exit_reason"] != "force_exit"]
    forced = [trade for trade in trades if trade["exit_reason"] == "force_exit"]

    report = {
        "strategy": strategy_name,
        "timerange": strategy["timerange"],
        "starting_balance": strategy["starting_balance"],
        "reported_all_trades": summarize(trades, strategy["starting_balance"]),
        "organic_closed_trades": summarize(organic, strategy["starting_balance"]),
        "backtest_end_force_exits": summarize(forced, strategy["starting_balance"]),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
