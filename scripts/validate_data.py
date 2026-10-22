#!/usr/bin/env python3
"""Validate an OHLCV CSV, JSON, Parquet, or Feather file before research."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.data.validator import validate_ohlcv_rows


def load_rows(path: Path) -> list[dict]:
    try:
        import pandas as pd
    except ImportError as exc:
        raise SystemExit("pandas is required; use the Freqtrade virtualenv for Feather files") from exc

    suffix = path.suffix.lower()
    if suffix == ".csv":
        frame = pd.read_csv(path)
    elif suffix in {".json", ".jsonl"}:
        frame = pd.read_json(path, lines=suffix == ".jsonl")
    elif suffix in {".parquet", ".pq"}:
        frame = pd.read_parquet(path)
    elif suffix == ".feather":
        frame = pd.read_feather(path)
    else:
        raise SystemExit(f"unsupported data format: {path.suffix}")

    date_column = "timestamp" if "timestamp" in frame.columns else "date" if "date" in frame.columns else None
    if date_column is None:
        raise SystemExit("data needs a timestamp or date column")
    if date_column == "date":
        # Feather preserves the original datetime resolution (often ms), so
        # do not divide its raw integer representation by a fixed factor.
        # Timestamp.value is consistently nanoseconds and yields UTC epoch ms.
        frame["timestamp"] = pd.to_datetime(frame["date"], utc=True).map(
            lambda timestamp: int(timestamp.value // 1_000_000)
        )
    return frame.to_dict("records")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data", type=Path)
    parser.add_argument("--interval-ms", type=int, required=True)
    parser.add_argument("--max-price-jump", type=float, default=0.50)
    args = parser.parse_args()
    report = validate_ohlcv_rows(
        load_rows(args.data), interval_ms=args.interval_ms, max_price_jump=args.max_price_jump
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    if not report.passed:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
