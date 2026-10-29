#!/usr/bin/env python3
"""Legacy evaluator for pre-capital-preservation Freqtrade experiments.

It cannot establish current-policy promotion evidence because its older
three-result interface lacks the required funding, adverse-cost, and
source-replayed capital-preservation inputs.  New candidates must not invoke
this path accidentally.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.gates import GatePolicy, evaluate_candidate
from quant.research.registry import ExperimentRegistry
from quant.research.result_loader import load_freqtrade_result


def require_explicit_legacy_protocol(legacy_protocol: bool) -> None:
    """Prevent a new candidate from silently using the retired generic gate."""
    if not legacy_protocol:
        raise ValueError(
            "research_pipeline uses the retired generic GatePolicy; current "
            "capital-preservation candidates require a dedicated preregistered "
            "cost, funding, integrity, and MTM evaluation path"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--hypothesis", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--parent-experiment", type=int)
    parser.add_argument("--reason-for-change")
    parser.add_argument("--hypothesis-family")
    parser.add_argument("--parameter-changes", default="none")
    parser.add_argument("--data-changes", default="none")
    parser.add_argument("--universe-changes", default="none")
    parser.add_argument("--development-seen", action="store_true")
    parser.add_argument("--validation-seen", action="store_true")
    parser.add_argument("--locked-seen", action="store_true")
    parser.add_argument("--locked-seen-count", type=int, default=0)
    parser.add_argument("--role", choices=("general", "short", "breakout"), default="general")
    parser.add_argument("--development", required=True, type=Path)
    parser.add_argument("--validation", required=True, type=Path)
    parser.add_argument("--cost-stress", required=True, type=Path)
    parser.add_argument("--slippage-stress", type=Path)
    parser.add_argument("--modeled-slippage-bps", type=float, default=0.0)
    parser.add_argument("--detail-data-dir", type=Path)
    parser.add_argument("--registry", type=Path, default=Path("research/registry.json"))
    parser.add_argument("--report-dir", type=Path, default=Path("reports"))
    parser.add_argument("--policy", type=Path, default=Path("config/research.json"))
    parser.add_argument("--min-oos-trades", type=int)
    parser.add_argument("--min-profit-factor", type=float)
    parser.add_argument("--max-drawdown", type=float)
    parser.add_argument("--lookahead-passed", action="store_true")
    parser.add_argument("--recursive-passed", action="store_true")
    parser.add_argument(
        "--legacy-protocol",
        action="store_true",
        help="acknowledge that this result is legacy research-only and cannot be current-policy promotion evidence",
    )
    args = parser.parse_args()
    try:
        require_explicit_legacy_protocol(args.legacy_protocol)
    except ValueError as error:
        raise SystemExit(f"FAIL: {error}") from error

    policy_values = json.loads(args.policy.read_text(encoding="utf-8"))["research_policy"]
    policy = GatePolicy(
        min_oos_trades=args.min_oos_trades if args.min_oos_trades is not None else policy_values["min_oos_trades"],
        min_profit_factor=args.min_profit_factor if args.min_profit_factor is not None else policy_values["min_profit_factor"],
        max_drawdown=args.max_drawdown if args.max_drawdown is not None else policy_values["max_drawdown"],
        min_oos_net_return=policy_values["min_oos_net_return"],
        max_short_single_trade_profit_concentration=policy_values.get("max_short_single_trade_profit_concentration", 0.50),
    )
    development = load_freqtrade_result(args.development, args.strategy, detail_data_dir=args.detail_data_dir)
    validation = load_freqtrade_result(args.validation, args.strategy, detail_data_dir=args.detail_data_dir)
    cost_stress = load_freqtrade_result(args.cost_stress, args.strategy, detail_data_dir=args.detail_data_dir)
    slippage_stress = (
        load_freqtrade_result(
            args.slippage_stress,
            args.strategy,
            detail_data_dir=args.detail_data_dir,
            modeled_slippage_bps=args.modeled_slippage_bps,
        )
        if args.slippage_stress
        else None
    )
    gate = evaluate_candidate(
        development=development,
        validation=validation,
        cost_stress=cost_stress,
        lookahead_passed=args.lookahead_passed,
        recursive_passed=args.recursive_passed,
        role=args.role,
        slippage_stress=slippage_stress,
        policy=policy,
    )
    outcome = {
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "gate": gate,
        "development": development,
        "validation": validation,
        "cost_stress": cost_stress,
        "slippage_stress": slippage_stress,
        "policy_scope": "LEGACY_RESEARCH_ONLY_NOT_CURRENT_CAPITAL_PRESERVATION_EVIDENCE",
    }
    record = ExperimentRegistry(args.registry).register(
        strategy=args.strategy,
        hypothesis=args.hypothesis,
        reason=args.reason,
        result=outcome,
        parent_experiment=args.parent_experiment,
        reason_for_change=args.reason_for_change,
        trial_accounting={
            "hypothesis_family": args.hypothesis_family,
            "parameter_changes": args.parameter_changes,
            "data_changes": args.data_changes,
            "universe_changes": args.universe_changes,
            "development_seen": args.development_seen,
            "validation_seen": args.validation_seen,
            "locked_seen": args.locked_seen,
            "locked_seen_count": args.locked_seen_count,
            "policy_scope": "LEGACY_RESEARCH_ONLY_NOT_CURRENT_CAPITAL_PRESERVATION_EVIDENCE",
        },
    )
    args.report_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.report_dir / f"experiment-{record['experiment_id']:04d}.json"
    report_path.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"experiment_id": record["experiment_id"], "status": gate["status"], "report": str(report_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
