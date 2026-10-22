#!/usr/bin/env python3
"""Append completed/accumulating Alpha #5-#9 records to the central registry."""

from __future__ import annotations

import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.registry import ExperimentRegistry


REGISTRY_PATH = Path("research/registry.json")


def loaded(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def organic(*, trades: int, net_return: float, profit_factor: float | None, wins: int | None = None, losses: int | None = None) -> dict:
    result = {
        "trades": trades,
        "net_return": net_return,
        "net_profit": net_return * 1000.0,
        "profit_factor": profit_factor,
    }
    if wins is not None:
        result["wins"] = wins
    if losses is not None:
        result["losses"] = losses
    return result


def main() -> None:
    current = loaded(str(REGISTRY_PATH)) if REGISTRY_PATH.exists() else {"experiments": []}
    existing = {str(item.get("strategy")) for item in current.get("experiments", [])}
    registry = ExperimentRegistry(REGISTRY_PATH)

    alpha5 = loaded("reports/alpha005-diagnostic.json")
    alpha6 = loaded("reports/alpha006-diagnostic.json")
    alpha7 = loaded("reports/alpha007-accumulation.json")
    alpha8 = loaded("reports/alpha008-diagnostic.json")
    alpha9 = loaded("reports/alpha009-diagnostic.json")
    prereg = {
        number: loaded(f"research/alpha{number:03d}-preregistration.json")
        for number in (5, 6, 7, 8, 9)
    }

    records = [
        {
            "strategy": alpha5["candidate"],
            "hypothesis": prereg[5]["hypothesis"],
            "reason": "Independent funding-crowding reversal family after Phase 2 OHLCV Alphas failed.",
            "parent_experiment": None,
            "reason_for_change": None,
            "trial_accounting": {"alpha_number": 5, "family": "funding_crowding_reversal", "family_trial": 1},
            "result": {
                "source_report": "reports/alpha005-diagnostic.json",
                "validation_opened": False,
                "development": {
                    "organic": organic(trades=alpha5["trades"], net_return=alpha5["net_return"], profit_factor=alpha5["profit_factor"]),
                    "max_drawdown": alpha5["max_drawdown"],
                    "pnl_components": alpha5["pnl_components"],
                    "estimated_fee_cost": -alpha5["pnl_components"]["trading_fees"],
                    "funding_net": alpha5["pnl_components"]["funding_pnl"],
                    "monthly": alpha5["monthly"],
                },
                "gate": {
                    "status": "FAIL",
                    "stage": "development",
                    "checks": [
                        {"name": "development_net_positive", "passed": alpha5["net_return"] > 0},
                        {"name": "development_profit_factor_minimum", "passed": alpha5["profit_factor"] >= 1.10},
                    ],
                },
            },
        },
        {
            "strategy": alpha6["candidate"],
            "hypothesis": prereg[6]["hypothesis"],
            "reason": "Independent weekly cross-sectional momentum swing family.",
            "parent_experiment": None,
            "reason_for_change": None,
            "trial_accounting": {"alpha_number": 6, "family": "cross_sectional_momentum", "family_trial": 1},
            "result": {
                "source_report": "reports/alpha006-diagnostic.json",
                "validation_opened": False,
                "development": {
                    "organic": organic(trades=alpha6["trades"], net_return=alpha6["net_return"], profit_factor=alpha6["profit_factor"]),
                    "max_drawdown": alpha6["max_drawdown"],
                    "pnl_components": alpha6["pnl_components"],
                    "estimated_fee_cost": -alpha6["pnl_components"]["trading_fees"],
                    "funding_net": alpha6["pnl_components"]["funding_pnl"],
                    "monthly": alpha6["monthly"],
                },
                "gate": {
                    "status": "FAIL",
                    "stage": "development",
                    "checks": [
                        {"name": "development_trade_sample", "passed": alpha6["trades"] >= 60},
                        {"name": "development_profit_factor_minimum", "passed": alpha6["profit_factor"] >= 1.10},
                    ],
                },
            },
        },
        {
            "strategy": alpha7["candidate"],
            "hypothesis": prereg[7]["hypothesis"],
            "reason": "Forward-only public order-book microstructure family with shadow maker execution.",
            "parent_experiment": None,
            "reason_for_change": None,
            "trial_accounting": {"alpha_number": 7, "family": "orderbook_microstructure", "family_trial": 1},
            "result": {
                "source_report": "reports/alpha007-accumulation.json",
                "accumulation": alpha7,
                "development": {},
                "gate": {
                    "status": "ACCUMULATING",
                    "stage": "forward_collection",
                    "checks": alpha7["checks"],
                },
            },
        },
        {
            "strategy": alpha8["candidate"],
            "hypothesis": prereg[8]["hypothesis"],
            "reason": "Replace relative weekly ranking with independent monthly time-series trend and ex-ante volatility sizing.",
            "parent_experiment": 6,
            "reason_for_change": "Alpha #6 lacked a robust cross-sectional edge; test a distinct absolute trend construction without tuning Alpha #6.",
            "trial_accounting": {"alpha_number": 8, "family": "time_series_momentum", "family_trial": 1},
            "result": {
                "source_report": "reports/alpha008-diagnostic.json",
                "validation_opened": False,
                "development": {
                    "organic": {
                        "trades": alpha8["organic"]["trades"],
                        "net_return": alpha8["organic"]["net_return"],
                        "net_profit": alpha8["organic"]["net_profit_usdt"],
                        "profit_factor": alpha8["organic"]["profit_factor"],
                        "wins": alpha8["organic"]["wins"],
                        "losses": alpha8["organic"]["losses"],
                    },
                    "max_drawdown": alpha8["risk"]["closed_trade_maximum_drawdown"],
                    "pnl_components": alpha8["pnl_components"],
                    "estimated_fee_cost": -alpha8["pnl_components"]["trading_fees"],
                    "funding_net": alpha8["pnl_components"]["funding_pnl"],
                    "monthly": alpha8["monthly_closed_trade_diagnostic"],
                },
                "gate": {
                    "status": "FAIL",
                    "stage": "development",
                    "checks": [
                        {"name": name, "passed": passed}
                        for name, passed in alpha8["development_gate"].items()
                    ],
                },
            },
        },
        {
            "strategy": alpha9["candidate"],
            "hypothesis": prereg[9]["hypothesis"],
            "reason": "Test whether Alpha #5's positive funding component can survive with weekly cross-sectional turnover and opposing legs.",
            "parent_experiment": 5,
            "reason_for_change": "Alpha #5's 1,009 trades lost most gross edge to fees; precommit a low-turnover funding spread instead of tuning its thresholds.",
            "trial_accounting": {"alpha_number": 9, "family": "funding_carry", "family_trial": 1},
            "result": {
                "source_report": "reports/alpha009-diagnostic.json",
                "validation_opened": False,
                "development": {
                    "organic": organic(
                        trades=alpha9["closed_legs"],
                        net_return=alpha9["net_return"],
                        profit_factor=alpha9["profit_factor"],
                    ),
                    "max_drawdown": alpha9["maximum_closed_weekly_drawdown"],
                    "pnl_components": {
                        "price_pnl_estimate": alpha9["pnl_components"]["raw_price_pnl"],
                        "trading_fees": alpha9["pnl_components"]["trading_fees"],
                        "funding_pnl": alpha9["pnl_components"]["funding_pnl"],
                        "modeled_slippage": alpha9["pnl_components"]["modeled_slippage"],
                        "net_pnl": alpha9["pnl_components"]["net_pnl"],
                    },
                    "estimated_fee_cost": -alpha9["pnl_components"]["trading_fees"],
                    "funding_net": alpha9["pnl_components"]["funding_pnl"],
                    "monthly": alpha9["monthly"],
                },
                "gate": {
                    "status": "FAIL",
                    "stage": "development",
                    "checks": [
                        {"name": name, "passed": passed}
                        for name, passed in alpha9["gate"].items()
                    ],
                },
            },
        },
    ]

    appended = []
    for record in records:
        if record["strategy"] in existing:
            continue
        registered = registry.register(**record)
        appended.append({"experiment_id": registered["experiment_id"], "strategy": registered["strategy"]})
        existing.add(record["strategy"])
    print(json.dumps({"appended": appended, "registry": str(REGISTRY_PATH)}, indent=2))


if __name__ == "__main__":
    main()
