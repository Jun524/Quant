#!/usr/bin/env python3
"""Create a no-lookahead daily MTM curve from frequent dry-run observations."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.monitoring.equity import select_causal_daily_boundaries


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--maximum-age-seconds", type=int, default=120)
    args = parser.parse_args()
    observations: list[dict] = []
    for path in args.inputs:
        observations.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    rows = select_causal_daily_boundaries(observations, maximum_age_seconds=args.maximum_age_seconds)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["timestamp", "equity", "equity_type", "observed_at", "boundary_lag_seconds"])
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({"status": "COMPLETE", "output": str(args.output), "daily_boundaries": len(rows)}, indent=2))


if __name__ == "__main__":
    main()
