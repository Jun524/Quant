#!/usr/bin/env python3
"""Run the preregistered Alpha #9 development simulation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.funding_carry import simulate_funding_carry


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("user_data/data/binance/futures"))
    parser.add_argument("--start", default="2021-01-01")
    parser.add_argument("--end", default="2024-01-01")
    parser.add_argument("--output", type=Path, default=Path("reports/alpha009-diagnostic.json"))
    args = parser.parse_args()
    report = simulate_funding_carry(args.data_dir, start=args.start, end=args.end)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key not in {"weekly", "trades"}}, indent=2))


if __name__ == "__main__":
    main()
