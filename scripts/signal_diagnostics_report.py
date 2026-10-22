#!/usr/bin/env python3
"""Create a source-bound, non-promotional cross-sectional signal report."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.signal_diagnostics import (
    SIGNAL_DIAGNOSTIC_PROTOCOL_FILES,
    load_signal_observations,
    summarize_signal_diagnostics,
)


def _format(value: object) -> str:
    return "N/A" if value is None else f"{float(value):.6f}"


def markdown(report: dict) -> str:
    quality = report["signal_quality"]
    coverage = report["coverage"]
    return f"""# Cross-sectional signal diagnostic

- Status: **{report['status']}**
- Promotion eligible: **NO**
- Live order authorization: **CLOSED**
- Periods / rows: {coverage['periods']} / {coverage['rows']}
- Cross-section size: {coverage['minimum_cross_section_observed']}–{coverage['maximum_cross_section_observed']}
- Mean Rank IC: {_format(quality['rank_ic']['mean'])}
- Rank IC information ratio: {_format(quality['rank_ic']['information_ratio'])}
- Rank IC overlap-aware t-statistic: {_format(quality['rank_ic']['overlap_aware_t_statistic'])}
- Mean gross top-bottom return: {_format(quality['top_bottom_gross_return']['mean'])}
- Mean one-way turnover: {_format(quality['one_way_turnover']['mean'])}
- Mean signal rank autocorrelation: {_format(quality['signal_rank_autocorrelation']['mean'])}
- Derived HAC lag from forward-label overlap: {coverage['overlap_aware_hac_lag']}

This report diagnoses a precomputed signal and forward-return panel. It does
not include fees, slippage, funding, or fill uncertainty and cannot authorize
strategy promotion or live orders.
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--minimum-cross-section", type=int, default=5)
    parser.add_argument("--quantile-fraction", type=float, default=0.20)
    args = parser.parse_args()

    source_bytes = args.input.read_bytes()
    observations = load_signal_observations(args.input)
    if args.input.read_bytes() != source_bytes:
        raise SystemExit("signal panel changed while it was being validated")
    report = summarize_signal_diagnostics(
        observations,
        minimum_cross_section=args.minimum_cross_section,
        quantile_fraction=args.quantile_fraction,
    )
    report.update(
        {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "source": {
                "path": str(args.input),
                "sha256": sha256(source_bytes).hexdigest(),
            },
            "protocol_current": True,
            "protocol_sha256": {
                relative: sha256((PROJECT_ROOT / relative).read_bytes()).hexdigest()
                for relative in SIGNAL_DIAGNOSTIC_PROTOCOL_FILES
            },
        }
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    args.output.with_suffix(".md").write_text(markdown(report), encoding="utf-8")
    print(json.dumps({"status": report["status"], "output": str(args.output)}, indent=2))


if __name__ == "__main__":
    main()
