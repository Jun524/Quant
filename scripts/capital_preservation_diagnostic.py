#!/usr/bin/env python3
"""Evaluate fixed historical backtests against the separate preservation goal."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.artifacts import artifact_reference
from quant.research.capital_preservation import evaluate_capital_preservation
from quant.research.result_loader import load_freqtrade_result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--development", type=Path, required=True)
    parser.add_argument("--development-stress", type=Path, required=True)
    parser.add_argument("--post-development", type=Path, required=True)
    parser.add_argument("--post-development-stress", type=Path, required=True)
    parser.add_argument("--objective", type=Path, default=Path("research/capital-preservation-objective.json"))
    parser.add_argument("--risk-policy", type=Path, default=Path("research/risk-policy.json"))
    parser.add_argument(
        "--execution-contract",
        type=Path,
        required=True,
        help="sealed candidate-specific leverage, risk, position-count, and adverse-add contract",
    )
    parser.add_argument(
        "--integrity-receipt",
        type=Path,
        required=True,
        help="sealed receipt proving data, lookahead, recursive, and rule-violation checks",
    )
    parser.add_argument("--output", type=Path, default=Path("reports/capital-preservation-diagnostic.json"))
    args = parser.parse_args()

    objective = json.loads(args.objective.read_text(encoding="utf-8"))
    risk_policy = json.loads(args.risk_policy.read_text(encoding="utf-8"))
    execution_contract = json.loads(args.execution_contract.read_text(encoding="utf-8"))
    integrity = json.loads(args.integrity_receipt.read_text(encoding="utf-8"))
    if not isinstance(execution_contract, dict):
        raise SystemExit("execution contract must be a JSON object")
    if not isinstance(integrity, dict):
        raise SystemExit("integrity receipt must be a JSON object")
    requirements = objective.get("capital_preservation_requirements") or {}
    modeled_slippage_bps = float(requirements["modeled_slippage_bps"])
    development = load_freqtrade_result(args.development, args.strategy)
    development_stress = load_freqtrade_result(
        args.development_stress,
        args.strategy,
        modeled_slippage_bps=modeled_slippage_bps,
    )
    post_development = load_freqtrade_result(args.post_development, args.strategy)
    post_development_stress = load_freqtrade_result(
        args.post_development_stress,
        args.strategy,
        modeled_slippage_bps=modeled_slippage_bps,
    )
    gate = evaluate_capital_preservation(
        development,
        development_stress,
        post_development,
        post_development_stress,
        objective=objective,
        risk_policy=risk_policy,
        execution_contract=execution_contract,
        integrity=integrity,
    )
    payload = {
        "candidate": args.strategy,
        "kind": "HISTORICAL_CAPITAL_PRESERVATION_DIAGNOSTIC_ONLY",
        "inputs": {
            "objective": artifact_reference(args.objective),
            "risk_policy": artifact_reference(args.risk_policy),
            "execution_contract": artifact_reference(args.execution_contract),
            "integrity_receipt": artifact_reference(args.integrity_receipt),
            "development": artifact_reference(args.development),
            "development_stress": artifact_reference(args.development_stress),
            "post_development": artifact_reference(args.post_development),
            "post_development_stress": artifact_reference(args.post_development_stress),
        },
        "execution_contract": execution_contract,
        "stress_contract": {
            "fee_multiple": 2.0,
            "modeled_slippage_bps_per_order_notional": modeled_slippage_bps,
            "adverse_funding": "remove favorable funding and double paid funding",
        },
        "gate": gate,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
