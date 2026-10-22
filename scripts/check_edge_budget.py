#!/usr/bin/env python3
"""Fail closed when a pre-registered expected gross edge cannot pay its costs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.evidence import EdgeBudget


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gross-edge-bps", required=True, type=float)
    parser.add_argument("--fee-bps", required=True, type=float)
    parser.add_argument("--spread-bps", default=0.0, type=float)
    parser.add_argument("--slippage-bps", default=0.0, type=float)
    parser.add_argument("--funding-bps", default=0.0, type=float)
    args = parser.parse_args()
    budget = EdgeBudget(
        expected_gross_edge_bps=args.gross_edge_bps,
        expected_fee_bps=args.fee_bps,
        expected_spread_bps=args.spread_bps,
        expected_slippage_bps=args.slippage_bps,
        expected_funding_bps=args.funding_bps,
    )
    print(json.dumps(budget.to_dict(), ensure_ascii=False))
    if not budget.viable:
        raise SystemExit("REJECT: expected edge buffer is not positive")


if __name__ == "__main__":
    main()
