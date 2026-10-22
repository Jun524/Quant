#!/usr/bin/env python3
"""Evaluate one Freqtrade export against the net 10% monthly objective."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.monthly_target import MonthlyTargetPolicy, evaluate_monthly_target
from quant.research.equity import apply_mark_to_market_equity, load_mark_to_market_equity
from quant.research.result_loader import load_freqtrade_result
from quant.research.risk import block_bootstrap_risk
from quant.research.artifacts import artifact_reference


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result", type=Path)
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--double-cost-result", type=Path)
    parser.add_argument("--slippage-stress-result", type=Path)
    parser.add_argument("--funding-stress-result", type=Path)
    parser.add_argument(
        "--equity-curve",
        type=Path,
        help="CSV/JSONL daily UTC-boundary account equity; must declare equity_type=mark_to_market",
    )
    parser.add_argument("--output", type=Path, default=Path("reports/monthly-objective.json"))
    args = parser.parse_args()
    base = load_freqtrade_result(args.result, args.strategy)
    if args.equity_curve:
        curve = load_mark_to_market_equity(
            args.equity_curve,
            expected_start=base.get("backtest_start"),
            expected_end=base.get("backtest_end"),
        )
        base = apply_mark_to_market_equity(base, curve)
    stress = load_freqtrade_result(args.double_cost_result, args.strategy) if args.double_cost_result else None
    slippage_stress = (
        load_freqtrade_result(args.slippage_stress_result, args.strategy)
        if args.slippage_stress_result
        else None
    )
    funding_stress = (
        load_freqtrade_result(args.funding_stress_result, args.strategy)
        if args.funding_stress_result
        else None
    )
    risk = block_bootstrap_risk([item["return"] for item in base["daily"].get("series", [])])
    payload = {
        "source": str(args.result),
        "input_artifacts": {
            "base_result": artifact_reference(args.result),
            "double_cost_result": artifact_reference(args.double_cost_result),
            "slippage_stress_result": artifact_reference(args.slippage_stress_result),
            "funding_stress_result": artifact_reference(args.funding_stress_result),
            "mark_to_market_equity_curve": artifact_reference(args.equity_curve),
        },
        "strategy": args.strategy,
        "monthly": base["monthly"],
        "organic": base["organic"],
        "max_drawdown": base["max_drawdown"],
        "equity_basis": base["equity_basis"],
        "equity_evidence": base.get("equity_evidence"),
        "trade_risk": base.get("trade_risk"),
        "risk_of_ruin": risk,
        "evidence": {
            "sample_role": "historical_development_backtest",
            "pre_registered": False,
            "parameter_changes_after_start": None,
            "policy": "MONTHLY_TARGET_FORWARD_EVIDENCE_V2",
            "registered_at_utc": None,
            "sample_started_at_utc": base.get("backtest_start"),
        },
        "gate": evaluate_monthly_target(
            base,
            double_cost=stress,
            slippage_stress=slippage_stress,
            funding_stress=funding_stress,
            risk=risk,
            evidence={
                "sample_role": "historical_development_backtest",
                "pre_registered": False,
                "parameter_changes_after_start": None,
                "policy": "MONTHLY_TARGET_FORWARD_EVIDENCE_V2",
                "registered_at_utc": None,
                "sample_started_at_utc": base.get("backtest_start"),
            },
            policy=MonthlyTargetPolicy(
                require_slippage_stress_positive=True,
                require_funding_stress_positive=True,
            ),
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
