#!/usr/bin/env python3
"""Analyze the supplied AOA/BitMEX history without placing orders."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.aoa_history import (
    build_report,
    fetch_bitmex_xbtusd_hourly,
    market_context,
    reconstruct_xbtusd_episodes,
    scan_executions,
    scan_wallet,
    summarize_episodes,
)


def serialize(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"cannot serialize {type(value)!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=Path("워뇨띠 매매"))
    parser.add_argument("--json-output", type=Path, default=Path("reports/aoa-history-analysis.json"))
    parser.add_argument("--markdown-output", type=Path, default=Path("reports/aoa-history-analysis.md"))
    parser.add_argument("--fetch-market-context", action="store_true")
    args = parser.parse_args()

    execution_paths = sorted(args.source_dir.glob("aoa-execution-*.csv"))
    wallet_path = args.source_dir / "aoa-wallet-2018-03-01-2021-12-31.csv"
    if not execution_paths or not wallet_path.exists():
        raise FileNotFoundError("AOA execution or wallet CSV files are missing")

    execution, xbt_fills, _ = scan_executions(execution_paths)
    wallet = scan_wallet(wallet_path)
    episodes, open_position = reconstruct_xbtusd_episodes(xbt_fills)
    episode_summary = summarize_episodes(episodes)
    context = None
    if args.fetch_market_context:
        bars = fetch_bitmex_xbtusd_hourly(
            datetime(2018, 3, 1, tzinfo=timezone.utc), datetime(2022, 1, 2, tzinfo=timezone.utc)
        )
        context = market_context(episodes, bars)

    result = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "user-supplied public BitMEX execution and wallet exports",
        "execution": execution,
        "wallet": wallet,
        "xbtusd_episodes": episode_summary,
        "xbtusd_open_position_contracts_at_export_end": open_position,
        "market_context": context,
        "limitations": [
            "execution history omits unfilled and cancelled orders",
            "wallet balance rows do not reconcile sequentially, so normalized returns and drawdown are unavailable",
            "one-contract residual positions are treated as economically flat to avoid artificial episode chaining",
            "the history identifies behavior correlations, not the trader's causal decision rule",
            "all derived conditions are in-sample and require new never-seen OOS data",
        ],
    }
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=serialize) + "\n", encoding="utf-8")
    args.markdown_output.write_text(build_report(execution, wallet, episode_summary, context), encoding="utf-8")
    print(
        json.dumps(
            {
                "json": str(args.json_output),
                "markdown": str(args.markdown_output),
                "orders": execution["ordinary_orders"],
                "episodes": episode_summary["episodes"],
                "liquidation_dates": execution["liquidations"]["distinct_dates"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
