#!/usr/bin/env python3
"""Write a non-promotional inventory of versioned calibration receipts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.monitoring.execution_calibration_stability import (
    summarize_versioned_calibration_receipts,
)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def main() -> None:
    report = summarize_versioned_calibration_receipts(PROJECT_ROOT / "reports")
    json_path = PROJECT_ROOT / "reports" / "execution-calibration-stability.json"
    markdown_path = PROJECT_ROOT / "reports" / "execution-calibration-stability.md"
    _atomic_write(json_path, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    _atomic_write(
        markdown_path,
        "# Versioned execution calibration inventory\n\n"
        f"Status: **{report['status']}**\n\n"
        f"Receipts: {report['valid_receipts']} / {report['minimum_receipts']}\n\n"
        "Execution paths observed: "
        + (", ".join(report["execution_path_coverage"]["observed_scopes"]) or "none")
        + "\n\nMissing execution paths: "
        + (", ".join(report["execution_path_coverage"]["missing_scopes"]) or "none")
        + "\n\n"
        "Largest gap between receipt windows: "
        + (
            f"{report['temporal_independence']['maximum_inter_receipt_gap_hours']:.3f} hours"
            if report["temporal_independence"]["maximum_inter_receipt_gap_hours"] is not None
            else "not available"
        )
        + " (minimum for descriptive comparison: "
        + f"{report['temporal_independence']['minimum_separation_hours']:.0f} hours)\n\n"
        "Next eligible independent-window start: "
        + (
            str(report["temporal_independence"]["next_eligible_independent_window_start_at"])
            if report["temporal_independence"]["next_eligible_independent_window_start_at"]
            else "not available"
        )
        + "\n\n"
        "Observed all-in entry-drag range: "
        + (
            f"{report['descriptive_cost_dispersion']['minimum_observed_all_in_entry_drag_bps']:.3f}–"
            f"{report['descriptive_cost_dispersion']['maximum_observed_all_in_entry_drag_bps']:.3f} bps"
            if report["descriptive_cost_dispersion"]["receipt_count"]
            else "not available"
        )
        + " (descriptive only; no cost model is changed)\n\n"
        "Highest observed descriptive stress floor: "
        + (
            f"{report['descriptive_cost_dispersion']['maximum_observed_descriptive_stress_floor_bps']:.3f} bps"
            if report["descriptive_cost_dispersion"]["maximum_observed_descriptive_stress_floor_bps"] is not None
            else "not available"
        )
        + " (new research must explicitly preregister any use)\n\n"
        "This is diagnostic only; it cannot authorize live trading or mutate costs.\n",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
