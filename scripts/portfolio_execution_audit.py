#!/usr/bin/env python3
"""Recompute portfolio execution-path evidence from sealed observations."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.portfolio_execution import (
    PORTFOLIO_EXECUTION_PROTOCOL_FILES,
    evaluate_portfolio_execution,
    load_execution_observation_manifest,
)
from quant.research.portfolio_promotion import (
    PortfolioPromotionPolicy,
    expected_execution_scopes,
)
from quant.research.portfolio_risk import load_portfolio_equity


def _load_object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--policy",
        type=Path,
        default=PROJECT_ROOT / "research" / "portfolio-promotion-policy.json",
    )
    parser.add_argument("--observation-manifest", type=Path, required=True)
    parser.add_argument("--portfolio-equity", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    manifest_bytes = args.manifest.read_bytes()
    policy_bytes = args.policy.read_bytes()
    equity_bytes = args.portfolio_equity.read_bytes()
    manifest = _load_object(args.manifest)
    policy_document = _load_object(args.policy)
    machine_policy = policy_document.get("machine_policy")
    policy = PortfolioPromotionPolicy()
    if machine_policy != asdict(policy):
        raise SystemExit("portfolio policy document does not match implemented policy")
    expected_scopes, failures = expected_execution_scopes(manifest)
    if failures:
        raise SystemExit("portfolio manifest execution scopes are invalid: " + "; ".join(failures))
    equity = load_portfolio_equity(args.portfolio_equity)
    observation_bundle = load_execution_observation_manifest(
        args.observation_manifest,
        portfolio_manifest_sha256=sha256(manifest_bytes).hexdigest(),
        portfolio_policy_sha256=sha256(policy_bytes).hexdigest(),
    )
    result = evaluate_portfolio_execution(
        observation_bundle["observations"],
        expected_scopes,
        minimum_observations_per_scope=policy.minimum_execution_observations_per_scope,
        minimum_passive_fills_per_scope=policy.minimum_passive_fills_per_scope,
        coverage_start=datetime.fromisoformat(equity["first_timestamp"]),
        coverage_end=datetime.fromisoformat(equity["last_timestamp"]),
    )
    result.update(
        {
            "portfolio_manifest_sha256": sha256(manifest_bytes).hexdigest(),
            "portfolio_promotion_policy_sha256": sha256(policy_bytes).hexdigest(),
            "protocol_current": True,
            "protocol_sha256": {
                relative: sha256((PROJECT_ROOT / relative).read_bytes()).hexdigest()
                for relative in PORTFOLIO_EXECUTION_PROTOCOL_FILES
            },
            "sources": {
                "observation_manifest": {
                    "path": observation_bundle["manifest_path"],
                    "sha256": observation_bundle["manifest_sha256"],
                    "segments": observation_bundle["segments"],
                },
                "mark_to_market_equity": {
                    "path": str(args.portfolio_equity),
                    "sha256": sha256(equity_bytes).hexdigest(),
                },
            },
        }
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "output": str(args.output)}, indent=2))
    raise SystemExit(0 if result["status"] == "PASS" else 2)


if __name__ == "__main__":
    main()
